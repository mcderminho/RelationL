"""Query the stored graph: neighbourhoods and shortest join paths.

The interesting question this answers is "how do I get from table A to table B?"
-- the sequence of joins, with their conditions, that connects them.  Edges can
be filtered by how many times a join was actually written, which is the main
tool for separating a load-bearing relationship from a one-off.
"""

from __future__ import annotations

import heapq
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, field

from .storage import Database


@dataclass(frozen=True)
class Node:
    id: int
    source: str
    table_key: str
    catalog: str | None
    schema: str | None
    name: str
    file_count: int
    join_count: int
    degree: int

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "table": self.table_key,
            "catalog": self.catalog,
            "schema": self.schema,
            "name": self.name,
            "file_count": self.file_count,
            "join_count": self.join_count,
            "degree": self.degree,
        }


@dataclass(frozen=True)
class Edge:
    id: int
    source: str
    left: str
    right: str
    join_type: str
    condition: str
    occurrence_count: int
    file_count: int
    implicit: bool
    ambiguous: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "left": self.left,
            "right": self.right,
            "join_type": self.join_type,
            "condition": self.condition,
            "occurrence_count": self.occurrence_count,
            "file_count": self.file_count,
            "implicit": self.implicit,
            "ambiguous": self.ambiguous,
        }


@dataclass
class Path:
    """One route between two tables."""

    tables: list[str] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.edges)

    @property
    def weakest_link(self) -> int:
        """Occurrence count of the least-attested join on the path."""
        return min((e.occurrence_count for e in self.edges), default=0)

    def as_dict(self) -> dict:
        return {
            "tables": self.tables,
            "edges": [e.as_dict() for e in self.edges],
            "length": self.length,
            "weakest_link": self.weakest_link,
        }


