"""Image identity round-trip: the resolver behind two-way image sync."""

import json

import pytest

from gdoc_sync.images import ImageResolver, _index_path

PNG_A = b"\x89PNG-A-bytes"
PNG_B = b"\x89PNG-B-bytes"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)


class FakeResponse:
    def __init__(self, content, ctype="image/png"):
        self.status_code = 200
        self.content = content
        self.headers = {"content-type": ctype}


class FakeSession:
    """Maps contentUri -> bytes, counting fetches."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get(self, uri, timeout=None):
        self.calls.append(uri)
        return FakeResponse(self.mapping[uri])


def note_with_image(tmp_path, name="note.md"):
    (tmp_path / "attachments").mkdir(exist_ok=True)
    (tmp_path / "attachments" / "diagram.png").write_bytes(PNG_A)
    md = tmp_path / name
    md.write_text("# T\n\n![my diagram](attachments/diagram.png)\n")
    return md


def test_doc_object_matching_local_bytes_renders_as_the_local_token(tmp_path):
    md = note_with_image(tmp_path)
    session = FakeSession({"uri://1": PNG_A})
    r = ImageResolver(md, session=session)

    token = r("kix.obj1", "uri://1")

    assert token == "![my diagram](attachments/diagram.png)"
    # matched by content — nothing new written anywhere
    assert not (tmp_path / "note-assets").exists()


def test_indexed_object_needs_no_fetch_on_later_renders(tmp_path):
    md = note_with_image(tmp_path)
    session = FakeSession({"uri://1": PNG_A})
    r = ImageResolver(md, session=session)
    r("kix.obj1", "uri://1")
    r.finish()

    session2 = FakeSession({})  # any fetch would KeyError
    r2 = ImageResolver(md, session=session2)
    assert r2("kix.obj1", "uri://ignored") == "![my diagram](attachments/diagram.png)"
    assert session2.calls == []


def test_new_remote_image_is_downloaded_beside_the_file(tmp_path):
    md = note_with_image(tmp_path)
    session = FakeSession({"uri://new": PNG_B})
    r = ImageResolver(md, session=session)

    token = r("kix.new", "uri://new")

    assert token.startswith("![image](note-assets/img-")
    saved = list((tmp_path / "note-assets").iterdir())
    assert len(saved) == 1 and saved[0].read_bytes() == PNG_B
    assert r.count() == 1


def test_global_image_dir_gets_relative_links(tmp_path):
    md = note_with_image(tmp_path / "docs" if (tmp_path / "docs").mkdir() or True else tmp_path)
    global_dir = tmp_path / "media"
    session = FakeSession({"uri://new": PNG_B})
    r = ImageResolver(md, image_dir=global_dir, session=session)

    token = r("kix.new", "uri://new")

    assert token.startswith("![image](../media/img-")
    assert list(global_dir.iterdir())


def test_same_doc_state_renders_the_same_text_twice(tmp_path):
    md = note_with_image(tmp_path)
    session = FakeSession({"uri://new": PNG_B})
    first = ImageResolver(md, session=session)("kix.new", "uri://new")
    # no finish(): a fresh resolver re-derives everything from bytes alone
    second = ImageResolver(md, session=FakeSession({"uri://new": PNG_B}))("kix.new2", "uri://new")
    assert first == second


def test_push_recreating_objects_reuses_tokens_without_new_files(tmp_path):
    """After a push every object id is new but the bytes are the file's own."""
    md = note_with_image(tmp_path)
    r = ImageResolver(md, session=FakeSession({"uri://1": PNG_A}))
    r("kix.before-push", "uri://1")
    r.finish()

    r2 = ImageResolver(md, session=FakeSession({"uri://2": PNG_A}))
    token = r2("kix.after-push", "uri://2")
    assert token == "![my diagram](attachments/diagram.png)"
    assert not (tmp_path / "note-assets").exists()


def test_finish_prunes_objects_the_doc_no_longer_contains(tmp_path):
    md = note_with_image(tmp_path)
    r = ImageResolver(md, session=FakeSession({"uri://1": PNG_A, "uri://2": PNG_B}))
    r("kix.keep", "uri://1")
    r("kix.gone", "uri://2")
    r.finish()

    r2 = ImageResolver(md, session=FakeSession({"uri://1": PNG_A}))
    r2("kix.keep", "uri://1")
    r2.finish()

    index = json.loads(_index_path(md).read_text())
    assert set(index) == {"kix.keep"}


def test_unfetchable_image_returns_none(tmp_path):
    md = note_with_image(tmp_path)

    class FailingSession:
        def get(self, uri, timeout=None):
            raise OSError("boom")

    assert ImageResolver(md, session=FailingSession())("kix.x", "uri://x") is None
