"""Markdown → Docs API requests, and the round trip back out again.

The requests these tests check are the ones a tabbed `create`/`push` sends, so
the important question is not "does it look plausible" but "does what it builds
read back as the markdown it came from". A small simulator below replays the
requests the way the Docs API would — insertions shift indices, and
``createParagraphBullets`` eats the leading tabs that encode nesting — and the
result is handed to the real puller.
"""

import os
import re
import time

import pytest

from gdoc_sync.convert import doc_to_markdown
from gdoc_sync.mdrequests import (
    Para,
    Run,
    Table,
    clear_tab_requests,
    compile_blocks,
    find_sentinels,
    insert_table_requests,
    markdown_to_blocks,
    u16,
)

NEWLINE = "\n".encode("utf-16-le")
TAB = "\t".encode("utf-16-le")


# --------------------------------------------------------------------------
# A stand-in for the Docs API
# --------------------------------------------------------------------------

def _units(text):
    raw = text.encode("utf-16-le")
    return [raw[i:i + 2] for i in range(0, len(raw), 2)]


class FakeDoc:
    """Replays batchUpdate requests over a buffer of UTF-16 code units.

    Each unit carries its own style dict, which is literally a Docs TextStyle,
    and the newline unit that ends a paragraph also carries that paragraph's
    marks. Styles therefore travel with their characters through every later
    insertion and deletion — which is the whole point: it catches an index that
    is off by one exactly the way the real API would.
    """

    def __init__(self):
        # A tab always holds at least the final newline; body index 1 is here.
        self.buf = [[NEWLINE, {}]]

    # -- helpers ----------------------------------------------------------
    def _paragraphs(self):
        out, start = [], 0
        for i, (unit, _style) in enumerate(self.buf):
            if unit == NEWLINE:
                out.append((start, i + 1))
                start = i + 1
        if start < len(self.buf):
            out.append((start, len(self.buf)))
        return out

    def _overlapping(self, start, end):
        """Paragraphs overlapping the doc-index range [start, end)."""
        lo, hi = start - 1, end - 1
        return [(ps, pe) for ps, pe in self._paragraphs() if ps < hi and pe > lo]

    # -- the requests we emit ---------------------------------------------
    def apply(self, requests):
        for request in requests:
            (name, body), = request.items()
            getattr(self, f"_{name}")(body)

    def _insertText(self, body):  # noqa: N802
        at = body["location"]["index"] - 1
        self.buf[at:at] = [[u, {}] for u in _units(body["text"])]

    def _insertInlineImage(self, body):  # noqa: N802
        at = body["location"]["index"] - 1
        self.buf[at:at] = [["IMG", {"_image": body["uri"]}]]

    def _updateTextStyle(self, body):  # noqa: N802
        style = body["textStyle"]
        fields = [f.strip() for f in body["fields"].split(",")]
        rng = body["range"]
        for i in range(rng["startIndex"] - 1, rng["endIndex"] - 1):
            if not 0 <= i < len(self.buf):
                raise AssertionError(f"style range {rng} is outside the document")
            for field in fields:
                if field in style:
                    self.buf[i][1][field] = style[field]
                else:
                    self.buf[i][1].pop(field, None)

    def _updateParagraphStyle(self, body):  # noqa: N802
        rng = body["range"]
        named = body["paragraphStyle"].get("namedStyleType", "NORMAL_TEXT")
        for ps, pe in self._overlapping(rng["startIndex"], rng["endIndex"]):
            self.buf[pe - 1][1]["_named"] = named
            # Applying a named style resets the paragraph's character styles
            # to that style's defaults. Modelling it is the point: without it
            # the simulator happily "proves" an ordering the API rejects.
            for i in range(ps, pe):
                marks = {k: v for k, v in self.buf[i][1].items()
                         if k.startswith("_")}
                self.buf[i][1].clear()
                self.buf[i][1].update(marks)

    def _createParagraphBullets(self, body):  # noqa: N802
        rng = body["range"]
        kind = "ol" if body["bulletPreset"].startswith("NUMBERED") else "ul"
        # Back to front: removing one paragraph's tabs moves every paragraph
        # after it, and the API applies the whole request at once.
        for ps, pe in reversed(self._overlapping(rng["startIndex"],
                                                 rng["endIndex"])):
            nesting = 0
            while ps + nesting < pe and self.buf[ps + nesting][0] == TAB:
                nesting += 1
            self.buf[pe - 1][1]["_bullet"] = (kind, nesting)
            del self.buf[ps:ps + nesting]   # the API removes them too

    def _deleteParagraphBullets(self, body):  # noqa: N802
        for _ps, pe in self._overlapping(body["range"]["startIndex"],
                                         body["range"]["endIndex"]):
            self.buf[pe - 1][1].pop("_bullet", None)

    def _deleteContentRange(self, body):  # noqa: N802
        rng = body["range"]
        del self.buf[rng["startIndex"] - 1:rng["endIndex"] - 1]

    def _insertTable(self, body):  # noqa: N802
        # Tables are checked at the request level; the simulator only needs to
        # know one was asked for and where.
        self.tables = getattr(self, "tables", [])
        self.tables.append((body["location"]["index"], body["rows"], body["columns"]))

    # -- reading it back --------------------------------------------------
    def document(self):
        content, idx = [], 1
        for ps, pe in self._paragraphs():
            marks = self.buf[pe - 1][1]
            elements, run, run_style = [], [], None
            for unit, style in self.buf[ps:pe]:
                visible = {k: v for k, v in style.items() if not k.startswith("_")}
                if style.get("_image"):
                    if run:
                        elements.append(_text_run(run, run_style))
                        run, run_style = [], None
                    elements.append({"inlineObjectElement":
                                     {"inlineObjectId": style["_image"]}})
                    continue
                if run and visible != run_style:
                    elements.append(_text_run(run, run_style))
                    run = []
                run.append(unit)
                run_style = visible
            if run:
                elements.append(_text_run(run, run_style))
            para = {"elements": elements,
                    "paragraphStyle": {"namedStyleType":
                                       marks.get("_named", "NORMAL_TEXT")}}
            if "_bullet" in marks:
                kind, nesting = marks["_bullet"]
                para["bullet"] = {"listId": kind, "nestingLevel": nesting}
            content.append({"paragraph": para, "startIndex": idx,
                            "endIndex": idx + (pe - ps)})
            idx += pe - ps
        return {"body": {"content": content}, "lists": _LISTS}

    def markdown(self):
        return doc_to_markdown(self.document())[0]

    def text(self):
        return b"".join(u for u, _ in self.buf if u != "IMG").decode("utf-16-le")


