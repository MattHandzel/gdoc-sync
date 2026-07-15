"""Google Docs JSON → markdown conversion."""

from gdoc_sync.convert import doc_to_markdown


def _run(text, **style):
    return {"textRun": {"content": text, "textStyle": style}}


def _para(runs, named_style=None, bullet=None):
    para = {"elements": runs}
    if named_style:
        para["paragraphStyle"] = {"namedStyleType": named_style}
    if bullet:
        para["bullet"] = bullet
    return {"paragraph": para, "startIndex": 0, "endIndex": 1}


def test_headings_and_body():
    doc = {"body": {"content": [
        _para([_run("Title\n")], named_style="HEADING_1"),
        _para([_run("Sub\n")], named_style="HEADING_2"),
        _para([_run("Body text.\n")]),
    ]}}
    md, _ = doc_to_markdown(doc)
    assert "# Title" in md
    assert "## Sub" in md
    assert "Body text." in md


def test_bold_italic_link():
    doc = {"body": {"content": [
        _para([
            _run("bold", bold=True),
            _run(" and "),
            _run("italic", italic=True),
            _run(" and "),
            _run("a link", link={"url": "https://example.com"}),
            _run("\n"),
        ]),
    ]}}
    md, _ = doc_to_markdown(doc)
    assert "**bold**" in md
    assert "*italic*" in md
    assert "[a link](https://example.com)" in md


def test_bullet_and_ordered_lists():
    lists = {
        "ul": {"listProperties": {"nestingLevels": [{"glyphType": "GLYPH_TYPE_UNSPECIFIED"}]}},
        "ol": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}},
    }
    doc = {"lists": lists, "body": {"content": [
        _para([_run("unordered\n")], bullet={"listId": "ul", "nestingLevel": 0}),
        _para([_run("ordered\n")], bullet={"listId": "ol", "nestingLevel": 0}),
    ]}}
    md, _ = doc_to_markdown(doc)
    assert "- unordered" in md
    assert "1. ordered" in md


def test_table_renders_as_pipe_table():
    cell = lambda t: {"content": [{"paragraph": {"elements": [_run(t)]}}]}  # noqa: E731
    doc = {"body": {"content": [{
        "table": {"tableRows": [
            {"tableCells": [cell("h1"), cell("h2")]},
            {"tableCells": [cell("a"), cell("b")]},
        ]},
    }]}}
    md, _ = doc_to_markdown(doc)
    assert "| h1 | h2 |" in md
    assert "|---|---|" in md
    assert "| a | b |" in md
