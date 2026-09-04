"""Configuration and state management.

Two kinds of data, kept separate:

* **Settings** — human-edited preferences (font, theme, share default, …) in a
  YAML config file.
* **State** — machine-written bookkeeping (local-file → doc-id mappings and
  last-seen revision ids) in a state file.

Config path resolution order:
  1. ``--config`` CLI flag (via :func:`set_config_override`)
  2. ``$GDOC_SYNC_CONFIG``
  3. ``$XDG_CONFIG_HOME/gdoc-sync/config.yaml`` (``~/.config/gdoc-sync/config.yaml``)

State path resolution order:
  1. ``state_file:`` key in the config
  2. the config file itself, when it already contains ``mappings:`` or
     ``revisions:`` (the pre-0.2 single-file format — point GDOC_SYNC_CONFIG at
     your old ``.gdoc-sync.yaml`` and everything keeps working)
  3. ``$XDG_STATE_HOME/gdoc-sync/state.yaml`` (``~/.local/state/gdoc-sync/state.yaml``)

Settings may live under a ``defaults:`` mapping (preferred) or at the top
level (legacy format).
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import yaml

try:  # POSIX only. Without it there is no advisory lock — see state_lock().
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

DEFAULT_FONT = "Garamond"
DEFAULT_THEME = "professional"
DEFAULT_SHARE = "comment"  # private | view | comment | edit
DEFAULT_CONFLICT_STYLE = "markers"  # markers | sidecar
DEFAULT_WATCH_INTERVAL = 15
# Below this, polling costs more in API quota than it buys in latency.
MIN_WATCH_INTERVAL = 5

_config_override: Path | None = None


def set_config_override(path: str | os.PathLike | None) -> None:
    """Set the config path from the ``--config`` CLI flag (highest priority)."""
    global _config_override
    _config_override = Path(path).expanduser() if path else None


def config_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "gdoc-sync"


def config_path() -> Path:
    if _config_override:
        return _config_override
    env = os.environ.get("GDOC_SYNC_CONFIG")
    if env:
        return Path(env).expanduser()
    return config_dir() / "config.yaml"


def load_config() -> dict:
    p = config_path()
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


def _setting(key: str, default):
    """A setting from ``defaults:`` (preferred) or the top level (legacy)."""
    config = load_config()
    defaults = config.get("defaults")
    if isinstance(defaults, dict) and key in defaults:
        return defaults[key]
    return config.get(key, default)


def get_font() -> str:
    """Font family applied on create/push — any name Google Docs' font picker knows."""
    font = _setting("font", DEFAULT_FONT)
    if isinstance(font, str) and font.strip():
        return font.strip()
    return DEFAULT_FONT


def get_theme() -> str | None:
    """Color theme applied on create/push, or None when disabled ("none"/"off")."""
    theme = _setting("theme", DEFAULT_THEME)
    if isinstance(theme, str) and theme.strip():
        t = theme.strip().lower()
        return None if t in ("none", "off", "false") else t
    return None


def get_share_default() -> str:
    share = _setting("share", DEFAULT_SHARE)
    if share in ("private", "view", "comment", "edit"):
        return share
    return DEFAULT_SHARE


def get_clipboard_default() -> bool:
    return bool(_setting("clipboard", True))


def get_clipboard_command() -> str | None:
    """An explicit clipboard command, overriding platform auto-detection.

    Set ``clipboard_command:`` when the detected tool is wrong for your setup
    (a remote session, an unusual multiplexer, a custom clipboard manager).
    """
    command = _setting("clipboard_command", None)
    if isinstance(command, str) and command.strip():
        return command.strip()
    if isinstance(command, list) and command:
        return " ".join(str(c) for c in command)
    return None


def get_conflict_style() -> str:
    """How an unmergeable conflict is presented.

    ``markers`` (default) writes git-style conflict markers into the file, so
    the divergence is visible exactly where it happened and any editor's
    conflict tooling works on it. ``sidecar`` leaves the file untouched and
    writes the doc's version to ``<name>.remote.md`` instead.
    """
    style = _setting("conflict_style", DEFAULT_CONFLICT_STYLE)
    if isinstance(style, str) and style.strip().lower() in ("markers", "sidecar"):
        return style.strip().lower()
    return DEFAULT_CONFLICT_STYLE


def get_watch_interval() -> int:
    """Seconds between watch polls."""
    raw = _setting("watch_interval", DEFAULT_WATCH_INTERVAL)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_WATCH_INTERVAL
    return max(value, MIN_WATCH_INTERVAL)


