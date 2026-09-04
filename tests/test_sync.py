"""Reconcile-engine tests — the guarantees that stop data loss.

Every test drives :func:`gdoc_sync.sync.reconcile` against a fake doc, so the
full decision table runs with no network. The scenarios at the bottom are
direct regressions for the 0.5.x bug where editing in both Google Docs and
markdown destroyed one side.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from gdoc_sync import config, sync, syncstate
from gdoc_sync.merge import content_hash
from gdoc_sync.push import RemoteChanged


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


@dataclass
class FakeRendered:
    markdown: str
    revision_id: str = "rev-1"
    fingerprint: str = ""


class FakeDoc:
    """A stand-in Google Doc: `push` uploads the file, `render` reads it back.

    ``round_trip`` models the fact that md → Google Doc → md is lossy; the
    engine has to stay correct in spite of it.

    ``push`` models the real one's lost-update check: given the fingerprint of
    a render that is no longer current, it refuses rather than overwriting
    what was typed in between.
    """

    def __init__(self, markdown: str = "", round_trip=None):
        self.markdown = markdown
        self.revision = 1
        self.pushes = 0
        self._round_trip = round_trip or (lambda s: s)

    @property
    def fingerprint(self) -> str:
        return content_hash(self.markdown)

    def render(self, _path=None) -> FakeRendered:
        return FakeRendered(self.markdown, f"rev-{self.revision}", self.fingerprint)

    def push(self, path, *, expected_fingerprint=None) -> None:
        if expected_fingerprint is not None and expected_fingerprint != self.fingerprint:
            raise RemoteChanged("the doc moved under the render")
        self.pushes += 1
        self.revision += 1
        self.markdown = self._round_trip(path.read_text())

    def edit(self, markdown: str) -> None:
        """Simulate someone typing in Google Docs."""
        self.markdown = markdown
        self.revision += 1


def run(path, doc, **kw):
    return sync.reconcile(path, "doc-id", render=doc.render, push=doc.push, **kw)


def linked(tmp_path, text: str, doc_text: str | None = None, round_trip=None):
    """A markdown file plus a doc, already sharing a sync baseline."""
    path = tmp_path / "note.md"
    path.write_text(text)
    doc = FakeDoc(doc_text if doc_text is not None else text, round_trip=round_trip)
    syncstate.set_bases(path, local=text, remote=doc.markdown)
    return path, doc


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------

def test_no_changes_is_a_noop(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    result = run(path, doc)
    assert result.action == sync.NOOP
    assert doc.pushes == 0


def test_local_only_change_pushes(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    path.write_text("# Title\n\nbody edited locally\n")

    result = run(path, doc)

    assert result.action == sync.PUSHED
    assert result.pushed
    assert "edited locally" in doc.markdown


def test_remote_only_change_lands_in_the_local_file(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    doc.edit("# Title\n\nbody edited in google docs\n")

    result = run(path, doc)

    assert result.action == sync.MERGED
    assert "edited in google docs" in path.read_text()
    assert doc.pushes == 0  # nothing to send back


def test_both_sides_edit_different_places_and_both_survive(tmp_path):
    """The reported bug, in its simplest form."""
    original = "# Notes\n\nintro paragraph\n\nmiddle paragraph\n\nfinal paragraph\n"
    path, doc = linked(tmp_path, original)

    path.write_text(original.replace("intro paragraph", "INTRO EDITED IN VIM"))
    doc.edit(original.replace("final paragraph", "FINAL EDITED IN GOOGLE DOCS"))

    result = run(path, doc)

    assert result.action == sync.MERGED
    merged = path.read_text()
    assert "INTRO EDITED IN VIM" in merged
    assert "FINAL EDITED IN GOOGLE DOCS" in merged
    # …and the merged result is what the doc now holds.
    assert "INTRO EDITED IN VIM" in doc.markdown
    assert "FINAL EDITED IN GOOGLE DOCS" in doc.markdown


def test_overlapping_edits_conflict_without_losing_either_side(tmp_path):
    original = "# Notes\n\nthe same line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMY VERSION\n")
    doc.edit("# Notes\n\nTHEIR VERSION\n")

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert result.conflicted
    text = path.read_text()
    assert "MY VERSION" in text
    assert "THEIR VERSION" in text
    assert doc.pushes == 0  # never push an unresolved conflict


# ---------------------------------------------------------------------------
# Sticky conflict state — the actual 0.5.x data-loss mechanism
# ---------------------------------------------------------------------------

def test_conflict_blocks_further_sync_until_resolved(tmp_path):
    """0.5.x forgot the conflict immediately and then clobbered the un-merged
    side on the next one-sided change. It must stay latched instead."""
    original = "# Notes\n\nshared line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMY VERSION\n")
    doc.edit("# Notes\n\nTHEIR VERSION\n")

    assert run(path, doc).action == sync.CONFLICT

    # Now the user edits locally again — the old code pushed here and destroyed
    # the Google Docs edit.
    before = doc.markdown
    result = run(path, doc)

    assert result.action == sync.BLOCKED
    assert doc.pushes == 0
    assert doc.markdown == before, "remote edits must not be overwritten"


def test_remote_change_after_conflict_does_not_overwrite_local(tmp_path):
    """The mirror image: a later remote-only change must not wipe the
    un-merged local file."""
    original = "# Notes\n\nshared line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMY VERSION\n")
    doc.edit("# Notes\n\nTHEIR VERSION\n")
    run(path, doc)

    conflicted_text = path.read_text()
    doc.edit("# Notes\n\nTHEIR SECOND VERSION\n")

    result = run(path, doc)

    assert result.action == sync.BLOCKED
    assert path.read_text() == conflicted_text
    assert "MY VERSION" in path.read_text()


def test_removing_the_markers_resolves_the_conflict(tmp_path):
    original = "# Notes\n\nshared line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMY VERSION\n")
    doc.edit("# Notes\n\nTHEIR VERSION\n")
    run(path, doc)

    # User merges by hand and removes the markers.
    path.write_text("# Notes\n\nMY VERSION and THEIR VERSION\n")

    result = run(path, doc)

    assert result.action == sync.PUSHED
    assert syncstate.get_conflict(path) is None
    assert "MY VERSION and THEIR VERSION" in doc.markdown


def test_conflict_survives_a_restart(tmp_path):
    """The flag lives in the state file, not in the watcher's memory."""
    original = "# Notes\n\nshared\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMINE\n")
    doc.edit("# Notes\n\nTHEIRS\n")
    run(path, doc)

    assert syncstate.get_conflict(path) is not None
    assert syncstate.all_conflicts()  # readable by a fresh process