class GraphStore:
    """Read-only access to a scanned graph."""

    def __init__(self, database: Database | str) -> None:
        self.database = database if isinstance(database, Database) else Database(database)

    def _connect(self) -> sqlite3.Connection:
        return self.database.connect(readonly=True)

    # -- catalogue ----------------------------------------------------------

    def sources(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT name FROM sources ORDER BY name").fetchall()
        return [row["name"] for row in rows]

    def summary(self) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT (SELECT COUNT(*) FROM nodes) AS nodes, "
                "       (SELECT COUNT(*) FROM edges) AS edges, "
                "       (SELECT COUNT(*) FROM occurrences) AS occurrences, "
                "       (SELECT COUNT(*) FROM files) AS files"
            ).fetchone()
            scan = connection.execute(
                "SELECT id, started_at, finished_at, files_scanned, joins_found, duration_ms "
                "FROM scans ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "nodes": row["nodes"],
            "edges": row["edges"],
            "occurrences": row["occurrences"],
            "files": row["files"],
            "last_scan": dict(scan) if scan else None,
            "sources": self.sources(),
        }

    def nodes(
        self,
        *,
        source: str | None = None,
        search: str | None = None,
        limit: int = 500,
    ) -> list[Node]:
        sql = ["SELECT * FROM v_nodes WHERE 1 = 1"]
        params: list[object] = []
        if source:
            sql.append("AND source = ?")
            params.append(source)
        if search:
            sql.append("AND table_key LIKE ?")
            params.append("%%%s%%" % search.lower())
        sql.append("ORDER BY join_count DESC, file_count DESC, table_key LIMIT ?")
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(" ".join(sql), params).fetchall()
        return [_node(row) for row in rows]

    def edges(
        self,
        *,
        source: str | None = None,
        min_occurrences: int = 1,
        limit: int = 5000,
        tables: Sequence[str] | None = None,
        include_ambiguous: bool = True,
    ) -> list[Edge]:
        sql = ["SELECT * FROM v_edges WHERE occurrence_count >= ?"]
        params: list[object] = [min_occurrences]
        if not include_ambiguous:
            sql.append("AND ambiguous = 0")
        if source:
            sql.append("AND source = ?")
            params.append(source)
        if tables:
            placeholders = ",".join("?" for _ in tables)
            sql.append(
                "AND left_table IN (%s) AND right_table IN (%s)"
                % (placeholders, placeholders)
            )
            params.extend(tables)
            params.extend(tables)
        sql.append("ORDER BY occurrence_count DESC LIMIT ?")
        params.append(limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(" ".join(sql), params).fetchall()
        return [_edge(row) for row in rows]

    def occurrences(self, edge_id: int, limit: int = 100) -> list[dict]:
        """Where an edge was seen, with the branch and commit it came from."""
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT rel_path, path, line, language, branch, commit_sha, remote "
                "FROM v_occurrences WHERE edge_id = ? ORDER BY rel_path, line LIMIT ?",
                (edge_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def edge_detail(self, edge_id: int) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM v_edges WHERE id = ?", (edge_id,)
            ).fetchone()
            if row is None:
                return None
            conditions = connection.execute(
                "SELECT predicate, operator, left_column, right_column "
                "FROM edge_conditions WHERE edge_id = ? ORDER BY ordinal",
                (edge_id,),
            ).fetchall()
        detail = _edge(row).as_dict()
        detail["conditions"] = [dict(c) for c in conditions]
        detail["occurrences"] = self.occurrences(edge_id)
        return detail

    # -- paths --------------------------------------------------------------

    def shortest_paths(
        self,
        start: str,
        end: str,
        *,
        source: str | None = None,
        min_occurrences: int = 1,
        k: int = 3,
        max_hops: int = 8,
        include_ambiguous: bool = True,
    ) -> list[Path]:
        """Up to ``k`` shortest join paths between two tables.

        Edges seen fewer than ``min_occurrences`` times are excluded, so the
        caller can demand that a route be well attested rather than incidental.
        Ties on hop count are broken by preferring the better-attested route.
        """
        start, end = start.lower(), end.lower()
        if start == end:
            return [Path(tables=[start], edges=[])]

        adjacency = self._adjacency(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
        )
        if start not in adjacency or end not in adjacency:
            return []

        found: list[Path] = []
        banned_edges: set[int] = set()
        for _ in range(max(1, k)):
            path = _dijkstra(adjacency, start, end, banned_edges, max_hops)
            if path is None:
                break
            found.append(path)
            # Yen-style diversification: drop the least-attested edge of the
            # route just found so the next search has to go around it.
            weakest = min(path.edges, key=lambda e: e.occurrence_count, default=None)
            if weakest is None:
                break
            banned_edges.add(weakest.id)
        return found

    def neighbourhood(
        self,
        table: str,
        *,
        depth: int = 1,
        source: str | None = None,
        min_occurrences: int = 1,
        limit: int = 300,
        include_ambiguous: bool = True,
    ) -> dict:
        """The subgraph within ``depth`` hops of one table."""
        table = table.lower()
        adjacency = self._adjacency(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
        )
        if table not in adjacency:
            return {"nodes": [], "edges": []}

        reached = {table}
        frontier = {table}
        edges: dict[int, Edge] = {}
        for _ in range(max(0, depth)):
            next_frontier: set[str] = set()
            for current in frontier:
                for neighbour, edge in adjacency.get(current, ()):
                    edges[edge.id] = edge
                    if neighbour not in reached and len(reached) < limit:
                        reached.add(neighbour)
                        next_frontier.add(neighbour)
            frontier = next_frontier
            if not frontier:
                break

        nodes = self.nodes_by_key(sorted(reached), source=source)
        keep = {n.table_key for n in nodes}
        return {
            "nodes": [n.as_dict() for n in nodes],
            "edges": [
                e.as_dict() for e in edges.values() if e.left in keep and e.right in keep
            ],
        }

    def subgraph(
        self,
        *,
        source: str | None = None,
        min_occurrences: int = 1,
        include_ambiguous: bool = True,
        limit: int = 400,
    ) -> dict:
        """Nodes and edges for the explorer, in one round trip.

        Nodes are ranked by how often they are joined, so a large graph
        degrades to its busiest core rather than an arbitrary slice.
        """
        edges = self.edges(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
            limit=200_000,
        )
        ranked = self.nodes(source=source, limit=limit)
        keep = {node.table_key for node in ranked}
        visible = [e for e in edges if e.left in keep and e.right in keep]

        # Drop tables left with no visible join.  This is a join graph, so an
        # isolated node carries no information, and in a force layout it drifts
        # to the margin and squeezes the connected core out of view.
        connected = {e.left for e in visible} | {e.right for e in visible}
        nodes = [n for n in ranked if n.table_key in connected]
        return {
            "nodes": [n.as_dict() for n in nodes],
            "edges": [e.as_dict() for e in visible],
            "isolated": len(ranked) - len(nodes),
            "truncated": len(ranked) >= limit,
        }

    def model(
        self,
        *,
        source: str | None = None,
        min_occurrences: int = 1,
        include_ambiguous: bool = True,
        limit: int = 200,
    ) -> dict:
        """Tables with their columns, and relationships keyed to those columns.

        This is what the semantic model view draws.  The columns are the ones
        the codebase references, not a schema read from a warehouse, so a table
        shows the columns it is actually used by.
        """
        edges = self.edges(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
            limit=200_000,
        )
        ranked = self.nodes(source=source, limit=limit)
        keep = {n.table_key for n in ranked}
        visible = [e for e in edges if e.left in keep and e.right in keep]
        connected = {e.left for e in visible} | {e.right for e in visible}
        tables = [n for n in ranked if n.table_key in connected]
        wanted = {n.table_key for n in tables}

        columns = self._columns_by_table(wanted, source=source)
        relationships = self._relationships(visible)

        # Whether a column is a join key is decided by the relationships that
        # are actually on screen, not by every join ever recorded: with the
        # ambiguous ones filtered out, their keys must stop being highlighted.
        keyed: set[tuple[str, str]] = set()
        for relationship in relationships:
            for pair in relationship["pairs"]:
                keyed.add((relationship["left"], pair["left_column"]))
                keyed.add((relationship["right"], pair["right_column"]))

        for table_key, entries in columns.items():
            for column in entries:
                column["is_join_key"] = (table_key, column["name"]) in keyed
            entries.sort(key=lambda c: (not c["is_join_key"], c["name"]))

        return {
            "tables": [
                {**node.as_dict(), "columns": columns.get(node.table_key, [])}
                for node in tables
            ],
            "relationships": relationships,
            "isolated": len(ranked) - len(tables),
            "truncated": len(ranked) >= limit,
        }

    def _columns_by_table(
        self, tables: set[str], *, source: str | None
    ) -> dict[str, list[dict]]:
        if not tables:
            return {}
        placeholders = ",".join("?" for _ in tables)
        sql = (
            "SELECT table_key, name, is_join_key, ref_count FROM v_columns "
            "WHERE table_key IN (%s)" % placeholders
        )
        params: list[object] = sorted(tables)
        if source:
            sql += " AND source = ?"
            params.append(source)
        # Join keys first: they are what the relationships attach to.
        sql += " ORDER BY table_key, is_join_key DESC, name"

        out: dict[str, list[dict]] = {}
        with closing(self._connect()) as connection:
            for row in connection.execute(sql, params):
                out.setdefault(row["table_key"], []).append(
                    {
                        "name": row["name"],
                        "is_join_key": bool(row["is_join_key"]),
                        "ref_count": row["ref_count"],
                    }
                )
        return out

    def _relationships(self, edges: Sequence[Edge]) -> list[dict]:
        """Attach each edge to the specific columns its predicates key on."""
        if not edges:
            return []
        by_id = {edge.id: edge for edge in edges}
        placeholders = ",".join("?" for _ in by_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT edge_id, ordinal, left_column, right_column, operator "
                "FROM edge_conditions WHERE edge_id IN (%s) ORDER BY edge_id, ordinal"
                % placeholders,
                sorted(by_id),
            ).fetchall()

        pairs: dict[int, list[dict]] = {}
        for row in rows:
            edge = by_id[row["edge_id"]]
            pair = _column_pair(edge, row["left_column"], row["right_column"])
            if pair is None:
                continue
            bucket = pairs.setdefault(edge.id, [])
            if pair not in bucket:
                pair["operator"] = row["operator"]
                bucket.append(pair)

        out = []
        for edge in edges:
            payload = edge.as_dict()
            payload["pairs"] = pairs.get(edge.id, [])
            out.append(payload)
        return out

    def nodes_by_key(self, keys: Sequence[str], *, source: str | None = None) -> list[Node]:
        if not keys:
            return []
        placeholders = ",".join("?" for _ in keys)
        sql = "SELECT * FROM v_nodes WHERE table_key IN (%s)" % placeholders
        params: list[object] = list(keys)
        if source:
            sql += " AND source = ?"
            params.append(source)
        with closing(self._connect()) as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_node(row) for row in rows]

    def _adjacency(
        self, *, source: str | None, min_occurrences: int, include_ambiguous: bool = True
    ) -> dict[str, list[tuple[str, Edge]]]:
        """Undirected adjacency list, built in one query."""
        adjacency: dict[str, list[tuple[str, Edge]]] = {}
        for edge in self.edges(
            source=source,
            min_occurrences=min_occurrences,
            include_ambiguous=include_ambiguous,
            limit=200_000,
        ):
            adjacency.setdefault(edge.left, []).append((edge.right, edge))
            adjacency.setdefault(edge.right, []).append((edge.left, edge))
        return adjacency


