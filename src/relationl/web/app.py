"""FastAPI app serving the RelationL graph explorer.

Read-only throughout: it opens the SQLite file in ``mode=ro``, so it is safe to
point at a database that a scheduled scan is rewriting.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..graph import GraphStore
from ..storage import Database

STATIC_DIR = Path(__file__).parent / "static"

#: Upper bounds on anything a query string can ask for, so a hand-crafted URL
#: cannot ask the server to materialise an unbounded graph.
MAX_NODES = 2000
MAX_PATHS = 10


def create_app(database: Database | str | Path) -> FastAPI:
    store = GraphStore(database if isinstance(database, Database) else Database(database))

    app = FastAPI(
        title="RelationL",
        version=__version__,
        description="Explore the join graph recovered from a codebase.",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.exception_handler(FileNotFoundError)
    async def _missing_database(_request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/api/summary")
    def summary() -> dict:
        data = store.summary()
        data["version"] = __version__
        return data

    @app.get("/api/graph")
    def graph(
        source: str | None = None,
        min_occurrences: int = Query(1, ge=1),
        include_ambiguous: bool = True,
        limit: int = Query(400, ge=1, le=MAX_NODES),
    ) -> dict:
        return store.subgraph(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
            limit=limit,
        )

    @app.get("/api/tables")
    def tables(
        search: str | None = None,
        source: str | None = None,
        limit: int = Query(200, ge=1, le=MAX_NODES),
    ) -> list[dict]:
        return [n.as_dict() for n in store.nodes(source=source, search=search, limit=limit)]

    @app.get("/api/edges/{edge_id}")
    def edge(edge_id: int) -> dict:
        detail = store.edge_detail(edge_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="no such join")
        return detail

    @app.get("/api/neighbourhood")
    def neighbourhood(
        table: str,
        depth: int = Query(1, ge=1, le=4),
        source: str | None = None,
        min_occurrences: int = Query(1, ge=1),
        include_ambiguous: bool = True,
    ) -> dict:
        return store.neighbourhood(
            table,
            depth=depth,
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
        )

    @app.get("/api/path")
    def path(
        start: str,
        end: str,
        source: str | None = None,
        min_occurrences: int = Query(1, ge=1),
        include_ambiguous: bool = True,
        k: int = Query(3, ge=1, le=MAX_PATHS),
    ) -> dict:
        routes = store.shortest_paths(
            start,
            end,
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
            k=k,
        )
        return {
            "start": start.lower(),
            "end": end.lower(),
            "min_occurrences": min_occurrences,
            "routes": [r.as_dict() for r in routes],
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
