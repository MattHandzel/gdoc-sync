#!/usr/bin/env python3
"""Fetch Google Docs comments and embed as CriticMarkup in markdown."""

from __future__ import annotations

from .convert import OffsetMapping


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
        ).execute()

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
