"""YAML configuration: sources, file filters and table filters.

A config declares one or more *sources*.  A source is a named body of code
(usually one repository) with its own roots, file filters, SQL dialect and
table-naming rules.  Every regex is compiled once, at load time, because the
scanner evaluates them against every candidate path.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_INCLUDE = ("**/*.py", "**/*.sql", "**/*.ipynb", "**/*.txt")
DEFAULT_EXCLUDE = (
    "**/.git/**",
    "**/.venv/**",
    "**/venv/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "**/site-packages/**",
    "**/.tox/**",
    "**/build/**",
    "**/dist/**",
    "**/.ipynb_checkpoints/**",
)
DEFAULT_MAX_FILE_BYTES = 2_000_000

#: Extension -> language handled by :mod:`relationl.extract`.
LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".ipynb": "notebook",
    ".sql": "sql",
    ".txt": "text",
    ".hql": "sql",
    ".ddl": "sql",
    ".md": "text",
}


class ConfigError(ValueError):
    """Raised when a config file is structurally invalid."""


@dataclass(frozen=True)
class Rewrite:
    """A regex substitution applied to a fully qualified table name."""

    pattern: re.Pattern[str]
    replacement: str

    def apply(self, name: str) -> str:
        return self.pattern.sub(self.replacement, name)


@dataclass
class Source:
    """One named body of code to scan."""

    name: str
    roots: tuple[Path, ...]
    include: tuple[str, ...] = DEFAULT_INCLUDE
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE
    file_include: tuple[re.Pattern[str], ...] = ()
    file_exclude: tuple[re.Pattern[str], ...] = ()
    table_include: tuple[re.Pattern[str], ...] = ()
    table_exclude: tuple[re.Pattern[str], ...] = ()
    table_rewrite: tuple[Rewrite, ...] = ()
    sql_dialect: str | None = None
    default_schema: str | None = None
    default_catalog: str | None = None
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    follow_symlinks: bool = False
    languages: tuple[str, ...] | None = None

    @property
    def digest(self) -> str:
        """Hash of the settings that change what parsing a file produces.

        The parse cache is keyed by this, so editing ``table_rewrite`` or the
        dialect correctly invalidates every cached result for this source.
        """
        material = json.dumps(
            [
                self.sql_dialect,
                self.default_schema,
                self.default_catalog,
                [p.pattern for p in self.table_include],
                [p.pattern for p in self.table_exclude],
                [[r.pattern.pattern, r.replacement] for r in self.table_rewrite],
            ],
            sort_keys=True,
        )
        return hashlib.sha256(material.encode()).hexdigest()[:16]

    def language_for(self, path: Path) -> str | None:
        language = LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language is None:
            return None
        if self.languages and language not in self.languages:
            return None
        return language

    def accepts_path(self, path: Path, root: Path) -> bool:
        """Cheap path-level filter, applied before the file is ever opened."""
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.as_posix()
        full = path.as_posix()
        if not any(_glob_match(rel, full, pat) for pat in self.include):
            return False
        if any(_glob_match(rel, full, pat) for pat in self.exclude):
            return False
        if self.file_include and not any(p.search(rel) for p in self.file_include):
            return False
        if any(p.search(rel) for p in self.file_exclude):
            return False
        return True

    def accepts_table(self, key: str) -> bool:
        if self.table_include and not any(p.search(key) for p in self.table_include):
            return False
        if any(p.search(key) for p in self.table_exclude):
            return False
        return True

    def rewrite_table(self, key: str) -> str:
        for rule in self.table_rewrite:
            key = rule.apply(key)
        return key


@dataclass
class Config:
    """A parsed ``relationl.yaml``."""

    sources: tuple[Source, ...]
    database: Path = Path("relationl.db")
    path: Path | None = None
    workers: int | None = None
    cache: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    def source(self, name: str) -> Source:
        for src in self.sources:
            if src.name == name:
                return src
        raise KeyError(name)

    @property
    def digest(self) -> str:
        """Stable hash of the config, recorded against each scan."""
        blob = json.dumps(self.raw, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:16]

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Config:
        path = Path(path).resolve()
        if not path.exists():
            raise ConfigError("config file not found: %s" % path)
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        return cls.from_dict(raw, base_dir=path.parent, path=path)

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, Any],
        *,
        base_dir: Path | None = None,
        path: Path | None = None,
    ) -> Config:
        if not isinstance(raw, dict):
            raise ConfigError("config root must be a mapping")
        base_dir = Path(base_dir or Path.cwd())
        defaults = raw.get("defaults") or {}
        if not isinstance(defaults, dict):
            raise ConfigError("'defaults' must be a mapping")

        entries = raw.get("sources")
        if not entries:
            raise ConfigError("config must declare at least one entry under 'sources'")
        if not isinstance(entries, list):
            raise ConfigError("'sources' must be a list")

        seen: set[str] = set()
        sources = []
        for index, entry in enumerate(entries):
            source = _build_source(entry, defaults, base_dir, index)
            if source.name in seen:
                raise ConfigError("duplicate source name: %s" % source.name)
            seen.add(source.name)
            sources.append(source)

        database = Path(raw.get("database", "relationl.db"))
        if not database.is_absolute():
            database = (base_dir / database).resolve()

        workers = raw.get("workers")
        if workers is not None:
            workers = int(workers)
            if workers < 1:
                raise ConfigError("'workers' must be >= 1")

        return cls(
            sources=tuple(sources),
            database=database,
            path=path,
            workers=workers,
            cache=bool(raw.get("cache", True)),
            raw=raw,
        )


def _build_source(entry: Any, defaults: dict, base_dir: Path, index: int) -> Source:
    if not isinstance(entry, dict):
        raise ConfigError("sources[%d] must be a mapping" % index)
    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ConfigError("sources[%d] is missing a 'name'" % index)

    def setting(key: str, fallback: Any = None) -> Any:
        if key in entry:
            return entry[key]
        if key in defaults:
            return defaults[key]
        return fallback

    raw_roots = entry.get("roots") or entry.get("root")
    if not raw_roots:
        raise ConfigError("source %r must declare 'roots'" % name)
    if isinstance(raw_roots, (str, os.PathLike)):
        raw_roots = [raw_roots]
    roots = []
    for item in raw_roots:
        root = Path(str(item)).expanduser()
        if not root.is_absolute():
            root = base_dir / root
        roots.append(Path(os.path.normpath(root)))

    max_bytes = int(setting("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    if max_bytes < 1:
        raise ConfigError("source %r has a non-positive 'max_file_bytes'" % name)

    languages = setting("languages")
    if languages is not None:
        languages = tuple(str(x).lower() for x in _as_list(languages, "languages", name))
        unknown = set(languages) - set(LANGUAGE_BY_SUFFIX.values())
        if unknown:
            raise ConfigError(
                "source %r lists unknown languages: %s" % (name, ", ".join(sorted(unknown)))
            )

    return Source(
        name=name,
        roots=tuple(roots),
        include=tuple(_as_list(setting("include", DEFAULT_INCLUDE), "include", name)),
        exclude=tuple(_as_list(setting("exclude", DEFAULT_EXCLUDE), "exclude", name)),
        file_include=_compile(setting("file_include"), "file_include", name),
        file_exclude=_compile(setting("file_exclude"), "file_exclude", name),
        table_include=_compile(setting("table_include"), "table_include", name),
        table_exclude=_compile(setting("table_exclude"), "table_exclude", name),
        table_rewrite=_compile_rewrites(setting("table_rewrite"), name),
        sql_dialect=setting("sql_dialect"),
        default_schema=setting("default_schema"),
        default_catalog=setting("default_catalog"),
        max_file_bytes=max_bytes,
        follow_symlinks=bool(setting("follow_symlinks", False)),
        languages=languages,
    )


def _as_list(value: Any, key: str, source: str) -> Sequence[str]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    raise ConfigError("source %r: %r must be a string or a list" % (source, key))


def _compile(value: Any, key: str, source: str) -> tuple[re.Pattern[str], ...]:
    out = []
    for pattern in _as_list(value, key, source):
        try:
            out.append(re.compile(pattern, re.IGNORECASE))
        except re.error as err:
            raise ConfigError(
                "source %r: invalid regex in %r (%s): %s" % (source, key, pattern, err)
            ) from err
    return tuple(out)


def _compile_rewrites(value: Any, source: str) -> tuple[Rewrite, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("source %r: 'table_rewrite' must be a list" % source)
    out = []
    for item in value:
        if not isinstance(item, dict) or "pattern" not in item:
            raise ConfigError(
                "source %r: each 'table_rewrite' entry needs 'pattern' and 'replacement'" % source
            )
        try:
            compiled = re.compile(str(item["pattern"]), re.IGNORECASE)
        except re.error as err:
            raise ConfigError(
                "source %r: invalid 'table_rewrite' regex %r: %s" % (source, item["pattern"], err)
            ) from err
        out.append(Rewrite(compiled, str(item.get("replacement", ""))))
    return tuple(out)


def _glob_match(rel: str, full: str, pattern: str) -> bool:
    """Match a glob against the root-relative path, then the absolute path.

    ``**/*.py`` intentionally also matches ``a.py`` at the root, which plain
    :func:`fnmatch` does not do.
    """
    if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(full, pattern):
        return True
    if pattern.startswith("**/"):
        tail = pattern[3:]
        return fnmatch.fnmatch(rel, tail) or fnmatch.fnmatch(full, tail)
    return False


