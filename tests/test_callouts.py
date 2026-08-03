"""Callouts (`> [!NOTE]`) survive the Google Docs round trip.

The bug these guard: pandoc's docx writer flattens an alert Div completely, so
a callout used to reach the shared doc as a bare paragraph reading "Note" —
and every Obsidian-only type reached it as the literal text `[!TLDR]`.
"""

import json
import shutil
import subprocess

import pytest

from gdoc_sync.callouts import (
    ALIASES,
    CALLOUT_TYPES,
    find_callouts,
    restore_callout_spellings,
    rewrite_for_pandoc,
    split_title,
    transform_ast,
)
from gdoc_sync.convert import doc_to_markdown
from gdoc_sync.style import callout_colors, callout_requests

# --------------------------------------------------------------------------
# The registry itself
# --------------------------------------------------------------------------

def test_every_github_alert_is_a_type():
    for name in ("note", "tip", "important", "warning", "caution"):
        assert name in CALLOUT_TYPES


def test_every_obsidian_callout_is_accepted():
    """The full Obsidian set, including the aliases it treats as synonyms."""
    for name in (
        "note", "abstract", "summary", "tldr", "info", "todo", "tip", "hint",
        "important", "success", "check", "done", "question", "help", "faq",
        "warning", "caution", "attention", "failure", "fail", "missing",
        "danger", "error", "bug", "example", "quote", "cite",
    ):
        assert name in ALIASES, name


def test_icons_are_unique():
    """The icon is how a pulled doc is read back — two types sharing one
    would silently relabel somebody's callout."""
    icons = [t.icon for t in CALLOUT_TYPES.values()]
    assert len(set(icons)) == len(icons)


@pytest.mark.parametrize("kind", sorted(CALLOUT_TYPES))
def test_every_title_reads_back_to_its_own_type(kind):
    spec = CALLOUT_TYPES[kind]
    assert split_title(f"{spec.icon} {spec.title}") == (spec, "")


def test_a_renamed_callout_keeps_its_custom_title():
    spec = CALLOUT_TYPES["warning"]
    assert split_title(f"{spec.icon} Do not deploy on a Friday") == (
        spec, "Do not deploy on a Friday")


def test_ordinary_text_is_not_a_title():
    assert split_title("Note: this is just prose") is None
    assert split_title("") is None


# --------------------------------------------------------------------------
# Finding them in markdown
# --------------------------------------------------------------------------

def test_finds_types_aliases_folds_and_custom_titles():
    md = (
        "> [!NOTE]\n> plain\n\n"
        "> [!info]\n> an alias\n\n"
        "> [!TLDR]+ Summary of it\n> folded open\n\n"
        "> [!WARNING]-\n> folded shut\n"
    )
    found = find_callouts(md)
    assert [c.name for c in found] == ["NOTE", "info", "TLDR", "WARNING"]
    assert [c.type.kind for c in found] == ["note", "note", "abstract", "warning"]
    assert [c.fold for c in found] == ["", "", "+", "-"]
    assert [c.title for c in found] == ["", "", "Summary of it", ""]


def test_an_unknown_type_is_left_as_an_ordinary_blockquote():
    assert find_callouts("> [!SOMETHINGELSE]\n> body\n") == []


def test_a_callout_inside_a_code_fence_is_not_one():
    assert find_callouts("```\n> [!NOTE]\n> body\n```\n") == []


def test_a_plain_blockquote_is_left_alone():
    md = "> Just a quotation.\n"
    assert find_callouts(md) == []
    assert rewrite_for_pandoc(md) == (md, 0)


def test_rewrite_gives_the_title_its_own_paragraph():
    out, n = rewrite_for_pandoc("> [!NOTE]\n> body\n")
    assert n == 1
    assert out == "> **ℹ️ Note**\n>\n> body\n"


def test_rewrite_keeps_a_custom_title():
    out, _ = rewrite_for_pandoc("> [!TIP] Try this first\n> body\n")
    assert out.startswith("> **\U0001f4a1 Try this first**\n>\n")


# --------------------------------------------------------------------------
# Push: the AST becomes a one-cell table
# --------------------------------------------------------------------------