def _dijkstra(
    adjacency: dict[str, list[tuple[str, Edge]]],
    start: str,
    end: str,
    banned_edges: set[int],
    max_hops: int,
) -> Path | None:
    """Fewest hops first, then best-attested.

    The cost of a hop is ``(1, -occurrence_count)`` compared lexicographically,
    so the search never trades an extra join for a more popular one.
    """
    queue: list[tuple[int, int, int, str]] = [(0, 0, 0, start)]
    best: dict[str, tuple[int, int]] = {start: (0, 0)}
    previous: dict[str, tuple[str, Edge]] = {}
    counter = 0

    while queue:
        hops, penalty, _, current = heapq.heappop(queue)
        if current == end:
            return _rebuild(previous, start, end)
        if best.get(current, (hops, penalty)) < (hops, penalty):
            continue
        if hops >= max_hops:
            continue
        for neighbour, edge in adjacency.get(current, ()):
            if edge.id in banned_edges:
                continue
            candidate = (hops + 1, penalty - edge.occurrence_count)
            if candidate < best.get(neighbour, (1 << 30, 0)):
                best[neighbour] = candidate
                previous[neighbour] = (current, edge)
                counter += 1
                heapq.heappush(queue, (candidate[0], candidate[1], counter, neighbour))
    return None


