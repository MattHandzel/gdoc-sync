"""Small quality-of-life commands: open, unlink, diff, export."""

from __future__ import annotations

import difflib
import sys
import webbrowser
from pathlib import Path

from .comments import strip_comments
from .config import extract_doc_id_from_url, get_doc_id, remove_mapping
from .mdutils import strip_frontmatter
from .services import NUM_RETRIES, get_services

EXPORT_MIMES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "txt": "text/plain",
    "html": "application/zip",  # html export comes zipped with its images
    "epub": "application/epub+zip",
}
_EXPORT_EXT = {"html": "zip"}


def resolve_doc_id(target: str) -> tuple[str, Path | None]:
    """Resolve a linked local file OR a doc URL/ID to (doc_id, local_path|None)."""
    p = Path(target).expanduser()
    if p.exists() and p.is_file():
        doc_id = get_doc_id(str(p.resolve()))
        if not doc_id:
            print(f"No Google Doc linked to {p}", file=sys.stderr)
            sys.exit(1)
        return doc_id, p.resolve()
    return extract_doc_id_from_url(target), None


def open_doc(target: str) -> None:
    doc_id, _ = resolve_doc_id(target)
    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    print(url)
    webbrowser.open(url)


def unlink(local_path: Path) -> None:
    if remove_mapping(str(local_path)):
        print(f"Unlinked {local_path}")
    else:
        print(f"No mapping found for {local_path}", file=sys.stderr)
        sys.exit(1)


def diff(local_path: Path) -> None:
    """Diff the local markdown against the doc's current remote markdown.

    Exit codes mirror ``diff``: 0 = no differences, 1 = differences.
    """
    from .convert import doc_to_markdown
    from .pull import _iter_tabs, _tab_to_markdown

    doc_id, _ = resolve_doc_id(str(local_path))
    _, docs_service = get_services()
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)

    # Images aren't downloaded for a diff; a placeholder keeps the sides
    # comparable (both collapse to ![image] in _normalize_for_diff).
    placeholder = lambda object_id, uri: "image"  # noqa: E731

    tabs = list(_iter_tabs(doc.get("tabs", [])))
    if tabs:
        remote = "\n\n---\n\n".join(
            _tab_to_markdown(t[1], image_saver=placeholder) for t in tabs)
    else:
        remote, _ = doc_to_markdown(doc, image_saver=placeholder)

    local = strip_comments(strip_frontmatter(local_path.read_text()))

    lines = list(difflib.unified_diff(
        _normalize_for_diff(remote),
        _normalize_for_diff(local),
        fromfile=f"remote:{doc.get('title', doc_id)}",
        tofile=f"local:{local_path.name}",
        lineterm="",
    ))
    if lines:
        print("\n".join(lines))
        sys.exit(1)
    print("No differences (formatting-lossy comparison — see README).")


def _normalize_for_diff(markdown: str) -> list[str]:
    """Comparable lines: image paths differ per side (remote images aren't
    downloaded for a diff), so collapse them; trailing whitespace is noise."""
    import re
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "![image]", markdown)
    return [line.rstrip() for line in md.strip().splitlines()]


def export(target: str, fmt: str = "pdf", output: Path | None = None) -> None:
    """Export the doc via Drive (pdf, docx, odt, txt, html, epub)."""
    mime = EXPORT_MIMES.get(fmt)
    if not mime:
        print(f"Unsupported format {fmt!r}; choose from {', '.join(EXPORT_MIMES)}",
              file=sys.stderr)
        sys.exit(1)

    doc_id, local = resolve_doc_id(target)
    drive_service, _ = get_services()
    data = drive_service.files().export(
        fileId=doc_id, mimeType=mime
    ).execute(num_retries=NUM_RETRIES)

    ext = _EXPORT_EXT.get(fmt, fmt)
    if output is None:
        stem = local.stem if local else doc_id[:12]
        output = Path.cwd() / f"{stem}.{ext}"
    output.write_bytes(data)
    print(f"Exported {fmt} → {output}")
