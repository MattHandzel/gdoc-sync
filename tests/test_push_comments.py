"""`push` must consume the comment-action markers it fired.

Three identical pushes of an unchanged file used to post three identical
replies, because push read the markers, sent them, and then stripped them only
from the *uploaded* copy. These tests drive the real ``push`` with a fake
Docs/Drive pair and the upload itself stubbed out — the pandoc→docx pipeline is
covered elsewhere and is not what is under test here.
"""

from __future__ import annotations

import pytest

from gdoc_sync import config, syncstate
from gdoc_sync import push as push_mod
from tests.test_comments import FakeDrive, authored_file


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)


class FakeDocs:
    """documents().get(...) only — the push itself is stubbed."""

    def __init__(self, title="Note", revision="rev-1"):
        self.doc = {"title": title, "revisionId": revision}

    def documents(self):
        return self

    def get(self, **_kw):
        return self

    def execute(self, **_kw):
        return dict(self.doc)


def _remote(author, content, quoted="quoted words"):
    return {
        "author": {"displayName": author},
        "content": content,
        "quotedFileContent": {"value": quoted},
        "replies": [],
    }


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    """A linked file plus the fakes ``push`` will talk to."""
    path = tmp_path / "note.md"
    path.write_text(authored_file())
    config.set_doc_id(path, "doc-1", revision_id="rev-1")

    drive = FakeDrive([_remote("Alice", "tighten this")])
    docs = FakeDocs()
    monkeypatch.setattr(push_mod, "get_services", lambda: (drive, docs))

    uploaded: list[str] = []
    monkeypatch.setattr(push_mod, "_push_docx",
                        lambda *a, **kw: uploaded.append(a[3]))

    # Recording a real baseline re-renders the doc, which needs OAuth.
    from gdoc_sync import sync

    baselines: list[str] = []
    monkeypatch.setattr(sync, "record_sync_baseline",
                        lambda p, d, text=None: baselines.append(text) or True)

    class H:
        pass

    H.path, H.drive, H.uploaded, H.capsys = path, drive, uploaded, capsys
    H.baselines = baselines
    return H


def test_push_consumes_applied_markers_and_is_idempotent(harness):
    returned = push_mod.push(harness.path)

    on_disk = harness.path.read_text()
    assert "{>>reply:" not in on_disk
    assert "{>>comment:" not in on_disk
    # The pulled comment is not an action and must survive untouched.
    assert "{>>Alice: tighten this<<}" in on_disk
    assert returned == on_disk

    # One reply and one new comment, once.
    assert [c[0] for c in harness.drive.writes] == [
        "replies.create", "comments.create"]

    # Two more pushes of the now-unchanged file write nothing further.
    push_mod.push(harness.path)
    push_mod.push(harness.path)
    assert [c[0] for c in harness.drive.writes] == [
        "replies.create", "comments.create"]
    assert harness.path.read_text() == on_disk


def test_push_backs_the_file_up_before_rewriting_it(harness):
    before = harness.path.read_text()
    push_mod.push(harness.path)
    backups = syncstate.list_backups(harness.path)
    assert backups, "expected a pre-push backup"
    assert any(b.read_text() == before for b in backups)


def test_push_uploads_the_consumed_text(harness):
    push_mod.push(harness.path)
    (body,) = harness.uploaded
    assert "reply: thanks" not in body
    assert "comment: needs source" not in body


def test_push_records_the_consumed_text_as_the_baseline(harness):
    """The ancestor must be the file as it now stands, not as it was read.

    Recording the pre-consumption text would make the marker removal look like
    an unsynced local edit on the very next sync pass.
    """
    returned = push_mod.push(harness.path)
    assert harness.baselines == [returned]
    assert "{>>reply:" not in returned


def test_push_leaves_markers_alone_when_the_file_changed_under_it(harness,
                                                                 monkeypatch):
    """A concurrent save must not be clobbered by the marker rewrite."""
    original = harness.path.read_text()
    real_apply = push_mod.apply_comment_actions

    def apply_then_edit(*args, **kwargs):
        result = real_apply(*args, **kwargs)
        harness.path.write_text(original + "\nan edit made mid-push\n")
        return result

    monkeypatch.setattr(push_mod, "apply_comment_actions", apply_then_edit)

    push_mod.push(harness.path)

    on_disk = harness.path.read_text()
    assert "an edit made mid-push" in on_disk
    assert "{>>reply: thanks<<}" in on_disk
    assert "changed while this push was running" in harness.capsys.readouterr().out


def test_push_warns_that_anchored_comments_lose_their_anchor(harness):
    push_mod.push(harness.path)
    out = harness.capsys.readouterr().out
    assert "1 anchored comment(s) will lose their anchor" in out


def test_push_lists_comments_at_most_once(harness):
    push_mod.push(harness.path)
    assert [c[0] for c in harness.drive.log].count("comments.list") == 1


def test_push_without_markers_still_warns_and_lists_once(tmp_path, monkeypatch,
                                                        capsys):
    path = tmp_path / "plain.md"
    path.write_text("# Title\n\nquoted words are here.\n")
    config.set_doc_id(path, "doc-1", revision_id="rev-1")

    from gdoc_sync import sync

    drive = FakeDrive([_remote("Alice", "tighten this")])
    monkeypatch.setattr(push_mod, "get_services", lambda: (drive, FakeDocs()))
    monkeypatch.setattr(push_mod, "_push_docx", lambda *a, **kw: None)
    monkeypatch.setattr(sync, "record_sync_baseline", lambda *a, **kw: True)

    returned = push_mod.push(path)

    assert returned == path.read_text()
    assert drive.writes == []
    assert [c[0] for c in drive.log].count("comments.list") == 1
    assert "1 anchored comment(s) will lose their anchor" in capsys.readouterr().out