def _rebuild(
    previous: dict[str, tuple[str, Edge]], start: str, end: str
) -> Path:
    tables = [end]
    edges: list[Edge] = []
    current = end
    while current != start:
        parent, edge = previous[current]
        edges.append(edge)
        tables.append(parent)
        current = parent
    tables.reverse()
    edges.reverse()
    return Path(tables=tables, edges=edges)


def _column_pair(edge: Edge, left: str | None, right: str | None) -> dict | None:
    """Resolve two qualified column names onto the edge's left and right sides.

    Returns ``None`` for a predicate that is not a plain column comparison
    (``LOWER(a.x) = LOWER(b.y)``), which has no single column to anchor to.
    """
    if not left or not right:
        return None
    first = _split_qualified(edge, left)
    second = _split_qualified(edge, right)
    if first is None or second is None:
        return None

    (left_table, left_column) = first
    (right_table, right_column) = second
    if left_table == edge.left and right_table == edge.right:
        return {"left_column": left_column, "right_column": right_column}
    if left_table == edge.right and right_table == edge.left:
        return {"left_column": right_column, "right_column": left_column}
    return None


def _split_qualified(edge: Edge, qualified: str) -> tuple[str, str] | None:
    """Split "schema.table.column" using the edge's own table names.

    Splitting on the last dot would be wrong for a table whose name contains
    one, so the known table keys are matched as prefixes instead.
    """
    for table in (edge.left, edge.right):
        prefix = table + "."
        if qualified.startswith(prefix):
            return table, qualified[len(prefix) :]
    return None


def _node(row: sqlite3.Row) -> Node:
    return Node(
        id=row["id"],
        source=row["source"],
        table_key=row["table_key"],
        catalog=row["catalog"],
        schema=row["schema_name"],
        name=row["table_name"],
        file_count=row["file_count"],
        join_count=row["join_count"],
        degree=row["degree"],
    )


def _edge(row: sqlite3.Row) -> Edge:
    return Edge(
        id=row["id"],
        source=row["source"],
        left=row["left_table"],
        right=row["right_table"],
        join_type=row["join_type"],
        condition=row["condition"],
        occurrence_count=row["occurrence_count"],
        file_count=row["file_count"],
        implicit=bool(row["implicit"]),
        ambiguous=bool(row["ambiguous"]),
    )


def _iter_unique(items: Iterable[Edge]) -> list[Edge]:  # pragma: no cover - helper
    seen: set[int] = set()
    out = []
    for item in items:
        if item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out
