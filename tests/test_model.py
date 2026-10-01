"""The semantic model: per-table columns, and relationships keyed to them."""

from __future__ import annotations

import pytest
from conftest import FIXTURES

from relationl.config import Config
from relationl.graph import GraphStore
from relationl.scanner import scan

COMPOSITE = ("warehouse.dim_product", "warehouse.fct_order_items")


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> GraphStore:
    tmp_path = tmp_path_factory.mktemp("model")
    config = Config.from_dict(
        {
            "database": str(tmp_path / "graph.db"),
            "sources": [
                {"name": "fixtures", "roots": [str(FIXTURES)], "sql_dialect": "spark"}
            ],
        },
        base_dir=tmp_path,
    )
    scan(config, workers=1)
    return GraphStore(config.database)


def table_of(model, key):
    return next(t for t in model["tables"] if t["table"] == key)


def relationship_between(model, left, right, *, pairs=None):
    for relationship in model["relationships"]:
        if {relationship["left"], relationship["right"]} != {left, right}:
            continue
        if pairs is None or len(relationship["pairs"]) == pairs:
            return relationship
    return None


class TestColumns:
    def test_columns_are_extracted_from_sql(self, store):
        columns = {c["name"] for c in table_of(store.model(), "warehouse.fct_orders")["columns"]}
        # Join keys and plain references alike.
        assert {"customer_id", "store_id", "order_id", "amount", "order_ts"} <= columns

    def test_columns_are_extracted_from_pyspark(self, store):
        columns = {c["name"] for c in table_of(store.model(), "warehouse.dim_store")["columns"]}
        assert {"store_id", "region_id", "is_active"} <= columns

    def test_join_keys_are_flagged(self, store):
        columns = {
            c["name"]: c["is_join_key"]
            for c in table_of(store.model(), "warehouse.dim_region")["columns"]
        }
        assert columns["region_id"] is True
        assert columns["region_name"] is False

    def test_join_keys_sort_first(self, store):
        columns = table_of(store.model(), "warehouse.fct_orders")["columns"]
        keys = [c["is_join_key"] for c in columns]
        assert keys == sorted(keys, reverse=True)

    def test_join_keys_follow_the_visible_relationships(self, store):
        """With candidates hidden, their keys must stop being highlighted."""
        loose = table_of(store.model(), "warehouse.dim_customer")["columns"]
        strict = table_of(
            store.model(include_ambiguous=False), "warehouse.dim_customer"
        )["columns"]
        loose_keys = {c["name"] for c in loose if c["is_join_key"]}
        strict_keys = {c["name"] for c in strict if c["is_join_key"]}
        assert "customer_id" in strict_keys
        assert strict_keys < loose_keys

    def test_a_column_is_never_attributed_to_two_tables(self, store):
        """``segment`` belongs to dim_customer and must not appear elsewhere."""
        model = store.model()
        owners = {
            table["table"]
            for table in model["tables"]
            if any(c["name"] == "segment" for c in table["columns"])
        }
        assert owners == {"warehouse.dim_customer"}


class TestRelationships:
    def test_single_column_relationship(self, store):
        relationship = relationship_between(
            store.model(include_ambiguous=False),
            "warehouse.dim_region",
            "warehouse.dim_store",
        )
        assert relationship["pairs"] == [
            {"left_column": "region_id", "right_column": "region_id", "operator": "="}
        ]

    def test_composite_relationship_carries_both_columns(self, store):
        """This is what the model view fans the arrow out to."""
        relationship = relationship_between(
            store.model(include_ambiguous=False), *COMPOSITE, pairs=2
        )
        assert relationship is not None
        pairs = {(p["left_column"], p["right_column"]) for p in relationship["pairs"]}
        assert pairs == {("product_id", "product_id"), ("region_id", "region_id")}

    def test_composite_relationship_is_one_edge_across_both_languages(self, store):
        relationship = relationship_between(
            store.model(include_ambiguous=False), *COMPOSITE, pairs=2
        )
        # Written once in SQL and once in PySpark, collapsed onto one edge.
        assert relationship["occurrence_count"] == 2
        assert relationship["file_count"] == 2

    def test_pairs_are_oriented_to_the_relationship(self, store):
        """``left_column`` must belong to ``left``, whichever way it was written."""
        model = store.model(include_ambiguous=False)
        columns = {t["table"]: {c["name"] for c in t["columns"]} for t in model["tables"]}
        for relationship in model["relationships"]:
            for pair in relationship["pairs"]:
                assert pair["left_column"] in columns[relationship["left"]]
                assert pair["right_column"] in columns[relationship["right"]]

    def test_predicate_without_plain_columns_has_no_pairs(self, sql, tmp_path):
        """``LOWER(a.x) = LOWER(b.y)`` has no single column to anchor to."""
        config = Config.from_dict(
            {
                "database": str(tmp_path / "g.db"),
                "sources": [{"name": "s", "roots": [str(tmp_path / "code")]}],
            },
            base_dir=tmp_path,
        )
        (tmp_path / "code").mkdir()
        (tmp_path / "code" / "q.sql").write_text(
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON lower(x.name) = lower(y.name)",
            encoding="utf-8",
        )
        scan(config, workers=1)
        model = GraphStore(config.database).model()
        relationship = relationship_between(model, "a.t1", "b.t2")
        assert relationship is not None
        assert relationship["pairs"] == []

    def test_relationships_respect_min_occurrences(self, store):
        tight = store.model(min_occurrences=3)
        assert tight["relationships"]
        assert all(r["occurrence_count"] >= 3 for r in tight["relationships"])

    def test_isolated_tables_are_excluded_and_counted(self, store):
        model = store.model(min_occurrences=3)
        keys = {t["table"] for t in model["tables"]}
        joined = {r["left"] for r in model["relationships"]} | {
            r["right"] for r in model["relationships"]
        }
        assert keys == joined
        assert model["isolated"] > 0

    def test_every_relationship_endpoint_has_a_table(self, store):
        model = store.model()
        keys = {t["table"] for t in model["tables"]}
        for relationship in model["relationships"]:
            assert relationship["left"] in keys
            assert relationship["right"] in keys


class TestModelApi:
    def test_endpoint(self, tmp_path):
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from relationl.web.app import create_app

        config = Config.from_dict(
            {
                "database": str(tmp_path / "graph.db"),
                "sources": [{"name": "f", "roots": [str(FIXTURES)], "sql_dialect": "spark"}],
            },
            base_dir=tmp_path,
        )
        scan(config, workers=1)
        client = TestClient(create_app(config.database))

        data = client.get("/api/model", params={"include_ambiguous": False}).json()
        assert data["tables"] and data["relationships"]
        assert any(len(r["pairs"]) == 2 for r in data["relationships"])
        assert client.get("/api/model", params={"limit": 0}).status_code == 422