def example_config() -> str:
    """The contents written by ``relationl init``."""
    return EXAMPLE_YAML


EXAMPLE_YAML = """\
# relationl.yaml - configuration for a join-lineage scan.
version: 1

# Where the scan results are written.  Relative paths resolve against this file.
database: relationl.db

# Optional: number of worker processes.  Defaults to (cpu_count - 1).
# workers: 8

# Applied to every source unless the source overrides them.
defaults:
  include: ["**/*.py", "**/*.sql", "**/*.ipynb", "**/*.txt"]
  exclude:
    - "**/.git/**"
    - "**/.venv/**"
    - "**/node_modules/**"
    - "**/__pycache__/**"
  max_file_bytes: 2000000
  sql_dialect: spark

sources:
  - name: analytics
    roots:
      - ../analytics-etl
    # Regexes (not globs) applied to the root-relative path.
    file_exclude:
      - "(^|/)tests?/"
      - "_archive/"
    # Bare table names inherit these, so `orders` becomes `prod.analytics.orders`.
    default_catalog: prod
    default_schema: analytics
    # Keep only warehouse tables, and drop scratch space.
    table_include:
      - "^prod\\\\.(analytics|raw)\\\\."
    table_exclude:
      - "_tmp$"
      - "^scratch\\\\."
    # Collapse environment prefixes so dev and prod land on the same node.
    table_rewrite:
      - pattern: "^(dev|staging)_"
        replacement: ""

  - name: reporting
    roots:
      - ../reporting
    sql_dialect: snowflake
    default_schema: reporting
"""
