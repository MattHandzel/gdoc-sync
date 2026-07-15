"""Live sync: watch linked files, auto-pull remote edits, auto-push local ones.

Drive's push-notification channel (files.watch) needs a public webhook, so
this polls instead: the remote ``revisionId`` and the local mtime, every
``interval`` seconds. Cheap — one metadata GET per file per tick.

Conflict policy: when BOTH sides changed within one tick, neither is
clobbered — the remote version is written to ``<name>.conflict.md`` beside the
file and both versions are left for the user to merge.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import get_doc_id, get_revision, remove_mapping
from .services import NUM_RETRIES, get_services


def _notify(title: str, body: str) -> None:
    """Best-effort desktop notification."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", title, body], timeout=5,
                           capture_output=True)
        except Exception:
            pass


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def watch(paths: list[Path], interval: int = 30, no_push: bool = False) -> None:
    """Watch files until interrupted. Ctrl-C to stop."""
    from .pull import pull
    from .push import push

    _, docs_service = get_services()

    tracked: dict[Path, dict] = {}
    for p in paths:
        doc_id = get_doc_id(str(p))
        if not doc_id:
            print(f"Skipping {p}: not linked to a Google Doc", file=sys.stderr, flush=True)
            continue
        remote_rev = docs_service.documents().get(
            documentId=doc_id, fields="revisionId"
        ).execute(num_retries=NUM_RETRIES).get("revisionId", "")
        stored = get_revision(str(p))
        if stored and stored != remote_rev:
            print(f"Note: {p.name} already has remote drift — run pull/push/diff "
                  "first; watch only reacts to changes made after it starts.")
        tracked[p] = {"doc_id": doc_id, "rev": remote_rev, "mtime": _mtime(p)}

    if not tracked:
        print("Nothing to watch.", file=sys.stderr, flush=True)
        sys.exit(1)

    mode = "pull-only" if no_push else "two-way"
    print(f"Watching {len(tracked)} file(s) every {interval}s ({mode}). Ctrl-C to stop.", flush=True)

    while True:
        time.sleep(interval)
        for p, t in tracked.items():
            try:
                _tick(p, t, docs_service, no_push, pull, push)
            except SystemExit:
                print(f"  Warning: sync of {p.name} failed this tick; will retry.",
                      file=sys.stderr, flush=True)
            except Exception as e:
                print(f"  Warning: {p.name}: {e}; will retry.", file=sys.stderr, flush=True)


def _tick(p: Path, t: dict, docs_service, no_push: bool, pull, push) -> None:
    remote_rev = docs_service.documents().get(
        documentId=t["doc_id"], fields="revisionId"
    ).execute(num_retries=NUM_RETRIES).get("revisionId", "")

    local_changed = _mtime(p) != t["mtime"]
    remote_changed = remote_rev != t["rev"]

    if not (local_changed or remote_changed):
        return

    stamp = time.strftime("%H:%M:%S")

    if local_changed and remote_changed:
        conflict = p.with_name(f"{p.stem}.conflict.md")
        pull(t["doc_id"], conflict)
        remove_mapping(str(conflict))  # the conflict copy must not steal the mapping
        print(f"[{stamp}] CONFLICT on {p.name}: both sides changed. "
              f"Remote saved to {conflict.name}; local left untouched.", flush=True)
        _notify("gdoc-sync conflict", f"{p.name}: remote saved to {conflict.name}")
        t["rev"] = remote_rev  # don't re-fire every tick; user merges by hand
        t["mtime"] = _mtime(p)

    elif remote_changed:
        print(f"[{stamp}] Remote changed → pulling {p.name}", flush=True)
        pull(t["doc_id"], p)
        _notify("gdoc-sync", f"Pulled remote changes into {p.name}")
        t["rev"] = get_revision(str(p)) or remote_rev
        t["mtime"] = _mtime(p)

    elif local_changed:
        if no_push:
            print(f"[{stamp}] Local change on {p.name} (push disabled with --no-push)", flush=True)
            t["mtime"] = _mtime(p)
            return
        print(f"[{stamp}] Local changed → pushing {p.name}", flush=True)
        push(p)  # remote is unchanged, so no overwrite prompt can trigger
        _notify("gdoc-sync", f"Pushed local changes from {p.name}")
        t["rev"] = get_revision(str(p)) or t["rev"]
        t["mtime"] = _mtime(p)