def _table_count(node) -> int:
    if isinstance(node, list):
        return sum(_table_count(x) for x in node)
    if not isinstance(node, dict):
        return 0
    return (1 if node.get("t") == "Table" else 0) + sum(
        _table_count(v) for v in node.values())


def _to_ast(markdown: str) -> dict:
    rewritten, _ = rewrite_for_pandoc(markdown)
    proc = subprocess.run(["pandoc", "-f", "gfm", "-t", "json"],
                          input=rewritten, capture_output=True, text=True, check=True)
    return transform_ast(json.loads(proc.stdout))


needs_pandoc = pytest.mark.skipif(
    shutil.which("pandoc") is None, reason="pandoc not installed")


@needs_pandoc
@pytest.mark.parametrize("kind", sorted(CALLOUT_TYPES))
def test_every_type_becomes_exactly_one_table(kind):
    """Pinned against the real pandoc rather than against my reading of it:
    its reader recognises only GitHub's five alerts, and only bare ones."""
    ast = _to_ast(f"> [!{kind.upper()}]\n> body text\n")
    assert _table_count(ast) == 1


@needs_pandoc
def test_a_plain_blockquote_does_not_become_a_table():
    assert _table_count(_to_ast("> just a quotation\n")) == 0


@needs_pandoc
def test_a_list_inside_a_callout_stays_inside_it():
    """The reason a callout is a table and not styled paragraphs: a list in a
    blockquote arrives with a bullet's indents and no indentEnd, so any
    indent-based notion of 'where the callout ends' stops at the first item."""
    ast = _to_ast("> [!WARNING]\n> before\n>\n> - one\n> - two\n")
    assert _table_count(ast) == 1
    dumped = json.dumps(ast)
    assert dumped.count("BulletList") == 1
    # The list is inside the table, not orphaned after it.
    assert dumped.index("Table") < dumped.index("BulletList")


@needs_pandoc
def test_pandocs_own_alert_parsing_does_not_double_up():
    """`> [!NOTE]` is an alert to pandoc *and* a callout to us. Rewriting it
    first is what keeps one path instead of two."""
    ast = _to_ast("> [!NOTE]\n> body\n")
    assert "Div" not in json.dumps(ast)


# --------------------------------------------------------------------------
# Pull: the table becomes markdown again
# --------------------------------------------------------------------------

def _para(text, bold=False):
    return {"paragraph": {"elements": [
        {"textRun": {"content": text + "\n", "textStyle": {"bold": bold}}}]},
        "startIndex": 0, "endIndex": len(text) + 1}


def _callout_table(kind, body_paras, title=None):
    spec = CALLOUT_TYPES[kind]
    head = _para(f"{spec.icon} {title or spec.title}", bold=True)
    return {"table": {"rows": 1, "columns": 1, "tableRows": [
        {"tableCells": [{"content": [head] + body_paras}]}]},
        "startIndex": 0, "endIndex": 1}


def test_a_callout_table_converts_back_to_markdown():
    doc = {"body": {"content": [_callout_table("note", [_para("The body.")])]}}
    md, _ = doc_to_markdown(doc)
    assert md.strip() == "> [!NOTE]\n> The body."


def test_a_renamed_callout_keeps_its_title_through_the_round_trip():
    doc = {"body": {"content": [
        _callout_table("warning", [_para("Careful.")], title="Read this first")]}}
    md, _ = doc_to_markdown(doc)
    assert md.strip() == "> [!WARNING] Read this first\n> Careful."


def test_multiple_paragraphs_all_stay_inside_the_quote():
    doc = {"body": {"content": [
        _callout_table("tip", [_para("First."), _para("Second.")])]}}
    md, _ = doc_to_markdown(doc)
    assert md.strip() == "> [!TIP]\n> First.\n>\n> Second."


def test_an_ordinary_table_is_still_a_table():
    doc = {"body": {"content": [{"table": {"rows": 1, "columns": 2, "tableRows": [
        {"tableCells": [{"content": [_para("a")]}, {"content": [_para("b")]}]}]},
        "startIndex": 0, "endIndex": 1}]}}
    md, _ = doc_to_markdown(doc)
    assert "| a | b |" in md
    assert "[!" not in md


