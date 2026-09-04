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

# A push whose file has shrunk past this fraction of the last-synced text is
# refused the same way: 20% of a note left behind is a truncation, not an edit.
PUSH_SHRINK_MIN_RATIO = 0.2

# The mirror image, and the one that was missing: a *render* that comes back
# below this fraction of the last-synced doc is treated as a bad read of the
# doc — a select-all-delete caught mid-poll, a partial `documents.get`, a tab
# regression — not as an edit to adopt. Merging one of those into the file is
# how a note becomes empty with `action=merged` and no conflict at all.
REMOTE_SHRINK_MIN_RATIO = 0.5

# Actions reported back to the caller / the editor.
NOOP = "noop"
PUSHED = "pushed"
MERGED = "merged"
CONFLICT = "conflict"
BLOCKED = "blocked"
SKIPPED = "skipped"
ADOPTED = "adopted"

RETRY_DETAIL = "doc changed during sync — retrying next pass"


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

    ``render(asset_path)`` must return an object with ``.markdown``,
    ``.revision_id`` and ``.fingerprint``; ``push(path, *,
    expected_fingerprint=None)`` uploads the file as-is and raises
    :class:`gdoc_sync.push.RemoteChanged` when the doc's text has moved since
    the render that fingerprint came from. Both are injected so the decision
    logic can be tested without Google.

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
        # An explicit "take the doc's version" is consent to the shrink below.
        return _adopt_remote(path, local, rendered, say)

    # --- A collapsed render is a bad read, not an edit ----------------------
    shrink = _shrink_guard(remote_md, bases.remote, force)
    if shrink:
        set_conflict(path, shrink)
        say(f"  CONFLICT on {path.name}: {shrink}")
        return SyncOutcome(CONFLICT, shrink, conflicted=True, notes=outcome.notes)

    # --- The four states ----------------------------------------------------
    if not local_changed and not remote_changed:
        set_bases(path, local=local, remote=remote_md)  # refresh churn-only drift
        return outcome

    if local_changed and not remote_changed:
        if not allow_push:
            # Pushing is off either because the caller passed --no-push or
            # because this file is marked pull-only; the engine is not told
            # which, so the message names neither rather than guessing wrong.
            return SyncOutcome(SKIPPED, "local change held back (pushing disabled)",
                               notes=outcome.notes)
        guard = _empty_guard(local, bases.local, force)
        if guard:
            set_conflict(path, guard)
            return SyncOutcome(CONFLICT, guard, conflicted=True)
        say(f"  local changed → pushing {path.name}")
        if not _push(push, path, rendered):
            return SyncOutcome(SKIPPED, RETRY_DETAIL, notes=outcome.notes)
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
        if local_changed and allow_push:
            # Only the *remote* ancestor advances before the push. Advancing
            # the local one too would record the local edit as already sent,
            # so a push that fails here would never be retried and the edit
            # would live only on disk.
            set_bases(path, local=bases.local, remote=remote_md)
            say(f"  pushing local changes to {path.name}")
            if not _push(push, path, rendered):
                return SyncOutcome(SKIPPED, RETRY_DETAIL, notes=outcome.notes)
            _refresh_after_push(path, local, render, say)
            return SyncOutcome(PUSHED, "pushed local changes", pushed=True)
        set_bases(path, local=local, remote=remote_md)
        return SyncOutcome(NOOP, "remote changes already present locally")

    # Guard the concurrent-save race: if the file moved under us while we were
    # talking to Google, abandon this pass rather than overwrite the new bytes.
    current = _read(path)
    if current is None or content_hash(current) != content_hash(local):
        return SyncOutcome(
            SKIPPED, f"{path.name} changed during sync — retrying next pass"
        )

    _write_local(path, merged.text, outcome, tag="pre-merge")

    if local_changed and allow_push:
        # The remote ancestor advances now — the doc's edits are on disk, so
        # they are no longer a pending remote change — but the local one only
        # after the upload has actually happened. The two used to advance
        # together here, which meant a push that raised left the engine
        # believing the local edit had been sent: it was never pushed again,
        # and lived only in the file.
        set_bases(path, local=bases.local, remote=remote_md)
        say(f"  merged remote + local → pushing {path.name}")
        if not _push(push, path, rendered):
            return SyncOutcome(
                SKIPPED, RETRY_DETAIL,
                wrote_local=True, backup=outcome.backup, notes=outcome.notes,
            )
        _refresh_after_push(path, merged.text, render, say)
        return SyncOutcome(
            MERGED, "merged remote and local changes, pushed",
            pushed=True, wrote_local=True, backup=outcome.backup,
        )

    set_bases(path, local=merged.text, remote=remote_md)
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
        # ``local_text`` is what lets the baseline carry the file's LaTeX. Skip
        # it and the ancestor holds `[equation]` placeholders while every later
        # render holds the real math, so the first tick reports a remote edit
        # that never happened. ``asset_path`` matters for the same reason:
        # without it images render as nothing here but as ![alt](path) on
        # every later render, and each image line becomes a phantom remote
        # edit. The image resolver maps the doc's objects back to the paths
        # this file already references, so recording a baseline right after a
        # push fetches each new object once to hash it, writes no new files,
        # and leaves the index warm for every later watch tick.
        rendered = render_doc(doc_id, asset_path=path, local_text=local)
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
    """Refuse to push a file that has lost most of itself.

    Emptied was the only case this caught, which left the near-miss wide open:
    a note truncated to a single character still counted as "has content" and
    replaced the whole doc with that character.
    """
    if force:
        return ""
    base = normalize(base_local)
    if len(base) < EMPTY_GUARD_MIN_CHARS:
        return ""
    text = normalize(local)
    if not text.strip():
        return (
            "local file is empty but the doc has content — refusing to wipe "
            "the doc. Use --force if this is intentional."
        )
    # A base carrying conflict markers is inflated by both sides plus the
    # ancestor, so every honest resolution of it "shrinks". Measuring against
    # that would turn resolving a conflict into another conflict.
    if has_conflict_markers(base_local):
        return ""
    if len(text) < len(base) * PUSH_SHRINK_MIN_RATIO:
        return (
            f"local file has shrunk by {_shrink_pct(len(text), len(base))}% "
            f"since the last sync — refusing to replace the doc with what "
            f"looks like a truncated file. Use --force if this is intentional."
        )
    return ""


