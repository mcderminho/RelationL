"""SQLite persistence for scan results.

The output is a graph: ``nodes`` are tables, ``edges`` are distinct joins
(a table pair plus a join type plus a canonical condition), and ``occurrences``
record every code site an edge was seen at, with its git provenance.

A scan replaces everything belonging to the sources it covered, inside one
transaction, so a reader never observes a half-written graph.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .models import FileFindings, Join

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scans (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    tool_version  TEXT,
    config_digest TEXT,
    files_scanned INTEGER NOT NULL DEFAULT 0,
    files_cached  INTEGER NOT NULL DEFAULT 0,
    joins_found   INTEGER NOT NULL DEFAULT 0,
    errors        INTEGER NOT NULL DEFAULT 0,
    duration_ms   INTEGER
);

CREATE TABLE IF NOT EXISTS sources (
    id   INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS repos (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    branch     TEXT,
    commit_sha TEXT,
    remote     TEXT,
    UNIQUE (root, branch, commit_sha)
);

CREATE TABLE IF NOT EXISTS files (
    id         INTEGER PRIMARY KEY,
    source_id  INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    rel_path   TEXT NOT NULL,
    language   TEXT NOT NULL,
    repo_id    INTEGER REFERENCES repos (id),
    digest     TEXT NOT NULL,
    size_bytes INTEGER,
    scan_id    INTEGER REFERENCES scans (id),
    UNIQUE (source_id, path)
);

CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,
    source_id   INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    table_key   TEXT NOT NULL,
    catalog     TEXT,
    schema_name TEXT,
    table_name  TEXT NOT NULL,
    file_count  INTEGER NOT NULL DEFAULT 0,
    join_count  INTEGER NOT NULL DEFAULT 0,
    degree      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, table_key)
);

CREATE TABLE IF NOT EXISTS node_files (
    node_id INTEGER NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, file_id)
);

CREATE TABLE IF NOT EXISTS edges (
    id               INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    left_node_id     INTEGER NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    right_node_id    INTEGER NOT NULL REFERENCES nodes (id) ON DELETE CASCADE,
    join_type        TEXT NOT NULL,
    condition        TEXT NOT NULL,
    condition_count  INTEGER NOT NULL DEFAULT 0,
    occurrence_count INTEGER NOT NULL DEFAULT 0,
    file_count       INTEGER NOT NULL DEFAULT 0,
    implicit         INTEGER NOT NULL DEFAULT 0,
    -- 1 when a column in the condition could not be pinned to one table, so
    -- this edge is a candidate rather than an established join.
    ambiguous        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (source_id, left_node_id, right_node_id, join_type, condition)
);

CREATE TABLE IF NOT EXISTS edge_conditions (
    edge_id      INTEGER NOT NULL REFERENCES edges (id) ON DELETE CASCADE,
    ordinal      INTEGER NOT NULL,
    predicate    TEXT NOT NULL,
    operator     TEXT,
    left_column  TEXT,
    right_column TEXT,
    PRIMARY KEY (edge_id, ordinal)
);

CREATE TABLE IF NOT EXISTS occurrences (
    id       INTEGER PRIMARY KEY,
    edge_id  INTEGER NOT NULL REFERENCES edges (id) ON DELETE CASCADE,
    file_id  INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    line     INTEGER NOT NULL DEFAULT 0,
    language TEXT,
    scan_id  INTEGER REFERENCES scans (id)
);

CREATE TABLE IF NOT EXISTS parse_errors (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    message TEXT NOT NULL
);

-- Content-addressed parse cache.  Keyed by the source's filter settings too,
-- because a change to table_rewrite alters what the same bytes produce.
CREATE TABLE IF NOT EXISTS parse_cache (
    source_name   TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    digest        TEXT NOT NULL,
    payload       TEXT NOT NULL,
    touched_at    REAL NOT NULL,
    PRIMARY KEY (source_name, source_digest, digest)
);

CREATE INDEX IF NOT EXISTS idx_nodes_key       ON nodes (table_key);
CREATE INDEX IF NOT EXISTS idx_edges_left      ON edges (left_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_right     ON edges (right_node_id);
CREATE INDEX IF NOT EXISTS idx_edges_count     ON edges (occurrence_count);
CREATE INDEX IF NOT EXISTS idx_edges_ambiguous ON edges (ambiguous);
CREATE INDEX IF NOT EXISTS idx_occ_edge        ON occurrences (edge_id);
CREATE INDEX IF NOT EXISTS idx_occ_file        ON occurrences (file_id);
CREATE INDEX IF NOT EXISTS idx_node_files_file ON node_files (file_id);
"""

