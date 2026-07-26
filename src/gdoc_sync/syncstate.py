"""Per-file sync snapshots, backups, and sticky conflict state.

Three responsibilities, all of them in service of "never lose the user's words":

**Base snapshots.** For each linked file the last successful sync stores two
texts: ``base_local`` (the file's bytes) and ``base_remote`` (what the doc
rendered to at that moment). Without these there is no merge ancestor and the
only options are "overwrite local" or "overwrite remote" — which is exactly how
0.5.x lost data. See :mod:`.merge` for why two snapshots rather than one.

**Backups.** Every write to a tracked file is preceded by a timestamped copy in
the state directory. A sync bug should cost the user a `gdoc-sync restore`, not
their afternoon.

**Conflict state.** A conflict is *sticky*: it is written to the state file and
survives a restart, and while it is set the watcher will not auto-push or
auto-pull that file. 0.5.x recorded a conflict, advanced both baselines, and
then let the next one-sided change overwrite the side that never got merged.
Marking the file conflicted until the user resolves it is what stops that.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import atomic_write, load_state, save_state

# How many timestamped backups to keep per file before pruning the oldest.
MAX_BACKUPS = 20


def _key(path: str | os.PathLike) -> str:
    return str(Path(path).expanduser().resolve())


def _slug(path: str | os.PathLike) -> str:
    """Filesystem-safe, collision-free stem for a path's snapshot files."""
    resolved = _key(path)
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
    return f"{Path(resolved).stem[:40]}-{digest}"


def sync_dir() -> Path:
    """Directory holding snapshots and backups.

    Always the XDG state directory, never "beside the state file": in the
    legacy combined-file layout the state file can live inside a synced
    notes vault, and silently filling that with baseline copies and dated
    backups would be rude — and would feed a Dropbox/Obsidian sync loop.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "gdoc-sync"


def _bases_dir() -> Path:
    return sync_dir() / "bases"


def backups_dir() -> Path:
    return sync_dir() / "backups"


# ---------------------------------------------------------------------------
# Base snapshots
# ---------------------------------------------------------------------------

@dataclass
class Bases:
    """The two ancestors for a file. Empty strings mean "never synced"."""

    local: str = ""
    remote: str = ""

    @property
    def known(self) -> bool:
        """True once a real sync has established an ancestor to merge against."""
        return bool(self.local or self.remote)


def _base_paths(path: str | os.PathLike) -> tuple[Path, Path]:
    d = _bases_dir()
    slug = _slug(path)
    return d / f"{slug}.local.md", d / f"{slug}.remote.md"


def get_bases(path: str | os.PathLike) -> Bases:
    local_p, remote_p = _base_paths(path)
    def read(p: Path) -> str:
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
    return Bases(local=read(local_p), remote=read(remote_p))


def set_bases(path: str | os.PathLike, local: str, remote: str) -> None:
    """Record both ancestors for a file after a successful sync."""
    local_p, remote_p = _base_paths(path)
    _bases_dir().mkdir(parents=True, exist_ok=True)
    atomic_write(local_p, local)
    atomic_write(remote_p, remote)


def clear_bases(path: str | os.PathLike) -> None:
    for p in _base_paths(path):
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def backup_file(path: str | os.PathLike, tag: str = "pre-sync") -> Path | None:
    """Copy ``path`` into the backup directory. Returns the backup path.

    Best-effort and never fatal: a failed backup must not abort the sync that
    the user asked for, but it is reported by the caller.
    """
    src = Path(path)
    if not src.exists():
        return None
    d = backups_dir()
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = d / f"{_slug(src)}.{stamp}.{tag}.md"
    n = 1
    while dest.exists():  # same-second backups
        dest = d / f"{_slug(src)}.{stamp}-{n}.{tag}.md"
        n += 1
    try:
        shutil.copy2(src, dest)
    except OSError:
        return None
    _prune_backups(src)
    return dest


def list_backups(path: str | os.PathLike) -> list[Path]:
    """Backups for ``path``, newest first."""
    d = backups_dir()
    if not d.is_dir():
        return []
    prefix = f"{_slug(path)}."
    found = [p for p in d.iterdir() if p.name.startswith(prefix)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _prune_backups(path: str | os.PathLike) -> None:
    for old in list_backups(path)[MAX_BACKUPS:]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Sticky conflict state
# ---------------------------------------------------------------------------

@dataclass
class Conflict:
    """A recorded, unresolved divergence between local file and doc."""

    path: str
    since: str
    detail: str = ""
    markers: bool = False
    remote_copy: str = ""

    def as_dict(self) -> dict:
        return {
            "since": self.since,
            "detail": self.detail,
            "markers": self.markers,
            "remote_copy": self.remote_copy,
        }


def get_conflict(path: str | os.PathLike) -> Conflict | None:
    raw = load_state().get("conflicts", {}).get(_key(path))
    if not isinstance(raw, dict):
        return None
    return Conflict(
        path=_key(path),
        since=str(raw.get("since", "")),
        detail=str(raw.get("detail", "")),
        markers=bool(raw.get("markers", False)),
        remote_copy=str(raw.get("remote_copy", "")),
    )


def set_conflict(
    path: str | os.PathLike,
    detail: str = "",
    *,
    markers: bool = False,
    remote_copy: str = "",
) -> Conflict:
    """Mark a file conflicted. Auto-sync stays suspended until cleared."""
    conflict = Conflict(
        path=_key(path),
        since=time.strftime("%Y-%m-%dT%H:%M:%S"),
        detail=detail,
        markers=markers,
        remote_copy=remote_copy,
    )
    state = load_state()
    state.setdefault("conflicts", {})[conflict.path] = conflict.as_dict()
    save_state(state)
    return conflict


def clear_conflict(path: str | os.PathLike) -> bool:
    """Clear a file's conflict flag. Returns True if one was set."""
    state = load_state()
    conflicts = state.get("conflicts")
    if not isinstance(conflicts, dict):
        return False
    if conflicts.pop(_key(path), None) is None:
        return False
    save_state(state)
    return True


def all_conflicts() -> dict[str, Conflict]:
    out: dict[str, Conflict] = {}
    for path, raw in (load_state().get("conflicts") or {}).items():
        if isinstance(raw, dict):
            out[path] = Conflict(
                path=path,
                since=str(raw.get("since", "")),
                detail=str(raw.get("detail", "")),
                markers=bool(raw.get("markers", False)),
                remote_copy=str(raw.get("remote_copy", "")),
            )
    return out
