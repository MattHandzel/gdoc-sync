"""Create a new Google Doc from a local markdown file, share it, copy the URL.

Pipeline:
    1. Strip YAML frontmatter and CriticMarkup comments
    2. Convert markdown → docx via pandoc (full fidelity)
    3. Upload to Drive as a Google Doc (auto-converts)
    4. Repair table borders, apply font + color theme
    5. Set the sharing permission (default: anyone with link can comment)
    6. Save the local→doc-id mapping so `push`/`pull` work later
    7. Copy the URL to the clipboard and print it

Steps 2-4 have a second form. A file whose top-level sections are
``# [TAB] <title>`` asks for a multi-tab document, and Drive's importer cannot
build one — it replaces a whole document and cannot address a tab. Such a file
takes :func:`_create_tabbed` instead, which makes an empty doc through the Docs
API and writes each section into its own tab (see :mod:`.tabs`). Everything
from step 5 on is the same either way.
"""

from __future__ import annotations

import tempfile
import webbrowser
from pathlib import Path

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from .comments import strip_comments
from .config import (
    get_clipboard_command,
    get_clipboard_default,
    get_font,
    get_theme,
    set_doc_id,
)
from .highlight import highlight_theme_for
from .mdutils import copy_to_clipboard, derive_title, pandoc_to_docx, strip_frontmatter
from .refdoc import styled_reference_docx
from .services import NUM_RETRIES, get_services
from .style import apply_document_styling
from .sync import record_sync_baseline
from .tabs import split_tab_sections, write_sections

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


def _create_from_docx(drive_service, docs_service, title: str, body_md: str, *,
                      font: str | None, theme: str | None,
                      resource_dir: Path) -> tuple[str, str]:
    """The ordinary path: pandoc → docx → Drive import → styling."""
    # Bake the theme into the docx's style definitions so the new doc's named
    # styles genuinely carry it (see refdoc) rather than having colour painted
    # over pandoc's blue defaults afterwards.
    reference_doc = styled_reference_docx(font, theme)
    highlight_style = highlight_theme_for(theme)

    print("Converting markdown → docx via pandoc...")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / "doc.docx"
        pandoc_to_docx(body_md, docx_path, resource_dir=resource_dir,
                       reference_doc=reference_doc,
                       highlight_style=highlight_style)

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
    except HttpError as e:
        print(f"  Warning: could not apply styling: {e}")

    return doc_id, url


def _create_tabbed(drive_service, docs_service, title: str, sections, *,
                   font: str | None, theme: str | None,
                   resource_dir: Path) -> tuple[str, str]:
    """The tabbed path: an empty document, then one tab per section.

    Created through the Docs API rather than a Drive import, because the
    import would have to be replaced tab by tab immediately afterwards. The
    document arrives with a single placeholder tab, which the first section
    adopts rather than being added alongside.
    """
    print(f"Creating Google Doc: {title}  ({len(sections)} tabs)")
    created = docs_service.documents().create(
        body={"title": title}).execute(num_retries=NUM_RETRIES)
    doc_id = created["documentId"]

    write_sections(docs_service, drive_service, doc_id, sections,
                   font=font, theme=theme, resource_dir=resource_dir,
                   adopt_placeholder=True, say=print)

    return doc_id, f"https://docs.google.com/document/d/{doc_id}/edit"


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

    # A file whose top-level sections are `# [TAB] <title>` asks for a tabbed
    # document, which Drive's docx importer cannot produce — see .tabs.
    sections = split_tab_sections(body_md, say=print)
    if sections:
        doc_id, url = _create_tabbed(
            drive_service, docs_service, title, sections,
            font=font, theme=theme, resource_dir=local_path.parent)
    else:
        doc_id, url = _create_from_docx(
            drive_service, docs_service, title, body_md,
            font=font, theme=theme, resource_dir=local_path.parent)

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
            # Store the new doc's revision alongside the mapping. Without it
            # the file looks to `push` exactly like one that was only ever
            # `link`ed — never pulled, never pushed — and the first push
            # after a create would stop and ask whether to replace a document
            # it had itself just written.
            set_doc_id(str(local_path), doc_id, _revision_of(docs_service, doc_id))
            print(f"  Mapped {local_path.name} → {doc_id[:12]}...")
            # The file and the brand-new doc agree right now, which is the one
            # moment a merge ancestor can be recorded for free. Without it the
            # first sync/watch sees two texts that differ only by the lossy
            # round trip and has to ask which side to keep.
            record_sync_baseline(local_path, doc_id, raw_md)
        except Exception as e:
            print(f"  Warning: could not save mapping: {e}")

    print(f"  URL: {url}")

    if copy:
        ok, tool = copy_to_clipboard(url, command=get_clipboard_command())
        if ok:
            print(f"  Copied to clipboard via {tool}")
        else:
            print("  Warning: no clipboard tool found. Install wl-clipboard, xclip, "
                  "or xsel — or set `clipboard_command:` in your config.")

    if open_in_browser:
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"  Warning: could not open browser: {e}")

    return url


def _revision_of(docs_service, doc_id: str) -> str:
    """The doc's current revisionId, or "" if it cannot be read.

    Best-effort like everything else on the create path: an unreadable
    revision costs one confirmation on the first push, and must not turn a
    successful create into an error.
    """
    try:
        return docs_service.documents().get(
            documentId=doc_id, fields="revisionId"
        ).execute(num_retries=NUM_RETRIES).get("revisionId", "")
    except Exception:  # noqa: BLE001
        return ""
