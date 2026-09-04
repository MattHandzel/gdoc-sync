"""The doc-text fingerprint the sync engine uses to catch a lost update.

A revision id cannot do this job: Google rewrites it on autosave and on
presence changes, so comparing revisions before a push would refuse forever
while anyone has the tab open. The fingerprint has to move only when the
document's *text* moves.
"""

from __future__ import annotations

from gdoc_sync.convert import doc_text_fingerprint


def para(*runs):
    return {"paragraph": {"elements": [{"textRun": {"content": r}} for r in runs]}}


def body(*paragraphs):
    return {"content": list(paragraphs)}


def test_the_same_text_fingerprints_the_same():
    a = {"revisionId": "rev-1", "body": body(para("Hello there.\n"))}
    b = {"revisionId": "rev-99999", "body": body(para("Hello ", "there.\n"))}
    assert doc_text_fingerprint(a) == doc_text_fingerprint(b)


def test_revision_churn_alone_does_not_move_it():
    doc = {"revisionId": "rev-1", "body": body(para("Steady.\n"))}
    before = doc_text_fingerprint(doc)
    doc["revisionId"] = "rev-2"  # autosave, presence, a cursor moving
    assert doc_text_fingerprint(doc) == before


def test_formatting_alone_does_not_move_it():
    plain = {"body": body({"paragraph": {"elements": [
        {"textRun": {"content": "Same words.\n"}}]}})}
    bold = {"body": body({"paragraph": {"elements": [
        {"textRun": {"content": "Same words.\n",
                     "textStyle": {"bold": True}}}]}})}
    assert doc_text_fingerprint(plain) == doc_text_fingerprint(bold)


def test_a_typed_character_moves_it():
    a = {"body": body(para("Ship on Thursday.\n"))}
    b = {"body": body(para("Ship on Friday.\n"))}
    assert doc_text_fingerprint(a) != doc_text_fingerprint(b)


def test_text_in_a_table_counts():
    cell = {"table": {"tableRows": [{"tableCells": [
        {"content": [para("in a cell\n")]}]}]}}
    empty = {"body": body()}
    with_table = {"body": body(cell)}
    assert doc_text_fingerprint(with_table) != doc_text_fingerprint(empty)


def test_every_tab_counts_including_children():
    def tab(title, text, children=()):
        return {"tabProperties": {"title": title},
                "documentTab": {"body": body(para(text))},
                "childTabs": list(children)}

    doc = {"body": body(), "tabs": [
        tab("One", "first\n", [tab("One.a", "nested\n")]),
        tab("Two", "second\n"),
    ]}
    moved = {"body": body(), "tabs": [
        tab("One", "first\n", [tab("One.a", "nested EDITED\n")]),
        tab("Two", "second\n"),
    ]}
    assert doc_text_fingerprint(doc) != doc_text_fingerprint(moved)


def test_an_edit_to_the_second_tab_is_seen():
    """A fingerprint of the first tab only would miss this entirely."""
    def doc(second):
        return {"body": body(), "tabs": [
            {"documentTab": {"body": body(para("first\n"))}},
            {"documentTab": {"body": body(para(second))}},
        ]}

    assert doc_text_fingerprint(doc("second\n")) != doc_text_fingerprint(
        doc("second, edited\n"))


def test_an_empty_doc_has_a_stable_digest():
    assert doc_text_fingerprint({}) == doc_text_fingerprint({"body": {}})
