"""Walk the configured sources and extract joins from every matching file.

Three things keep a large scan quick:

* path filtering happens on directory entries, before a file is opened, and
  excluded directories are pruned rather than descended into;
* a content-addressed parse cache means an unchanged file is never re-parsed,
  so re-scans cost little more than hashing;
* parsing runs across a process pool, since it is CPU-bound and the GIL would
  otherwise serialise it.  Small scans stay in-process, where the pool's
  start-up cost would dominate.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import gitinfo
from .config import Config, Source
from .extract import Extractor
from .models import FileFindings
from .storage import Database, FileRecord

#: Below this many files the process pool costs more than it saves.
_POOL_THRESHOLD = 48

#: Files handed to a worker in one batch.
_CHUNK_SIZE = 16

ProgressHook = Callable[[int, int], None]


@dataclass
class ScanStats:
    """Summary of one scan, returned to the CLI."""

    files: int = 0
    cached: int = 0
    joins: int = 0
    tables: int = 0
    edges: int = 0
    errors: int = 0
    skipped: int = 0
    duration_s: float = 0.0
    scan_id: int | None = None
    sources: tuple[str, ...] = ()
    error_messages: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Task:
    """One file queued for parsing."""

    source: str
    path: str
    rel_path: str
    language: str
    digest: str
    size_bytes: int
    text: str


def scan(
    config: Config,
    *,
    sources: Sequence[str] | None = None,
    workers: int | None = None,
    use_cache: bool | None = None,
    progress: ProgressHook | None = None,
) -> ScanStats:
    """Scan every configured source and write the graph to SQLite."""
    import time

    started = time.time()
    selected = _select_sources(config, sources)
    database = Database(config.database)
    database.initialise()

    use_cache = config.cache if use_cache is None else use_cache
    workers = workers or config.workers or _default_workers()

    records: list[FileRecord] = []
    by_path: dict[str, FileRecord] = {}
    stats = ScanStats(sources=tuple(s.name for s in selected))

    for source in selected:
        cache = database.load_cache(source.name, source.digest) if use_cache else {}
        fresh: dict[str, dict] = {}
        tasks: list[_Task] = []

        for path, rel_path, language, root in _walk(source):
            read = _read(path, source.max_file_bytes)
            if read is None:
                stats.skipped += 1
                continue
            text, digest, size = read

            record = _record(source, path, rel_path, language, digest, size, root)
            by_path[record.path] = record
            cached_payload = cache.get(digest)
            if cached_payload is not None:
                record.findings = FileFindings.from_payload(str(path), cached_payload)
                record.cached = True
                records.append(record)
                stats.cached += 1
                if progress:
                    progress(len(records), 0)
            else:
                tasks.append(
                    _Task(source.name, str(path), rel_path, language, digest, size, text)
                )
                records.append(record)

        for task, findings in _parse_all(tasks, [source], workers, progress):
            fresh[task.digest] = findings.to_payload()
            by_path[task.path].findings = findings

        if use_cache:
            database.save_cache(source.name, source.digest, fresh)

    for record in records:
        stats.files += 1
        stats.joins += len(record.findings.joins)
        stats.errors += len(record.findings.errors)
        stats.error_messages.extend(
            "%s: %s" % (record.rel_path, message) for message in record.findings.errors[:3]
        )

    stats.tables = len(
        {(r.source, t.key) for r in records for t in r.findings.tables}
    )
    stats.edges = len(
        {(r.source,) + j.edge_key for r in records for j in r.findings.joins}
    )
    stats.scan_id = database.write(
        records,
        sources=[s.name for s in selected],
        config_digest=config.digest,
        tool_version=_version(),
        started_at=started,
    )
    stats.duration_s = time.time() - started
    return stats


def _select_sources(config: Config, names: Sequence[str] | None) -> list[Source]:
    if not names:
        return list(config.sources)
    selected = []
    for name in names:
        try:
            selected.append(config.source(name))
        except KeyError as err:
            known = ", ".join(s.name for s in config.sources)
            raise KeyError("unknown source %r (configured: %s)" % (name, known)) from err
    return selected


def _default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


# ---------------------------------------------------------------------------
# walking
# ---------------------------------------------------------------------------


def _walk(source: Source) -> Iterator[tuple[Path, str, str, Path]]:
    """Yield ``(path, rel_path, language, root)`` for every file to parse."""
    seen: set[str] = set()
    for root in source.roots:
        if not root.exists():
            continue
        for path in _iter_files(root, source):
            language = source.language_for(path)
            if language is None:
                continue
            if not source.accepts_path(path, root):
                continue
            resolved = str(path)
            if resolved in seen:
                continue
            seen.add(resolved)
            yield path, _relative(path, root), language, root


def _iter_files(root: Path, source: Source) -> Iterator[Path]:
    """Depth-first walk that prunes excluded directories instead of entering them."""
    stack = [root]
    suffixes = _candidate_suffixes(source)
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=source.follow_symlinks):
                    child = Path(entry.path)
                    if _pruned(child, root, source):
                        continue
                    stack.append(child)
                elif entry.is_file(follow_symlinks=source.follow_symlinks):
                    if suffixes is not None:
                        suffix = os.path.splitext(entry.name)[1].lower()
                        if suffix not in suffixes:
                            continue
                    yield Path(entry.path)
            except OSError:
                continue


def _candidate_suffixes(source: Source) -> set[str] | None:
    """Extensions worth stat-ing, derived from the source's language filter."""
    from .config import LANGUAGE_BY_SUFFIX

    if not source.languages:
        return set(LANGUAGE_BY_SUFFIX)
    return {s for s, lang in LANGUAGE_BY_SUFFIX.items() if lang in source.languages}