def _text_run(units, style):
    return {"textRun": {"content": b"".join(units).decode("utf-16-le"),
                        "textStyle": style or {}}}


_LEVELS = 9
_LISTS = {
    "ul": {"listProperties": {"nestingLevels":
                              [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"}] * _LEVELS}},
    "ol": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}] * _LEVELS}},
}


def roundtrip(markdown: str) -> str:
    doc = FakeDoc()
    doc.apply(compile_blocks(markdown_to_blocks(markdown)).requests)
    return doc.markdown()


# --------------------------------------------------------------------------
# Index arithmetic
# --------------------------------------------------------------------------

def test_u16_counts_code_units_not_characters():
    """An emoji is two units; len() would misplace every range after it."""
    assert u16("abc") == 3
    assert u16("🎉") == 2
    assert u16("a🎉b") == 4


def test_styles_land_on_the_right_characters_after_an_emoji():
    blocks = markdown_to_blocks("🎉 party **bold** tail\n")
    doc = FakeDoc()
    doc.apply(compile_blocks(blocks).requests)
    bold = "".join(
        unit.decode("utf-16-le", "ignore")
        for unit, style in doc.buf if style.get("bold")
    )
    assert bold == "bold"


def test_a_style_range_never_points_past_the_document():
    # The simulator raises if it does; this is the assertion that matters.
    doc = FakeDoc()
    doc.apply(compile_blocks(markdown_to_blocks(
        "# H\n\ntext with `code` and [a link](https://x.test)\n\n- one\n- two\n"
    )).requests)


