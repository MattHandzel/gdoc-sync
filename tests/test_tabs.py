"""Splitting `# [TAB]` markdown, and reconciling it against a doc's tabs."""

import pytest

from gdoc_sync.tabs import (
    _prune,
    find_tab,
    has_tab_sections,
    read_tabs,
    split_tab_sections,
    stamp_tab_id,
    sync_tabs,
    tab_tree,
    write_sections,
)

THREE_TABS = """# [TAB] Overview

The week ahead.

---

# [TAB] Monday

Coffee.

---

# [TAB] Tuesday

Consensus.
"""


# --------------------------------------------------------------------------
# Splitting
# --------------------------------------------------------------------------

def test_a_file_without_tab_headers_is_not_tabbed():
    """The whole feature hangs off this: no headers, no change in behaviour."""
    assert split_tab_sections("# Ordinary\n\nBody\n") == []
    assert not has_tab_sections("# Ordinary\n\nBody\n")


def test_three_sections_in_order():
    sections = split_tab_sections(THREE_TABS)
    assert [s.title for s in sections] == ["Overview", "Monday", "Tuesday"]
    assert [s.depth for s in sections] == [0, 0, 0]
    assert has_tab_sections(THREE_TABS)


def test_separator_rule_is_not_content():
    """`pull` writes `---` between tabs; pushing it back would add a rule."""
    sections = split_tab_sections(THREE_TABS)
    assert sections[0].markdown == "The week ahead.\n"
    assert sections[1].markdown == "Coffee.\n"
    assert sections[2].markdown == "Consensus.\n"


def test_a_rule_inside_a_tab_survives():
    md = "# [TAB] One\n\nbefore\n\n---\n\nafter\n\n---\n\n# [TAB] Two\n\nx\n"
    sections = split_tab_sections(md)
    assert "---" in sections[0].markdown
    assert sections[0].markdown.endswith("after\n")


def test_child_tabs_by_header_depth():
    md = ("# [TAB] Parent\n\na\n\n---\n\n## [TAB] Child\n\nb\n\n"
          "---\n\n# [TAB] Sibling\n\nc\n")
    sections = split_tab_sections(md)
    assert [(s.title, s.depth) for s in sections] == [
        ("Parent", 0), ("Child", 1), ("Sibling", 0)]
    assert tab_tree(sections) == [None, 0, None]


def test_headers_inside_a_fence_are_content():
    md = "# [TAB] One\n\n```\n# [TAB] Not a tab\n```\n\ntext\n"
    sections = split_tab_sections(md)
    assert len(sections) == 1
    assert "# [TAB] Not a tab" in sections[0].markdown


def test_text_before_the_first_header_is_folded_in_with_a_warning():
    said = []
    sections = split_tab_sections("Preamble.\n\n# [TAB] One\n\nbody\n",
                                  say=said.append)
    assert sections[0].markdown.startswith("Preamble.")
    assert "body" in sections[0].markdown
    assert any("first tab" in line for line in said)


def test_an_untitled_header_still_names_a_tab():
    assert split_tab_sections("# [TAB]\n\nx\n")[0].title == "Untitled tab"


# --------------------------------------------------------------------------
# Reading a document's tabs
# --------------------------------------------------------------------------

def _tab(tab_id, title, children=(), text="x"):
    return {
        "tabProperties": {"tabId": tab_id, "title": title, "index": 0},
        "documentTab": {"body": {"content": [
            {"startIndex": 1, "endIndex": 1 + len(text) + 1,
             "paragraph": {"elements": [{"textRun": {"content": text + "\n"}}]}},
        ]}},
        "childTabs": list(children),
    }


def test_read_tabs_flattens_children_in_document_order():
    doc = {"tabs": [_tab("t1", "One", [_tab("t2", "Child")]), _tab("t3", "Two")]}
    tabs = read_tabs(doc)
    assert [(t.tab_id, t.title, t.parent_id) for t in tabs] == [
        ("t1", "One", ""), ("t2", "Child", "t1"), ("t3", "Two", "")]
    assert find_tab(doc, "t2")["tabProperties"]["title"] == "Child"
    assert find_tab(doc, "nope") is None


def test_stamp_tab_id_reaches_every_addressable_field():
    requests = [
        {"updateTextStyle": {"range": {"startIndex": 1, "endIndex": 2}}},
        {"insertText": {"location": {"index": 1}, "text": "hi"}},
        {"updateDocumentStyle": {"documentStyle": {}, "fields": "background"}},
        {"updateTableCellStyle": {"tableRange": {"tableCellLocation": {
            "tableStartLocation": {"index": 5}}}}},
    ]
    stamped = stamp_tab_id(requests, "TAB")
    assert stamped[0]["updateTextStyle"]["range"]["tabId"] == "TAB"
    assert stamped[1]["insertText"]["location"]["tabId"] == "TAB"
    assert stamped[2]["updateDocumentStyle"]["tabId"] == "TAB"
    assert (stamped[3]["updateTableCellStyle"]["tableRange"]["tableCellLocation"]
            ["tableStartLocation"]["tabId"] == "TAB")