def _pruned(directory: Path, root: Path, source: Source) -> bool:
    """Whether a directory can be skipped whole.

    A glob such as ``**/node_modules/**`` is matched against the directory
    itself, so the entire subtree is never walked.
    """
    from .config import _glob_match

    try:
        rel = directory.relative_to(root).as_posix()
    except ValueError:
        rel = directory.as_posix()
    full = directory.as_posix()
    for pattern in source.exclude:
        trimmed = pattern[:-3] if pattern.endswith("/**") else pattern
        if _glob_match(rel, full, trimmed):
            return True
    return False


def _relative(path: Path, root: Path) -> str:
    """Path relative to the containing repository, falling back to the root."""
    repo = gitinfo.repo_root(str(path))
    base = Path(repo) if repo else root
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()


def _read(path: Path, max_bytes: int) -> tuple[str, str, int] | None:
    """Read a file, returning ``(text, digest, size)`` or ``None`` if unusable."""
    try:
        size = path.stat().st_size
        if size > max_bytes:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:  # pragma: no cover - latin-1 never fails
            return None
    if "\x00" in text[:1024]:
        return None  # binary file that happened to match a glob
    return text, digest, len(raw)


def _record(
    source: Source,
    path: Path,
    rel_path: str,
    language: str,
    digest: str,
    size: int,
    root: Path,
) -> FileRecord:
    info = gitinfo.describe_path(path)
    return FileRecord(
        source=source.name,
        path=str(path),
        rel_path=rel_path,
        language=language,
        digest=digest,
        size_bytes=size,
        repo_root=info.root,
        branch=info.branch,
        commit_sha=info.commit,
        remote=info.remote,
        findings=FileFindings(path=str(path), language=language),
    )


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

_EXTRACTORS: dict[str, Extractor] = {}


def _init_worker(sources: Sequence[Source]) -> None:
    """Build one :class:`Extractor` per source, once per worker process."""
    _EXTRACTORS.clear()
    for source in sources:
        _EXTRACTORS[source.name] = Extractor(source)


def _run_task(task: _Task) -> tuple[str, dict]:
    extractor = _EXTRACTORS.get(task.source)
    if extractor is None:  # pragma: no cover - defensive
        return task.path, FileFindings(task.path, task.language).to_payload()
    findings = extractor.extract(task.path, task.language, task.text)
    return task.path, findings.to_payload()


def _parse_all(
    tasks: Sequence[_Task],
    sources: Sequence[Source],
    workers: int,
    progress: ProgressHook | None,
) -> Iterator[tuple[_Task, FileFindings]]:
    if not tasks:
        return
    by_path = {task.path: task for task in tasks}

    if workers <= 1 or len(tasks) < _POOL_THRESHOLD:
        _init_worker(sources)
        for index, task in enumerate(tasks, start=1):
            path, payload = _run_task(task)
            if progress:
                progress(index, len(tasks))
            yield by_path[path], FileFindings.from_payload(path, payload)
        return

    with ProcessPoolExecutor(
        max_workers=workers, initializer=_init_worker, initargs=(list(sources),)
    ) as executor:
        results = executor.map(_run_task, tasks, chunksize=_CHUNK_SIZE)
        for index, (path, payload) in enumerate(results, start=1):
            if progress:
                progress(index, len(tasks))
            yield by_path[path], FileFindings.from_payload(path, payload)


def _version() -> str:
    from . import __version__

    return __version__
