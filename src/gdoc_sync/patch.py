"""Push by editing the Google Doc in place, so comments keep their anchors.

WHY THIS EXISTS
---------------
A plain push hands Drive a .docx and lets its importer replace the document's
body. That is the highest-fidelity path there is, and it has one cost that
turned out to matter more than any other: every comment in the document is
anchored to text that the import just deleted. Google Docs shows each of them
as "Original content deleted", and there is no way back — the Drive API cannot
create or move an anchor. Under ``watch`` a push follows every save, so a
reviewer's comment survived only until the author next typed a word.

HOW
---
The .docx is imported into a short-lived *staging* document instead, which is
exactly the document a full replace would have produced. The live document is
then diffed against it and brought into line with ordinary Docs API edits:

1. **Text.** Paragraphs are aligned first; inside each changed stretch, words
   are aligned. Only the words that differ are deleted and inserted, so text
   that did not change — and every anchor on it — is never touched.
2. **Style.** With the text now identical, both documents have the same
   indices, so each paragraph's style, bullets and character runs are copied
   from the staging document wherever they differ.
3. **Proof.** The document is fetched again and must match the staging
   document paragraph for paragraph and render to the same markdown.

Anything this cannot express — a table or image that changed, a new footnote,
equations, a changed theme — raises :class:`NotPatchable` *before* anything is
written, and the caller falls back to the full replace. A failed proof raises
it afterwards, and the full replace then repairs the document. The worst case
is therefore exactly the old behaviour, never something new.

Every write carries the revision it was computed from, so a concurrent edit in
the doc makes the write fail instead of landing at the wrong offsets.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from .mdrequests import u16
from .services import NUM_RETRIES

GDOC_MIME = "application/vnd.google-apps.document"

# Marks the staging document as ours, so the reaper that already clears up
# staged images removes one left behind by a killed push.
STAGING_PROPERTY = {"gdoc-sync": "staging"}

# Attempts at a write that keeps failing because the revision moved. Google
# bumps the revision id with nobody typing, so one retry is not enough, but a
# genuinely busy doc should give up and fall back rather than spin.
_WRITE_ATTEMPTS = 3

# A diff over more tokens than this is a rewrite, not an edit; SequenceMatcher
# goes quadratic long before it is worth waiting for.
_MAX_REGION_TOKENS = 40_000

# Paragraph-style fields the API reports but will not accept back.
_READ_ONLY_PARA_FIELDS = ("headingId", "tabStops")

# Keys that identify rather than describe, and so differ between two imports
# of the same content.
_ID_KEYS = frozenset({"startIndex", "endIndex", "headingId", "listId",
                      "footnoteId", "inlineObjectId", "objectId",
                      "contentUri", "footnoteNumber"})

_TOKEN_RE = re.compile(r"\w+|\s|[^\w\s]", re.UNICODE)


class NotPatchable(Exception):
    """This change cannot be made in place; replace the body instead."""


@dataclass
class PatchResult:
    text_edits: int
    style_edits: int


# ---------------------------------------------------------------------------
# Flattening a document into comparable items
# ---------------------------------------------------------------------------

@dataclass
class _Unit:
    """One comparable character: text, or a token for an embedded object."""

    ch: str
    start: int
    size: int


@dataclass
class _Item:
    """One top-level structural element of the body."""

    kind: str                 # "para" | "block"
    key: str
    start: int
    end: int
    units: list[_Unit] = field(default_factory=list)
    para: dict | None = None
    segment: str = ""         # "" for the body, else the footnote's id


class _Tokens:
    """Stable stand-in characters for embedded objects, shared by both docs.

    Supplementary private-use code points: they cannot collide with anything
    a document realistically contains, and they are never inserted anywhere.
    """

    def __init__(self):
        self._by_sig: dict[str, str] = {}

    def __call__(self, sig: str) -> str:
        if sig not in self._by_sig:
            self._by_sig[sig] = chr(0xF0000 + len(self._by_sig))
        return self._by_sig[sig]

    @staticmethod
    def is_token(ch: str) -> bool:
        return 0xF0000 <= ord(ch) <= 0xFFFFD


def _strip_ids(value):
    if isinstance(value, dict):
        return {k: _strip_ids(v) for k, v in value.items()
                if k not in _ID_KEYS and not k.startswith("suggest")}
    if isinstance(value, list):
        return [_strip_ids(v) for v in value]
    return value


def _sig(value) -> str:
    blob = json.dumps(_strip_ids(value), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()


def _element_sig(elem: dict, tab: dict) -> str:
    """What makes an embedded object the same object in both documents.

    A footnote reference is only a position: its text is a segment of its
    own, compared and edited separately (see :func:`_segments`)."""
    if "footnoteReference" in elem:
        return "footnote"
    if "inlineObjectElement" in elem:
        oid = elem["inlineObjectElement"].get("inlineObjectId", "")
        props = (tab.get("inlineObjects") or {}).get(oid, {})
        return "image:" + _sig(props)
    kind = next((k for k in elem if k not in ("startIndex", "endIndex")), "?")
    return f"{kind}:" + _sig(elem.get(kind))


def _table_shape(table: dict) -> dict:
    """A table without its text: rows, columns and every style."""
    return {"columns": table.get("columns"), "style": table.get("tableStyle"),
            "rows": [{"style": row.get("tableRowStyle"),
                      "cells": [{k: v for k, v in cell.items() if k != "content"}
                                for cell in row.get("tableCells", [])]}
                     for row in table.get("tableRows", [])]}


def _flatten(tab: dict, tokens: _Tokens, content: list | None = None,
             segment: str = "") -> list[_Item]:
    """The body (or one footnote's ``content``) as a flat list of items.

    A table becomes markers for its start, each cell and its end, with each
    cell's paragraphs in between, so text inside a cell is edited like any
    other; the table's shape and styles are all in the start marker, so a
    changed table stops the patch instead."""
    items: list[_Item] = []
    if content is None:
        content = tab.get("body", {}).get("content", [])
    _flatten_into(items, content, tab, tokens, segment)
    return items


def _flatten_into(items: list[_Item], content: list, tab: dict, tokens: _Tokens,
                  segment: str) -> None:
    for el in content:
        start, end = el.get("startIndex", 0), el["endIndex"]
        if "table" in el:
            table = el["table"]
            items.append(_Item("block", "\0table:" + _sig(_table_shape(table)),
                               start, start, segment=segment))
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    at = cell.get("startIndex", 0)
                    items.append(_Item("block", "\0cell", at, at, segment=segment))
                    _flatten_into(items, cell.get("content", []), tab, tokens,
                                  segment)
            items.append(_Item("block", "\0end-table", end, end, segment=segment))
            continue
        if "paragraph" not in el:
            kind = next(k for k in el if k not in ("startIndex", "endIndex"))
            items.append(_Item("block", f"\0{kind}:{_sig(el[kind])}", start, end,
                               segment=segment))
            continue
        para = el["paragraph"]
        units: list[_Unit] = []
        for elem in para.get("elements", []):
            s, e = elem.get("startIndex", 0), elem.get("endIndex", 0)
            run = elem.get("textRun")
            if run is not None:
                at = s
                for ch in run.get("content", ""):
                    n = u16(ch)
                    units.append(_Unit(ch, at, n))
                    at += n
                if at != e:
                    raise NotPatchable("a text run's length does not add up")
                continue
            if "equation" in elem:
                raise NotPatchable("the document has equations, which the Docs "
                                   "API does not let this compare")
            units.append(_Unit(tokens(_element_sig(elem, tab)), s, e - s))
        items.append(_Item("para", "".join(u.ch for u in units), start, end,
                           units, para, segment))


def _footnote_ids(content: list) -> list[str]:
    """Footnote ids in the order their references appear."""
    out: list[str] = []
    for el in content:
        for elem in (el.get("paragraph") or {}).get("elements", []):
            ref = elem.get("footnoteReference")
            if ref is not None:
                out.append(ref.get("footnoteId", ""))
        for row in (el.get("table") or {}).get("tableRows", []):
            for cell in row.get("tableCells", []):
                out.extend(_footnote_ids(cell.get("content", [])))
    return out


def _segments(live_tab: dict, target_tab: dict,
              tokens: _Tokens) -> list[tuple[str, list[_Item], list[_Item]]]:
    """``(live segment id, live items, target items)`` for the body ("") and
    each footnote.

    Footnotes pair up by the order of their references, so the pairing only
    holds while both documents have the same number of them; the body is
    edited first when they do not (a deleted reference deletes its note)."""
    pairs = [("", _flatten(live_tab, tokens), _flatten(target_tab, tokens))]
    live_ids = _footnote_ids(live_tab.get("body", {}).get("content", []))
    target_ids = _footnote_ids(target_tab.get("body", {}).get("content", []))
    if len(live_ids) != len(target_ids):
        return pairs
    live_notes = live_tab.get("footnotes") or {}
    target_notes = target_tab.get("footnotes") or {}
    for lid, tid in zip(live_ids, target_ids):
        pairs.append((
            lid,
            _flatten(live_tab, tokens, live_notes.get(lid, {}).get("content", []), lid),
            _flatten(target_tab, tokens, target_notes.get(tid, {}).get("content", []), tid)))
    return pairs


# ---------------------------------------------------------------------------
# Phase 1: text
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _bullet_kind(para: dict | None, lists: dict) -> tuple | None:
    bullet = (para or {}).get("bullet")
    if not bullet:
        return None
    nesting = bullet.get("nestingLevel", 0)
    levels = (lists.get(bullet.get("listId", ""), {})
              .get("listProperties", {}).get("nestingLevels", []))
    glyph = levels[nesting].get("glyphType", "") if len(levels) > nesting else ""
    return (nesting, bool(glyph) and glyph != "GLYPH_TYPE_UNSPECIFIED")


def _text_edits(live: list[_Item], target: list[_Item], live_lists: dict,
                target_lists: dict) -> list[tuple[int, int, str]]:
    """``(start, end, text)`` edits, in live-document indices, that turn the
    live body's text into the target's."""
    edits: list[tuple[int, int, str]] = []
    para_ops = SequenceMatcher(None, [i.key for i in live], [i.key for i in target],
                               autojunk=False).get_opcodes()

    for tag, i1, i2, j1, j2 in para_ops:
        if tag == "equal":
            continue
        olds, news = live[i1:i2], target[j1:j2]
        if any(i.kind != "para" for i in olds + news):
            raise NotPatchable("a table or callout changed")

        if tag == "insert":
            text = "".join(i.key for i in news)
            _refuse_new_objects(text)
            edits.append(_attach_insert(live, i1, text, news[0], live_lists,
                                        target_lists))
            continue

        if tag == "delete":
            start, end = olds[0].start, olds[-1].end
            if i2 == len(live) or live[i2].kind != "para":
                # The last newline of the body, of a cell or of a footnote, and
                # the one just before a table, cannot be deleted: take the one
                # before the deleted run instead, and leave that one in place.
                if i1 == 0 or live[i1 - 1].kind != "para":
                    raise NotPatchable("the change empties a table cell or the "
                                       "document")
                start, end = start - 1, end - 1
            edits.append((start, end, ""))
            continue

        # replace: both sides are paragraphs. Each run ends with a newline;
        # keep the last one fixed so the edit never has to delete it.
        units = [u for i in olds for u in i.units]
        new_text = "".join(i.key for i in news)
        if not units or units[-1].ch != "\n" or not new_text.endswith("\n"):
            raise NotPatchable("a paragraph without a closing newline")
        tail = units[-1].start
        units, new_text = units[:-1], new_text[:-1]
        edits.extend(_word_edits(units, new_text, tail))

    return edits


def _attach_insert(live: list[_Item], i1: int, text: str, first_new: _Item,
                   live_lists: dict, target_lists: dict) -> tuple[int, int, str]:
    """Where to insert whole new paragraphs, and in which form.

    A paragraph created by inserting a newline takes the properties of the
    paragraph it was split from — including membership of a list. So new list
    items are attached to the list item before them, and everything else to
    the paragraph after, matching whichever neighbour they resemble.
    """
    before = live[i1 - 1] if i1 > 0 and live[i1 - 1].kind == "para" else None
    after = live[i1] if i1 < len(live) and live[i1].kind == "para" else None
    want = _bullet_kind(first_new.para, target_lists)
    use_before = before is not None and (
        after is None
        or (_bullet_kind(before.para, live_lists) == want
            and _bullet_kind(after.para, live_lists) != want))
    if use_before:
        # "\nNEW" just before the previous paragraph's newline.
        return (before.end - 1, before.end - 1, "\n" + text[:-1])
    if after is None:
        raise NotPatchable("no paragraph to attach the new text to")
    return (after.start, after.start, text)


def _word_edits(units: list[_Unit], new_text: str,
                tail: int) -> list[tuple[int, int, str]]:
    """Word-level edits inside one changed stretch of paragraphs.

    ``units`` stops short of the stretch's closing newline, which sits at
    ``tail``; an insert at the very end goes just before it.
    """
    old_text = "".join(u.ch for u in units)
    a, b = _tokenize(old_text), _tokenize(new_text)
    if len(a) + len(b) > _MAX_REGION_TOKENS:
        raise NotPatchable("the change is too large to apply word by word")
    a_off = _offsets(a)
    b_off = _offsets(b)
    out = []
    for tag, a1, a2, b1, b2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        ca1, ca2 = a_off[a1], a_off[a2]
        insert = new_text[b_off[b1]:b_off[b2]]
        _refuse_new_objects(insert)
        if ca2 > ca1:
            start = units[ca1].start
            end = units[ca2 - 1].start + units[ca2 - 1].size
        else:
            start = end = units[ca1].start if ca1 < len(units) else tail
        out.append((start, end, insert))
    return out


def _offsets(tokens: list[str]) -> list[int]:
    offs = [0]
    for t in tokens:
        offs.append(offs[-1] + len(t))
    return offs


def _refuse_new_objects(text: str) -> None:
    if any(_Tokens.is_token(c) for c in text):
        raise NotPatchable("an image, footnote or other embedded object was "
                           "added or moved")


def _edit_requests(edits: list[tuple[int, int, str]], segment: str = "") -> list[dict]:
    requests = []
    where = {"segmentId": segment} if segment else {}
    for start, end, text in sorted(edits, key=lambda e: e[0], reverse=True):
        if end > start:
            requests.append({"deleteContentRange": {
                "range": {"startIndex": start, "endIndex": end, **where}}})
        if text:
            requests.append({"insertText": {"location": {"index": start, **where},
                                            "text": text}})
    return requests


# ---------------------------------------------------------------------------
# Phase 2: style
# ---------------------------------------------------------------------------

def _clean(value):
    """A style for comparison: unset and explicitly-empty mean the same."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _clean(v)
            if v in ({}, None, []):
                continue
            out[k] = v
        return out
    return value


def _para_style(para: dict) -> dict:
    style = dict(para.get("paragraphStyle") or {})
    for k in _READ_ONLY_PARA_FIELDS:
        style.pop(k, None)
    return style


class _Named:
    """What a paragraph and its text look like before their own styling: the
    document's named style for the paragraph, over NORMAL_TEXT.

    Styles are compared *through* this. An import spells every run's style
    out in full, while the Docs API stores a value it is sent that equals the
    named style's as ``{}`` (inherit), so the same look arrives as two
    different payloads."""

    def __init__(self, tab: dict):
        self._styles = {s.get("namedStyleType"): s for s in
                        (tab.get("namedStyles") or {}).get("styles", [])}

    def _inherited(self, name: str | None, which: str) -> dict:
        base = _clean(self._styles.get("NORMAL_TEXT", {}).get(which) or {})
        if name and name != "NORMAL_TEXT":
            base = {**base, **_clean(self._styles.get(name, {}).get(which) or {})}
        return base

    def paragraph(self, para: dict) -> dict:
        own = _clean(_para_style(para))
        inherited = self._inherited(own.get("namedStyleType"), "paragraphStyle")
        inherited.pop("namedStyleType", None)
        for k in _READ_ONLY_PARA_FIELDS:
            inherited.pop(k, None)
        return {**inherited, **own}

    def text(self, para: dict, style: dict) -> dict:
        name = (para.get("paragraphStyle") or {}).get("namedStyleType")
        return {**self._inherited(name, "textStyle"), **_clean(style)}


def _runs(para: dict, named: _Named) -> list[tuple[int, int, dict, dict]]:
    """Character runs as ``(start, end, style as seen, raw style)``, adjacent
    runs that look the same merged."""
    runs: list[tuple[int, int, dict, dict]] = []
    for elem in para.get("elements", []):
        run = elem.get("textRun")
        if run is None:
            continue
        raw = run.get("textStyle") or {}
        cmp = named.text(para, raw)
        link = cmp.get("link")
        if link is not None and "url" not in link:
            cmp = {**cmp, "link": "internal"}
        s, e = elem.get("startIndex", 0), elem["endIndex"]
        if runs and runs[-1][1] == s and runs[-1][2] == cmp:
            runs[-1] = (runs[-1][0], e, cmp, runs[-1][3])
        else:
            runs.append((s, e, cmp, raw))
    return runs


def _style_requests(live: list[_Item], target: list[_Item], live_lists: dict,
                    target_lists: dict, named: _Named) -> tuple[list[dict], list[str]]:
    """Requests that make each live paragraph look like its target twin, and
    a description of every difference found (what the proof reports)."""
    requests: list[dict] = []
    found: list[str] = []
    for lv, tg in zip(live, target):
        if lv.kind != "para":
            continue
        where = repr(tg.key[:30]) + (" (a footnote)" if lv.segment else "")
        seg = {"segmentId": lv.segment} if lv.segment else {}
        rng = {"startIndex": tg.start, "endIndex": tg.end, **seg}
        restyle_text = False

        have = _bullet_kind(lv.para, live_lists)
        want = _bullet_kind(tg.para, target_lists)
        if have != want:
            found.append(f"list {have} → {want} at {where}")
            if want is None:
                # Before the paragraph style: deleting a bullet indents the
                # paragraph to keep it in place, which the style then undoes.
                requests.append({"deleteParagraphBullets": {"range": rng}})
            elif have is None and want[0] == 0 and not lv.key.startswith("\t"):
                preset = ("NUMBERED_DECIMAL_ALPHA_ROMAN" if want[1]
                          else "BULLET_DISC_CIRCLE_SQUARE")
                requests.append({"createParagraphBullets": {
                    "range": rng, "bulletPreset": preset}})

        have_ps, want_ps = named.paragraph(lv.para), named.paragraph(tg.para)
        if have_ps != want_ps or have != want:
            if have_ps != want_ps:
                keys = sorted(k for k in set(have_ps) | set(want_ps)
                              if have_ps.get(k) != want_ps.get(k))
                found.append(f"paragraph style {keys} at {where}")
            requests.append({"updateParagraphStyle": {
                "range": rng, "paragraphStyle": _para_style(tg.para), "fields": "*"}})
            # A named style resets the paragraph's character styles, so every
            # run has to be put back afterwards.
            restyle_text = True

        live_runs, want_runs = _runs(lv.para, named), _runs(tg.para, named)
        if [r[:3] for r in live_runs] != [r[:3] for r in want_runs]:
            found.append(f"character styles at {where}: "
                         f"{_first_run_difference(live_runs, want_runs)}")
            restyle_text = True
        if restyle_text:
            for s, e, cmp, raw in want_runs:
                if cmp.get("link") == "internal":
                    raise NotPatchable("a link to a heading inside the document "
                                       "changed")
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": s, "endIndex": e, **seg},
                    "textStyle": raw, "fields": "*"}})
    return requests, found


