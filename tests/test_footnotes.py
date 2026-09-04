"""Footnotes survive a pull.

The docx push path writes real footnotes, so a puller that skipped them lost
the text *and* deleted it from the document on the next push (push replaces the
whole body). These tests pin the exact markdown, the renumbering, the orphan
rule and the count-preservation warning.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from gdoc_sync import pull as pull_mod
from gdoc_sync.convert import doc_to_markdown

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _run(text, **style):
    return {"textRun": {"content": text, "textStyle": style}}


def _ref(footnote_id, number="1"):
    return {"footnoteReference": {"footnoteId": footnote_id,
                                  "footnoteNumber": number, "textStyle": {}}}


def _para(elements):
    return {"paragraph": {"elements": elements}, "startIndex": 0, "endIndex": 1}


# ---------------------------------------------------------------------------
# body → markdown
# ---------------------------------------------------------------------------

EXPECTED = """\
Intro text[^1] and more[^2].

[^1]: A [linked](https://example.com) note.

[^2]: The **bold** part.

    Second paragraph of the note.

[^3]: Nobody points at this one.
"""


def test_footnotes_render_inline_and_as_definitions():
    md, _ = doc_to_markdown(_fixture("footnotes_doc.json"))
    assert md == EXPECTED


def test_numbering_follows_reference_order_not_google_ids():
    """`kix.first` is first in the footnotes map but referenced second."""
    md, _ = doc_to_markdown(_fixture("footnotes_doc.json"))
    assert "Intro text[^1] and more[^2]." in md
    # [^1] is kix.second's text, [^2] is kix.first's — ids ignored entirely.
    assert "[^1]: A [linked](https://example.com) note." in md
    assert "[^2]: The **bold** part." in md


def test_multi_paragraph_footnote_indents_its_continuation():
    md, _ = doc_to_markdown(_fixture("footnotes_doc.json"))
    assert "\n\n    Second paragraph of the note." in md


def test_orphan_footnote_is_still_emitted():
    """Nothing references kix.orphan, but its prose is real content."""
    md, _ = doc_to_markdown(_fixture("footnotes_doc.json"))
    assert "[^3]: Nobody points at this one." in md


def test_repeated_reference_reuses_its_number():
    doc = {
        "body": {"content": [_para([
            _run("one"), _ref("kix.x"), _run(" two"), _ref("kix.x"),
            _run(".\n")])]},
        "footnotes": {"kix.x": {"content": [_para([_run("Shared.\n")])]}},
    }
    md, _ = doc_to_markdown(doc)
    assert md == "one[^1] two[^1].\n\n[^1]: Shared.\n"


def test_footnote_inside_a_footnote_degrades_to_plain_text():
    """A definition cannot nest inside a definition, so the inner reference
    becomes Docs' own number — and the inner note still gets a definition."""
    doc = {
        "body": {"content": [_para([_run("body"), _ref("kix.outer"), _run("\n")])]},
        "footnotes": {
            "kix.outer": {"content": [_para([
                _run("outer note"), _ref("kix.inner", number="2"), _run("\n")])]},
            "kix.inner": {"content": [_para([_run("inner note\n")])]},
        },
    }
    md, _ = doc_to_markdown(doc)
    assert md == (
        "body[^1]\n\n"
        "[^1]: outer note[2]\n\n"
        "[^2]: inner note\n"
    )


def test_footnote_reference_in_a_table_cell_survives():
    doc = {
        "body": {"content": [{"table": {"tableRows": [
            {"tableCells": [{"content": [_para([_run("cell"), _ref("kix.t")])]}]},
        ]}}]},
        "footnotes": {"kix.t": {"content": [_para([_run("Cell note.\n")])]}},
    }
    md, _ = doc_to_markdown(doc)
    assert "| cell[^1] |" in md
    assert "[^1]: Cell note." in md


def test_document_without_footnotes_is_unchanged():
    doc = {"body": {"content": [_para([_run("Plain.\n")])]}}
    md, _ = doc_to_markdown(doc)
    assert md == "Plain.\n"


def test_start_number_offsets_the_labels():
    doc = {
        "body": {"content": [_para([_run("x"), _ref("kix.q"), _run("\n")])]},
        "footnotes": {"kix.q": {"content": [_para([_run("Note.\n")])]}},
    }
    md, _ = doc_to_markdown(doc, start_number=7)
    assert md == "x[^7]\n\n[^7]: Note.\n"


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------

def test_tabs_number_footnotes_continuously():
    """Each tab has its own footnotes map; restarting at 1 per tab would put
    two different `[^1]:` definitions in one file."""
    doc = _fixture("footnotes_tabs.json")
    markdown, *_ = pull_mod._render_body(
        doc, asset_path=None, local_text=None, say=lambda *_: None)
    assert "Alpha[^1]." in markdown
    assert "[^1]: First tab note." in markdown
    assert "Beta[^2]." in markdown
    assert "[^2]: Second tab note." in markdown
    assert markdown.count("[^1]:") == 1


def test_single_tab_doc_renders_footnotes():
    doc = _fixture("footnotes_tabs.json")
    doc["tabs"] = doc["tabs"][:1]
    markdown, *_ = pull_mod._render_body(
        doc, asset_path=None, local_text=None, say=lambda *_: None)
    assert markdown == "Alpha[^1].\n\n[^1]: First tab note.\n"


# ---------------------------------------------------------------------------
# count-preservation warning
# ---------------------------------------------------------------------------

def test_no_warning_when_every_footnote_survives():
    said: list[str] = []
    pull_mod._render_body(_fixture("footnotes_doc.json"), asset_path=None,
                          local_text=None, say=lambda *a: said.append(" ".join(a)))
    assert not [line for line in said if "WARNING" in line]


def test_warning_names_both_counts_when_a_footnote_is_lost(monkeypatch):
    """The safety net: if anything downstream ever eats a definition, the pull
    says so rather than letting the next push delete the footnote."""
    monkeypatch.setattr(pull_mod, "doc_to_markdown",
                        lambda *a, **k: ("Intro text.\n", None))
    said: list[str] = []
    pull_mod._render_body(_fixture("footnotes_doc.json"), asset_path=None,
                          local_text=None, say=lambda *a: said.append(" ".join(a)))
    warnings = [line for line in said if "WARNING" in line]
    assert len(warnings) == 1
    assert "3 footnote(s)" in warnings[0]
    assert "0 footnote definition(s)" in warnings[0]


# ---------------------------------------------------------------------------
# pandoc round trip (the push path)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_emitted_markdown_becomes_real_docx_footnotes(tmp_path):
    """`push` goes markdown → pandoc → docx → Google Docs footnotes, so what
    the puller writes has to read back as footnotes on the way out."""
    md, _ = doc_to_markdown(_fixture("footnotes_doc.json"))
    docx = tmp_path / "out.docx"
    subprocess.run(
        ["pandoc", "-f", "gfm+yaml_metadata_block", "-t", "docx", "-o", str(docx)],
        input=md, text=True, check=True, capture_output=True,
    )
    with zipfile.ZipFile(docx) as z:
        assert "word/footnotes.xml" in z.namelist()
        xml = z.read("word/footnotes.xml").decode("utf-8")
    assert "linked" in xml
    assert "bold" in xml
    assert "Second paragraph of the note." in xml
    # The orphan is deliberately NOT asserted here: pandoc drops a definition
    # nothing references, because there is no reference to hang it off. Its
    # text stays in the markdown file (which is the point — the pull no longer
    # loses it); it just cannot be re-created in the doc it was orphaned in.
    assert "Nobody points at this one." not in xml
