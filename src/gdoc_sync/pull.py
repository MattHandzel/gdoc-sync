"""Pull a Google Doc as markdown with CriticMarkup comments."""

from __future__ import annotations

from pathlib import Path

from .comments import embed_comments, fetch_comments
from .config import set_doc_id
from .convert import doc_to_markdown
from .services import get_services


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


def _tab_to_markdown(doc_tab: dict) -> str:
    md, _ = doc_to_markdown({"body": doc_tab.get("body", {}),
                             "lists": doc_tab.get("lists", {})})
    return md


def pull(doc_id: str, output_path: Path | None = None) -> str:
    """Pull a Google Doc (all tabs) and return markdown with embedded comments."""
    drive_service, docs_service = get_services()

    # Fetch WITH tab content (else only the first tab is returned)
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()
    title = doc.get("title", "Untitled")
    revision_id = doc.get("revisionId", "")

    tabs = list(_iter_tabs(doc.get("tabs", [])))
    print(f"Pulling: {title}" + (f"  ({len(tabs)} tabs)" if len(tabs) > 1 else ""))

    # Multi-tab docs get one "# [TAB] <title>" section each.
    if len(tabs) > 1:
        parts = []
        for tab_title, doc_tab, depth in tabs:
            hashes = "#" * min(depth + 1, 6)
            parts.append(f"{hashes} [TAB] {tab_title}\n\n{_tab_to_markdown(doc_tab)}")
        markdown = "\n\n---\n\n".join(parts)
    elif tabs:
        markdown = _tab_to_markdown(tabs[0][1])
    else:  # no tab metadata at all — legacy top-level body
        markdown, _ = doc_to_markdown(doc)

    # Comments are anchored by quoted text, so tabs are fine.
    comments = fetch_comments(drive_service, doc_id)
    print(f"  {len(comments)} unresolved comment(s)")
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
        print(f"  Written to {output_path}")
    else:
        print(markdown)

    return markdown
