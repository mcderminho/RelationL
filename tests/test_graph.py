"""Graph queries: neighbourhoods, and shortest join paths with filtering."""

from __future__ import annotations

import pytest
from conftest import FIXTURES

from relationl.config import Config
from relationl.graph import GraphStore
from relationl.scanner import scan


@pytest.fixture(scope="module")
def store(tmp_path_factory) -> GraphStore:
    tmp_path = tmp_path_factory.mktemp("graph")
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


class TestCatalogue:
    def test_summary(self, store):
        summary = store.summary()
        assert summary["nodes"] > 0
        assert summary["edges"] > 0
        assert summary["sources"] == ["fixtures"]
        assert summary["last_scan"]["files_scanned"] == 5

    def test_nodes_are_ranked_by_usage(self, store):
        nodes = store.nodes(limit=5)
        counts = [n.join_count for n in nodes]
        assert counts == sorted(counts, reverse=True)

    def test_node_search(self, store):
        nodes = store.nodes(search="dim_")
        assert nodes
        assert all("dim_" in n.table_key for n in nodes)

    def test_min_occurrences_filter(self, store):
        assert len(store.edges(min_occurrences=1)) > len(store.edges(min_occurrences=3))

    def test_ambiguous_filter(self, store):
        confident = store.edges(include_ambiguous=False)
        assert confident
        assert all(not e.ambiguous for e in confident)
        assert len(confident) < len(store.edges())

    def test_edge_detail_includes_conditions_and_sites(self, store):
        edge = next(
            e
            for e in store.edges(include_ambiguous=False)
            if e.condition and e.occurrence_count >= 2
        )
        detail = store.edge_detail(edge.id)
        assert detail["conditions"]
        assert detail["occurrences"]
        assert all("rel_path" in o for o in detail["occurrences"])

    def test_edge_detail_of_a_missing_edge(self, store):
        assert store.edge_detail(999_999) is None


class TestNeighbourhood:
    def test_depth_one(self, store):
        result = store.neighbourhood("warehouse.fct_orders", depth=1)
        keys = {n["table"] for n in result["nodes"]}
        assert "warehouse.fct_orders" in keys
        assert "warehouse.dim_customer" in keys

    def test_depth_grows_the_subgraph(self, store):
        one = store.neighbourhood("staging.raw_customers", depth=1)
        two = store.neighbourhood("staging.raw_customers", depth=2)
        assert len(two["nodes"]) > len(one["nodes"])

    def test_unknown_table(self, store):
        assert store.neighbourhood("nope.nope")["nodes"] == []

    def test_edges_are_confined_to_returned_nodes(self, store):
        result = store.neighbourhood("warehouse.dim_region", depth=1)
        keys = {n["table"] for n in result["nodes"]}
        for edge in result["edges"]:
            assert edge["left"] in keys and edge["right"] in keys


class TestShortestPath:
    def test_direct_join(self, store):
        paths = store.shortest_paths(
            "warehouse.dim_customer", "warehouse.fct_orders", k=1
        )
        assert paths
        assert paths[0].length == 1
        assert paths[0].tables == ["warehouse.dim_customer", "warehouse.fct_orders"]

    def test_multi_hop_path(self, store):
        paths = store.shortest_paths(
            "warehouse.dim_region", "warehouse.fct_order_items", min_occurrences=2, k=1
        )
        assert paths
        path = paths[0]
        assert path.tables[0] == "warehouse.dim_region"
        assert path.tables[-1] == "warehouse.fct_order_items"
        # Consecutive tables must actually be joined by the corresponding edge.
        for index, edge in enumerate(path.edges):
            assert {path.tables[index], path.tables[index + 1]} == {edge.left, edge.right}

    def test_every_edge_meets_the_threshold(self, store):
        paths = store.shortest_paths(
            "warehouse.dim_region", "warehouse.fct_order_items", min_occurrences=2
        )
        assert paths
        for path in paths:
            assert all(e.occurrence_count >= 2 for e in path.edges)
            assert path.weakest_link >= 2

    def test_raising_the_threshold_can_remove_a_path(self, store):
        assert store.shortest_paths("staging.raw_customers", "warehouse.dim_product", k=1)
        assert (
            store.shortest_paths(
                "staging.raw_customers", "warehouse.dim_product", min_occurrences=99, k=1
            )
            == []
        )

    def test_k_returns_distinct_routes(self, store):
        paths = store.shortest_paths(
            "warehouse.dim_region", "warehouse.fct_order_items", min_occurrences=2, k=3
        )
        signatures = [tuple(e.id for e in p.edges) for p in paths]
        assert len(signatures) == len(set(signatures))

    def test_paths_are_ordered_shortest_first(self, store):
        paths = store.shortest_paths(
            "warehouse.dim_region", "warehouse.fct_order_items", min_occurrences=2, k=3
        )
        lengths = [p.length for p in paths]
        assert lengths == sorted(lengths)

    def test_same_table(self, store):
        paths = store.shortest_paths("warehouse.fct_orders", "warehouse.fct_orders")
        assert paths[0].length == 0

    def test_unknown_tables(self, store):
        assert store.shortest_paths("nope.a", "warehouse.fct_orders") == []
        assert store.shortest_paths("warehouse.fct_orders", "nope.b") == []

    def test_case_insensitive(self, store):
        assert store.shortest_paths(
            "WAREHOUSE.DIM_CUSTOMER", "Warehouse.Fct_Orders", k=1
        )

    def test_max_hops_is_respected(self, store):
        assert (
            store.shortest_paths(
                "staging.raw_customers", "warehouse.dim_product", max_hops=1, k=1
            )
            == []
        )

    def test_serialisation(self, store):
        path = store.shortest_paths(
            "warehouse.dim_customer", "warehouse.fct_orders", k=1
        )[0]
        payload = path.as_dict()
        assert payload["length"] == 1
        assert payload["edges"][0]["condition"]