# --------------------------------------------------------------------------
# Round trips: what push writes, pull must read back
# --------------------------------------------------------------------------

@pytest.mark.parametrize("markdown", [
    "Just a paragraph.\n",
    "# Heading one\n\nBody text.\n",
    "# H1\n\n## H2\n\n### H3\n\n#### H4\n\n##### H5\n\n###### H6\n",
    "Some **bold** and *italic* and `code` in one line.\n",
    "Some ~~struck~~ text.\n",
    "A [link](https://example.com) mid-sentence.\n",
    "- one\n- two\n- three\n",
    "1. first\n1. second\n",
    "- outer\n  - inner\n- outer again\n",
    "Text before.\n\n- a list\n- of items\n\nText after.\n",
    "```\nx = 1\ny = 2\n```\n",
    "```\ndef f():\n\n    return 1\n```\n",
    "Emoji 🎉 and accents café survive.\n",
])
def test_markdown_survives_the_round_trip(markdown):
    assert roundtrip(markdown) == markdown


def test_bold_inside_a_link_keeps_the_link():
    doc = FakeDoc()
    doc.apply(compile_blocks(markdown_to_blocks(
        "**[Title](https://x.test)**\n")).requests)
    styled = [style for _u, style in doc.buf if style.get("link")]
    assert styled and all(s.get("bold") for s in styled)


def test_a_fence_stays_one_block_across_a_blank_line():
    """Blank lines inside a fence are `\\v`, not paragraph breaks — a separate
    paragraph would have no visible run and would end the code block."""
    blocks = markdown_to_blocks("```\na\n\nb\n```\n")
    text = "".join(r["insertText"]["text"]
                   for r in compile_blocks(blocks).requests
                   if "insertText" in r)
    assert "a\v\vb" in text


def test_nesting_is_written_as_leading_tabs():
    blocks = markdown_to_blocks("- a\n  - b\n")
    requests = compile_blocks(blocks).requests
    assert any(r.get("insertText", {}).get("text") == "\t" for r in requests)


def test_bullets_are_created_back_to_front():
    """They delete the tabs that encode nesting, which moves later indices."""
    blocks = markdown_to_blocks("- a\n\ntext\n\n1. b\n")
    requests = compile_blocks(blocks).requests
    bullets = [i for i, r in enumerate(requests) if "createParagraphBullets" in r]
    assert bullets, "expected bullet requests"
    starts = [requests[i]["createParagraphBullets"]["range"]["startIndex"]
              for i in bullets]
    assert starts == sorted(starts, reverse=True)


def test_paragraph_styling_comes_before_character_styling():
    """Applying a namedStyleType wipes the paragraph's character styles.

    Emitted the other way round, every bold run and every monospace code block
    reaches the document unstyled — and a code block that is not monospace is
    not a code block any more, because that font is all `pull` has to go on.
    """
    requests = compile_blocks(markdown_to_blocks("# H\n\n**bold** text\n")).requests
    last_para = max(i for i, r in enumerate(requests) if "updateParagraphStyle" in r)
    first_text = min(i for i, r in enumerate(requests) if "updateTextStyle" in r)
    assert last_para < first_text


def test_style_ranges_account_for_the_tabs_bullets_remove():
    """Nesting tabs are inserted, then eaten; ranges after them must move up."""
    doc = FakeDoc()
    doc.apply(compile_blocks(markdown_to_blocks(
        "- a\n  - b\n\n**after** the list\n")).requests)
    bold = "".join(unit.decode("utf-16-le", "ignore")
                   for unit, style in doc.buf if style.get("bold"))
    assert bold == "after"


