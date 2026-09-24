"""The HTTP API behind the graph explorer."""

from __future__ import annotations

import pytest
from conftest import FIXTURES

from relationl.config import Config
from relationl.scanner import scan

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from relationl.web.app import create_app  # noqa: E402


@pytest.fixture(scope="module")
def client(tmp_path_factory) -> TestClient:
    tmp_path = tmp_path_factory.mktemp("web")
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
    return TestClient(create_app(config.database))


class TestPages:
    def test_index_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "RelationL" in response.text

    @pytest.mark.parametrize(
        "path",
        ["/static/styles.css", "/static/app.js", "/static/model.js", "/static/favicon.svg"],
    )
    def test_assets_are_served(self, client, path):
        assert client.get(path).status_code == 200

    def test_logo_is_inlined_so_it_follows_the_theme(self, client):
        body = client.get("/").text
        assert "logo-ink" in body and "logo-mark" in body

    def test_no_external_requests_in_the_frontend(self, client):
        """This has to work offline, so nothing may be fetched from a CDN."""
        for path in ("/", "/static/styles.css", "/static/app.js"):
            body = client.get(path).text
            assert "https://" not in body.replace("https://www.w3.org/2000/svg", "")


class TestApi:
    def test_summary(self, client):
        data = client.get("/api/summary").json()
        assert data["nodes"] > 0
        assert data["edges"] > 0
        assert data["sources"] == ["fixtures"]
        assert data["version"]

    def test_graph_shape(self, client):
        data = client.get("/api/graph").json()
        assert data["nodes"] and data["edges"]
        keys = {node["table"] for node in data["nodes"]}
        for edge in data["edges"]:
            assert edge["left"] in keys and edge["right"] in keys

    def test_graph_min_occurrences_filter(self, client):
        loose = client.get("/api/graph", params={"min_occurrences": 1}).json()
        tight = client.get("/api/graph", params={"min_occurrences": 3}).json()
        assert len(tight["edges"]) < len(loose["edges"])

    def test_graph_confident_only_filter(self, client):
        data = client.get("/api/graph", params={"include_ambiguous": False}).json()
        assert data["edges"]
        assert all(not edge["ambiguous"] for edge in data["edges"])

    def test_tables_search(self, client):
        data = client.get("/api/tables", params={"search": "dim_"}).json()
        assert data
        assert all("dim_" in row["table"] for row in data)

    def test_tables_expose_file_count_and_source(self, client):
        data = client.get("/api/tables").json()
        row = next(r for r in data if r["table"] == "warehouse.dim_store")
        assert row["file_count"] >= 4
        assert row["source"] == "fixtures"

    def test_edge_detail(self, client):
        edges = client.get("/api/graph", params={"include_ambiguous": False}).json()["edges"]
        edge = next(e for e in edges if e["condition"])
        detail = client.get("/api/edges/%d" % edge["id"]).json()
        assert detail["conditions"]
        assert detail["occurrences"]
        assert "rel_path" in detail["occurrences"][0]

    def test_missing_edge_is_404(self, client):
        assert client.get("/api/edges/987654").status_code == 404

    def test_neighbourhood(self, client):
        data = client.get(
            "/api/neighbourhood", params={"table": "warehouse.fct_orders", "depth": 1}
        ).json()
        assert "warehouse.fct_orders" in {n["table"] for n in data["nodes"]}

    def test_path(self, client):
        data = client.get(
            "/api/path",
            params={
                "start": "warehouse.dim_region",
                "end": "warehouse.fct_order_items",
                "min_occurrences": 2,
            },
        ).json()
        assert data["routes"]
        route = data["routes"][0]
        assert route["tables"][0] == "warehouse.dim_region"
        assert route["tables"][-1] == "warehouse.fct_order_items"
        assert all(edge["occurrence_count"] >= 2 for edge in route["edges"])

    def test_path_with_no_route(self, client):
        data = client.get(
            "/api/path",
            params={
                "start": "warehouse.dim_region",
                "end": "warehouse.fct_order_items",
                "min_occurrences": 99,
            },
        ).json()
        assert data["routes"] == []

    @pytest.mark.parametrize(
        "params",
        [
            {"min_occurrences": 0},
            {"limit": 99999},
            {"limit": 0},
        ],
    )
    def test_out_of_range_parameters_are_rejected(self, client, params):
        assert client.get("/api/graph", params=params).status_code == 422

    def test_missing_database_reports_unavailable(self, tmp_path):
        app = create_app(tmp_path / "absent.db")
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/summary")
        assert response.status_code == 503