def test_stamp_tab_id_does_not_mutate_the_input():
    original = [{"updateTextStyle": {"range": {"startIndex": 1}}}]
    stamp_tab_id(original, "TAB")
    assert "tabId" not in original[0]["updateTextStyle"]["range"]


# --------------------------------------------------------------------------
# Reconciling
# --------------------------------------------------------------------------

class FakeDocs:
    """Just enough Docs service to drive sync_tabs: get + batchUpdate."""

    def __init__(self, tabs):
        self.tabs = list(tabs)
        self.sent: list[list[dict]] = []
        self._next = 0

    # -- the googleapiclient shape: .documents().get(...).execute()
    def documents(self):
        return self

    def get(self, **kw):
        self._kw = kw
        return self

    def batchUpdate(self, documentId=None, body=None):  # noqa: N803
        self._body = body
        return self

    def execute(self, **_kw):
        if hasattr(self, "_body"):
            body, self._body = self._body, None
            del self._body
            return {"replies": [self._apply(r) for r in body["requests"]]}
        return {"tabs": self.tabs, "revisionId": "rev"}

    def _apply(self, request):
        self.sent.append(request)
        if "addDocumentTab" in request:
            props = dict(request["addDocumentTab"]["tabProperties"])
            self._next += 1
            props["tabId"] = f"new{self._next}"
            parent = props.get("parentTabId")
            new = {"tabProperties": props, "documentTab": {"body": {"content": []}},
                   "childTabs": []}
            if parent:
                for t in self.tabs:
                    if t["tabProperties"]["tabId"] == parent:
                        t["childTabs"].append(new)
            else:
                self.tabs.append(new)
            return {"addDocumentTab": {"tabProperties": props}}
        if "updateDocumentTabProperties" in request:
            props = request["updateDocumentTabProperties"]["tabProperties"]
            for t in self.tabs:
                if t["tabProperties"]["tabId"] == props["tabId"]:
                    t["tabProperties"].update(props)
        if "deleteTab" in request:
            wanted = request["deleteTab"]["tabId"]
            self.tabs = [t for t in self.tabs
                         if t["tabProperties"]["tabId"] != wanted]
        return {}


def test_existing_tabs_are_matched_by_title_not_recreated():
    docs = FakeDocs([_tab("a", "Monday"), _tab("b", "Tuesday")])
    sections = split_tab_sections(
        "# [TAB] Monday\n\nx\n\n---\n\n# [TAB] Tuesday\n\ny\n")
    ids, _ = sync_tabs(docs, "DOC", sections)
    assert ids == ["a", "b"]
    assert not any("addDocumentTab" in r for r in docs.sent)


def test_a_missing_tab_is_added():
    docs = FakeDocs([_tab("a", "Monday")])
    sections = split_tab_sections(
        "# [TAB] Monday\n\nx\n\n---\n\n# [TAB] Tuesday\n\ny\n")
    ids, _ = sync_tabs(docs, "DOC", sections)
    assert ids[0] == "a"
    added = [r for r in docs.sent if "addDocumentTab" in r]
    assert len(added) == 1
    assert added[0]["addDocumentTab"]["tabProperties"]["title"] == "Tuesday"


def test_a_hand_added_tab_survives_a_push():
    """The acceptance criterion: extra tabs are not ours to delete."""
    docs = FakeDocs([_tab("a", "Monday"), _tab("x", "Someone else's notes")])
    sections = split_tab_sections("# [TAB] Monday\n\nx\n")
    ids, _ = sync_tabs(docs, "DOC", sections)
    assert ids == ["a"]
    assert not any("deleteTab" in r for r in docs.sent)


def test_prune_tabs_removes_them_when_asked():
    docs = FakeDocs([_tab("a", "Monday"), _tab("x", "Stale")])
    sections = split_tab_sections("# [TAB] Monday\n\nx\n")
    sync_tabs(docs, "DOC", sections, prune=True)
    deleted = [r["deleteTab"]["tabId"] for r in docs.sent if "deleteTab" in r]
    assert deleted == ["x"]


def test_prune_keeps_a_parent_whose_child_is_still_ours():
    """deleteTab takes the children with it, so an ancestor of a kept tab stays."""
    docs = FakeDocs([_tab("p", "Parent", [_tab("c", "Child")])])
    existing = read_tabs({"tabs": docs.tabs})
    _prune(docs, "DOC", existing, {"c"}, lambda *_: None)
    deleted = [r["deleteTab"]["tabId"] for r in docs.sent if "deleteTab" in r]
    assert deleted == []