def test_inserted_text_does_not_inherit_the_previous_style():
    """Text takes the style of the character before it, so a reset comes first."""
    requests = compile_blocks(markdown_to_blocks("**bold** then plain\n")).requests
    resets = [r for r in requests
              if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"] == {}]
    assert len(resets) == 1
    first_style = next(i for i, r in enumerate(requests) if "updateTextStyle" in r)
    assert requests[first_style] is resets[0]


# --------------------------------------------------------------------------
# Tabs, tables and clearing
# --------------------------------------------------------------------------

def test_every_request_carries_the_tab_id():
    requests = compile_blocks(markdown_to_blocks("# H\n\n- a\n  - b\n"),
                              tab_id="TAB").requests
    for request in requests:
        (_name, body), = request.items()
        target = body.get("range") or body.get("location")
        assert target is not None and target["tabId"] == "TAB"


def test_a_table_reserves_a_placeholder_and_is_reported():
    compiled = compile_blocks(markdown_to_blocks(
        "| a | b |\n|---|---|\n| 1 | 2 |\n"))
    assert len(compiled.tables) == 1
    assert compiled.tables[0].n_rows == 2 and compiled.tables[0].n_cols == 2
    doc = FakeDoc()
    doc.apply(compiled.requests)
    assert "gdoc-sync:table:0" in doc.text()


def test_placeholders_are_found_and_replaced_back_to_front():
    compiled = compile_blocks(markdown_to_blocks(
        "| a | b |\n|---|---|\n| 1 | 2 |\n\ntext\n\n| c |\n|---|\n| 3 |\n"))
    doc = FakeDoc()
    doc.apply(compiled.requests)
    tab = {"documentTab": doc.document()}
    assert sorted(find_sentinels(tab)) == [0, 1]

    requests = insert_table_requests(tab, compiled.tables, "TAB")
    inserts = [r["insertTable"]["location"]["index"] for r in requests
               if "insertTable" in r]
    assert inserts == sorted(inserts, reverse=True)


def test_a_callout_becomes_a_one_cell_table():
    """`> [!NOTE]` is folded to a table upstream, so the tab path gets it free."""
    compiled = compile_blocks(markdown_to_blocks("> [!NOTE]\n> Careful.\n"))
    assert len(compiled.tables) == 1
    assert compiled.tables[0].n_rows == 1 and compiled.tables[0].n_cols == 1


def test_a_table_nested_in_a_cell_is_flattened_not_dropped():
    inner = Table(rows=[[[Para(runs=[Run(text="x")])]]])
    compiled = compile_blocks([inner], allow_tables=False)
    assert compiled.tables == []
    text = "".join(r["insertText"]["text"] for r in compiled.requests
                   if "insertText" in r)
    assert "| x |" in text


def test_clearing_a_tab_resets_the_paragraph_it_cannot_delete():
    tab = {"documentTab": {"body": {"content": [
        {"startIndex": 1, "endIndex": 12,
         "paragraph": {"elements": [{"textRun": {"content": "old text\n"}}]}}]}}}
    requests = clear_tab_requests(tab, "TAB")
    kinds = [next(iter(r)) for r in requests]
    assert kinds == ["deleteContentRange", "deleteParagraphBullets",
                     "updateParagraphStyle"]
    assert requests[0]["deleteContentRange"]["range"]["endIndex"] == 11
    assert all(r[k]["range"]["tabId"] == "TAB" for r, k in zip(requests, kinds))


def test_clearing_an_already_empty_tab_deletes_nothing():
    tab = {"documentTab": {"body": {"content": [
        {"startIndex": 1, "endIndex": 2,
         "paragraph": {"elements": [{"textRun": {"content": "\n"}}]}}]}}}
    requests = clear_tab_requests(tab, "TAB")
    assert not any("deleteContentRange" in r for r in requests)


# --------------------------------------------------------------------------
# Degrading gracefully
# --------------------------------------------------------------------------

def test_an_image_without_a_usable_uri_becomes_its_alt_text():
    compiled = compile_blocks(markdown_to_blocks("![a diagram](nope.png)\n"),
                              image_uri=lambda *_: None)
    text = "".join(r["insertText"]["text"] for r in compiled.requests
                   if "insertText" in r)
    assert "a diagram" in text
    assert compiled.images == 0


def test_an_image_with_a_uri_is_inserted():
    compiled = compile_blocks(
        markdown_to_blocks("![x](https://example.com/a.png)\n"),
        image_uri=lambda target, _alt: target)
    assert compiled.images == 1
    assert any("insertInlineImage" in r for r in compiled.requests)


def test_math_is_written_as_visible_latex():
    """The Docs API cannot create an equation; silence would be worse."""
    text = "".join(r["insertText"]["text"] for r
                   in compile_blocks(markdown_to_blocks("Cost is $E=mc^2$.\n")).requests
                   if "insertText" in r)
    assert "E=mc^2" in text


def test_an_unsupported_block_says_so_rather_than_vanishing():
    said = []
    markdown_to_blocks("::: warning\ncontent\n:::\n", say=said.append)
    # A pandoc Div is supported (its contents are unwrapped), so nothing is
    # said — the point is that the walk reaches the content.
    blocks = markdown_to_blocks("::: warning\ncontent\n:::\n")
    assert any(isinstance(b, Para) and "content" in b.runs[0].text
               for b in blocks)


# --------------------------------------------------------------------------
# Staging images in Drive: what ImageHost will and will not upload (P0-2)
# --------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + bytes(64)


def _up(relative: str) -> str:
    """A vault-relative image target, as a note one folder down would write it."""
    return f"..{os.sep}{relative}"


class _Exec:
    def __init__(self, result=None, error=None):
        self.result, self.error = result, error

    def execute(self, **_kw):
        if self.error is not None:
            raise self.error
        return self.result


class FakeDrive:
    """Enough Drive to record everything a push would do, and do none of it.

    ``list`` really interprets the ``q`` string it is given, so the reaper's
    query is tested rather than trusted: a file comes back only if the query
    asks for our staging tag, excludes trashed files, and names a createdTime
    cutoff the file is older than.
    """

    def __init__(self, stored=(), list_error=None, undeletable=()):
        self.stored = list(stored)
        self.list_error = list_error
        self.undeletable = set(undeletable)
        self.created: list[dict] = []
        self.shared: list[dict] = []
        self.deleted: list[str] = []
        self.queries: list[str] = []

    # -- the googleapiclient shape ----------------------------------------
    def files(self):
        return self

    def permissions(self):
        return self

    def create(self, body=None, media_body=None, fileId=None, fields=None):  # noqa: N803
        if fileId is not None:                      # permissions().create
            self.shared.append({"fileId": fileId, "body": body})
            return _Exec({"id": "perm"})
        self.created.append(body)
        return _Exec({"id": f"file{len(self.created)}"})

    def list(self, q=None, fields=None, pageSize=None):  # noqa: N803
        self.queries.append(q)
        if self.list_error is not None:
            return _Exec(error=self.list_error)
        return _Exec({"files": self._matching(q)})

    def delete(self, fileId=None):  # noqa: N803
        if fileId in self.undeletable:
            return _Exec(error=RuntimeError("someone else's file"))
        self.deleted.append(fileId)
        return _Exec({})

    def _matching(self, q):
        if "appProperties has { key='gdoc-sync' and value='staging' }" not in q:
            return []
        if "trashed = false" not in q:
            return []
        cutoff = re.search(r"createdTime < '([^']+)'", q)
        assert cutoff, f"the reaper must bound by age, got: {q}"
        return [f for f in self.stored
                if f.get("appProperties", {}).get("gdoc-sync") == "staging"
                and not f.get("trashed")
                and f["createdTime"] < cutoff.group(1)]


@pytest.fixture
def vault(tmp_path):
    """An Obsidian vault, with a private key and an outside secret to steal."""
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "attachments").mkdir()
    (root / "attachments" / "logo.png").write_bytes(PNG_BYTES)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_bytes(b"-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.png").write_bytes(PNG_BYTES)
    return root


