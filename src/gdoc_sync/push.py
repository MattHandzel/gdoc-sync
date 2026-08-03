"""Push local markdown to a linked Google Doc.

Pushes go through the same pandoc → docx pipeline as `create`; the existing
doc's content is replaced in place via Drive's files().update, which preserves
the doc id, URL, and sharing.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from .comments import apply_comment_actions, strip_comments
from .config import get_doc_id, get_font, get_revision, get_theme, set_revision
from .create import DOCX_MIME
from .highlight import highlight_theme_for
from .mdutils import pandoc_to_docx, strip_frontmatter
from .refdoc import styled_reference_docx
from .services import NUM_RETRIES, get_services
from .style import apply_document_styling


def push(local_path: Path, *, yes: bool = False, font: str | None = None,
         theme: str | None = None, merged: bool = False) -> None:
    """Push a markdown file to its linked Google Doc.

    ``merged`` says the caller is the sync engine and has already merged the
    doc's changes into this file. The drift warning below is then not just
    noise but actively wrong — nothing is being discarded — so it is skipped.
    """
    doc_id = get_doc_id(str(local_path))
    if not doc_id:
        print(f"No Google Doc linked to {local_path}", file=sys.stderr)
        print("Link one with: gdoc-sync link <file> <doc_url>", file=sys.stderr)
        sys.exit(1)

    drive_service, docs_service = get_services()

    # Optimistic locking: warn when the remote changed since our last pull/push.
    doc = docs_service.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)
    current_rev = doc.get("revisionId", "")
    stored_rev = get_revision(str(local_path))

    if stored_rev and current_rev != stored_rev and not merged:
        print("WARNING: Google Doc has been modified since last pull.")
        print(f"  Stored revision:  {stored_rev[:16]}...")
        print(f"  Current revision: {current_rev[:16]}...")
        if yes:
            print("  --yes given; overwriting remote.")
        elif not sys.stdin.isatty():
            print("Refusing to overwrite non-interactively without --yes.", file=sys.stderr)
            sys.exit(2)
        else:
            response = input("Overwrite remote? [y/N] ")
            if response.lower() != "y":
                print("Aborted.")
                sys.exit(1)

    markdown = local_path.read_text()

    # {>>reply: ...<<} / {>>resolve<<} / {>>comment: ...<<} markers sync back
    # to the doc's comment threads before being stripped from the content.
    for line in apply_comment_actions(drive_service, doc_id, markdown):
        print(f"  {line}")

    body_md = strip_comments(strip_frontmatter(markdown))

    title = doc.get("title", "Untitled")
    print(f"Pushing to: {title}")

    if font is None:
        font = get_font()
    if theme is None:
        theme = get_theme()

    # Styling goes into the docx's own style definitions where possible, so the
    # Google Doc's named styles carry the theme instead of having it painted
    # over the top (see refdoc). Falls back to API-side styling if that fails.
    reference_doc = styled_reference_docx(font, theme)
    highlight_style = highlight_theme_for(theme)

    print("Converting markdown → docx via pandoc...")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / "doc.docx"
        pandoc_to_docx(body_md, docx_path, resource_dir=local_path.parent,
                       reference_doc=reference_doc,
                       highlight_style=highlight_style)
        media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
        drive_service.files().update(fileId=doc_id, media_body=media).execute(num_retries=NUM_RETRIES)

    # Styling and table borders go up as one fetch and one batch; as two calls
    # apiece this was four sequential round trips, about a second of the push.
    try:
        baked = reference_doc is not None
        styled, n, n_callouts = apply_document_styling(
            docs_service, doc_id, font=font, theme=theme, baked=baked)
        if n:
            print(f"  Applied visible borders to {n} table(s)")
        if n_callouts:
            print(f"  Styled {n_callouts} callout(s)")
        if styled:
            where = "in the doc's named styles" if baked else "to the doc's text"
            print(f"  Applied font: {font}" + (f" + theme: {theme}" if theme else "")
                  + f" ({where})")
    except Exception as e:
        print(f"  Warning: could not apply styling: {e}")

    new_rev = docs_service.documents().get(
        documentId=doc_id, fields="revisionId"
    ).execute(num_retries=NUM_RETRIES).get("revisionId", current_rev)
    set_revision(str(local_path), new_rev)

    # A plain push leaves both sides in agreement, so it is a free chance to
    # record the merge ancestor. The engine manages its own baselines, so skip
    # this when the push came from there.
    if not merged:
        from .sync import record_sync_baseline
        record_sync_baseline(local_path, doc_id, markdown)

    print("  Pushed successfully.")