#: Convenience views for anyone querying the database by hand.
VIEWS = """
CREATE VIEW IF NOT EXISTS v_nodes AS
SELECT n.id, s.name AS source, n.table_key, n.catalog, n.schema_name, n.table_name,
       n.file_count, n.join_count, n.degree
FROM nodes n JOIN sources s ON s.id = n.source_id;

CREATE VIEW IF NOT EXISTS v_edges AS
SELECT e.id, s.name AS source, l.table_key AS left_table, r.table_key AS right_table,
       e.join_type, e.condition, e.condition_count, e.occurrence_count, e.file_count,
       e.implicit, e.ambiguous
FROM edges e
JOIN sources s ON s.id = e.source_id
JOIN nodes   l ON l.id = e.left_node_id
JOIN nodes   r ON r.id = e.right_node_id;

CREATE VIEW IF NOT EXISTS v_occurrences AS
SELECT o.id, e.id AS edge_id, l.table_key AS left_table, r.table_key AS right_table,
       e.join_type, e.condition, f.rel_path, f.path, o.line, o.language,
       rp.branch, rp.commit_sha, rp.remote, s.name AS source
FROM occurrences o
JOIN edges   e  ON e.id = o.edge_id
JOIN nodes   l  ON l.id = e.left_node_id
JOIN nodes   r  ON r.id = e.right_node_id
JOIN files   f  ON f.id = o.file_id
JOIN sources s  ON s.id = e.source_id
LEFT JOIN repos rp ON rp.id = f.repo_id;
"""


@dataclass
class FileRecord:
    """One scanned file plus everything extracted from it."""

    source: str
    path: str
    rel_path: str
    language: str
    digest: str
    size_bytes: int
    repo_root: str | None
    branch: str | None
    commit_sha: str | None
    remote: str | None
    findings: FileFindings
    cached: bool = False