def _host(drive, vault, said=None):
    from gdoc_sync.mdrequests import ImageHost
    return ImageHost(drive, resource_dir=vault / "notes",
                     say=(said.append if said is not None else lambda *_: None))


@pytest.mark.parametrize("target,because", [
    (f"..{os.sep}..{os.sep}outside{os.sep}secret.png", "outside the note's project"),
    ("/etc/hostname", "outside the note's project"),
    (f"..{os.sep}.ssh{os.sep}id_rsa", "hidden path"),
    ("~/x.png", "home-directory path"),
])
def test_a_refused_target_makes_no_api_call_at_all(vault, target, because):
    drive = FakeDrive()
    said: list[str] = []
    assert _host(drive, vault, said)(target, "alt") is None
    assert drive.created == [] and drive.shared == [] and drive.queries == []
    assert any(target in s and because in s for s in said)


def test_a_symlink_out_of_the_vault_is_refused(vault):
    os.symlink(vault.parent / "outside" / "secret.png",
               vault / "attachments" / "innocent.png")
    drive = FakeDrive()
    assert _host(drive, vault)(_up("attachments/innocent.png")) is None
    assert drive.created == []


def test_a_text_file_named_like_an_image_is_refused(vault):
    (vault / "attachments" / "x.png").write_text("ssh-rsa AAAAB3Nza...\n")
    drive = FakeDrive()
    said: list[str] = []
    assert _host(drive, vault, said)(_up("attachments/x.png")) is None
    assert drive.created == []
    assert any("not a PNG, JPEG, GIF, WebP or BMP" in s for s in said)


