"""Config parsing, file filters, table filters and rewrites."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import FIXTURES, make_source

from relationl.config import Config, ConfigError
from relationl.extract.sql import SqlAnalyzer
from relationl.naming import TableNormaliser


class TestLoading:
    def test_minimal_config(self, tmp_path):
        config = Config.from_dict(
            {"sources": [{"name": "a", "roots": ["."]}]}, base_dir=tmp_path
        )
        assert len(config.sources) == 1
        assert config.database == (tmp_path / "relationl.db").resolve()

    def test_roots_resolve_against_the_config_file(self, tmp_path):
        config = Config.from_dict(
            {"sources": [{"name": "a", "roots": ["../code"]}]}, base_dir=tmp_path / "conf"
        )
        assert config.sources[0].roots[0] == Path(tmp_path / "code")

    def test_single_root_string_is_accepted(self, tmp_path):
        config = Config.from_dict(
            {"sources": [{"name": "a", "root": "src"}]}, base_dir=tmp_path
        )
        assert len(config.sources[0].roots) == 1

    def test_defaults_are_inherited_and_overridable(self, tmp_path):
        config = Config.from_dict(
            {
                "defaults": {"sql_dialect": "spark", "max_file_bytes": 10},
                "sources": [
                    {"name": "a", "roots": ["."]},
                    {"name": "b", "roots": ["."], "sql_dialect": "snowflake"},
                ],
            },
            base_dir=tmp_path,
        )
        assert config.source("a").sql_dialect == "spark"
        assert config.source("b").sql_dialect == "snowflake"
        assert config.source("b").max_file_bytes == 10

    @pytest.mark.parametrize(
        "payload,message",
        [
            ({}, "at least one"),
            ({"sources": [{"roots": ["."]}]}, "missing a 'name'"),
            ({"sources": [{"name": "a"}]}, "must declare 'roots'"),
            (
                {"sources": [{"name": "a", "roots": ["."], "table_include": ["("]}]},
                "invalid regex",
            ),
            (
                {"sources": [{"name": "a", "roots": ["."]}, {"name": "a", "roots": ["."]}]},
                "duplicate source name",
            ),
            (
                {"sources": [{"name": "a", "roots": ["."], "languages": ["cobol"]}]},
                "unknown languages",
            ),
        ],
    )
    def test_invalid_configs_are_rejected(self, payload, message, tmp_path):
        with pytest.raises(ConfigError, match=message):
            Config.from_dict(payload, base_dir=tmp_path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            Config.load(tmp_path / "nope.yaml")

    def test_roundtrip_through_yaml(self, tmp_path):
        from relationl.config import example_config

        path = tmp_path / "relationl.yaml"
        path.write_text(example_config(), encoding="utf-8")
        config = Config.load(path)
        assert {s.name for s in config.sources} == {"analytics", "reporting"}
        assert config.source("analytics").default_schema == "analytics"

    def test_digest_changes_with_settings(self, tmp_path):
        def build(**extra):
            return Config.from_dict(
                {"sources": [dict(name="a", roots=["."], **extra)]}, base_dir=tmp_path
            ).sources[0]

        assert build().digest == build().digest
        assert build().digest != build(sql_dialect="snowflake").digest
        assert build().digest != build(table_exclude=["^tmp"]).digest


class TestFileFilters:
    def test_include_globs(self):
        source = make_source(include=("**/*.sql",))
        assert source.accepts_path(FIXTURES / "warehouse" / "store_region.sql", FIXTURES)
        assert not source.accepts_path(FIXTURES / "etl" / "build_orders.py", FIXTURES)

    def test_glob_matches_files_at_the_root(self, tmp_path):
        (tmp_path / "a.sql").write_text("", encoding="utf-8")
        source = make_source(roots=(tmp_path,), include=("**/*.sql",))
        assert source.accepts_path(tmp_path / "a.sql", tmp_path)

    def test_exclude_wins_over_include(self):
        source = make_source(exclude=("**/warehouse/**",))
        assert not source.accepts_path(
            FIXTURES / "warehouse" / "store_region.sql", FIXTURES
        )

    def test_file_regexes(self):
        source = make_source(file_exclude=(__import__("re").compile("orders"),))
        assert not source.accepts_path(FIXTURES / "etl" / "build_orders.py", FIXTURES)
        assert source.accepts_path(FIXTURES / "etl" / "adhoc_queries.txt", FIXTURES)

    def test_language_filter(self):
        source = make_source(languages=("sql",))
        assert source.language_for(Path("a.sql")) == "sql"
        assert source.language_for(Path("a.py")) is None

    def test_unknown_extension(self):
        assert make_source().language_for(Path("a.parquet")) is None


class TestTableFilters:
    def test_include_and_exclude(self):
        normalise = TableNormaliser(
            make_source(table_include=(__import__("re").compile("^warehouse\\."),))
        )
        assert normalise("warehouse.orders").key == "warehouse.orders"
        assert normalise("staging.orders") is None

    def test_exclude(self):
        normalise = TableNormaliser(
            make_source(table_exclude=(__import__("re").compile("_tmp$"),))
        )
        assert normalise("a.scratch_tmp") is None
        assert normalise("a.real") is not None

    def test_defaults_qualify_bare_names(self):
        normalise = TableNormaliser(
            make_source(default_schema="analytics", default_catalog="prod")
        )
        assert normalise("orders").key == "prod.analytics.orders"
        assert normalise("other.orders").key == "prod.other.orders"
        assert normalise("cat.other.orders").key == "cat.other.orders"

    def test_rewrite_collapses_environments(self):
        source = make_source(
            table_rewrite=(
                __import__("relationl.config", fromlist=["Rewrite"]).Rewrite(
                    __import__("re").compile("^(dev|staging)_"), ""
                ),
            )
        )
        normalise = TableNormaliser(source)
        assert normalise("dev_sales.orders").key == "sales.orders"
        assert normalise("sales.orders").key == "sales.orders"

    def test_rewrite_is_reflected_in_the_join_condition(self):
        """A rewritten name must appear in the predicate, or dev and prod
        versions of one join would not collapse onto a single edge."""
        import re as _re

        from relationl.config import Rewrite

        source = make_source(table_rewrite=(Rewrite(_re.compile("^dev_"), ""),))
        analyzer = SqlAnalyzer(TableNormaliser(source), dialect="spark")
        joins, _, _ = analyzer.analyze(
            "SELECT 1 FROM dev_sales.orders o JOIN dev_crm.customers c ON o.cid = c.id"
        )
        assert joins[0].condition == "crm.customers.id = sales.orders.cid"

    def test_excluded_table_drops_the_join(self):
        import re as _re

        source = make_source(table_exclude=(_re.compile("^crm\\."),))
        analyzer = SqlAnalyzer(TableNormaliser(source), dialect="spark")
        joins, tables, _ = analyzer.analyze(
            "SELECT 1 FROM sales.orders o JOIN crm.customers c ON o.cid = c.id"
        )
        assert joins == []
        assert {t.key for t in tables} == {"sales.orders"}
