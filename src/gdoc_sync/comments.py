#!/usr/bin/env python3
"""Fetch Google Docs comments and embed as CriticMarkup in markdown."""

from __future__ import annotations

import re

from .convert import OffsetMapping
from .services import NUM_RETRIES


def fetch_comments(drive_service, file_id: str) -> list[dict]:
    """Fetch all unresolved comments from a Google Doc via Drive API."""
    comments = []
    page_token = None

    while True:
        response = drive_service.comments().list(
            fileId=file_id,
            fields="comments(id,content,author/displayName,quotedFileContent/value,anchor,resolved,replies(content,author/displayName)),nextPageToken",
            includeDeleted=False,
            pageToken=page_token,
        ).execute(num_retries=NUM_RETRIES)

        for comment in response.get("comments", []):
            if not comment.get("resolved", False):
                comments.append(comment)

        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return comments


def embed_comments(
    markdown: str,
    comments: list[dict],
    offset_map: OffsetMapping | None = None,
) -> str:
    """Insert CriticMarkup annotations into markdown for each comment.

    Uses quotedFileContent to find the anchor position in markdown text.
    Falls back to appending at end if anchor can't be found.
    """
    if not comments:
        return markdown

    # Build list of (position_in_md, criticmarkup_string) sorted by position desc
    insertions: list[tuple[int, str]] = []

    for comment in comments:
        quoted = comment.get("quotedFileContent", {}).get("value", "")
        author = comment.get("author", {}).get("displayName", "Unknown")
        content = comment.get("content", "")

        # Build the CriticMarkup string
        cm = _format_comment(author, content, comment.get("replies", []))

        if quoted:
            # Find the quoted text in markdown
            pos = markdown.find(quoted)
            if pos != -1:
                # Insert after the quoted text
                insert_at = pos + len(quoted)
                insertions.append((insert_at, cm))
                continue

            # Try case-insensitive or normalized search
            pos = _fuzzy_find(markdown, quoted)
            if pos is not None:
                insertions.append((pos, cm))
                continue

        # Fallback: append as orphaned comment at end
        insertions.append((len(markdown), f"\n\n<!-- orphaned comment -->{cm}"))

    # Sort by position descending so insertions don't shift later positions
    insertions.sort(key=lambda x: x[0], reverse=True)

    for pos, cm_text in insertions:
        markdown = markdown[:pos] + cm_text + markdown[pos:]

    return markdown


def strip_comments(markdown: str) -> str:
    """Remove comment annotations so they don't leak into a pushed doc.

    Strips both CriticMarkup comments ({>>...<<}) and HTML comments
    (<!-- ... -->), the latter covering the `<!-- orphaned comment -->` markers
    that embed_comments() inserts when a pulled comment's anchor can't be found.
    """
    import re
    md = re.sub(r"\{>>.*?<<\}", "", markdown, flags=re.DOTALL)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL)
    return md


def _format_comment(author: str, content: str, replies: list[dict]) -> str:
    """Format a comment + replies as CriticMarkup."""
    # Sanitize content (no newlines, no CriticMarkup delimiters)
    content = content.replace("\n", " ").replace("{>>", "").replace("<<}", "")
    parts = [f"{author}: {content}"]

    for reply in replies:
        r_author = reply.get("author", {}).get("displayName", "Unknown")
        r_content = reply.get("content", "").replace("\n", " ")
        r_content = r_content.replace("{>>", "").replace("<<}", "")
        parts.append(f"{r_author}: {r_content}")

    return "{>>" + " | ".join(parts) + "<<}"


def _fuzzy_find(markdown: str, quoted: str) -> int | None:
    """Try to find quoted text with normalized whitespace."""
    import re
    # Normalize whitespace in both
    norm_md = re.sub(r"\s+", " ", markdown)
    norm_q = re.sub(r"\s+", " ", quoted.strip())

    pos = norm_md.find(norm_q)
    if pos == -1:
        return None

    # Map back to original markdown position (approximate)
    # Count characters up to pos in normalized, map to original
    orig_pos = 0
    norm_pos = 0
    for ch in markdown:
        if norm_pos >= pos + len(norm_q):
            break
        if norm_pos >= pos and orig_pos == 0:
            return orig_pos + len(quoted)
        if ch.isspace():
            if norm_pos == 0 or not markdown[orig_pos - 1].isspace():
                norm_pos += 1
        else:
            norm_pos += 1
        orig_pos += 1

    return orig_pos


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
_ACTION_RE = re.compile(
    r"^\s*(reply|resolve|comment)\s*(?::\s*(.*))?\s*$",
    re.DOTALL | re.IGNORECASE,
)


def parse_comment_actions(markdown: str) -> list[dict]:
    """Extract action markers, each bound to the nearest preceding pulled comment.

    Returns dicts: {type, text, target (inner text of the pulled comment the
    action applies to, or None), context (preceding line, for new comments)}.
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
            context = before.rsplit("\n", 1)[-1].strip()[-120:]
        actions.append({
            "type": kind,
            "text": text,
            "target": last_pulled if kind in ("reply", "resolve") else None,
            "context": context,
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


def apply_comment_actions(drive_service, doc_id: str, markdown: str) -> list[str]:
    """Execute reply/resolve/comment markers found in ``markdown`` against the doc.

    Returns human-readable result lines; API failures become warnings rather
    than aborting the caller's push.
    """
    from googleapiclient.errors import HttpError

    actions = parse_comment_actions(markdown)
    if not actions:
        return []

    remote = fetch_comments(drive_service, doc_id)
    results = []

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
        except HttpError as e:
            results.append(f"Warning: comment action failed ({action['type']}): {e}")

    return results