def test_sidecar_conflict_style_leaves_the_file_untouched(tmp_path, monkeypatch):
    cfg = tmp_path / "c.yaml"
    cfg.write_text("defaults:\n  conflict_style: sidecar\n")
    config.set_config_override(cfg)

    original = "# Notes\n\nshared line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Notes\n\nMY VERSION\n")
    doc.edit("# Notes\n\nTHEIR VERSION\n")

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert path.read_text() == "# Notes\n\nMY VERSION\n"  # untouched
    sidecar = tmp_path / "note.remote.md"
    assert sidecar.exists()
    assert "THEIR VERSION" in sidecar.read_text()


# ---------------------------------------------------------------------------
# Round-trip noise must not look like an edit
# ---------------------------------------------------------------------------

def test_lossy_round_trip_does_not_trigger_spurious_syncs(tmp_path):
    """`md → doc → md` is not the identity. Comparing each side against its own
    snapshot is what keeps that from reading as an endless edit war."""
    def lossy(md: str) -> str:
        # The doc renders emphasis differently and pads blank lines.
        return md.replace("*emphasis*", "_emphasis_") + "\n"

    text = "# Title\n\nsome *emphasis* here\n"
    path = tmp_path / "note.md"
    path.write_text(text)
    doc = FakeDoc(lossy(text), round_trip=lossy)
    syncstate.set_bases(path, local=text, remote=doc.markdown)

    for _ in range(5):
        result = run(path, doc)
        assert result.action == sync.NOOP, f"spurious {result.action}: {result.detail}"

    assert doc.pushes == 0
    assert path.read_text() == text  # the local file was never rewritten


