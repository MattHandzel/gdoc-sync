"""Push local markdown to a linked Google Doc.

Pushes go through the same pandoc → docx pipeline as `create`; the existing
doc's content is replaced in place via Drive's files().update, which preserves
the doc id, URL, and sharing.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .comments import strip_comments
from .config import get_doc_id, get_font, get_revision, get_theme, set_revision
from .create import DOCX_MIME
from .mdutils import pandoc_to_docx, strip_frontmatter
from .services import get_services
from .style import apply_styles, apply_table_borders


def push(local_path: Path, *, yes: bool = False, font: str | None = None,
         theme: str | None = None) -> None:
    """Push a markdown file to its linked Google Doc."""
    doc_id = get_doc_id(str(local_path))
    if not doc_id:
        print(f"No Google Doc linked to {local_path}", file=sys.stderr)
        print("Link one with: gdoc-sync link <file> <doc_url>", file=sys.stderr)
        sys.exit(1)

    drive_service, docs_service = get_services()

    # Optimistic locking: warn when the remote changed since our last pull/push.
    doc = docs_service.documents().get(documentId=doc_id).execute()
    current_rev = doc.get("revisionId", "")
    stored_rev = get_revision(str(local_path))

    if stored_rev and current_rev != stored_rev:
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
    body_md = strip_comments(strip_frontmatter(markdown))

    title = doc.get("title", "Untitled")
    print(f"Pushing to: {title}")

    print("Converting markdown → docx via pandoc...")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / "doc.docx"
        pandoc_to_docx(body_md, docx_path)
        media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
        drive_service.files().update(fileId=doc_id, media_body=media).execute()

    try:
        n = apply_table_borders(docs_service, doc_id)
        if n:
            print(f"  Applied visible borders to {n} table(s)")
    except HttpError as e:
        print(f"  Warning: could not apply table borders: {e}")

    if font is None:
        font = get_font()
    if theme is None:
        theme = get_theme()
    try:
        if apply_styles(docs_service, doc_id, font=font, theme=theme):
            print(f"  Applied font: {font}" + (f" + theme: {theme}" if theme else ""))
    except Exception as e:
        print(f"  Warning: could not apply styling: {e}")

    new_rev = docs_service.documents().get(
        documentId=doc_id, fields="revisionId"
    ).execute().get("revisionId", current_rev)
    set_revision(str(local_path), new_rev)
    print("  Pushed successfully.")
