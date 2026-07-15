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

import os
import re
from pathlib import Path

import yaml

DEFAULT_FONT = "Garamond"
DEFAULT_THEME = "professional"
DEFAULT_SHARE = "comment"  # private | view | comment | edit

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


def get_custom_themes() -> dict:
    """User-defined themes from the config's ``themes:`` section."""
    themes = load_config().get("themes")
    return themes if isinstance(themes, dict) else {}


# ---------------------------------------------------------------------------
# State (mappings + revisions)
# ---------------------------------------------------------------------------

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


def load_state() -> dict:
    p = state_path()
    if p.exists():
        return yaml.safe_load(p.read_text()) or {}
    return {}


def save_state(state: dict) -> None:
    """Write state, preserving any non-state keys already in the file.

    In legacy combined mode the state file is also the config file, so settings
    keys (font, theme, …) ride along untouched.
    """
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.dump(state, default_flow_style=False))


def get_doc_id(local_path: str | os.PathLike) -> str | None:
    state = load_state()
    return state.get("mappings", {}).get(str(Path(local_path).resolve()))


def set_doc_id(local_path: str | os.PathLike, doc_id: str, revision_id: str = "") -> None:
    state = load_state()
    state.setdefault("mappings", {})
    state.setdefault("revisions", {})
    resolved = str(Path(local_path).resolve())
    state["mappings"][resolved] = doc_id
    if revision_id:
        state["revisions"][resolved] = revision_id
    save_state(state)


def get_revision(local_path: str | os.PathLike) -> str | None:
    state = load_state()
    return state.get("revisions", {}).get(str(Path(local_path).resolve()))


def set_revision(local_path: str | os.PathLike, revision_id: str) -> None:
    state = load_state()
    state.setdefault("revisions", {})
    state["revisions"][str(Path(local_path).resolve())] = revision_id
    save_state(state)


def remove_mapping(local_path: str | os.PathLike) -> bool:
    """Unlink a local file from its doc. Returns True if a mapping was removed."""
    state = load_state()
    resolved = str(Path(local_path).resolve())
    removed = state.get("mappings", {}).pop(resolved, None) is not None
    state.get("revisions", {}).pop(resolved, None)
    if removed:
        save_state(state)
    return removed


def all_mappings() -> dict[str, str]:
    """All local-file → doc-id mappings."""
    return dict(load_state().get("mappings", {}))


# ---------------------------------------------------------------------------
# Doc-id helpers
# ---------------------------------------------------------------------------

def extract_doc_id_from_url(url: str) -> str:
    """Extract the document ID from a Google Docs URL (or pass through a bare ID)."""
    match = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    if match:
        return match.group(1)
    return url