def _first_run_difference(have: list, want: list) -> str:
    for h, w in zip(have, want):
        if h[:2] != w[:2]:
            return f"run boundary {h[:2]} vs {w[:2]}"
        if h[2] != w[2]:
            keys = sorted(k for k in set(h[2]) | set(w[2]) if h[2].get(k) != w[2].get(k))
            return f"{keys} over {w[:2]}"
    return f"{len(have)} runs vs {len(want)}"


# ---------------------------------------------------------------------------
# The whole thing
# ---------------------------------------------------------------------------

def _single_tab(doc: dict) -> dict:
    tabs = doc.get("tabs") or []
    if len(tabs) != 1 or tabs[0].get("childTabs"):
        raise NotPatchable("the document has more than one tab")
    tab = tabs[0].get("documentTab")
    if tab is None:
        raise NotPatchable("the document has no body")
    return tab


def _render(tab: dict) -> str:
    from .convert import doc_to_markdown
    md, _ = doc_to_markdown({"body": tab.get("body", {}),
                             "lists": tab.get("lists", {}),
                             "inlineObjects": tab.get("inlineObjects", {}),
                             "footnotes": tab.get("footnotes", {})})
    return md


def _check_compatible(live_tab: dict, target_tab: dict) -> None:
    if '"suggest' in json.dumps([live_tab.get("body", {}),
                                 live_tab.get("footnotes", {})]):
        raise NotPatchable("the document has pending suggestions")
    if _clean(live_tab.get("namedStyles")) != _clean(target_tab.get("namedStyles")):
        raise NotPatchable("the theme or font changed")
    if _clean(_strip_ids(live_tab.get("documentStyle"))) != _clean(
            _strip_ids(target_tab.get("documentStyle"))):
        raise NotPatchable("the page setup changed")