def get_import_dir() -> Path | None:
    """Default directory for `import`-derived filenames.

    ``None`` means the current working directory, which keeps `import` behaving
    like every other CLI that writes a file where you are standing. Set
    ``import_dir:`` in the config to send imports to one notes folder instead.
    """
    raw = _setting("import_dir", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser()


def get_image_dir() -> Path | None:
    """Directory where images added in a Google Doc are downloaded.

    ``None`` (the default) keeps them beside each markdown file in
    ``<stem>-assets/``. Set ``image_dir:`` in the config to collect every
    downloaded image in one global folder instead; links written into the
    markdown stay relative to the markdown file either way.
    """
    raw = _setting("image_dir", None)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser()


def get_custom_themes() -> dict:
    """User-defined themes from the config's ``themes:`` section."""
    themes = load_config().get("themes")
    return themes if isinstance(themes, dict) else {}


# ---------------------------------------------------------------------------
# State (mappings + revisions)
# ---------------------------------------------------------------------------

# How long a mutator waits for the state lock before giving up. Generous: the
# critical section is a small read-modify-write, so anything near this means a
# stuck process, not honest contention.
STATE_LOCK_TIMEOUT = 10.0
_STATE_LOCK_POLL = 0.01


def state_path() -> Path:
    config = load_config()
    explicit = config.get("state_file")
    if explicit:
        return Path(explicit).expanduser()
    if "mappings" in config or "revisions" in config:
        return config_path()  # legacy combined settings+state file
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "gdoc-sync" / "state.yaml"


def atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + rename).

    Used for anything whose truncation would be data loss — the state file
    (which holds every local-file → doc-id mapping) and synced markdown. A
    plain ``write_text`` interrupted midway leaves a truncated file behind;
    ``os.replace`` either fully succeeds or leaves the original untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state() -> dict:
    """Read the state file, tolerating a corrupt one rather than crashing.

    Returning ``{}`` on unparseable YAML would silently discard every mapping
    on the next save, so the damaged file is moved aside first — the user keeps
    a recoverable copy and gets told where it went.
    """
    p = state_path()
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError, UnicodeDecodeError) as e:
        salvage = p.with_suffix(p.suffix + ".corrupt")
        try:
            os.replace(p, salvage)
            print(f"WARNING: state file was unreadable ({e}); moved to {salvage}",
                  file=sys.stderr)
        except OSError:
            print(f"WARNING: state file is unreadable ({e})", file=sys.stderr)
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict) -> None:
    """Write state, preserving any non-state keys already in the file.

    In legacy combined mode the state file is also the config file, so settings
    keys (font, theme, …) ride along untouched.
    """
    atomic_write(state_path(), yaml.dump(state, default_flow_style=False))


def state_lock_path() -> Path:
    """The advisory lock file guarding the state file (a sibling ``.lock``)."""
    p = state_path()
    return p.with_name(p.name + ".lock")


@contextlib.contextmanager
def state_lock(timeout: float = STATE_LOCK_TIMEOUT) -> Iterator[Path | None]:
    """Hold an exclusive advisory lock on the state file for the block's duration.

    Every write to the state file is a read-modify-write of the whole YAML
    document, so two unsynchronised writers interleave as "both read, both
    modify their own copy, last one wins" — and the loser's mapping is gone,
    with both processes exiting 0. Twenty parallel ``gdoc-sync link`` runs used
    to leave thirteen mappings, and a dropped mapping means the next ``create``
    makes a *duplicate* Google Doc. Holding this lock across the load and the
    save is what makes those twenty runs leave twenty mappings.

    The lock is a ``flock`` on ``<state file>.lock`` — a sibling file rather
    than the state file itself, because :func:`save_state` replaces the state
    file by rename, which would strand a lock taken on the old inode.

    Acquisition polls with ``LOCK_NB`` and gives up after ``timeout`` seconds
    with a :class:`RuntimeError` naming the lock file, so a stale holder is a
    legible error rather than a hang.

    Not reentrant: a second :func:`state_lock` inside one already held blocks
    until it times out, in this process as in any other.

    On platforms without ``fcntl`` (Windows) this degrades to a no-op context
    manager and yields ``None``. Concurrent mutators there remain racy, exactly
    as they were before; single-process use is unaffected.
    """
    if fcntl is None:  # pragma: no cover - Windows
        yield None
        return

    lock_path = state_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"timed out after {timeout:g}s waiting for the gdoc-sync state lock "
                        f"({lock_path}); another gdoc-sync process may be stuck — if none is "
                        f"running it is safe to delete that file"
                    ) from e
                time.sleep(_STATE_LOCK_POLL)
        try:
            yield lock_path
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def mutate_state(fn: Callable[[dict], object], timeout: float = STATE_LOCK_TIMEOUT) -> object:
    """Read-modify-write the state file under :func:`state_lock`.

    ``fn`` is handed the loaded state and mutates it in place. Its return value
    is the whole contract, and it means two things at once:

    * **truthy** — the state is written back, and the value is returned to the
      caller (so a mutator can hand back its own result: ``True``, the removed
      flag, a :class:`~gdoc_sync.syncstate.Conflict`, …).
    * **falsey** — nothing is written, and the value is still returned. This is
      how an idempotent no-op (marking an already-marked file) avoids
      rewriting the file.

    Load, mutate and save all happen inside one lock, which is the point: the
    lock has to span the read *and* the write or the race is unchanged.
    """
    with state_lock(timeout):
        state = load_state()
        result = fn(state)
        if result:
            save_state(state)
        return result


