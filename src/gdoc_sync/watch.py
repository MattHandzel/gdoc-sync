"""Live two-way sync: poll linked files and reconcile each one safely.

All of the decision-making lives in :mod:`.sync`; this module is the loop that
supplies I/O to it. Drive's push-notification channel (files.watch) needs a
public webhook, so changes are found by polling — a cheap ``revisionId`` fetch
per file per tick, escalating to a full render only when something may have
moved.

Every tick calls :func:`gdoc_sync.sync.reconcile`, which merges rather than
overwrites, backs up before every write, and latches a conflict until the user
resolves it. Nothing here decides which side wins.
"""

from __future__ import annotations

import contextlib
import json
import signal
import sys
import time
from pathlib import Path

from .config import get_doc_id, get_revision, is_pull_only, set_revision
from .services import NUM_RETRIES, get_services
from .sync import BLOCKED, NOOP, SKIPPED, reconcile
from .syncstate import get_conflict

# Consecutive failures before a file's poll interval starts backing off, and
# the ceiling on that backoff. Stops a broken token or a deleted doc from
# hammering the API every tick for hours.
_BACKOFF_AFTER = 3
_BACKOFF_MAX = 16

# Deliberately no desktop notifications. `watch` is normally spawned by
# gdoc-sync.nvim for the file being edited, so a sync fires on nearly every
# tick while writing — a notification per tick is pure noise for an operation
# the user just performed. Every event is printed instead, which is where the
# editor picks it up.


class _Stopped(Exception):
    """Raised in the main loop when a termination signal arrives."""


def _peek_revision(docs_service, doc_id: str) -> str:
    return (
        docs_service.documents()
        .get(documentId=doc_id, fields="revisionId")
        .execute(num_retries=NUM_RETRIES)
        .get("revisionId", "")
    )


def _emit(json_lines: bool, event: str, path: Path, detail: str, **extra) -> None:
    """Report one event, either human-readable or as a JSON line.

    The JSON form exists for gdoc-sync.nvim: it needs to know whether the file
    on disk changed (reload the buffer) and whether a conflict was raised (show
    it) — facts that are unreliable to scrape out of prose.
    """
    if json_lines:
        payload = {"event": event, "file": str(path), "detail": detail}
        payload.update(extra)
        print(json.dumps(payload), flush=True)
    else:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {path.name}: {detail}", flush=True)