def _require_same_layout(segments: list[tuple[str, list[_Item], list[_Item]]],
                         live_tab: dict, target_tab: dict, why: str) -> None:
    """Same text, same objects, same indices, in the body and every footnote
    — what makes the target's offsets usable against the live document."""
    notes = len(_footnote_ids(target_tab.get("body", {}).get("content", [])))
    if len(segments) != notes + 1:
        raise NotPatchable(why)
    for _, live, target in segments:
        if [(i.key, i.start, i.end) for i in live] != [
                (i.key, i.start, i.end) for i in target]:
            raise NotPatchable(why)


def _is_revision_error(err) -> bool:
    status = getattr(getattr(err, "resp", None), "status", None)
    return status == 400 and "revision" in str(err).lower()


def build_target(drive_service, docs_service, docx_path: Path, *,
                 font: str | None, theme: str | None, baked: bool,
                 say=lambda *_: None) -> dict:
    """Import ``docx_path`` into a throwaway doc, style it the way a push
    would, and return its content. The staging doc is deleted before this
    returns."""
    from googleapiclient.http import MediaFileUpload

    from .create import DOCX_MIME
    from .style import apply_document_styling

    _reap_once(drive_service, say)
    media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
    created = drive_service.files().create(
        body={"name": "gdoc-sync staging (safe to delete)", "mimeType": GDOC_MIME,
              "appProperties": dict(STAGING_PROPERTY)},
        media_body=media, fields="id",
    ).execute(num_retries=NUM_RETRIES)
    staging_id = created["id"]
    try:
        try:
            apply_document_styling(docs_service, staging_id, font=font,
                                   theme=theme, baked=baked)
        except Exception as e:  # noqa: BLE001
            raise NotPatchable(f"could not style the staging copy ({e})") from e
        return docs_service.documents().get(
            documentId=staging_id, includeTabsContent=True,
        ).execute(num_retries=NUM_RETRIES)
    finally:
        try:
            drive_service.files().delete(fileId=staging_id).execute(
                num_retries=NUM_RETRIES)
        except Exception:  # noqa: BLE001 — the reaper gets it next time
            say(f"  Warning: left a staging doc in Drive: {staging_id}")


