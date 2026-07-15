"""Pull a Google Doc as markdown with CriticMarkup comments and images."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .comments import embed_comments, fetch_comments
from .config import set_doc_id
from .convert import doc_to_markdown
from .services import NUM_RETRIES, get_services

_IMAGE_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def _iter_tabs(tabs, depth=0):
    """Yield (title, documentTab, depth) for every tab, recursing childTabs.

    A tabbed doc's real content lives in doc['tabs'][*]['documentTab'] (each of
    the same shape doc_to_markdown expects), NOT the legacy top-level body — so
    without this we'd only ever see the first tab.
    """
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "Untitled tab")
        doc_tab = tab.get("documentTab")
        if doc_tab is not None:
            yield title, doc_tab, depth
        yield from _iter_tabs(tab.get("childTabs", []), depth + 1)


def _tab_to_markdown(doc_tab: dict, image_saver=None) -> str:
    md, _ = doc_to_markdown({"body": doc_tab.get("body", {}),
                             "lists": doc_tab.get("lists", {}),
                             "inlineObjects": doc_tab.get("inlineObjects", {})},
                            image_saver=image_saver)
    return md


def _make_image_saver(output_path: Path):
    """Build a saver that downloads inline images into ``<output>-assets/``.

    Returns (save_fn, count_fn). contentUri links are short-lived and need an
    authorized request, hence the AuthorizedSession.
    """
    from google.auth.transport.requests import AuthorizedSession

    from .auth import get_credentials

    session = AuthorizedSession(get_credentials())
    assets_dir = output_path.parent / f"{output_path.stem}-assets"
    saved: dict[str, str] = {}
    counter = 0

    def save(object_id: str, content_uri: str) -> str | None:
        nonlocal counter
        if object_id in saved:
            return saved[object_id]
        try:
            resp = session.get(content_uri, timeout=30)
            if resp.status_code != 200:
                return None
            ctype = resp.headers.get("content-type", "").split(";")[0].strip()
            ext = _IMAGE_EXTS.get(ctype, ".png")
            counter += 1
            assets_dir.mkdir(parents=True, exist_ok=True)
            fname = f"img-{counter:03d}{ext}"
            (assets_dir / fname).write_bytes(resp.content)
            rel = f"{assets_dir.name}/{fname}"
            saved[object_id] = rel
            return rel
        except Exception:
            return None

    return save, (lambda: counter)


def pull(doc_id: str, output_path: Path | None = None, json_out: bool = False) -> str:
    """Pull a Google Doc (all tabs) and return markdown with embedded comments."""
    # In --json mode all progress chatter goes to stderr; stdout is the JSON.
    def say(*args):
        print(*args, file=sys.stderr if json_out else sys.stdout)

    drive_service, docs_service = get_services()

    # Fetch WITH tab content (else only the first tab is returned)
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)
    title = doc.get("title", "Untitled")
    revision_id = doc.get("revisionId", "")

    image_saver, images_saved = None, (lambda: 0)
    if output_path:
        image_saver, images_saved = _make_image_saver(output_path)

    tabs = list(_iter_tabs(doc.get("tabs", [])))
    say(f"Pulling: {title}" + (f"  ({len(tabs)} tabs)" if len(tabs) > 1 else ""))

    # Multi-tab docs get one "# [TAB] <title>" section each.
    if len(tabs) > 1:
        parts = []
        for tab_title, doc_tab, depth in tabs:
            hashes = "#" * min(depth + 1, 6)
            parts.append(f"{hashes} [TAB] {tab_title}\n\n"
                         f"{_tab_to_markdown(doc_tab, image_saver)}")
        markdown = "\n\n---\n\n".join(parts)
    elif tabs:
        markdown = _tab_to_markdown(tabs[0][1], image_saver)
    else:  # no tab metadata at all — legacy top-level body
        markdown, _ = doc_to_markdown(doc, image_saver=image_saver)

    # Comments are anchored by quoted text, so tabs are fine.
    comments = fetch_comments(drive_service, doc_id)
    say(f"  {len(comments)} unresolved comment(s)")
    markdown = embed_comments(markdown, comments)

    # Preserve existing YAML frontmatter in the local file.
    if output_path and output_path.exists():
        existing = output_path.read_text()
        if existing.startswith("---\n"):
            end_idx = existing.find("\n---\n", 4)
            if end_idx != -1:
                frontmatter = existing[: end_idx + 5]
                markdown = frontmatter + "\n" + markdown

    if output_path:
        output_path.write_text(markdown)
        set_doc_id(str(output_path), doc_id, revision_id)
        say(f"  Written to {output_path}")
        if images_saved():
            say(f"  Downloaded {images_saved()} image(s)")

    if json_out:
        payload = {
            "doc_id": doc_id,
            "title": title,
            "revision_id": revision_id,
            "tabs": max(len(tabs), 1),
            "comments": len(comments),
            "images": images_saved(),
            "output": str(output_path) if output_path else None,
        }
        if not output_path:
            payload["markdown"] = markdown
        print(json.dumps(payload))
    elif not output_path:
        print(markdown)

    return markdown
