"""End-to-end scanning: walking, the parse cache, git provenance and storage."""

from __future__ import annotations

import subprocess

import pytest
from conftest import FIXTURES

from relationl import gitinfo
from relationl.config import Config
from relationl.scanner import scan
from relationl.storage import Database


def config_for(tmp_path, **source_overrides) -> Config:
    source = {"name": "fixtures", "roots": [str(FIXTURES)], "sql_dialect": "spark"}
    source.update(source_overrides)
    return Config.from_dict(
        {"database": str(tmp_path / "graph.db"), "sources": [source]}, base_dir=tmp_path
    )


def query(database: Database, sql: str, *params):
    connection = database.connect(readonly=True)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


class TestScan:
    def test_scan_finds_every_fixture_file(self, tmp_path):
        stats = scan(config_for(tmp_path), workers=1)
        assert stats.files == 5
        assert stats.errors == 0
        assert stats.tables > 0
        assert stats.edges > 0

    def test_languages_are_recorded(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(Database(config.database), "SELECT DISTINCT language FROM files")
        assert {r["language"] for r in rows} == {"sql", "python", "notebook", "text"}

    def test_nodes_carry_file_count_and_source(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT source, table_key, file_count FROM v_nodes WHERE table_key = ?",
            "warehouse.dim_store",
        )
        assert len(rows) == 1
        assert rows[0]["source"] == "fixtures"
        # dim_store appears in the SQL, PySpark, notebook and text fixtures.
        assert rows[0]["file_count"] >= 4

    def test_edges_carry_occurrence_and_file_counts(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT occurrence_count, file_count FROM v_edges "
            "WHERE left_table = ? AND right_table = ? AND ambiguous = 0",
            "warehouse.dim_customer",
            "warehouse.fct_orders",
        )
        assert rows
        assert max(r["occurrence_count"] for r in rows) >= 3
        assert max(r["file_count"] for r in rows) >= 2

    def test_occurrences_point_at_real_files_and_lines(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT rel_path, line, language FROM v_occurrences WHERE left_table = ?",
            "warehouse.dim_customer",
        )
        assert rows
        assert all(r["line"] > 0 for r in rows)
        assert any(r["rel_path"].endswith(".sql") for r in rows)

    def test_edge_conditions_are_broken_out(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT ec.predicate, ec.operator FROM edge_conditions ec "
            "JOIN v_edges e ON e.id = ec.edge_id "
            "WHERE e.left_table = ? AND e.right_table = ? AND e.condition_count = 1",
            "warehouse.dim_region",
            "warehouse.dim_store",
        )
        assert rows
        assert rows[0]["operator"] == "="

    def test_source_selection(self, tmp_path):
        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [
                    {"name": "a", "roots": [str(FIXTURES / "warehouse")]},
                    {"name": "b", "roots": [str(FIXTURES / "etl")]},
                ],
            },
            base_dir=tmp_path,
        )
        stats = scan(config, sources=["a"], workers=1)
        assert stats.sources == ("a",)
        rows = query(Database(config.database), "SELECT name FROM sources")
        assert {r["name"] for r in rows} == {"a"}

    def test_unknown_source_is_rejected(self, tmp_path):
        with pytest.raises(KeyError, match="unknown source"):
            scan(config_for(tmp_path), sources=["nope"], workers=1)

    def test_rescan_replaces_rather_than_duplicates(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        before = query(Database(config.database), "SELECT COUNT(*) AS n FROM edges")[0]["n"]
        scan(config, workers=1)
        after = query(Database(config.database), "SELECT COUNT(*) AS n FROM edges")[0]["n"]
        assert before == after

    def test_language_filter_limits_the_walk(self, tmp_path):
        stats = scan(config_for(tmp_path, languages=["sql"]), workers=1)
        assert stats.files == 2

    def test_large_files_are_skipped(self, tmp_path):
        stats = scan(config_for(tmp_path, max_file_bytes=10), workers=1)
        assert stats.files == 0
        assert stats.skipped == 5

    def test_commented_out_joins_never_reach_the_graph(self, tmp_path):
        """The fixtures contain retired joins in every comment style."""
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT table_key FROM v_nodes WHERE table_key LIKE '%retired%'",
        )
        assert rows == []

    def test_excluded_directory_is_pruned(self, tmp_path):
        stats = scan(config_for(tmp_path, exclude=["**/etl/**"]), workers=1)
        assert stats.files == 2