class Database:
    """Thin wrapper over the SQLite file holding a scan."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            # ``as_uri`` is what makes this work on Windows, where a bare
            # "file:C:/..." URI is not understood by SQLite.
            resolved = self.path.resolve()
            if not resolved.exists():
                raise FileNotFoundError("no RelationL database at %s" % resolved)
            connection = sqlite3.connect(
                "%s?mode=ro" % resolved.as_uri(), uri=True, check_same_thread=False
            )
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.path, check_same_thread=False)
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialise(self) -> None:
        with closing_connection(self.connect()) as connection:
            connection.executescript(SCHEMA)
            connection.executescript(VIEWS)
            connection.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()

    # -- parse cache --------------------------------------------------------

    def load_cache(self, source_name: str, source_digest: str) -> dict[str, dict]:
        """Every cached parse result for one source, keyed by content digest."""
        try:
            with closing_connection(self.connect()) as connection:
                rows = connection.execute(
                    "SELECT digest, payload FROM parse_cache "
                    "WHERE source_name = ? AND source_digest = ?",
                    (source_name, source_digest),
                ).fetchall()
        except sqlite3.Error:
            return {}
        out = {}
        for row in rows:
            try:
                out[row["digest"]] = json.loads(row["payload"])
            except ValueError:  # pragma: no cover - corrupt row
                continue
        return out

    def save_cache(
        self, source_name: str, source_digest: str, entries: dict[str, dict]
    ) -> None:
        if not entries:
            return
        now = time.time()
        with closing_connection(self.connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO parse_cache "
                "(source_name, source_digest, digest, payload, touched_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (source_name, source_digest, digest, json.dumps(payload), now)
                    for digest, payload in entries.items()
                ],
            )
            # Drop cache rows for older versions of this source's settings.
            connection.execute(
                "DELETE FROM parse_cache WHERE source_name = ? AND source_digest <> ?",
                (source_name, source_digest),
            )
            connection.commit()

    # -- writing ------------------------------------------------------------

    def write(
        self,
        records: Iterable[FileRecord],
        *,
        sources: Sequence[str],
        config_digest: str,
        tool_version: str,
        started_at: float,
    ) -> int:
        """Replace the stored graph for ``sources`` with ``records``.

        Returns the scan id.
        """
        self.initialise()
        records = list(records)

        with closing_connection(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                scan_id = self._insert_scan(
                    connection, config_digest, tool_version, started_at
                )
                source_ids = self._reset_sources(connection, sources)
                counts = self._insert_records(connection, records, source_ids, scan_id)
                self._recount(connection, list(source_ids.values()))
                self._finish_scan(connection, scan_id, counts, started_at)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return scan_id

    def _insert_scan(
        self,
        connection: sqlite3.Connection,
        config_digest: str,
        tool_version: str,
        started_at: float,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO scans (started_at, tool_version, config_digest) VALUES (?, ?, ?)",
            (_iso(started_at), tool_version, config_digest),
        )
        return int(cursor.lastrowid)

    def _reset_sources(
        self, connection: sqlite3.Connection, sources: Sequence[str]
    ) -> dict[str, int]:
        """Create source rows and clear their previous graph."""
        ids: dict[str, int] = {}
        for name in sources:
            connection.execute("INSERT OR IGNORE INTO sources (name) VALUES (?)", (name,))
            row = connection.execute("SELECT id FROM sources WHERE name = ?", (name,)).fetchone()
            ids[name] = int(row["id"])
        if ids:
            placeholders = ",".join("?" for _ in ids)
            values = tuple(ids.values())
            # Order matters: occurrences and node_files reference files/edges.
            connection.execute(
                "DELETE FROM occurrences WHERE edge_id IN "
                "(SELECT id FROM edges WHERE source_id IN (%s))" % placeholders,
                values,
            )
            connection.execute(
                "DELETE FROM edge_conditions WHERE edge_id IN "
                "(SELECT id FROM edges WHERE source_id IN (%s))" % placeholders,
                values,
            )
            connection.execute(
                "DELETE FROM parse_errors WHERE file_id IN "
                "(SELECT id FROM files WHERE source_id IN (%s))" % placeholders,
                values,
            )
            connection.execute(
                "DELETE FROM node_files WHERE file_id IN "
                "(SELECT id FROM files WHERE source_id IN (%s))" % placeholders,
                values,
            )
            for table in ("edges", "nodes", "files"):
                connection.execute(
                    "DELETE FROM %s WHERE source_id IN (%s)" % (table, placeholders), values
                )
        return ids

    def _insert_records(
        self,
        connection: sqlite3.Connection,
        records: Sequence[FileRecord],
        source_ids: dict[str, int],
        scan_id: int,
    ) -> dict[str, int]:
        node_ids: dict[tuple[int, str], int] = {}
        edge_ids: dict[tuple, int] = {}
        repo_ids: dict[tuple, int | None] = {}
        counts = {"files": 0, "cached": 0, "joins": 0, "errors": 0}

        def node_id(source_id: int, ref) -> int:
            key = (source_id, ref.key)
            existing = node_ids.get(key)
            if existing is not None:
                return existing
            cursor = connection.execute(
                "INSERT INTO nodes (source_id, table_key, catalog, schema_name, table_name) "
                "VALUES (?, ?, ?, ?, ?)",
                (source_id, ref.key, ref.catalog, ref.schema, ref.name),
            )
            node_ids[key] = int(cursor.lastrowid)
            return node_ids[key]

        for record in records:
            source_id = source_ids[record.source]
            repo_key = (record.repo_root, record.branch, record.commit_sha)
            if repo_key not in repo_ids:
                repo_ids[repo_key] = self._repo_id(connection, record)
            repo_id = repo_ids[repo_key]

            cursor = connection.execute(
                "INSERT INTO files "
                "(source_id, path, rel_path, language, repo_id, digest, size_bytes, scan_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    record.path,
                    record.rel_path,
                    record.language,
                    repo_id,
                    record.digest,
                    record.size_bytes,
                    scan_id,
                ),
            )
            file_id = int(cursor.lastrowid)
            counts["files"] += 1
            if record.cached:
                counts["cached"] += 1

            findings = record.findings
            for message in findings.errors:
                connection.execute(
                    "INSERT INTO parse_errors (file_id, message) VALUES (?, ?)",
                    (file_id, message),
                )
                counts["errors"] += 1

            seen_nodes: set[int] = set()
            for ref in findings.tables:
                seen_nodes.add(node_id(source_id, ref))

            for join in findings.joins:
                left_id = node_id(source_id, join.left)
                right_id = node_id(source_id, join.right)
                seen_nodes.add(left_id)
                seen_nodes.add(right_id)
                edge_id = self._edge_id(
                    connection, edge_ids, source_id, left_id, right_id, join
                )
                connection.execute(
                    "INSERT INTO occurrences (edge_id, file_id, line, language, scan_id) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (edge_id, file_id, join.line, join.language, scan_id),
                )
                counts["joins"] += 1

            connection.executemany(
                "INSERT OR IGNORE INTO node_files (node_id, file_id) VALUES (?, ?)",
                [(nid, file_id) for nid in seen_nodes],
            )
        return counts

    def _repo_id(self, connection: sqlite3.Connection, record: FileRecord) -> int | None:
        if not record.repo_root:
            return None
        connection.execute(
            "INSERT OR IGNORE INTO repos (root, branch, commit_sha, remote) VALUES (?, ?, ?, ?)",
            (record.repo_root, record.branch, record.commit_sha, record.remote),
        )
        row = connection.execute(
            "SELECT id FROM repos WHERE root = ? AND branch IS ? AND commit_sha IS ?",
            (record.repo_root, record.branch, record.commit_sha),
        ).fetchone()
        return int(row["id"]) if row else None

    def _edge_id(
        self,
        connection: sqlite3.Connection,
        cache: dict[tuple, int],
        source_id: int,
        left_id: int,
        right_id: int,
        join: Join,
    ) -> int:
        key = (source_id, left_id, right_id, join.join_type, join.condition)
        existing = cache.get(key)
        if existing is not None:
            return existing
        cursor = connection.execute(
            "INSERT INTO edges (source_id, left_node_id, right_node_id, join_type, "
            "condition, condition_count, implicit, ambiguous) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                source_id,
                left_id,
                right_id,
                join.join_type,
                join.condition,
                len(join.conditions),
                int(join.implicit),
                int(join.ambiguous),
            ),
        )
        edge_id = int(cursor.lastrowid)
        cache[key] = edge_id
        connection.executemany(
            "INSERT INTO edge_conditions "
            "(edge_id, ordinal, predicate, operator, left_column, right_column) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (edge_id, i, c.predicate, c.operator, c.left_column, c.right_column)
                for i, c in enumerate(join.conditions)
            ],
        )
        return edge_id

    def _recount(self, connection: sqlite3.Connection, source_ids: Sequence[int]) -> None:
        """Roll up the counts the UI reads, in SQL rather than in Python."""
        if not source_ids:
            return
        placeholders = ",".join("?" for _ in source_ids)
        values = tuple(source_ids)
        connection.execute(
            "UPDATE edges SET "
            "  occurrence_count = (SELECT COUNT(*) FROM occurrences o WHERE o.edge_id = edges.id),"
            "  file_count = (SELECT COUNT(DISTINCT o.file_id) FROM occurrences o "
            "                WHERE o.edge_id = edges.id) "
            "WHERE source_id IN (%s)" % placeholders,
            values,
        )
        connection.execute(
            "UPDATE nodes SET "
            "  file_count = (SELECT COUNT(*) FROM node_files nf WHERE nf.node_id = nodes.id),"
            "  degree = (SELECT COUNT(*) FROM edges e "
            "            WHERE e.left_node_id = nodes.id OR e.right_node_id = nodes.id),"
            "  join_count = (SELECT COALESCE(SUM(e.occurrence_count), 0) FROM edges e "
            "                WHERE e.left_node_id = nodes.id OR e.right_node_id = nodes.id) "
            "WHERE source_id IN (%s)" % placeholders,
            values,
        )

    def _finish_scan(
        self,
        connection: sqlite3.Connection,
        scan_id: int,
        counts: dict[str, int],
        started_at: float,
    ) -> None:
        now = time.time()
        connection.execute(
            "UPDATE scans SET finished_at = ?, files_scanned = ?, files_cached = ?, "
            "joins_found = ?, errors = ?, duration_ms = ? WHERE id = ?",
            (
                _iso(now),
                counts["files"],
                counts["cached"],
                counts["joins"],
                counts["errors"],
                int((now - started_at) * 1000),
                scan_id,
            ),
        )


@contextmanager
def closing_connection(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield connection
    finally:
        connection.close()


def _iso(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))