def test_a_one_cell_table_without_an_icon_is_still_a_table():
    doc = {"body": {"content": [{"table": {"rows": 1, "columns": 1, "tableRows": [
        {"tableCells": [{"content": [_para("just a cell")]}]}]},
        "startIndex": 0, "endIndex": 1}]}}
    md, _ = doc_to_markdown(doc)
    assert "| just a cell |" in md


# --------------------------------------------------------------------------
# Restoring what the doc cannot record
# --------------------------------------------------------------------------

def test_an_alias_is_not_rewritten_to_its_canonical_type():
    """`[!INFO]` and `[!NOTE]` are the same box. Rewriting the file's word for
    it on every pull would be a diff nobody made."""
    local = "> [!INFO]\n> body\n"
    pulled = "> [!NOTE]\n> body\n"
    assert restore_callout_spellings(pulled, local) == local


def test_a_fold_marker_survives():
    local = "> [!NOTE]-\n> body\n"
    pulled = "> [!NOTE]\n> body\n"
    assert restore_callout_spellings(pulled, local) == local


def test_a_custom_title_edited_in_docs_wins_over_the_local_one():
    local = "> [!info] Old title\n> body\n"
    pulled = "> [!NOTE] New title\n> body\n"
    assert restore_callout_spellings(pulled, local) == "> [!info] New title\n> body\n"


def test_nothing_is_restored_once_a_callout_was_added_remotely():
    """Position stops identifying the same callout, and relabelling a
    [!WARNING] as a [!TIP] is worse than a canonical spelling."""
    local = "> [!INFO]\n> a\n"
    pulled = "> [!NOTE]\n> a\n\n> [!WARNING]\n> b\n"
    assert restore_callout_spellings(pulled, local) == pulled


def test_nothing_is_restored_when_the_types_no_longer_line_up():
    local = "> [!INFO]\n> a\n\n> [!TIP]\n> b\n"
    pulled = "> [!WARNING]\n> a\n\n> [!NOTE]\n> b\n"
    assert restore_callout_spellings(pulled, local) == pulled


def test_a_document_without_callouts_is_untouched():
    text = "Just prose, and a > quote.\n"
    assert restore_callout_spellings(text, text) == text


# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

def test_colours_are_derived_from_the_page_so_dark_themes_stay_readable():
    from gdoc_sync.style import _luminance

    spec = CALLOUT_TYPES["note"]
    light_accent, light_bg = callout_colors(spec, {"background": "#ffffff"})
    dark_accent, dark_bg = callout_colors(spec, {"background": "#1e1e2e"})

    assert light_accent == spec.accent  # already tuned for a light page

    # The tint stays near whatever the page is — that is what keeps ordinary
    # body text readable on top of it — rather than near the accent.
    for page, tint, accent in (("#ffffff", light_bg, light_accent),
                               ("#1e1e2e", dark_bg, dark_accent)):
        assert abs(_luminance(tint) - _luminance(page)) < 0.15
        # And the accent has to clear that tint by enough to read as a title.
        assert abs(_luminance(accent) - _luminance(tint)) > 0.3


def test_a_callout_gets_a_tinted_cell_and_an_accent_title():
    doc = {"body": {"content": [_callout_table("caution", [_para("Careful.")])]}}
    requests, n = callout_requests(doc, {"background": "#ffffff"})
    assert n == 1
    kinds = [next(iter(r)) for r in requests]
    assert kinds == ["updateTableCellStyle", "updateTextStyle"]
    cell = requests[0]["updateTableCellStyle"]["tableCellStyle"]
    assert cell["borderLeft"]["width"] == {"magnitude": 3, "unit": "PT"}
    assert cell["borderTop"]["width"]["magnitude"] == 0
    assert requests[1]["updateTextStyle"]["textStyle"]["bold"] is True


def test_callouts_are_skipped_by_the_plain_table_borders():
    """Both run in one batch, so a black box drawn over a callout would win."""
    from gdoc_sync.style import table_border_requests

    doc = {"body": {"content": [_callout_table("note", [_para("body")])]}}
    requests, tables = table_border_requests(doc)
    assert (requests, tables) == ([], 0)