class TestCache:
    def test_second_scan_uses_the_cache(self, tmp_path):
        config = config_for(tmp_path)
        first = scan(config, workers=1)
        assert first.cached == 0
        second = scan(config, workers=1)
        assert second.cached == second.files == first.files

    def test_cached_results_match_uncached(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        cached = query(
            Database(config.database),
            "SELECT left_table, right_table, join_type, condition FROM v_edges ORDER BY 1,2,3,4",
        )
        scan(config, workers=1)
        again = query(
            Database(config.database),
            "SELECT left_table, right_table, join_type, condition FROM v_edges ORDER BY 1,2,3,4",
        )
        assert [tuple(r) for r in cached] == [tuple(r) for r in again]

    def test_changing_settings_invalidates_the_cache(self, tmp_path):
        scan(config_for(tmp_path), workers=1)
        # A different dialect is a different parse, so nothing may be reused.
        stats = scan(config_for(tmp_path, sql_dialect="snowflake"), workers=1)
        assert stats.cached == 0

    def test_cache_can_be_disabled(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        stats = scan(config, workers=1, use_cache=False)
        assert stats.cached == 0


class TestGitProvenance:
    def test_branch_and_commit_are_recorded(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "sql").mkdir(parents=True)
        (repo / "sql" / "q.sql").write_text(
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k", encoding="utf-8"
        )

        def git(*args):
            subprocess.run(
                ("git", *args), cwd=repo, check=True, capture_output=True, text=True
            )

        git("init", "-b", "feature/joins")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        git("add", "-A")
        git("commit", "-m", "initial")
        git("remote", "add", "origin", "https://user:secret@example.com/org/repo.git")

        gitinfo.clear_cache()
        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "repo", "roots": [str(repo)]}],
            },
            base_dir=tmp_path,
        )
        scan(config, workers=1)
        gitinfo.clear_cache()

        rows = query(
            Database(config.database),
            "SELECT branch, commit_sha, remote FROM v_occurrences",
        )
        assert rows
        assert rows[0]["branch"] == "feature/joins"
        assert len(rows[0]["commit_sha"]) == 40
        # Credentials must never reach the database.
        assert "secret" not in (rows[0]["remote"] or "")
        assert rows[0]["remote"] == "https://example.com/org/repo.git"

    def test_a_folder_outside_git_still_scans(self, tmp_path):
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "q.sql").write_text(
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k", encoding="utf-8"
        )
        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "loose", "roots": [str(tmp_path / "code")]}],
            },
            base_dir=tmp_path,
        )
        stats = scan(config, workers=1)
        assert stats.files == 1


class TestParallel:
    """The process pool must produce exactly what the serial path produces."""

    @staticmethod
    def _corpus(tmp_path):
        code = tmp_path / "code"
        code.mkdir()
        # Comfortably above the pool threshold, so the pool is really used.
        for index in range(120):
            (code / ("q%d.sql" % index)).write_text(
                "SELECT 1 FROM s%d.orders o JOIN s%d.customers c ON o.cid = c.id;"
                % (index, index),
                encoding="utf-8",
            )
        return Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "big", "roots": [str(code)], "sql_dialect": "spark"}],
            },
            base_dir=tmp_path,
        )

    def test_parallel_matches_serial(self, tmp_path):
        config = self._corpus(tmp_path)

        scan(config, workers=4)
        parallel = query(
            Database(config.database),
            "SELECT left_table, right_table, join_type, condition FROM v_edges ORDER BY 1,2,3,4",
        )

        scan(config, workers=1, use_cache=False)
        serial = query(
            Database(config.database),
            "SELECT left_table, right_table, join_type, condition FROM v_edges ORDER BY 1,2,3,4",
        )

        assert len(parallel) == 120
        assert [tuple(r) for r in parallel] == [tuple(r) for r in serial]

    def test_parallel_scan_reports_no_errors(self, tmp_path):
        stats = scan(self._corpus(tmp_path), workers=4)
        assert stats.files == 120
        assert stats.errors == 0


class TestRobustness:
    def test_unreadable_and_binary_files_are_skipped(self, tmp_path):
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "good.sql").write_text(
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k", encoding="utf-8"
        )
        (tmp_path / "code" / "binary.sql").write_bytes(b"\x00\x01\x02\x03" * 100)
        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "mixed", "roots": [str(tmp_path / "code")]}],
            },
            base_dir=tmp_path,
        )
        stats = scan(config, workers=1)
        assert stats.files == 1
        assert stats.skipped == 1

    def test_missing_root_is_not_fatal(self, tmp_path):
        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "gone", "roots": [str(tmp_path / "nope")]}],
            },
            base_dir=tmp_path,
        )
        assert scan(config, workers=1).files == 0

    def test_scan_row_is_written(self, tmp_path):
        config = config_for(tmp_path)
        stats = scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT files_scanned, joins_found, finished_at FROM scans WHERE id = ?",
            stats.scan_id,
        )
        assert rows[0]["files_scanned"] == stats.files
        assert rows[0]["finished_at"]

    def test_readonly_open_of_a_missing_database(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            Database(tmp_path / "nothing.db").connect(readonly=True)

    def test_foreign_keys_hold(self, tmp_path):
        config = config_for(tmp_path)
        scan(config, workers=1)
        rows = query(
            Database(config.database),
            "SELECT COUNT(*) AS n FROM occurrences o "
            "LEFT JOIN edges e ON e.id = o.edge_id WHERE e.id IS NULL",
        )
        assert rows[0]["n"] == 0