def watch(
    paths: list[Path],
    interval: int = 15,
    no_push: bool = False,
    *,
    force: bool = False,
    json_lines: bool = False,
) -> None:
    """Watch files until interrupted. Ctrl-C to stop."""
    from .pull import render_doc
    from .push import push as push_file

    _, docs_service = get_services()

    def render_for(path: Path):
        return render_doc(get_doc_id(str(path)) or "", asset_path=path)

    def push_for(path: Path, *, expected_fingerprint: str | None = None) -> None:
        # The engine only pushes content it has already merged, so the CLI's
        # interactive overwrite prompt would be asking a question that has
        # been answered — and there is no tty here to answer it.
        #
        # push() narrates its progress ("Pushing to: …", "Converting markdown
        # → docx via pandoc...", "  Pushed successfully.") on stdout. Under
        # --json that stdout is a machine stream, and those four lines are not
        # events, so send them to stderr instead: consumers get a clean
        # one-JSON-object-per-line stdout and the narration is still there for
        # anyone watching the log.
        if json_lines:
            with contextlib.redirect_stdout(sys.stderr):
                push_file(path, yes=True, merged=True,
                          expected_fingerprint=expected_fingerprint)
        else:
            push_file(path, yes=True, merged=True,
                      expected_fingerprint=expected_fingerprint)

    tracked: dict[Path, dict] = {}
    for p in paths:
        doc_id = get_doc_id(str(p))
        if not doc_id:
            print(f"Skipping {p}: not linked to a Google Doc", file=sys.stderr, flush=True)
            continue
        tracked[p] = {
            "doc_id": doc_id,
            "rev": get_revision(str(p)) or "",
            "fails": 0,
            "skip": 0,
            # Read once here rather than per tick: a long-running watcher should
            # not change what it is allowed to do to a file halfway through.
            "one_way": is_pull_only(str(p)),
        }
        conflict = get_conflict(p)
        if conflict:
            _emit(json_lines, "conflict", p,
                  f"unresolved conflict from {conflict.since} — sync is paused "
                  f"for this file until you resolve it",
                  conflict=True)

    if not tracked:
        print("Nothing to watch.", file=sys.stderr, flush=True)
        sys.exit(1)

    mode = "pull-only" if no_push else "two-way"
    one_way = sum(1 for t in tracked.values() if t["one_way"])
    note = f", {one_way} of them pull-only" if one_way and not no_push else ""
    _emit(json_lines, "start", Path("."),
          f"watching {len(tracked)} file(s) every {interval}s ({mode}{note})",
          files=[str(p) for p in tracked], interval=interval, mode=mode,
          pull_only=one_way)

    def _stop(_signum, _frame):
        raise _Stopped

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass  # not on the main thread; the KeyboardInterrupt path still works

    # An initial pass, so drift that predates the watcher is handled by the
    # same safe merge as everything else instead of lying in wait.
    try:
        for p, t in tracked.items():
            _safe_tick(p, t, docs_service, no_push, force, json_lines,
                       render_for, push_for)
        while True:
            time.sleep(interval)
            for p, t in tracked.items():
                if t["skip"] > 0:
                    t["skip"] -= 1
                    continue
                _safe_tick(p, t, docs_service, no_push, force, json_lines,
                           render_for, push_for)
    except (_Stopped, KeyboardInterrupt):
        _emit(json_lines, "stop", Path("."), "watch stopped")


def _safe_tick(p, t, docs_service, no_push, force, json_lines, render_for, push_for) -> None:
    """One reconcile, with failures isolated to the file that caused them."""
    try:
        _tick(p, t, docs_service, no_push, force, json_lines, render_for, push_for)
        t["fails"] = 0
    except _Stopped:
        raise
    except SystemExit as e:
        # push()/pull() exit on unrecoverable API problems; a watcher must not
        # die with them.
        _fail(p, t, json_lines, f"sync failed (exit {e.code}); will retry")
    except Exception as e:  # noqa: BLE001
        _fail(p, t, json_lines, f"{type(e).__name__}: {e}; will retry")


def _fail(p: Path, t: dict, json_lines: bool, detail: str) -> None:
    t["fails"] += 1
    if t["fails"] >= _BACKOFF_AFTER:
        # Skip an exponentially growing number of ticks rather than retrying a
        # persistent failure on every one.
        t["skip"] = min(2 ** (t["fails"] - _BACKOFF_AFTER + 1), _BACKOFF_MAX)
        detail += f" (backing off {t['skip']} tick(s))"
    _emit(json_lines, "error", p, detail, error=True)


def _tick(p, t, docs_service, no_push, force, json_lines, render_for, push_for) -> None:
    outcome = reconcile(
        p,
        t["doc_id"],
        render=render_for,
        push=push_for,
        allow_push=not (no_push or t["one_way"]),
        force=force,
        stored_revision=t["rev"],
        peek_revision=lambda: _peek_revision(docs_service, t["doc_id"]),
    )

    if outcome.revision and outcome.revision != t["rev"]:
        t["rev"] = outcome.revision
        set_revision(str(p), outcome.revision)

    # A quiet tick is the common case and should stay quiet — the editor would
    # otherwise show a notification every interval for a file nobody touched.
    # A newly-raised conflict reports as CONFLICT, so suppressing the BLOCKED
    # ticks that follow it hides repetition, not news.
    if outcome.action in (NOOP, SKIPPED, BLOCKED):
        return

    _emit(
        json_lines,
        outcome.action,
        p,
        outcome.detail,
        reload=outcome.wrote_local,
        pushed=outcome.pushed,
        conflict=outcome.conflicted,
        backup=outcome.backup,
    )