def test_revision_bump_without_content_change_is_a_noop(tmp_path):
    """Google rewrites revisionId on autosave/presence; that is not an edit."""
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    doc.revision += 5  # revision churn, identical content

    result = run(path, doc)

    assert result.action == sync.NOOP
    assert doc.pushes == 0


def test_whitespace_only_drift_is_not_an_edit(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    doc.edit("# Title\n\n\n\nbody   \n")
    assert run(path, doc).action == sync.NOOP


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------

def test_record_sync_baseline_makes_the_first_sync_clean(tmp_path, monkeypatch):
    """`create`/`push` leave both sides in agreement, so they record the
    ancestor. Without it the first sync sees two texts that differ only by the
    lossy round trip and — correctly but uselessly — refuses to guess."""
    def lossy(md: str) -> str:
        return md.replace("*emphasis*", "_emphasis_")

    path = tmp_path / "note.md"
    text = "# Fresh\n\nbody with *emphasis*\n"
    path.write_text(text)
    doc = FakeDoc(lossy(text), round_trip=lossy)

    # Without a baseline the divergence is unresolvable.
    assert run(path, doc).action == sync.CONFLICT
    syncstate.clear_conflict(path)

    # What create/push now do at the moment the two sides agree.
    import gdoc_sync.pull
    monkeypatch.setattr(gdoc_sync.pull, "render_doc",
                        lambda doc_id, **kw: doc.render())
    assert sync.record_sync_baseline(path, "doc-id", text)
    assert syncstate.get_bases(path).known
    assert syncstate.get_conflict(path) is None

    # And now a fresh sync is a no-op instead of a conflict.
    assert run(path, doc).action == sync.NOOP
    assert doc.pushes == 0


def test_record_sync_baseline_never_raises(tmp_path, monkeypatch):
    """A failed baseline costs one prompt later; it must not fail a create."""
    import gdoc_sync.pull

    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(gdoc_sync.pull, "render_doc", boom)
    path = tmp_path / "note.md"
    path.write_text("x")
    assert sync.record_sync_baseline(path, "doc-id", "x") is False


def test_first_sync_with_divergence_refuses_to_guess(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Local version\n\nlocal content here\n")
    doc = FakeDoc("# Remote version\n\nremote content here\n")

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert "adopt-local" in result.detail
    assert doc.pushes == 0
    assert "local content here" in path.read_text()


def test_first_sync_when_already_in_agreement_just_records_the_baseline(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Same\n\ncontent\n")
    doc = FakeDoc("# Same\n\ncontent\n")

    result = run(path, doc)

    assert result.action == sync.NOOP
    assert syncstate.get_bases(path).known


def test_adopt_local_pushes_over_the_doc(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Local\n\nkeep this\n")
    doc = FakeDoc("# Remote\n\ndifferent\n")

    result = run(path, doc, adopt="local")

    assert result.action == sync.ADOPTED
    assert "keep this" in doc.markdown


def test_adopt_remote_overwrites_the_local_file(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Local\n\ndiscard this\n")
    doc = FakeDoc("# Remote\n\nkeep this\n")

    result = run(path, doc, adopt="remote")

    assert result.action == sync.ADOPTED
    assert "keep this" in path.read_text()
    assert syncstate.list_backups(path), "the overwritten file must be recoverable"


def test_adopt_remote_preserves_frontmatter(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("---\ntitle: Mine\n---\n\n# Local\n\nold\n")
    doc = FakeDoc("# Remote\n\nnew\n")

    run(path, doc, adopt="remote")

    text = path.read_text()
    assert text.startswith("---\ntitle: Mine\n---")
    assert "new" in text


def test_emptied_file_does_not_wipe_the_doc(tmp_path):
    original = "# Notes\n\nA substantial amount of content that must not vanish.\n"
    path, doc = linked(tmp_path, original)
    path.write_text("")  # editor crash, bad truncation, wrong file…

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert doc.pushes == 0
    assert "substantial amount" in doc.markdown


def test_force_allows_an_intentional_empty_push(tmp_path):
    original = "# Notes\n\nA substantial amount of content that must not vanish.\n"
    path, doc = linked(tmp_path, original)
    path.write_text("")

    result = run(path, doc, force=True)

    assert result.action == sync.PUSHED


def test_no_push_holds_local_changes_back(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    path.write_text("# Title\n\nlocal edit\n")

    result = run(path, doc, allow_push=False)

    assert result.action == sync.SKIPPED
    assert doc.pushes == 0


def test_concurrent_local_save_aborts_the_write(tmp_path, monkeypatch):
    """If the file changes while we are talking to Google, the in-flight merge
    is stale — it must be abandoned, not written over the newer bytes."""
    original = "# Notes\n\nintro\n\nbody\n"
    path, doc = linked(tmp_path, original)
    doc.edit("# Notes\n\nintro REMOTE\n\nbody\n")

    real_render = doc.render

    def render_then_user_saves(p=None):
        rendered = real_render(p)
        path.write_text("# Notes\n\nintro\n\nbody\n\nJUST TYPED THIS\n")
        return rendered

    result = sync.reconcile(path, "doc-id", render=render_then_user_saves, push=doc.push)

    assert result.action == sync.SKIPPED
    assert "JUST TYPED THIS" in path.read_text()


def test_every_local_overwrite_leaves_a_backup(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    doc.edit("# Title\n\nremote edit\n")

    result = run(path, doc)

    assert result.wrote_local
    assert result.backup, "a merge that rewrites the file must back it up first"
    assert "body" in open(result.backup).read()


def test_unreadable_file_is_skipped_not_synced(tmp_path):
    path = tmp_path / "gone.md"
    doc = FakeDoc("# Remote\n")
    assert run(path, doc).action == sync.SKIPPED


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------

def test_repeated_reconciles_converge_and_stay_quiet(tmp_path):
    """After a two-sided sync settles, further passes must do nothing at all."""
    original = "# Title\n\nalpha\n\nbeta\n\ngamma\n\ndelta\n\nepsilon\n"
    path, doc = linked(tmp_path, original)
    path.write_text(original.replace("alpha", "ALPHA LOCAL"))
    doc.edit(original.replace("epsilon", "EPSILON REMOTE"))

    first = run(path, doc)
    assert first.action == sync.MERGED
    assert first.changed

    pushes_after_first = doc.pushes
    for _ in range(4):
        assert run(path, doc).action == sync.NOOP
    assert doc.pushes == pushes_after_first
    assert "ALPHA LOCAL" in path.read_text()
    assert "EPSILON REMOTE" in path.read_text()


def test_conflict_then_resolution_converges(tmp_path):
    """An overlapping edit conflicts once, then settles for good once resolved."""
    original = "# Title\n\nthe contested line\n"
    path, doc = linked(tmp_path, original)
    path.write_text("# Title\n\nlocal wording\n")
    doc.edit("# Title\n\nremote wording\n")

    assert run(path, doc).action == sync.CONFLICT
    assert run(path, doc).action == sync.BLOCKED  # stays latched

    path.write_text("# Title\n\nagreed wording\n")  # user resolves by hand

    assert run(path, doc).action == sync.PUSHED
    for _ in range(3):
        assert run(path, doc).action == sync.NOOP
    assert "agreed wording" in doc.markdown


def test_peek_revision_skips_the_expensive_render(tmp_path):
    path, doc = linked(tmp_path, "# Title\n\nbody\n")
    calls = {"render": 0}

    def counting_render(p=None):
        calls["render"] += 1
        return doc.render(p)

    result = sync.reconcile(
        path, "doc-id", render=counting_render, push=doc.push,
        stored_revision="rev-1", peek_revision=lambda: "rev-1",
    )

    assert result.action == sync.NOOP
    assert calls["render"] == 0, "an unchanged file must not be re-downloaded"


# ---------------------------------------------------------------------------
# A bad read of the doc is not an edit (P0-7)
# ---------------------------------------------------------------------------

# Long enough to clear EMPTY_GUARD_MIN_CHARS on both sides.
SUBSTANTIAL = (
    "# Meeting notes\n\nWe agreed the migration ships on Thursday.\n\n"
    "Owner: Priya. Rollback plan is in the runbook.\n"
)


def test_a_collapsed_remote_render_does_not_empty_the_file(tmp_path):
    """The headline data-loss bug: a doc that renders empty ate the note.

    For a note that round-trips cleanly ``ours == base``, so merge3's fast
    path returns the render verbatim — and a select-all-delete caught
    mid-poll, or a partial `documents.get`, became the file with
    ``action=merged`` and no conflict at all.
    """
    path, doc = linked(tmp_path, SUBSTANTIAL)
    doc.edit("")  # the doc read back as nothing

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert result.conflicted
    assert not result.wrote_local
    assert path.read_text() == SUBSTANTIAL
    assert "shorter" in result.detail
    assert "--adopt-remote" in result.detail


def test_a_truncated_remote_render_does_not_truncate_the_file(tmp_path):
    path, doc = linked(tmp_path, SUBSTANTIAL)
    doc.edit("# Meeting notes\n")  # ~15% of the doc came back

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert path.read_text() == SUBSTANTIAL
    assert "%" in result.detail


def test_a_small_remote_deletion_still_merges(tmp_path):
    """The guard must not fire on an ordinary edit that shortens the doc."""
    path, doc = linked(tmp_path, SUBSTANTIAL)
    doc.edit(SUBSTANTIAL.replace("Rollback plan is in the runbook.\n", ""))

    result = run(path, doc)

    assert result.action == sync.MERGED
    assert "runbook" not in path.read_text()


def test_adopt_remote_accepts_a_shrunken_doc(tmp_path):
    """The escape hatch the conflict names has to actually work."""
    path, doc = linked(tmp_path, SUBSTANTIAL)
    doc.edit("# Meeting notes\n")

    result = run(path, doc, adopt="remote")

    assert result.action == sync.ADOPTED
    assert path.read_text().strip() == "# Meeting notes"


def test_force_accepts_a_shrunken_doc(tmp_path):
    path, doc = linked(tmp_path, SUBSTANTIAL)
    doc.edit("# Meeting notes\n")

    assert run(path, doc, force=True).action == sync.MERGED


# ---------------------------------------------------------------------------
# A truncated file is not an edit either (P0-8)
# ---------------------------------------------------------------------------

def test_a_truncated_file_does_not_replace_the_doc(tmp_path):
    """The old guard only caught a file emptied to exactly nothing."""
    path, doc = linked(tmp_path, SUBSTANTIAL)
    path.write_text("x\n")

    result = run(path, doc)

    assert result.action == sync.CONFLICT
    assert doc.pushes == 0
    assert "Rollback plan" in doc.markdown
    assert "%" in result.detail
    assert "--force" in result.detail


def test_force_allows_an_intentional_truncation(tmp_path):
    path, doc = linked(tmp_path, SUBSTANTIAL)
    path.write_text("x\n")

    assert run(path, doc, force=True).action == sync.PUSHED
    assert doc.markdown == "x\n"


def test_an_ordinary_shortening_edit_still_pushes(tmp_path):
    path, doc = linked(tmp_path, SUBSTANTIAL)
    path.write_text("# Meeting notes\n\nShips Thursday. Priya owns it.\n")

    assert run(path, doc).action == sync.PUSHED


# ---------------------------------------------------------------------------
# The merged-push window (P0-12)
# ---------------------------------------------------------------------------

def test_a_doc_edited_during_the_merged_push_window_is_not_overwritten(tmp_path):
    """Between the render and the upload, `merged=True` skipped every check."""
    original = "# Title\n\nalpha\n\nbeta\n\ngamma\n\ndelta\n"
    path, doc = linked(tmp_path, original)
    path.write_text(original.replace("alpha", "ALPHA LOCAL"))
    doc.edit(original.replace("delta", "DELTA REMOTE"))

    real_render = doc.render

    def render_then_someone_types(p=None):
        rendered = real_render(p)
        doc.edit(doc.markdown.replace("beta", "BETA TYPED WHILE WE MERGED"))
        return rendered

    result = sync.reconcile(path, "doc-id",
                            render=render_then_someone_types, push=doc.push)

    assert result.action == sync.SKIPPED
    assert "retrying next pass" in result.detail
    assert doc.pushes == 0
    assert "BETA TYPED WHILE WE MERGED" in doc.markdown


def test_a_failed_merged_push_is_retried_with_both_edits_intact(tmp_path):
    """The baselines must not record a push that never happened.

    Both ancestors used to advance before the upload, so a push that raised
    left the engine believing the local edit had been sent. It was never
    pushed again and survived only on disk.
    """
    original = "# Title\n\nalpha\n\nbeta\n\ngamma\n\ndelta\n"
    path, doc = linked(tmp_path, original)
    path.write_text(original.replace("alpha", "ALPHA LOCAL"))
    doc.edit(original.replace("delta", "DELTA REMOTE"))

    def push_that_fails(p, *, expected_fingerprint=None):
        raise RemoteChanged("doc moved")

    first = sync.reconcile(path, "doc-id", render=doc.render, push=push_that_fails)
    assert first.action == sync.SKIPPED
    assert not first.pushed
    assert "ALPHA LOCAL" in path.read_text()
    assert "DELTA REMOTE" in path.read_text(), "the merge itself still landed"

    second = run(path, doc)

    assert second.pushed, "the local edit must still be pending after a failed push"
    assert "ALPHA LOCAL" in doc.markdown
    assert "DELTA REMOTE" in doc.markdown


def test_any_push_exception_leaves_the_local_edit_pending(tmp_path):
    """Not just RemoteChanged: a network blow-up must not eat the edit."""
    original = "# Title\n\nalpha\n\nbeta\n\ngamma\n\ndelta\n"
    path, doc = linked(tmp_path, original)
    path.write_text(original.replace("alpha", "ALPHA LOCAL"))
    doc.edit(original.replace("delta", "DELTA REMOTE"))

    def exploding_push(p, *, expected_fingerprint=None):
        raise RuntimeError("network gone")

    with pytest.raises(RuntimeError):
        sync.reconcile(path, "doc-id", render=doc.render, push=exploding_push)

    second = run(path, doc)

    assert second.pushed
    assert "ALPHA LOCAL" in doc.markdown
    assert "DELTA REMOTE" in doc.markdown


def test_a_plain_push_also_checks_the_fingerprint(tmp_path):
    """The local-only-change path races too: the doc can move mid-pass."""
    path, doc = linked(tmp_path, SUBSTANTIAL)
    path.write_text(SUBSTANTIAL + "\nOne more line.\n")

    real_render = doc.render

    def render_then_someone_types(p=None):
        rendered = real_render(p)
        doc.edit(doc.markdown + "\nTyped in the browser.\n")
        return rendered

    result = sync.reconcile(path, "doc-id",
                            render=render_then_someone_types, push=doc.push)

    assert result.action == sync.SKIPPED
    assert doc.pushes == 0
    assert "Typed in the browser." in doc.markdown