def get_doc_id(local_path: str | os.PathLike) -> str | None:
    state = load_state()
    return state.get("mappings", {}).get(str(Path(local_path).resolve()))


def set_doc_id(local_path: str | os.PathLike, doc_id: str, revision_id: str = "") -> None:
    resolved = str(Path(local_path).resolve())

    def apply(state: dict) -> bool:
        state.setdefault("mappings", {})
        state.setdefault("revisions", {})
        state["mappings"][resolved] = doc_id
        if revision_id:
            state["revisions"][resolved] = revision_id
        return True

    mutate_state(apply)


def get_revision(local_path: str | os.PathLike) -> str | None:
    state = load_state()
    return state.get("revisions", {}).get(str(Path(local_path).resolve()))


def set_revision(local_path: str | os.PathLike, revision_id: str) -> None:
    resolved = str(Path(local_path).resolve())

    def apply(state: dict) -> bool:
        state.setdefault("revisions", {})
        state["revisions"][resolved] = revision_id
        return True

    mutate_state(apply)


def remove_mapping(local_path: str | os.PathLike) -> bool:
    """Unlink a local file from its doc. Returns True if a mapping was removed."""
    resolved = str(Path(local_path).resolve())

    def apply(state: dict) -> bool:
        removed = state.get("mappings", {}).pop(resolved, None) is not None
        state.get("revisions", {}).pop(resolved, None)
        if resolved in state.get("pull_only", []):
            state["pull_only"].remove(resolved)
        return removed

    return bool(mutate_state(apply))


def all_mappings() -> dict[str, str]:
    """All local-file → doc-id mappings."""
    return dict(load_state().get("mappings", {}))


def is_pull_only(local_path: str | os.PathLike) -> bool:
    """Whether this file is marked one-way (doc → markdown, never the reverse).

    Some docs cannot survive a round trip — a document full of equations,
    footnotes, charts or suggestions loses them on the way back. Marking such
    a file pull-only lets it take part in `sync --all` and `watch --all`
    without that risk.

    Tabbed docs used to be the headline case and are no longer: `push` writes
    each ``# [TAB] <title>`` section into its own tab, and refuses to push a
    file that has lost those headers into a document that still has the tabs
    (see :func:`gdoc_sync.push._guard_flatten`). That guard reads the file in
    hand at the moment of the push, which is a truer test than a flag recorded
    once at import time.
    """
    return str(Path(local_path).resolve()) in load_state().get("pull_only", [])


def set_pull_only(local_path: str | os.PathLike, enabled: bool = True) -> None:
    """Mark (or unmark) a file as one-way. Idempotent."""
    resolved = str(Path(local_path).resolve())

    def apply(state: dict) -> bool:
        marked = state.setdefault("pull_only", [])
        if enabled and resolved not in marked:
            marked.append(resolved)
        elif not enabled and resolved in marked:
            marked.remove(resolved)
        else:
            return False  # already in the wanted state; don't rewrite the file
        return True

    mutate_state(apply)


# ---------------------------------------------------------------------------
# Doc-id helpers
# ---------------------------------------------------------------------------

def extract_doc_id_from_url(url: str) -> str:
    """Extract the document ID from a Google Docs URL (or pass through a bare ID)."""
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return url


# A Docs file id is a long opaque base64url-ish string. The length floor is
# what separates a real id from the things people paste by mistake — a title,
# a path, a truncated URL — every one of which `link` used to accept happily
# and store as the doc this file now points at.
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def validate_doc_id(url_or_id: str) -> str:
    """Return the doc id in ``url_or_id``, or raise ``ValueError``.

    :func:`extract_doc_id_from_url` passes anything it does not recognise
    straight through, which is right for a lenient lookup and wrong for
    `link`: a typo there silently maps a file to a doc that does not exist,
    and the first push then reports something confusing far from the cause.
    """
    doc_id = extract_doc_id_from_url((url_or_id or "").strip())
    if _DOC_ID_RE.match(doc_id):
        return doc_id
    raise ValueError(
        f"{url_or_id!r} is not a Google Doc. Give either the document's URL "
        f"(https://docs.google.com/document/d/<id>/edit) or its bare id "
        f"(at least 20 characters of letters, digits, `-` or `_`)."
    )