_reaped = False


def _reap_once(drive_service, say) -> None:
    """Clear staging files a killed push left behind — once per process."""
    global _reaped
    if _reaped:
        return
    _reaped = True
    from .mdrequests import ImageHost
    ImageHost(drive_service, say=say).reap_orphans()


def patch_document(docs_service, doc_id: str, target: dict, *,
                   expected_text: str | None = None,
                   say=lambda *_: None) -> PatchResult:
    """Edit ``doc_id`` in place until it matches ``target`` (a fetched doc).

    ``expected_text`` is the fingerprint of the doc as the caller last saw
    it. If the doc's text has moved since, :class:`gdoc_sync.push.RemoteChanged`
    is raised before anything is written, exactly as the full push does.

    Raises :class:`NotPatchable` when the change cannot be made this way; if
    that happens after a write, the document is mid-way and the caller's full
    replace puts it right.
    """
    from googleapiclient.errors import HttpError

    from .convert import doc_text_fingerprint
    from .push import RemoteChanged

    target_tab = _single_tab(target)
    # The named styles are the same on both sides (`_check_compatible`).
    named = _Named(target_tab)

    def fetch() -> dict:
        return docs_service.documents().get(
            documentId=doc_id, includeTabsContent=True,
        ).execute(num_retries=NUM_RETRIES)

    def write(requests: list[dict], revision: str) -> None:
        docs_service.documents().batchUpdate(documentId=doc_id, body={
            "requests": requests,
            "writeControl": {"requiredRevisionId": revision},
        }).execute(num_retries=NUM_RETRIES)

    def write_all(requests: list[dict], revision: str, what: str) -> bool:
        """True if written, False if the revision moved (retry)."""
        try:
            write(requests, revision)
            return True
        except HttpError as e:
            if not _is_revision_error(e):
                raise NotPatchable(f"the Docs API refused {what} ({e})") from e
            return False

    tokens = _Tokens()

    def style_pass(live_tab: dict, why: str) -> tuple[list[dict], list[str]]:
        segments = _segments(live_tab, target_tab, tokens)
        _require_same_layout(segments, live_tab, target_tab, why)
        requests: list[dict] = []
        found: list[str] = []
        for _, live_items, target_items in segments:
            r, f = _style_requests(live_items, target_items,
                                   live_tab.get("lists", {}),
                                   target_tab.get("lists", {}), named)
            requests += r
            found += f
        return requests, found

    # --- Phase 1: text ------------------------------------------------------
    # Usually one round. A second is needed when a footnote reference was
    # deleted: footnotes pair up only once the body has as many as the target.
    text_edits = 0
    rounds = 0
    first = True
    while True:
        live = fetch()
        if first and expected_text is not None and \
                doc_text_fingerprint(live) != expected_text:
            raise RemoteChanged(f"doc {doc_id} was edited while this push was "
                                "being prepared")
        live_tab = _single_tab(live)
        _check_compatible(live_tab, target_tab)
        requests: list[dict] = []
        n = 0
        for segment, live_items, target_items in _segments(live_tab, target_tab,
                                                           tokens):
            edits = _text_edits(live_items, target_items, live_tab.get("lists", {}),
                                target_tab.get("lists", {}))
            n += len(edits)
            requests += _edit_requests(edits, segment)
        if not requests:
            break
        rounds += 1
        if rounds > _WRITE_ATTEMPTS + 1:
            raise NotPatchable("the document kept changing under the edit")
        if write_all(requests, live["revisionId"], "the edit"):
            text_edits += n
            first = False

    # --- Phase 2: style -----------------------------------------------------
    style_edits = 0
    for attempt in range(_WRITE_ATTEMPTS):
        live = fetch()
        requests, _ = style_pass(_single_tab(live), "the text did not come out "
                                 "identical")
        if not requests:
            break
        if write_all(requests, live["revisionId"], "a style fix"):
            style_edits = len(requests)
            break
        if attempt == _WRITE_ATTEMPTS - 1:
            raise NotPatchable("the document kept changing under the edit")

    # --- Phase 3: proof -----------------------------------------------------
    if style_edits or text_edits:
        live_tab = _single_tab(fetch())
        _, leftover = style_pass(live_tab, "verification: the text differs")
        if leftover:
            raise NotPatchable(f"verification: {len(leftover)} style "
                               f"difference(s) remain, first: {leftover[0]}")
        if _render(live_tab) != _render(target_tab):
            raise NotPatchable("verification: the doc renders differently")

    return PatchResult(text_edits=text_edits, style_edits=style_edits)
