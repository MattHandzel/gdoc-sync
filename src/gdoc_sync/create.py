"""Create a new Google Doc from a local markdown file, share it, copy the URL.

Pipeline:
    1. Strip YAML frontmatter and CriticMarkup comments
    2. Convert markdown → docx via pandoc (full fidelity)
    3. Upload to Drive as a Google Doc (auto-converts)
    4. Repair table borders, apply font + color theme
    5. Set the sharing permission (default: anyone with link can comment)
    6. Save the local→doc-id mapping so `push`/`pull` work later
    7. Copy the URL to the clipboard and print it
"""

from __future__ import annotations

import tempfile
import webbrowser
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .comments import strip_comments
from .config import get_clipboard_default, get_font, get_theme, set_doc_id
from .mdutils import copy_to_clipboard, derive_title, pandoc_to_docx, strip_frontmatter
from .services import NUM_RETRIES, get_services
from .style import apply_styles, apply_table_borders

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

SHARE_ROLES = {"edit": "writer", "comment": "commenter", "view": "reader"}


def parse_share_with(entry: str) -> tuple[str, str]:
    """Parse an ``email[:view|comment|edit]`` spec into (email, Drive role)."""
    email, _, role_word = entry.partition(":")
    email = email.strip()
    role_word = (role_word.strip().lower() or "comment")
    if "@" not in email:
        raise ValueError(f"not an email address: {email!r}")
    role = SHARE_ROLES.get(role_word)
    if not role:
        raise ValueError(f"role must be view|comment|edit, got {role_word!r}")
    return email, role


def create_doc(
    local_path: Path,
    *,
    title: str | None = None,
    font: str | None = None,
    theme: str | None = None,
    share_mode: str = "comment",  # private | view | comment | edit
    share_with: list[str] | None = None,  # "email[:view|comment|edit]"
    copy: bool | None = None,
    save_mapping: bool = True,
    open_in_browser: bool = False,
) -> str:
    """Create a new Google Doc from a markdown file. Returns the doc URL."""
    drive_service, docs_service = get_services()

    raw_md = local_path.read_text()

    if not title:
        title = derive_title(raw_md, local_path.stem)
    if font is None:
        font = get_font()
    if theme is None:
        theme = get_theme()
    if copy is None:
        copy = get_clipboard_default()

    body_md = strip_comments(strip_frontmatter(raw_md))

    print("Converting markdown → docx via pandoc...")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / "doc.docx"
        pandoc_to_docx(body_md, docx_path, resource_dir=local_path.parent)

        print(f"Creating Google Doc: {title}")
        media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
        created = drive_service.files().create(
            body={"name": title, "mimeType": "application/vnd.google-apps.document"},
            media_body=media,
            fields="id,webViewLink,name",
        ).execute(num_retries=NUM_RETRIES)

    doc_id = created["id"]
    url = created.get("webViewLink") or f"https://docs.google.com/document/d/{doc_id}/edit"

    # pandoc tables import without visible borders — set them explicitly.
    try:
        n = apply_table_borders(docs_service, doc_id)
        if n:
            print(f"  Applied visible borders to {n} table(s)")
    except HttpError as e:
        print(f"  Warning: could not apply table borders: {e}")

    try:
        if apply_styles(docs_service, doc_id, font=font, theme=theme):
            print(f"  Applied font: {font}" + (f" + theme: {theme}" if theme else ""))
    except HttpError as e:
        print(f"  Warning: could not apply styling: {e}")

    if share_mode != "private":
        role = SHARE_ROLES.get(share_mode, "reader")
        try:
            drive_service.permissions().create(
                fileId=doc_id,
                body={"type": "anyone", "role": role},
                fields="id",
            ).execute(num_retries=NUM_RETRIES)
            print(f"  Shared: anyone with link can {role}")
        except HttpError as e:
            print(f"  Warning: could not set sharing permission: {e}")
    else:
        print("  Kept private")

    for entry in share_with or []:
        try:
            email, role = parse_share_with(entry)
            drive_service.permissions().create(
                fileId=doc_id,
                body={"type": "user", "role": role, "emailAddress": email},
                fields="id",
            ).execute(num_retries=NUM_RETRIES)
            print(f"  Shared with {email} ({role})")
        except (ValueError, HttpError) as e:
            print(f"  Warning: could not share with {entry}: {e}")

    if save_mapping:
        try:
            set_doc_id(str(local_path), doc_id)
            print(f"  Mapped {local_path.name} → {doc_id[:12]}...")
        except Exception as e:
            print(f"  Warning: could not save mapping: {e}")

    print(f"  URL: {url}")

    if copy:
        ok, tool = copy_to_clipboard(url)
        if ok:
            print(f"  Copied to clipboard via {tool}")
        else:
            print("  Warning: no clipboard tool found (tried wl-copy, xclip, pbcopy)")

    if open_in_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"  Warning: could not open browser: {e}")

    return url
