"""RelationL -- recover table-level join lineage from a codebase.

RelationL parses SQL, PySpark, notebooks and text files, follows CTE and
DataFrame lineage back to the physical tables involved, and writes the result
as a graph of tables (nodes) and joins (edges) into SQLite.

    from relationl import Config, scan, GraphStore

    stats = scan(Config.load("relationl.yaml"))
    graph = GraphStore("relationl.db")
    routes = graph.shortest_paths("sales.orders", "crm.regions", min_occurrences=2)
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import Config, ConfigError, Source
from .graph import Edge, GraphStore, Node, Path
from .models import Join, JoinCondition, TableRef
from .scanner import ScanStats, scan
from .storage import Database

__all__ = [
    "Config",
    "ConfigError",
    "Database",
    "Edge",
    "GraphStore",
    "Join",
    "JoinCondition",
    "Node",
    "Path",
    "ScanStats",
    "Source",
    "TableRef",
    "__version__",
    "scan",
]