def test_prune_deletes_a_parent_once_not_its_children_too():
    """A second deleteTab for a child already gone would fail the whole batch."""
    docs = FakeDocs([_tab("p", "Parent", [_tab("c", "Child")]), _tab("k", "Keep")])
    existing = read_tabs({"tabs": docs.tabs})
    _prune(docs, "DOC", existing, {"k"}, lambda *_: None)
    deleted = [r["deleteTab"]["tabId"] for r in docs.sent if "deleteTab" in r]
    assert deleted == ["p"]


def test_prune_never_empties_the_document():
    docs = FakeDocs([_tab("a", "Only")])
    said = []
    sync_tabs(docs, "DOC", [], prune=True, say=said.append)
    assert not any("deleteTab" in r for r in docs.sent)


def test_child_tabs_are_created_under_their_parent():
    docs = FakeDocs([])
    sections = split_tab_sections(
        "# [TAB] Parent\n\na\n\n---\n\n## [TAB] Child\n\nb\n")
    ids, _ = sync_tabs(docs, "DOC", sections)
    adds = [r["addDocumentTab"]["tabProperties"] for r in docs.sent
            if "addDocumentTab" in r]
    assert adds[0]["title"] == "Parent" and "parentTabId" not in adds[0]
    assert adds[1]["title"] == "Child"
    assert adds[1]["parentTabId"] == ids[0]


def test_a_new_documents_placeholder_tab_is_renamed_not_doubled():
    docs = FakeDocs([_tab("t1", "Tab 1")])
    sections = split_tab_sections(
        "# [TAB] Overview\n\na\n\n---\n\n# [TAB] Monday\n\nb\n")
    ids, _ = sync_tabs(docs, "DOC", sections, adopt_placeholder=True)
    assert ids[0] == "t1"
    renames = [r for r in docs.sent if "updateDocumentTabProperties" in r]
    assert renames[0]["updateDocumentTabProperties"]["tabProperties"]["title"] \
        == "Overview"
    assert len([r for r in docs.sent if "addDocumentTab" in r]) == 1


@pytest.mark.parametrize("title", ["Q&A", "Notes: 2026", "Tab — with a dash"])
def test_titles_with_punctuation_round_trip(title):
    sections = split_tab_sections(f"# [TAB] {title}\n\nbody\n")
    assert sections[0].title == title


class _FlakyDocs(FakeDocs):
    """Like FakeDocs, but one addDocumentTab comes back without a tabId."""

    def __init__(self, tabs, fails: str):
        super().__init__(tabs)
        self.fails = fails

    def _apply(self, request):
        if ("addDocumentTab" in request
                and request["addDocumentTab"]["tabProperties"]["title"] == self.fails):
            self.sent.append(request)
            return {"addDocumentTab": {"tabProperties": {}}}
        return super()._apply(request)


def test_a_failed_tab_add_keeps_its_slot():
    docs = _FlakyDocs([_tab("a", "Overview"), _tab("d", "Appendix")], fails="Plans")
    sections = split_tab_sections(
        "# [TAB] Overview\n\nx\n\n---\n\n# [TAB] Plans\n\ny\n\n---\n\n"
        "# [TAB] Appendix\n\nz\n")
    said: list[str] = []
    ids, _ = sync_tabs(docs, "DOC", sections, say=said.append)
    assert ids == ["a", None, "d"]
    assert any("could not create tab 'Plans'" in s for s in said)
    assert not any("Added tab: Plans" in s for s in said)


def test_a_failed_tab_add_never_shifts_a_section_into_another_tab():
    """Regression: Plans' body used to be written into the Appendix tab."""
    docs = _FlakyDocs([_tab("a", "Overview"), _tab("d", "Appendix")], fails="Plans")
    sections = split_tab_sections(
        "# [TAB] Overview\n\noverview body\n\n---\n\n"
        "# [TAB] Plans\n\nplans body\n\n---\n\n"
        "# [TAB] Appendix\n\nappendix body\n")
    said: list[str] = []
    written = write_sections(docs, drive_service=None, doc_id="DOC",
                             sections=sections, say=said.append)
    assert written == 2
    inserts = [(r["insertText"]["location"].get("tabId"), r["insertText"]["text"])
               for r in docs.sent if "insertText" in r]
    into_appendix = "".join(t for tab, t in inserts if tab == "d")
    assert "appendix body" in into_appendix
    assert "plans body" not in "".join(t for _, t in inserts)
    assert any("no tab for 'Plans'" in s for s in said)
