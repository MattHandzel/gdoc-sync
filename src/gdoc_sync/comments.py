#!/usr/bin/env python3
"""Fetch Google Docs comments and embed as CriticMarkup in markdown."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import NamedTuple

from .anchors import docx_comment_ranges, find_ranges, highlight_pieces, plain_quote, project
from .convert import OffsetMapping
from .services import NUM_RETRIES


def fetch_comments(drive_service, file_id: str, *, quotes: bool = True) -> list[dict]:
    """Fetch all unresolved comments from a Google Doc via Drive API.

    With ``quotes``, each comment's quote is replaced by what it covers in
    the doc *now*, read from the doc's .docx export (see :func:`_live_quotes`).
        """
    comments = []
    page_token = None

    while True:
        response = drive_service.comments().list(
            fileId=file_id,
            fields="comments(id,content,author/displayName,quotedFileContent(mimeType,value),anchor,resolved,replies(content,author/displayName)),nextPageToken",
            includeDeleted=False,
            pageToken=page_token,
        ).execute(num_retries=NUM_RETRIES)

        for comment in response.get("comments", []):
            if not comment.get("resolved", False):
                # Drive sends the selection HTML-escaped (`wasn&#39;t`); turn
                # it into the plain text the doc actually shows, once, here.
                quoted = comment.get("quotedFileContent")
                if quoted and quoted.get("value"):
                    comment["quotedFileContent"] = {
                        "mimeType": "text/plain", "value": plain_quote(quoted)}
                comments.append(comment)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if quotes:
        _live_quotes(drive_service, file_id, comments)
    return comments


# Set on a comment whose "quote" is only where it sits, not what it selected
# (left at a cursor): it is placed there, and nothing is highlighted.
POINT_KEY = "_gdoc_sync_point"
# Set on a comment that is still open but no longer attached to any text in
# the doc, because everything it selected was deleted. Google Docs shows it
# with no highlight; the markdown lists it at the end, with what it was on.
ORPHAN_KEY = "_gdoc_sync_orphaned"

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _live_quotes(drive_service, file_id: str, comments: list[dict]) -> None:
    """Set each anchored comment's quote to the text it covers in the doc now.

    Drive's ``quotedFileContent`` is a snapshot of the selection taken when
    the comment was made, and it never changes. The anchor itself does: it
    grows over words typed inside it and shrinks as its words are deleted,
    and Google Docs highlights whatever it covers today. Placing a comment by
    the snapshot highlights the wrong words once the text is edited, or none.

    The doc's .docx export carries the live anchors, as ``commentRangeStart``
    / ``commentRangeEnd`` around the covered runs, so it is fetched once and
    each comment takes its quote from there. A comment the export has no
    range for is attached to nothing (its text is gone; Docs counts it in no
    tab) and is marked orphaned. If the export fails, the snapshots stand.
    """
    anchored = [c for c in comments if c.get("anchor")]
    if not anchored:
        return
    try:
        data = drive_service.files().export(
            fileId=file_id, mimeType=_DOCX_MIME).execute(num_retries=NUM_RETRIES)
        ranges = docx_comment_ranges(data)
    except Exception:  # noqa: BLE001 — the snapshots are still usable
        return
    for comment in anchored:
        author = (comment.get("author") or {}).get("displayName", "")
        want = _norm(comment.get("content", ""))
        snapshot = plain_quote(comment.get("quotedFileContent"))
        same = [i for i, (who, text, covered, _) in enumerate(ranges)
                if who == author and _norm(text) == want and covered.strip()]
        if not same:
            comment[ORPHAN_KEY] = True
            continue
        # Two comments can say the same thing ("too long"); the one whose
        # range reads most like this comment's snapshot is this comment's.
        best = max(same, key=lambda i: SequenceMatcher(
            None, _norm(ranges[i][2]), _norm(snapshot), autojunk=False).ratio())
        _, _, covered, point = ranges.pop(best)
        comment["quotedFileContent"] = {"mimeType": "text/plain", "value": covered}
        if point:
            comment[POINT_KEY] = True


def embed_comments(
    markdown: str,
    comments: list[dict],
    offset_map: OffsetMapping | None = None,
) -> str:
    """Insert CriticMarkup annotations into markdown for each comment.

    The text a comment selected is wrapped in ``{==...==}`` and the comment
    follows it, the way Google Docs shows it::

        The {==quick brown fox==}{>>Ada: nice phrase<<} jumps.

    A selection is not always one stretch of the file. One that spans
    paragraphs, table cells or list items becomes one highlight per block;
    one whose middle was edited since keeps a highlight on each part that is
    still there. Either way the comment follows the last piece::

        {==First paragraph.==}

        {==Last paragraph.==}{>>Ada: both of these<<}

    Highlights never nest: where two comments' selections overlap, the
    highlight is cut at each comment instead. A comment left at a cursor, or
    a doc-level note, is placed with nothing highlighted.

    The quote is the doc's plain text and the markdown is not, so the search
    runs over what a reader sees of the markdown (see :mod:`.anchors`) rather
    than its raw source; a comment is appended at the end as orphaned only
    when no recognisable part of its quote is left anywhere in the file, or
    it has no quote at all.
    """
    if not comments:
        return markdown

    n = len(markdown)
    covered = bytearray(n)
    markers: dict[int, list[str]] = {}
    orphans: list[str] = []
    projection = None

    for comment in comments:
        quoted = plain_quote(comment.get("quotedFileContent"))
        point = bool(comment.get(POINT_KEY))
        if not quoted:
            # A doc-level comment made with `{>>comment: ...<<}` goes back
            # after the line it was written on.
            m = _CONTEXT_RE.match(comment.get("content", ""))
            quoted = project(m.group(1)).text if m else ""
            point = True
        author = comment.get("author", {}).get("displayName", "Unknown")
        cm = _format_comment(author, comment.get("content", ""),
                             comment.get("replies", []))

        pieces: list[tuple[int, int]] = []
        pos = None
        if quoted and not comment.get(ORPHAN_KEY):
            if projection is None:
                projection = project(markdown)
            for start, end in find_ranges(projection, quoted):
                pieces += highlight_pieces(projection, start, end)
                pos = end
            if pos is None:
                # Literal text the projection drops as markup, like a quote
                # that itself contains `<!-- ... -->`.
                at = markdown.find(quoted)
                pos = at + len(quoted) if at != -1 else None
        if pos is None:
            orphans.append(f"\n\n{_orphan_note(quoted)}{cm}")
            continue
        if not point and pieces:
            for start, end in pieces:
                covered[start:end] = b"\x01" * (end - start)
            pos = pieces[-1][1]
        markers.setdefault(pos, []).append(cm)

    out: list[str] = []
    lit = False
    for i in range(n + 1):
        here = markers.get(i)
        if here:
            if lit:
                out.append("==}")
                lit = False
            out.extend(here)
        want = i < n and covered[i]
        if want and not lit:
            out.append("{==")
            lit = True
        elif lit and not want:
            out.append("==}")
            lit = False
        if i < n:
            out.append(markdown[i])
    return "".join(out) + "".join(orphans)


def _orphan_note(quoted: str) -> str:
    """``<!-- orphaned comment ... -->``, naming the text it was on if known."""
    if not quoted:
        return "<!-- orphaned comment -->"
    was = " ".join(quoted.split()).replace("--", "–")
    if len(was) > 200:
        was = was[:199] + "…"
    return f"<!-- orphaned comment, was on: “{was}” -->"


def strip_comments(markdown: str) -> str:
    """Remove comment annotations so they don't leak into a pushed doc.

    Strips CriticMarkup comments ({>>...<<}), the highlight delimiters around
    the text they selected ({==...==}, text kept), and HTML comments
    (<!-- ... -->), the latter covering the `<!-- orphaned comment -->` markers
    that embed_comments() inserts when a pulled comment's anchor can't be found.
    """
    md = re.sub(r"\{>>.*?<<\}", "", markdown, flags=re.DOTALL)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
    return strip_highlights(md)


def strip_highlights(markdown: str) -> str:
    """Drop the ``{==`` / ``==}`` around highlighted text, keeping the text."""
    return markdown.replace("{==", "").replace("==}", "")


# A display name is *remote, attacker-controlled text*. It lands at the very
# front of the marker span, which is exactly where `_ACTION_RE` looks for a
# verb — so a Google account called "resolve" would make every pulled comment
# read as `{>>resolve: <their words><<}` and the next push would execute it
# against somebody else's thread. Names that would parse as an action are
# quoted so the span can never match; see `_sanitize_author`.
_AUTHOR_ACTION_RE = re.compile(r"^\s*(?:reply|resolve|comment)\s*$", re.IGNORECASE)


def _sanitize_author(name: str | None) -> str:
    """Make a remote display name safe to interpolate into a marker span."""
    cleaned = (name or "").replace("{>>", "").replace("<<}", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "Unknown"
    if _AUTHOR_ACTION_RE.match(cleaned):
        # Straight double quotes are enough: `"resolve": ok` no longer starts
        # with a bare verb, so `_ACTION_RE` cannot match the span.
        return f'"{cleaned}"'
    return cleaned


def count_anchored_comments(comments: list[dict]) -> int:
    """How many of ``comments`` are attached to a span of the document text."""
    return sum(
        1 for c in comments
        if (c.get("quotedFileContent") or {}).get("value")
    )


def anchored_push_warning(comments: list[dict]) -> str | None:
    """Warn that a whole-body push will detach every anchored comment.

    Both push paths replace the document's content, so Google Docs shows
    "Original content deleted" on every text-anchored thread afterwards. The
    threads themselves survive in Drive and re-attach on the next pull, so
    this is a warning and not a refusal — the real fix is a paragraph-level
    diff push.
    """
    n = count_anchored_comments(comments)
    if not n:
        return None
    return (
        f"Warning: {n} anchored comment(s) will lose their anchor in Google "
        "Docs after this push (they stay in the comments panel and re-attach "
        "on the next pull)."
    )


def _format_comment(author: str, content: str, replies: list[dict]) -> str:
    """Format a comment + replies as CriticMarkup."""
    # Sanitize content (no newlines, no CriticMarkup delimiters)
    content = content.replace("\n", " ").replace("{>>", "").replace("<<}", "")
    parts = [f"{_sanitize_author(author)}: {content}"]

    for reply in replies:
        r_author = reply.get("author", {}).get("displayName", "Unknown")
        r_content = reply.get("content", "").replace("\n", " ")
        r_content = r_content.replace("{>>", "").replace("<<}", "")
        parts.append(f"{_sanitize_author(r_author)}: {r_content}")

    return "{>>" + " | ".join(parts) + "<<}"


# ---------------------------------------------------------------------------
# Comment actions written in markdown (reply / resolve / new comment)
# ---------------------------------------------------------------------------
#
# The Drive API cannot create text-anchored comments on Google Docs (the
# anchor is accepted but silently ignored), so the markdown → doc direction
# only offers what actually works:
#
#   {>>reply: thanks, fixed<<}     after a pulled comment → posts a reply
#   {>>resolve<<}                  after a pulled comment → resolves it
#   {>>resolve: done in r2<<}      resolve with a closing reply
#   {>>comment: needs a source<<}  anywhere → new unanchored doc-level
#                                  comment quoting the preceding line

_SPAN_RE = re.compile(r"\{>>(.*?)<<\}", re.DOTALL)
# How a `{>>comment: ...<<}` doc-level comment records where it was written.
_CONTEXT_RE = re.compile(r"Re: “(.+?)”\n\n", re.DOTALL)
_ACTION_RE = re.compile(
    r"^\s*(reply|resolve|comment)\s*(?::\s*(.*))?\s*$",
    re.DOTALL | re.IGNORECASE,
)


def parse_comment_actions(markdown: str) -> list[dict]:
    """Extract action markers, each bound to the nearest preceding pulled comment.

    Returns dicts: {type, text, target (inner text of the pulled comment the
    action applies to, or None), context (preceding line, for new comments),
    span ((start, end) offsets of the whole ``{>>...<<}`` marker, so a caller
    that applied the action can cut it back out of the file)}.
    """
    actions = []
    last_pulled: str | None = None

    for m in _SPAN_RE.finditer(markdown):
        inner = m.group(1)
        am = _ACTION_RE.match(inner)
        if not am:
            last_pulled = inner.strip()
            continue

        kind = am.group(1).lower()
        text = (am.group(2) or "").strip()
        context = ""
        if kind == "comment":
            before = _SPAN_RE.sub("", markdown[: m.start()]).rstrip()
            lit = re.search(r"\{==((?:(?!\{==).)*?)==\}$", before, re.DOTALL)
            if lit:
                # `{==these words==}{>>comment: ...<<}` quotes exactly them.
                context = " ".join(lit.group(1).split())[-500:]
            else:
                before = strip_highlights(before)
                context = before.rsplit("\n", 1)[-1].strip()[-120:]
        actions.append({
            "type": kind,
            "text": text,
            "target": last_pulled if kind in ("reply", "resolve") else None,
            "context": context,
            "span": (m.start(), m.end()),
        })

    return actions


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def match_comment(target_inner: str, remote_comments: list[dict]) -> dict | None:
    """Find the remote comment whose pulled CriticMarkup form matches ``target_inner``.

    Prefix matching in both directions tolerates replies added remotely after
    the pull (remote grows) or reply markers the user already wrote (local grows).
    """
    want = _norm(target_inner)
    for comment in remote_comments:
        author = comment.get("author", {}).get("displayName", "Unknown")
        formatted = _format_comment(author, comment.get("content", ""),
                                    comment.get("replies", []))
        have = _norm(formatted[3:-3])  # strip {>> <<}
        if want == have or want.startswith(have) or have.startswith(want):
            return comment
    return None


class CommentActionResult(NamedTuple):
    """What one :func:`apply_comment_actions` pass did.

    ``applied`` holds the ``(start, end)`` span of every marker whose API call
    actually succeeded — and only those, so a skipped or failed action keeps
    its marker and can be retried. ``remote`` is the comment list that was
    fetched to resolve the actions, or ``None`` when there were no actions and
    nothing was fetched; callers reuse it instead of listing comments twice.
    """

    lines: list[str]
    applied: list[tuple[int, int]]
    remote: list[dict] | None


def consume_action_markers(markdown: str, spans: list[tuple[int, int]]) -> str:
    """Cut the given marker spans out of ``markdown``.

    Applied actions must not survive in the file: re-pushing an unchanged file
    would otherwise post the same reply or comment again, every time.
    """
    for start, end in sorted(spans, reverse=True):
        markdown = markdown[:start] + markdown[end:]
    return markdown


def apply_comment_actions(drive_service, doc_id: str,
                          markdown: str) -> CommentActionResult:
    """Execute reply/resolve/comment markers found in ``markdown`` against the doc.

    Returns human-readable result lines, the spans of the markers that were
    applied, and the comment list that was fetched. API failures become
    warnings rather than aborting the caller's push — and, because their span
    is not reported as applied, their marker stays in the file.
    """
    from googleapiclient.errors import HttpError

    actions = parse_comment_actions(markdown)
    if not actions:
        return CommentActionResult([], [], None)

    remote = fetch_comments(drive_service, doc_id, quotes=False)
    results: list[str] = []
    applied: list[tuple[int, int]] = []

    for action in actions:
        try:
            if action["type"] in ("reply", "resolve"):
                if not action["target"]:
                    results.append(
                        f"Warning: {{>>{action['type']}<<}} has no preceding "
                        "pulled comment to act on — skipped")
                    continue
                target = match_comment(action["target"], remote)
                if not target:
                    results.append(
                        f"Warning: could not match '{action['target'][:60]}' to an "
                        "unresolved doc comment — skipped")
                    continue
                body: dict = {}
                if action["type"] == "resolve":
                    body["action"] = "resolve"
                if action["text"]:
                    body["content"] = action["text"]
                elif action["type"] == "reply":
                    results.append("Warning: empty {>>reply:<<} — skipped")
                    continue
                drive_service.replies().create(
                    fileId=doc_id, commentId=target["id"], body=body, fields="id",
                ).execute(num_retries=NUM_RETRIES)
                verb = "Resolved" if action["type"] == "resolve" else "Replied to"
                results.append(f"{verb}: {action['target'][:60]}")
                applied.append(action["span"])

            elif action["type"] == "comment":
                if not action["text"]:
                    results.append("Warning: empty {>>comment:<<} — skipped")
                    continue
                content = action["text"]
                if action["context"]:
                    content = f"Re: “{action['context']}”\n\n{content}"
                drive_service.comments().create(
                    fileId=doc_id, body={"content": content}, fields="id",
                ).execute(num_retries=NUM_RETRIES)
                results.append(f"New doc-level comment: {action['text'][:60]}")
                applied.append(action["span"])
        except HttpError as e:
            results.append(f"Warning: comment action failed ({action['type']}): {e}")

    return CommentActionResult(results, applied, remote)
