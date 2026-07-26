"""The reconcile engine: one safe two-way sync of one file.

This is the whole of the sync decision-making, deliberately kept free of
network code so it can be tested exhaustively against fakes. :mod:`.watch`
supplies the I/O and calls :func:`reconcile` on a timer; ``gdoc-sync sync``
calls it once.

The contract
------------
1. **No edit is ever discarded silently.** Divergence is merged (see
   :mod:`.merge`); what cannot be merged becomes a marked conflict that
   suspends auto-sync until the user resolves it.
2. **Every overwrite is recoverable** — a timestamped backup precedes any
   write to a tracked file, and writes are atomic.
3. **Uncertainty stops the machine.** With no merge ancestor and a real
   divergence, the engine refuses to guess and asks for an explicit
   ``--adopt-local`` / ``--adopt-remote``.

What went wrong before
----------------------
0.5.x compared the local mtime and the doc's ``revisionId``. Both are proxies
that lie: Google rewrites the revisionId on autosave and presence changes
(so an open browser tab looked like a constant stream of remote edits), and
mtime says nothing about content. Worse, on detecting a genuine both-sides
conflict it wrote a ``.conflict.md`` copy and then advanced *both* baselines —
so the divergence was immediately forgotten, and the next one-sided change
overwrote the side that had never been merged. Editing in Google Docs and in
markdown and then touching either one again destroyed the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .merge import content_hash, has_conflict_markers, merge3, normalize, same_content
from .syncstate import (
    backup_file,
    clear_conflict,
    get_bases,
    get_conflict,
    set_bases,
    set_conflict,
)

# A push that would replace a substantial doc with (near-)nothing is treated as
# an accident — a truncated file, a crashed editor, the wrong path — and is
# refused unless explicitly forced.
EMPTY_GUARD_MIN_CHARS = 40

# Actions reported back to the caller / the editor.
NOOP = "noop"
PUSHED = "pushed"
MERGED = "merged"
CONFLICT = "conflict"
BLOCKED = "blocked"
SKIPPED = "skipped"
ADOPTED = "adopted"


@dataclass
class SyncOutcome:
    """What a reconcile did, in a form the plugin can render verbatim."""

    action: str
    detail: str
    conflicted: bool = False
    pushed: bool = False
    wrote_local: bool = False
    backup: str = ""
    notes: list[str] = field(default_factory=list)
    # The doc revision this pass last observed, so a caller polling in a loop
    # can keep its cheap-skip hint current without an extra API round trip.
    revision: str = ""

    @property
    def changed(self) -> bool:
        return self.pushed or self.wrote_local

    def line(self, name: str) -> str:
        return f"{name}: {self.detail}"


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return None


def _write_local(path: Path, text: str, outcome: SyncOutcome, tag: str) -> bool:
    """Back up then atomically replace ``path``. Records the backup on ``outcome``."""
    from .config import atomic_write

    backup = backup_file(path, tag=tag)
    if backup:
        outcome.backup = str(backup)
    atomic_write(path, text)
    outcome.wrote_local = True
    return True


def reconcile(
    path: Path,
    doc_id: str,
    *,
    render,
    push,
    stored_revision: str | None = None,
    **kwargs,
) -> SyncOutcome:
    """Reconcile ``path`` with its doc, reporting the revision it last saw.

    Thin wrapper over :func:`_reconcile` that remembers the newest revision id
    observed through ``render`` — including the re-read after a push — so a
    polling caller can refresh its cheap-skip hint for free.
    """
    seen = {"rev": stored_revision or ""}

    def tracking_render(p=None):
        rendered = render(p)
        seen["rev"] = getattr(rendered, "revision_id", "") or seen["rev"]
        return rendered

    outcome = _reconcile(
        path, doc_id, render=tracking_render, push=push,
        stored_revision=stored_revision, **kwargs,
    )
    outcome.revision = outcome.revision or seen["rev"]
    return outcome


def _reconcile(
    path: Path,
    doc_id: str,
    *,
    render,
    push,
    allow_push: bool = True,
    adopt: str | None = None,
    force: bool = False,
    stored_revision: str | None = None,
    peek_revision=None,
    say=lambda *_: None,
) -> SyncOutcome:
    """Bring ``path`` and its Google Doc into agreement, losing nothing.

    ``render(asset_path)`` must return an object with ``.markdown`` and
    ``.revision_id``; ``push(path)`` uploads the file as-is. Both are injected
    so the decision logic can be tested without Google.

    ``peek_revision()``, when given, is a cheap revision-id fetch used to skip
    the full render when nothing can have changed.

    ``adopt`` resolves a no-ancestor divergence: ``"local"`` pushes local over
    the doc, ``"remote"`` overwrites local with the doc. ``force`` additionally
    overrides the empty-content guard.
    """
    path = Path(path)
    outcome = SyncOutcome(NOOP, "up to date")

    local = _read(path)
    if local is None:
        return SyncOutcome(SKIPPED, f"cannot read {path.name}")

    bases = get_bases(path)
    conflict = get_conflict(path)

    # --- An unresolved conflict suspends automatic sync ---------------------
    if conflict and not adopt:
        if conflict.markers and not has_conflict_markers(local):
            # The user edited the markers away — that is the resolution.
            clear_conflict(path)
            outcome.notes.append("conflict resolved locally")
            conflict = None
        else:
            return SyncOutcome(
                BLOCKED,
                f"conflicted since {conflict.since} — resolve it, then "
                f"`gdoc-sync resolve {path.name}`",
                conflicted=True,
            )

    # --- Cheap exit: nothing can have changed ------------------------------
    local_changed = bases.known and not same_content(local, bases.local)
    if (
        peek_revision is not None
        and bases.known
        and not local_changed
        and stored_revision
    ):
        try:
            if peek_revision() == stored_revision:
                return outcome
        except Exception:  # noqa: BLE001 — a failed peek just means do the full check
            pass

    rendered = render(path)
    remote_md = rendered.markdown

    # --- No ancestor: never guess ------------------------------------------
    if not bases.known:
        return _first_sync(
            path, local, rendered, adopt=adopt, allow_push=allow_push,
            force=force, push=push, render=render, say=say,
        )

    remote_changed = not same_content(remote_md, bases.remote)

    if adopt == "local":
        return _adopt_local(path, local, rendered, push, render, force, say)
    if adopt == "remote":
        return _adopt_remote(path, local, rendered, say)

    # --- The four states ----------------------------------------------------
    if not local_changed and not remote_changed:
        set_bases(path, local=local, remote=remote_md)  # refresh churn-only drift
        return outcome

    if local_changed and not remote_changed:
        if not allow_push:
            outcome.notes.append("local change not pushed (--no-push)")
            return SyncOutcome(SKIPPED, "local change held back (--no-push)",
                               notes=outcome.notes)
        guard = _empty_guard(local, bases.local, force)
        if guard:
            set_conflict(path, guard)
            return SyncOutcome(CONFLICT, guard, conflicted=True)
        say(f"  local changed → pushing {path.name}")
        push(path)
        _refresh_after_push(path, local, render, say)
        return SyncOutcome(PUSHED, "pushed local changes", pushed=True)

    # Remote moved. Replay the doc's edits onto the local file rather than
    # overwriting it, so local-only formatting survives.
    merged = merge3(
        local, bases.remote, remote_md,
        label_ours=f"{path.name} (local)",
        label_base="last synced",
        label_theirs="Google Doc",
    )

    if merged.conflicted:
        return _record_conflict(path, local, merged, rendered, say)

    # Nothing to write when the merge reproduced what is already on disk.
    if same_content(merged.text, local):
        set_bases(path, local=local, remote=remote_md)
        if local_changed and allow_push:
            say(f"  pushing local changes to {path.name}")
            push(path)
            _refresh_after_push(path, local, render, say)
            return SyncOutcome(PUSHED, "pushed local changes", pushed=True)
        return SyncOutcome(NOOP, "remote changes already present locally")

    # Guard the concurrent-save race: if the file moved under us while we were
    # talking to Google, abandon this pass rather than overwrite the new bytes.
    current = _read(path)
    if current is None or content_hash(current) != content_hash(local):
        return SyncOutcome(
            SKIPPED, f"{path.name} changed during sync — retrying next pass"
        )

    _write_local(path, merged.text, outcome, tag="pre-merge")
    set_bases(path, local=merged.text, remote=remote_md)

    if local_changed and allow_push:
        say(f"  merged remote + local → pushing {path.name}")
        push(path)
        _refresh_after_push(path, merged.text, render, say)
        return SyncOutcome(
            MERGED, "merged remote and local changes, pushed",
            pushed=True, wrote_local=True, backup=outcome.backup,
        )

    return SyncOutcome(
        MERGED, "merged remote changes into local file",
        wrote_local=True, backup=outcome.backup,
    )


def record_sync_baseline(path: Path, doc_id: str, local_text: str | None = None) -> bool:
    """Record the merge ancestors for a file whose doc now matches it.

    Called after ``create`` and a plain ``push``, both of which leave the two
    sides in agreement. Without it the file has no ancestor, so the very first
    ``sync`` or ``watch`` would find two texts that differ — because the
    round trip is lossy, not because anyone edited anything — and correctly but
    uselessly refuse to guess which side to keep.

    Best-effort: failing to record a baseline costs one `--adopt-` prompt
    later, so it must never turn a successful create/push into an error.
    """
    from .pull import render_doc

    try:
        path = Path(path)
        local = local_text if local_text is not None else path.read_text(encoding="utf-8")
        rendered = render_doc(doc_id, asset_path=None)
        set_bases(path, local=local, remote=rendered.markdown)
        clear_conflict(path)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"  Note: could not record the sync baseline ({e}); "
              f"the first `gdoc-sync sync` may ask which side to keep.")
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _empty_guard(local: str, base_local: str, force: bool) -> str:
    """Refuse to push an emptied file over a doc that had real content."""
    if force:
        return ""
    if normalize(local).strip():
        return ""
    if len(normalize(base_local)) < EMPTY_GUARD_MIN_CHARS:
        return ""
    return (
        "local file is empty but the doc has content — refusing to wipe the "
        "doc. Use --force if this is intentional."
    )


def _refresh_after_push(path: Path, local_text: str, render, say) -> None:
    """Re-anchor the ancestors on what the doc actually says after a push.

    A push rewrites the doc, so without this the very next pass would see the
    doc's new revision as an unexplained remote edit and merge against a stale
    ancestor.
    """
    try:
        after = render(path)
        set_bases(path, local=local_text, remote=after.markdown)
    except Exception as e:  # noqa: BLE001
        # Leave the old ancestor rather than a wrong one; the next pass
        # re-derives it. Worst case is one redundant merge.
        say(f"  note: could not re-read doc after push ({e})")


def _record_conflict(path: Path, local: str, merged, rendered, say) -> SyncOutcome:
    """Write the conflicted merge to disk and latch the conflict flag."""
    from .config import get_conflict_style

    outcome = SyncOutcome(CONFLICT, "", conflicted=True)
    style = get_conflict_style()
    remote_copy = ""

    if style == "sidecar":
        # Leave the file untouched; drop the doc's version beside it.
        sidecar = path.with_name(f"{path.stem}.remote.md")
        _write_local(sidecar, rendered.markdown, SyncOutcome(NOOP, ""), tag="remote")
        remote_copy = str(sidecar)
        new_base_local = local
        detail = (
            f"both sides changed and could not be merged automatically. "
            f"The doc's version is in {sidecar.name}; your file is untouched."
        )
    else:
        current = _read(path)
        if current is None or content_hash(current) != content_hash(local):
            return SyncOutcome(
                SKIPPED, f"{path.name} changed during sync — retrying next pass"
            )
        _write_local(path, merged.text, outcome, tag="pre-conflict")
        new_base_local = merged.text
        detail = (
            f"both sides changed in the same place ({merged.conflict_count} "
            f"conflict(s)). Merge markers written to {path.name}; the original "
            f"is backed up."
        )

    # Re-anchor both ancestors on the conflicted state. The user's resolution
    # already accounts for the doc's version — it is sitting right there in the
    # markers — so once they resolve it, the file reads as a plain local change
    # and pushes cleanly. Leaving the old ancestor in place would instead make
    # every resolution collide with the same remote edit forever.
    set_bases(path, local=new_base_local, remote=rendered.markdown)
    set_conflict(path, detail, markers=(style != "sidecar"), remote_copy=remote_copy)
    say(f"  CONFLICT on {path.name}: {detail}")
    outcome.detail = detail
    return outcome


def _first_sync(
    path: Path, local: str, rendered, *, adopt, allow_push, force, push, render, say
) -> SyncOutcome:
    """Establish ancestors for a file that has never been reconciled.

    Covers both a fresh link and an upgrade from 0.5.x, which stored no
    ancestor at all.
    """
    remote_md = rendered.markdown

    if same_content(local, remote_md):
        set_bases(path, local=local, remote=remote_md)
        return SyncOutcome(NOOP, "already in agreement; sync baseline recorded")

    if adopt == "local":
        return _adopt_local(path, local, rendered, push, render, force, say)
    if adopt == "remote":
        return _adopt_remote(path, local, rendered, say)

    detail = (
        "no sync baseline for this file and the two sides differ, so there is "
        "nothing safe to merge against. Run `gdoc-sync diff` to compare, then "
        "`gdoc-sync sync --adopt-local` (push yours) or `--adopt-remote` "
        "(take the doc's)."
    )
    set_conflict(path, detail)
    return SyncOutcome(CONFLICT, detail, conflicted=True)


def _adopt_local(path: Path, local: str, rendered, push, render, force, say) -> SyncOutcome:
    guard = _empty_guard(local, rendered.markdown, force)
    if guard:
        return SyncOutcome(CONFLICT, guard, conflicted=True)
    say(f"  adopting local → pushing {path.name}")
    push(path)
    _refresh_after_push(path, local, render, say)
    clear_conflict(path)
    return SyncOutcome(ADOPTED, "pushed local over the doc", pushed=True)


def _adopt_remote(path: Path, local: str, rendered, say) -> SyncOutcome:
    from .pull import preserve_frontmatter

    text = preserve_frontmatter(local, rendered.markdown)
    outcome = SyncOutcome(ADOPTED, "", wrote_local=True)
    _write_local(path, text, outcome, tag="pre-adopt-remote")
    set_bases(path, local=text, remote=rendered.markdown)
    clear_conflict(path)
    say(f"  adopting remote → wrote {path.name}")
    outcome.detail = "overwrote local with the doc's contents"
    return outcome
