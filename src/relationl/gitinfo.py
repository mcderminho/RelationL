"""Discover the git provenance of each scanned folder.

Every join is attributed to the branch and commit it was read from.  Repository
metadata is resolved once per repository root and memoised, so a scan of ten
thousand files still shells out to ``git`` only a handful of times.

The ``.git`` directory is read directly where possible (HEAD, packed-refs),
falling back to ``git`` itself only for worktrees and remotes.  Reading the
files is roughly two orders of magnitude cheaper than spawning a subprocess,
which matters when a config points at many repositories.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_GIT_TIMEOUT = 10


@dataclass(frozen=True)
class GitInfo:
    """Provenance for one repository."""

    root: str | None = None
    branch: str | None = None
    commit: str | None = None
    remote: str | None = None

    @property
    def short_commit(self) -> str | None:
        return self.commit[:12] if self.commit else None


EMPTY = GitInfo()


@lru_cache(maxsize=4096)
def repo_root(start: str) -> str | None:
    """Walk up from ``start`` looking for a ``.git`` entry."""
    current = Path(start)
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return str(candidate)
    return None


@lru_cache(maxsize=256)
def describe(root: str) -> GitInfo:
    """Resolve branch/commit/remote for a repository root."""
    git_path = Path(root) / ".git"
    git_dir = _resolve_git_dir(git_path)
    if git_dir is None:
        return GitInfo(root=root)

    branch, commit = _read_head(git_dir)
    if commit is None:
        commit = _run(root, "rev-parse", "HEAD")
    if branch is None:
        branch = _run(root, "rev-parse", "--abbrev-ref", "HEAD")
        if branch == "HEAD":  # detached
            branch = None
    remote = _read_remote(git_dir) or _run(root, "config", "--get", "remote.origin.url")
    return GitInfo(root=root, branch=branch, commit=commit, remote=remote)


def describe_path(path: str | os.PathLike[str]) -> GitInfo:
    """Provenance for whichever repository contains ``path``."""
    root = repo_root(str(path))
    return describe(root) if root else EMPTY


def _resolve_git_dir(git_path: Path) -> Path | None:
    if git_path.is_dir():
        return git_path
    if git_path.is_file():
        # A worktree or submodule: ".git" is a file containing "gitdir: <path>".
        try:
            text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return None
        if text.startswith("gitdir:"):
            target = Path(text.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (git_path.parent / target).resolve()
            return target if target.exists() else None
    return None


def _read_head(git_dir: Path) -> tuple[str | None, str | None]:
    """Read branch and commit straight out of ``.git``."""
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None

    if not head.startswith("ref:"):
        # Detached HEAD: the file already holds the commit sha.
        return None, head or None

    ref = head.split(":", 1)[1].strip()
    # Strip the prefix rather than taking the last segment: branch names very
    # often contain slashes ("feature/joins").
    prefix = "refs/heads/"
    branch = ref[len(prefix) :] if ref.startswith(prefix) else ref
    commit = _read_ref(git_dir, ref)
    return branch or None, commit


def _read_ref(git_dir: Path, ref: str) -> str | None:
    loose = git_dir / ref
    try:
        value = loose.read_text(encoding="utf-8").strip()
        if value:
            return value.split()[0]
    except OSError:
        pass

    packed = git_dir / "packed-refs"
    try:
        with packed.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line or line[0] in "#^":
                    continue
                sha, _, name = line.strip().partition(" ")
                if name == ref:
                    return sha
    except OSError:
        pass
    return None


def _read_remote(git_dir: Path) -> str | None:
    """Pull ``remote.origin.url`` out of the config without spawning git."""
    try:
        lines = (git_dir / "config").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_origin = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            in_origin = stripped.replace(" ", "").replace('"', "").lower() == "[remoteorigin]"
        elif in_origin and stripped.lower().startswith("url"):
            _, _, value = stripped.partition("=")
            return _scrub_remote(value.strip()) or None
    return None


def _scrub_remote(url: str) -> str:
    """Strip any embedded credentials before the URL reaches the database."""
    if "@" in url and "://" in url:
        scheme, _, rest = url.partition("://")
        _, _, host = rest.rpartition("@")
        return "%s://%s" % (scheme, host)
    return url


def _run(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return _scrub_remote(value) if value else None


def clear_cache() -> None:
    """Drop memoised repository state (used by the tests)."""
    repo_root.cache_clear()
    describe.cache_clear()