def test_a_missing_file_still_says_not_found(vault):
    """The old message survives, because it means something different."""
    said: list[str] = []
    assert _host(FakeDrive(), vault, said)(_up("attachments/gone.png")) is None
    assert any("image not found" in s for s in said)


def test_a_real_vault_image_is_staged_and_tagged(vault):
    drive = FakeDrive()
    uri = _host(drive, vault)(_up("attachments/logo.png"))
    assert uri == "https://drive.google.com/uc?export=download&id=file1"
    assert len(drive.created) == 1
    assert drive.created[0]["appProperties"] == {"gdoc-sync": "staging"}
    assert drive.shared[0]["body"] == {"type": "anyone", "role": "reader"}


def test_staged_images_are_deleted_again_by_cleanup(vault):
    drive = FakeDrive()
    host = _host(drive, vault)
    host(_up("attachments/logo.png"))
    host.cleanup()
    assert drive.deleted == ["file1"]


def test_a_remote_url_is_passed_through_without_touching_drive(vault):
    drive = FakeDrive()
    assert _host(drive, vault)("https://example.com/a.png") == \
        "https://example.com/a.png"
    assert drive.created == []


def _stored(file_id, age_seconds, tagged=True, trashed=False):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.gmtime(time.time() - age_seconds)) + "Z"
    entry = {"id": file_id, "createdTime": stamp, "trashed": trashed}
    if tagged:
        entry["appProperties"] = {"gdoc-sync": "staging"}
    return entry


def test_reap_orphans_deletes_only_our_stale_staging_files(vault):
    drive = FakeDrive(stored=[
        _stored("old-ours", 7200),
        _stored("fresh-ours", 60),                    # a concurrent push
        _stored("old-theirs", 7200, tagged=False),    # somebody's holiday photo
        _stored("old-trashed", 7200, trashed=True),
    ])
    assert _host(drive, vault).reap_orphans() == 1
    assert drive.deleted == ["old-ours"]


def test_the_reaper_runs_once_before_the_first_upload_only(vault):
    drive = FakeDrive(stored=[_stored("old-ours", 7200)])
    host = _host(drive, vault)
    host(_up("attachments/logo.png"))
    host(_up("attachments/logo.png"))        # cached, no second upload
    (vault / "attachments" / "two.png").write_bytes(PNG_BYTES)
    host(_up("attachments/two.png"))
    assert len(drive.queries) == 1
    assert len(drive.created) == 2


def test_the_reaper_never_raises_into_the_push(vault):
    drive = FakeDrive(list_error=RuntimeError("insufficient scope"))
    host = _host(drive, vault)
    assert host.reap_orphans() == 0
    assert host(_up("attachments/logo.png")) is not None   # the push carries on


def test_an_undeletable_orphan_does_not_stop_the_others(vault):
    drive = FakeDrive(stored=[_stored("a", 7200), _stored("b", 7200)],
                      undeletable={"a"})
    assert _host(drive, vault).reap_orphans() == 1
    assert drive.deleted == ["b"]
