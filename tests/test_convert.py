"""Google Docs JSON → markdown conversion."""

from gdoc_sync.convert import doc_to_markdown, restore_fence_languages


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


def test_inline_image_uses_saver():
    doc = {
        "body": {"content": [{
            "startIndex": 1, "endIndex": 10,
            "paragraph": {"elements": [
                {"textRun": {"content": "before "}},
                {"inlineObjectElement": {"inlineObjectId": "kix.img1"}},
                {"textRun": {"content": " after\n"}},
            ]},
        }]},
        "inlineObjects": {"kix.img1": {"inlineObjectProperties": {"embeddedObject": {
            "imageProperties": {"contentUri": "https://example.com/i.png"}}}}},
    }
    from gdoc_sync.convert import doc_to_markdown

    seen = {}

    def saver(object_id, uri):
        seen[object_id] = uri
        return "![image](doc-assets/img-001.png)"

    md, _ = doc_to_markdown(doc, image_saver=saver)
    assert "![image](doc-assets/img-001.png)" in md
    assert seen == {"kix.img1": "https://example.com/i.png"}

    # without a saver, images are skipped (legacy behavior)
    md_plain, _ = doc_to_markdown(doc)
    assert "![image]" not in md_plain


def test_blocks_separated_by_blank_lines():
    """pull → push must not merge adjacent paragraphs (gfm soft-wrap) or
    swallow a table that follows text."""
    from gdoc_sync.convert import doc_to_markdown

    def para(text, bullet=False):
        p = {"elements": [{"textRun": {"content": text + "\n"}}]}
        if bullet:
            p["bullet"] = {"nestingLevel": 0, "listId": "l1"}
        return {"paragraph": p}

    doc = {"body": {"content": [
        para("First paragraph."),
        para("Second paragraph."),
        para("item one", bullet=True),
        para("item two", bullet=True),
        para("After the list."),
        {"table": {"tableRows": [
            {"tableCells": [{"content": [para("a")]}, {"content": [para("b")]}]},
            {"tableCells": [{"content": [para("1")]}, {"content": [para("2")]}]},
        ]}},
    ]}, "lists": {}}

    md, _ = doc_to_markdown(doc)
    assert "First paragraph.\n\nSecond paragraph." in md
    assert "- item one\n- item two" in md          # list stays tight
    assert "item two\n\nAfter the list." in md     # blank line closes the list
    assert "After the list.\n\n| a | b |" in md    # table needs its blank line


# --------------------------------------------------------------------------
# Fence languages across the round trip
# --------------------------------------------------------------------------

def test_fence_language_is_restored_from_the_local_file():
    local = "```python\nx = 1\n```\n"
    pulled = "```\nx = 1\n```\n"
    assert restore_fence_languages(pulled, local) == local


def test_fence_language_is_not_guessed_when_blocks_were_added_remotely():
    """Position stops identifying the same block, and a mislabelled language
    is worse than a bare fence."""
    local = "```python\nx = 1\n```\n"
    pulled = "```\nnew block\n```\n\n```\nx = 1\n```\n"
    assert restore_fence_languages(pulled, local) == pulled


def test_fence_restoration_retires_the_spurious_merge_conflict():
    """Why this happens inside render_doc and not on the `pull` path alone.

    Like the equations in test_math, a bare-fence base makes *both* sides
    differ from it, so a remote edit on the line after a fence stopped the
    watcher and demanded a hand resolution. Restoring the language before the
    comparison makes it the ordinary clean merge it always was.
    """
    from gdoc_sync.merge import merge3

    local = "```python\nx = 1\n```\n"
    base_render = "```\nx = 1\n```\n"
    their_render = "```\nx = 2\n```\n"

    before = merge3(local, base_render, their_render,
                    label_ours="local", label_base="base", label_theirs="doc")
    assert before.conflicted

    base = restore_fence_languages(base_render, local)
    theirs = restore_fence_languages(their_render, local)
    after = merge3(local, base, theirs,
                   label_ours="local", label_base="base", label_theirs="doc")
    assert not after.conflicted
    assert after.text == "```python\nx = 2\n```\n"
