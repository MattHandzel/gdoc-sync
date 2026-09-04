"""`link` then `push` must not replace a document nobody has looked at.

`link` is offline by design — it records a mapping and nothing else — so the
file it links has no stored revision. `push`'s drift guard was written as
"stored revision present *and* different", which made a missing revision a
silent bypass of the one check standing between a linked file and someone
else's document. These are the tests for closing that, plus the validator
that stops `link` recording a doc id that was never a doc id.
"""

from __future__ import annotations

import io
import sys

import pytest

from gdoc_sync import config, create
from gdoc_sync import push as push_mod
from gdoc_sync.config import validate_doc_id

DOC_ID = "1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q8R9s0T"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("given", [
    DOC_ID,
    f"https://docs.google.com/document/d/{DOC_ID}/edit",
    f"https://docs.google.com/document/d/{DOC_ID}/edit#heading=h.abc",
    f"  https://docs.google.com/document/d/{DOC_ID}/edit?usp=sharing  ",
])
def test_a_url_or_a_bare_id_is_accepted(given):
    assert validate_doc_id(given) == DOC_ID


@pytest.mark.parametrize("given", [
    "",
    "   ",
    "my notes",
    "notes.md",
    "short",
    "https://example.com/document/d/whatever",
    "https://docs.google.com/spreadsheets/d/" + DOC_ID,
])
def test_anything_that_is_not_a_doc_is_refused(given):
    with pytest.raises(ValueError) as exc:
        validate_doc_id(given)
    message = str(exc.value)
    assert "docs.google.com/document/d/" in message, "the message must show the forms"
    assert "20" in message


# ---------------------------------------------------------------------------
# The push stop
# ---------------------------------------------------------------------------

class FakeDrive:
    """Just enough Drive to notice an upload. `update` must never be reached."""

    def __init__(self):
        self.updates = 0

    def files(self):
        return self

    def update(self, **_kw):
        self.updates += 1
        return self

    def execute(self, **_kw):
        return {}


class FakeDocs:
    """documents().get(...).execute() — the only Docs call a stopped push makes."""

    def __init__(self, doc):
        self.doc = doc
        self.gets = 0

    def documents(self):
        return self

    def get(self, **_kw):
        self.gets += 1
        return self

    def execute(self, **_kw):
        return self.doc


DOC = {
    "title": "Someone else's plan",
    "revisionId": "rev-remote-1",
    "body": {"content": [{"paragraph": {"elements": [
        {"textRun": {"content": "Content the user has never seen.\n"}}]}}]},
}


@pytest.fixture
def linked_file(tmp_path, monkeypatch):
    """A file linked the way `link` links one: a mapping, no revision."""
    path = tmp_path / "note.md"
    path.write_text("# Mine\n\nMy own notes.\n")
    config.set_doc_id(str(path), DOC_ID)  # no revision, exactly as `link` leaves it
    assert not config.get_revision(str(path))

    drive, docs = FakeDrive(), FakeDocs(DOC)
    monkeypatch.setattr(push_mod, "get_services", lambda: (drive, docs))
    monkeypatch.setattr(push_mod, "apply_comment_actions", lambda *a, **k: [])
    monkeypatch.setattr(push_mod, "_push_docx",
                        lambda *a, **k: pushed.append("docx"))
    monkeypatch.setattr(push_mod, "_guard_flatten", lambda *a, **k: None)
    monkeypatch.setattr("gdoc_sync.sync.record_sync_baseline",
                        lambda *a, **k: True)
    pushed: list[str] = []
    return path, drive, pushed


def test_push_stops_when_the_file_has_never_been_synced(capsys, monkeypatch,
                                                        linked_file):
    path, drive, pushed = linked_file
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # non-interactive

    with pytest.raises(SystemExit) as exc:
        push_mod.push(path)

    assert exc.value.code == 2
    assert drive.updates == 0, "the document must not be replaced"
    assert pushed == []
    out = capsys.readouterr()
    assert "never been pulled from or pushed to this doc" in out.out
    assert "gdoc-sync pull" in out.out
    assert "gdoc-sync diff" in out.out
    assert "--yes" in out.out


def test_yes_replaces_the_document(monkeypatch, linked_file):
    path, drive, pushed = linked_file
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    push_mod.push(path, yes=True)

    assert pushed == ["docx"], "with --yes the push goes ahead"


def test_declining_the_prompt_aborts(monkeypatch, linked_file):
    path, drive, pushed = linked_file
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _q: "n")

    with pytest.raises(SystemExit) as exc:
        push_mod.push(path)

    assert exc.value.code == 1
    assert pushed == []


def test_answering_yes_at_the_prompt_pushes(monkeypatch, linked_file):
    path, drive, pushed = linked_file
    asked = []
    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or "y")

    push_mod.push(path)

    assert pushed == ["docx"]
    assert asked == ["Replace the document's content? [y/N] "]


def test_a_stored_revision_means_no_stop(monkeypatch, linked_file):
    """The stop is for never-synced files only; a pulled file pushes as before."""
    path, drive, pushed = linked_file
    config.set_revision(str(path), DOC["revisionId"])
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    push_mod.push(path)

    assert pushed == ["docx"]


def test_the_sync_engine_is_not_stopped(monkeypatch, linked_file):
    """`merged=True` says the engine already reconciled; it must not be asked."""
    path, drive, pushed = linked_file
    monkeypatch.setattr(sys, "stdin", io.StringIO())

    push_mod.push(path, merged=True)

    assert pushed == ["docx"]


# ---------------------------------------------------------------------------
# The lost-update fingerprint
# ---------------------------------------------------------------------------

def test_push_refuses_when_the_doc_moved_under_the_render(monkeypatch,
                                                          linked_file):
    path, drive, pushed = linked_file
    config.set_revision(str(path), DOC["revisionId"])

    with pytest.raises(push_mod.RemoteChanged):
        push_mod.push(path, merged=True, expected_fingerprint="a stale digest")

    assert drive.updates == 0
    assert pushed == [], "nothing may be uploaded once the doc has moved"


def test_push_proceeds_when_the_fingerprint_still_matches(monkeypatch,
                                                          linked_file):
    from gdoc_sync.convert import doc_text_fingerprint

    path, drive, pushed = linked_file
    push_mod.push(path, merged=True,
                  expected_fingerprint=doc_text_fingerprint(DOC))

    assert pushed == ["docx"]


# ---------------------------------------------------------------------------
# `create` records a revision (else every first push would hit the stop)
# ---------------------------------------------------------------------------

def test_create_records_the_new_doc_revision(tmp_path, monkeypatch):
    path = tmp_path / "new.md"
    path.write_text("# New\n\nBody.\n")

    docs = FakeDocs({"revisionId": "rev-just-created"})
    monkeypatch.setattr(create, "get_services", lambda: (FakeDrive(), docs))
    monkeypatch.setattr(create, "_create_from_docx",
                        lambda *a, **k: (DOC_ID, "https://docs.google.com/d/x"))
    monkeypatch.setattr(create, "record_sync_baseline", lambda *a, **k: True)

    create.create_doc(path, share_mode="private", copy=False)

    assert config.get_doc_id(str(path)) == DOC_ID
    assert config.get_revision(str(path)) == "rev-just-created"
