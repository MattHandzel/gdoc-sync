"""Pull a Google Doc as markdown with CriticMarkup comments and images."""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .callouts import restore_callout_spellings
from .comments import embed_comments, fetch_comments
from .config import atomic_write, set_doc_id
from .convert import doc_to_markdown, restore_fence_languages
from .mathmd import restore_math
from .services import NUM_RETRIES, get_services
from .syncstate import backup_file, clear_conflict, set_bases

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


@dataclass
class RenderedDoc:
    """A Google Doc rendered to markdown, with nothing written to disk."""

    markdown: str
    revision_id: str
    title: str
    tabs: int
    comments: int
    images: int


def render_doc(
    doc_id: str,
    *,
    asset_path: Path | None = None,
    local_text: str | None = None,
    say=lambda *_: None,
) -> RenderedDoc:
    """Fetch a doc and convert it to markdown without touching the local file.

    Split out of :func:`pull` so the sync engine can ask "what does the doc say
    right now?" as a pure query. Comparing that against the last-synced
    snapshot is how a real remote edit is told apart from a revisionId bump
    (Google rewrites the revision on autosave and presence changes, so the id
    alone is not evidence that anything changed).

    ``asset_path`` is the markdown file inline images should be saved beside;
    omit it to skip image download entirely.

    ``local_text`` supplies the current contents of that file for restoring
    what the round trip cannot carry: the LaTeX behind each equation (see
    :mod:`.mathmd`), the ```` ```language ```` tag on each fence (pandoc never
    writes a fence's info string into the docx), and the exact spelling of
    each callout marker (see :mod:`.callouts`). It defaults to reading
    ``asset_path``.

    Both restorations happen *here* rather than in :func:`pull` on purpose.
    The sync engine compares ``rendered.markdown`` against its stored ancestor
    and merges it into the file, so restoring only on the `pull` path leaves
    both sides differing from a base that is missing the formula or the
    language — and an edit anywhere near one then surfaces as a conflict on
    the next watch tick rather than as the clean merge it is.
    """
    drive_service, docs_service = get_services()

    # The body and the comments are independent requests, each a round trip of
    # roughly 200ms, and they were being made one after the other. Overlapping
    # them — and converting the doc to markdown while the comments are still in
    # flight — is most of a render's wall clock, paid by every pull, every sync
    # and every watch tick that gets past the revision peek.
    #
    # Safe to thread: `drive_service` and `docs_service` are separate clients
    # with separate httplib2 connections (httplib2 itself is not thread-safe,
    # so sharing one would not be), and get_credentials() has already refreshed
    # the token, so neither thread can trigger a concurrent refresh.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending_comments = pool.submit(fetch_comments, drive_service, doc_id)
        try:
            # Fetch WITH tab content (else only the first tab is returned)
            doc = docs_service.documents().get(
                documentId=doc_id, includeTabsContent=True
            ).execute(num_retries=NUM_RETRIES)
        except BaseException:
            # Collect the sibling's outcome so a failed render reports the
            # error the caller asked about, not an unretrieved-exception noise
            # message from the interpreter on the way out.
            pending_comments.exception()
            raise
        markdown, title, revision_id, tab_count, images_saved = _render_body(
            doc, asset_path=asset_path, local_text=local_text, say=say)
        comments = pending_comments.result()

    say(f"  {len(comments)} unresolved comment(s)")
    markdown = embed_comments(markdown, comments)

    return RenderedDoc(
        markdown=markdown,
        revision_id=revision_id,
        title=title,
        tabs=max(tab_count, 1),
        comments=len(comments),
        images=images_saved(),
    )


def _render_body(doc, *, asset_path, local_text, say):
    """Convert a fetched doc to markdown. Split out so it can run while the
    comments request is still in flight."""
    title = doc.get("title", "Untitled")
    revision_id = doc.get("revisionId", "")

    image_saver, images_saved = None, (lambda: 0)
    if asset_path is not None:
        image_saver, images_saved = _make_image_saver(asset_path)

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

    # Put back everything the round trip cannot carry, before anything
    # compares, merges or writes this text.
    if local_text is None and asset_path is not None and asset_path.exists():
        try:
            local_text = asset_path.read_text(encoding="utf-8")
        except OSError:
            local_text = None
    if local_text is not None:
        markdown, lost = restore_math(markdown, local_text)
        if lost:
            say(f"  WARNING: {lost} equation(s) could not be matched to local "
                f"LaTeX and are marked `[equation]` — the Docs API does not "
                f"expose equation contents. Check them before saving.")
        markdown = restore_fence_languages(markdown, local_text)
        markdown = restore_callout_spellings(markdown, local_text)

    return markdown, title, revision_id, len(tabs), images_saved


def preserve_frontmatter(existing: str, markdown: str) -> str:
    """Re-attach the local file's YAML frontmatter to freshly-pulled markdown.

    Google Docs has nowhere to store frontmatter, so a pull would otherwise
    drop it every time.
    """
    if existing.startswith("---\n"):
        end_idx = existing.find("\n---\n", 4)
        if end_idx != -1:
            return existing[: end_idx + 5] + "\n" + markdown
    return markdown


def pull(doc_id: str, output_path: Path | None = None, json_out: bool = False) -> str:
    """Pull a Google Doc (all tabs) and return markdown with embedded comments."""
    # In --json mode all progress chatter goes to stderr; stdout is the JSON.
    def say(*args):
        print(*args, file=sys.stderr if json_out else sys.stdout)

    rendered = render_doc(doc_id, asset_path=output_path, say=say)
    markdown = rendered.markdown

    # Math and fence languages were already restored inside render_doc, so
    # that the sync engine sees them too. Frontmatter is re-attached only
    # here: it is genuinely absent from the doc, and the merge ancestor should
    # say so rather than claim the doc carries it.
    if output_path and output_path.exists():
        markdown = preserve_frontmatter(output_path.read_text(), markdown)

    if output_path:
        # Overwriting the user's file is the one irreversible step here, so it
        # gets a backup and an atomic write.
        backup_file(output_path, tag="pre-pull")
        atomic_write(output_path, markdown)
        set_doc_id(str(output_path), doc_id, rendered.revision_id)
        # A direct pull is an explicit "make local match remote", so it also
        # re-establishes the merge ancestors — otherwise the next watch tick
        # would see the rewritten file as an unexplained local edit.
        set_bases(output_path, local=markdown, remote=rendered.markdown)
        clear_conflict(output_path)
        say(f"  Written to {output_path}")
        if rendered.images:
            say(f"  Downloaded {rendered.images} image(s)")

    if json_out:
        payload = {
            "doc_id": doc_id,
            "title": rendered.title,
            "revision_id": rendered.revision_id,
            "tabs": rendered.tabs,
            "comments": rendered.comments,
            "images": rendered.images,
            "output": str(output_path) if output_path else None,
        }
        if not output_path:
            payload["markdown"] = markdown
        print(json.dumps(payload))
    elif not output_path:
        print(markdown)

    return markdown
