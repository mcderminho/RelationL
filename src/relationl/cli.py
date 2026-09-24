"""Command line interface for RelationL."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, example_config
from .graph import GraphStore
from .scanner import ScanStats, scan
from .storage import Database

DEFAULT_CONFIG = "relationl.yaml"


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return int(args.handler(args) or 0)
    except ConfigError as err:
        print("config error: %s" % err, file=sys.stderr)
        return 2
    except KeyError as err:
        print("error: %s" % err.args[0], file=sys.stderr)
        return 2
    except FileNotFoundError as err:
        print("error: %s" % err, file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="relationl",
        description="Recover table-level join lineage from SQL, PySpark and notebooks.",
    )
    parser.add_argument("--version", action="version", version="relationl %s" % __version__)
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="write a starter relationl.yaml")
    init.add_argument("path", nargs="?", default=DEFAULT_CONFIG)
    init.add_argument("--force", action="store_true", help="overwrite an existing file")
    init.set_defaults(handler=_cmd_init)

    scan_cmd = subparsers.add_parser("scan", help="scan the configured sources")
    _add_config_arg(scan_cmd)
    scan_cmd.add_argument("--source", action="append", dest="sources", metavar="NAME")
    scan_cmd.add_argument("--workers", type=int)
    scan_cmd.add_argument("--no-cache", action="store_true", help="ignore the parse cache")
    scan_cmd.add_argument("--quiet", action="store_true")
    scan_cmd.add_argument("--json", action="store_true", dest="as_json")
    scan_cmd.set_defaults(handler=_cmd_scan)

    tables = subparsers.add_parser("tables", help="list discovered tables")
    _add_config_arg(tables)
    tables.add_argument("--search")
    tables.add_argument("--source")
    tables.add_argument("--limit", type=int, default=50)
    tables.add_argument("--json", action="store_true", dest="as_json")
    tables.set_defaults(handler=_cmd_tables)

    joins = subparsers.add_parser("joins", help="list discovered joins")
    _add_config_arg(joins)
    joins.add_argument("--source")
    joins.add_argument("--min-count", type=int, default=1)
    joins.add_argument("--limit", type=int, default=50)
    joins.add_argument("--json", action="store_true", dest="as_json")
    joins.set_defaults(handler=_cmd_joins)

    path_cmd = subparsers.add_parser("path", help="shortest join path between two tables")
    _add_config_arg(path_cmd)
    path_cmd.add_argument("start")
    path_cmd.add_argument("end")
    path_cmd.add_argument("--source")
    path_cmd.add_argument("--min-count", type=int, default=1)
    path_cmd.add_argument("-k", type=int, default=3, help="number of routes to return")
    path_cmd.add_argument("--json", action="store_true", dest="as_json")
    path_cmd.set_defaults(handler=_cmd_path)

    serve = subparsers.add_parser("serve", help="run the graph explorer web app")
    _add_config_arg(serve)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(handler=_cmd_serve)

    return parser


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, metavar="FILE")
    parser.add_argument(
        "--db",
        metavar="FILE",
        help="database path, overriding the one in the config",
    )


def _database(args: argparse.Namespace) -> Database:
    """Resolve the database from ``--db``, or from the config file."""
    if getattr(args, "db", None):
        return Database(args.db)
    return Database(Config.load(args.config).database)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path)
    if target.exists() and not args.force:
        print("%s already exists (use --force to overwrite)" % target, file=sys.stderr)
        return 1
    target.write_text(example_config(), encoding="utf-8")
    print("wrote %s" % target)
    print("Edit the 'sources' section, then run: relationl scan")
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    if args.db:
        config.database = Path(args.db)

    hook = None if args.quiet or args.as_json else _progress()
    stats = scan(
        config,
        sources=args.sources,
        workers=args.workers,
        use_cache=not args.no_cache,
        progress=hook,
    )
    if hook:
        print("", file=sys.stderr)

    if args.as_json:
        print(json.dumps(_stats_dict(stats), indent=2))
    else:
        _print_stats(stats, config)
    return 0


def _stats_dict(stats: ScanStats) -> dict:
    return {
        "sources": list(stats.sources),
        "files": stats.files,
        "cached": stats.cached,
        "skipped": stats.skipped,
        "tables": stats.tables,
        "edges": stats.edges,
        "join_occurrences": stats.joins,
        "errors": stats.errors,
        "duration_s": round(stats.duration_s, 3),
        "scan_id": stats.scan_id,
    }


def _print_stats(stats: ScanStats, config: Config) -> None:
    print("scanned %d file(s) in %.2fs (%d from cache, %d skipped)" % (
        stats.files, stats.duration_s, stats.cached, stats.skipped
    ))
    print("  tables : %d" % stats.tables)
    print("  joins  : %d distinct, %d occurrence(s)" % (stats.edges, stats.joins))
    if stats.errors:
        print("  errors : %d (see the parse_errors table)" % stats.errors)
        for message in stats.error_messages[:5]:
            print("      %s" % message)
    print("  written to %s" % config.database)


def _progress():
    state = {"n": 0}

    def hook(done: int, total: int) -> None:
        state["n"] += 1
        if state["n"] % 25 == 0 or (total and done == total):
            suffix = "/%d" % total if total else ""
            print("\r  parsing %d%s ..." % (done, suffix), end="", file=sys.stderr)

    return hook


def _cmd_tables(args: argparse.Namespace) -> int:
    store = GraphStore(_database(args))
    nodes = store.nodes(source=args.source, search=args.search, limit=args.limit)
    if args.as_json:
        print(json.dumps([n.as_dict() for n in nodes], indent=2))
        return 0
    if not nodes:
        print("no tables found -- has a scan been run?")
        return 0
    width = max(len(n.table_key) for n in nodes)
    print("%-*s  %6s  %6s  %6s  %s" % (width, "TABLE", "FILES", "JOINS", "DEGREE", "SOURCE"))
    for node in nodes:
        print(
            "%-*s  %6d  %6d  %6d  %s"
            % (width, node.table_key, node.file_count, node.join_count, node.degree, node.source)
        )
    return 0


def _cmd_joins(args: argparse.Namespace) -> int:
    store = GraphStore(_database(args))
    edges = store.edges(
        source=args.source, min_occurrences=args.min_count, limit=args.limit
    )
    if args.as_json:
        print(json.dumps([e.as_dict() for e in edges], indent=2))
        return 0
    if not edges:
        print("no joins found -- has a scan been run?")
        return 0
    for edge in edges:
        print(
            "%s  %s  %s   [x%d in %d file(s)]"
            % (
                edge.left,
                edge.join_type,
                edge.right,
                edge.occurrence_count,
                edge.file_count,
            )
        )
        if edge.condition:
            print("    ON %s" % edge.condition)
    return 0


def _cmd_path(args: argparse.Namespace) -> int:
    store = GraphStore(_database(args))
    paths = store.shortest_paths(
        args.start,
        args.end,
        source=args.source,
        min_occurrences=args.min_count,
        k=args.k,
    )
    if args.as_json:
        print(json.dumps([p.as_dict() for p in paths], indent=2))
        return 0
    if not paths:
        print("no path from %s to %s at min-count %d" % (args.start, args.end, args.min_count))
        return 1
    for index, path in enumerate(paths, start=1):
        print(
            "route %d: %d hop(s), weakest link seen %dx"
            % (index, path.length, path.weakest_link)
        )
        for table, edge in zip(path.tables, path.edges, strict=False):
            print("  %s" % table)
            print("    --%s--> [x%d]" % (edge.join_type, edge.occurrence_count))
            if edge.condition:
                print("      ON %s" % edge.condition)
        print("  %s" % path.tables[-1])
        print()
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "the web app needs the optional extras: pip install 'relationl[web]'",
            file=sys.stderr,
        )
        return 2

    from .web.app import create_app

    database = _database(args)
    if not database.path.exists():
        print("no database at %s -- run 'relationl scan' first" % database.path, file=sys.stderr)
        return 2

    print("RelationL explorer on http://%s:%d  (db: %s)" % (args.host, args.port, database.path))
    uvicorn.run(
        create_app(database), host=args.host, port=args.port, log_level="warning"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