def _shrink_guard(remote_md: str, base_remote: str, force: bool) -> str:
    """Refuse to adopt a render that came back mostly empty.

    The symmetry with :func:`_empty_guard` is the point. A doc that renders
    short is indistinguishable, to the merge, from someone having deleted most
    of it — and because a cleanly round-tripping note has ``ours == base``,
    merge3's fast path hands the render back verbatim. So a bad read of the
    doc becomes the file, silently, reported as a clean merge.
    """
    if force:
        return ""
    base = normalize(base_remote)
    if len(base) < EMPTY_GUARD_MIN_CHARS:
        return ""
    now = len(normalize(remote_md))
    if now >= len(base) * REMOTE_SHRINK_MIN_RATIO:
        return ""
    return (
        f"the doc came back {_shrink_pct(now, len(base))}% shorter than the "
        f"last synced version. That is more likely a bad read of the doc than "
        f"an edit, so your file has been left untouched and nothing was "
        f"merged. Check the doc; if the shrink is real, "
        f"`gdoc-sync sync --adopt-remote` accepts it."
    )


def _shrink_pct(now: int, before: int) -> int:
    """How much smaller ``now`` is than ``before``, as a whole percentage."""
    if before <= 0:
        return 0
    return max(0, min(100, round((before - now) * 100 / before)))


def _push(push, path: Path, rendered) -> bool:
    """Push ``path``, returning False when the doc moved under us.

    The engine merged the doc as of ``rendered``; anything typed into it since
    would be overwritten by this upload with no conflict recorded anywhere.
    Passing the fingerprint lets the push refuse, and refusing is free: the
    ancestors are arranged so the next pass merges the new remote edit and
    sends the local one again.
    """
    from .push import RemoteChanged

    try:
        push(path, expected_fingerprint=getattr(rendered, "fingerprint", None))
    except RemoteChanged:
        return False
    return True


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

    # Math and fence languages were already restored inside render_doc.
    text = preserve_frontmatter(local, rendered.markdown)
    outcome = SyncOutcome(ADOPTED, "", wrote_local=True)
    _write_local(path, text, outcome, tag="pre-adopt-remote")
    set_bases(path, local=text, remote=rendered.markdown)
    clear_conflict(path)
    say(f"  adopting remote → wrote {path.name}")
    outcome.detail = "overwrote local with the doc's contents"
    return outcome
