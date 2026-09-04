"""Push local markdown to a linked Google Doc.

Most pushes go through the same pandoc → docx pipeline as `create`; the
existing doc's content is replaced in place via Drive's files().update, which
preserves the doc id, URL, and sharing.

A file whose top-level sections are ``# [TAB] <title>`` takes the other path.
A Drive import replaces a *whole document*, so pushing a tabbed file that way
would flatten every tab into the first one — which is why such documents used
to be marked pull-only. Instead the sections are matched against the
document's tabs by title and each one is rewritten in place through the Docs
API (see :mod:`.tabs` and :mod:`.mdrequests`). Tabs the file does not mention
are left alone unless ``prune_tabs`` says otherwise.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

from googleapiclient.http import MediaFileUpload

from .comments import (
    anchored_push_warning,
    apply_comment_actions,
    consume_action_markers,
    fetch_comments,
    strip_comments,
)
from .config import (
    atomic_write,
    get_doc_id,
    get_font,
    get_revision,
    get_theme,
    set_revision,
)
from .create import DOCX_MIME
from .highlight import highlight_theme_for
from .mdutils import pandoc_to_docx, strip_frontmatter
from .refdoc import styled_reference_docx
from .services import NUM_RETRIES, get_services
from .style import apply_document_styling
from .syncstate import backup_file
from .tabs import read_tabs, split_tab_sections, write_sections


def push(local_path: Path, *, yes: bool = False, font: str | None = None,
         theme: str | None = None, merged: bool = False,
         prune_tabs: bool = False, flatten: bool = False) -> str:
    """Push a markdown file to its linked Google Doc.

    Returns the markdown that was actually pushed — the file's text with any
    comment-action markers this push consumed removed — so the caller can use
    it as the local sync baseline instead of the pre-push text.

    ``merged`` says the caller is the sync engine and has already merged the
    doc's changes into this file. The drift warning below is then not just
    noise but actively wrong — nothing is being discarded — so it is skipped.

    ``prune_tabs`` deletes tabs the markdown does not mention (tabbed files
    only). ``flatten`` allows the destructive push of a file with no ``[TAB]``
    headers into a document that has several tabs.
    """
    doc_id = get_doc_id(str(local_path))
    if not doc_id:
        print(f"No Google Doc linked to {local_path}", file=sys.stderr)
        print("Link one with: gdoc-sync link <file> <doc_url>", file=sys.stderr)
        sys.exit(1)

    drive_service, docs_service = get_services()

    # Fetched WITH tab content because the tab titles decide which path this
    # push takes, and `tabs` is simply absent without it.
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)
    current_rev = doc.get("revisionId", "")
    stored_rev = get_revision(str(local_path))

    # Optimistic locking: warn when the remote changed since our last pull/push.
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
    result = apply_comment_actions(drive_service, doc_id, markdown)
    for line in result.lines:
        print(f"  {line}")

    # An applied marker must not survive in the file: three pushes of an
    # unchanged file would otherwise post the same reply three times. Rewrite
    # *before* the upload, so a failed upload cannot leave the actions
    # queued up to fire again.
    markdown = _consume_applied_markers(local_path, markdown, result.applied)

    # Both push paths replace the whole body, so every text-anchored thread
    # is about to read "Original content deleted" in Docs. Reuse the fetch
    # apply_comment_actions already made rather than listing comments twice.
    remote_comments = result.remote
    if remote_comments is None:
        remote_comments = fetch_comments(drive_service, doc_id)
    anchored = anchored_push_warning(remote_comments)
    if anchored:
        print(f"  {anchored}")

    body_md = strip_comments(strip_frontmatter(markdown))

    title = doc.get("title", "Untitled")
    print(f"Pushing to: {title}")

    if font is None:
        font = get_font()
    if theme is None:
        theme = get_theme()

    sections = split_tab_sections(body_md, say=print)
    if sections:
        _push_tabs(drive_service, docs_service, doc_id, sections,
                   resource_dir=local_path.parent, font=font, theme=theme,
                   prune_tabs=prune_tabs)
    else:
        _guard_flatten(doc, local_path, flatten)
        _push_docx(drive_service, docs_service, doc_id, body_md,
                   resource_dir=local_path.parent, font=font, theme=theme)

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
    return markdown


def _consume_applied_markers(local_path: Path, markdown: str,
                             applied: list[tuple[int, int]]) -> str:
    """Remove the action markers that were applied, and save the file.

    Only rewrites when the bytes on disk are still exactly what this push
    read; if the user (or the watcher) saved in between, the markers are left
    alone rather than clobbering the newer file. Returns the text that should
    be pushed and recorded as the baseline.
    """
    if not applied:
        return markdown

    try:
        on_disk = local_path.read_text()
    except OSError as e:
        print(f"  Warning: could not re-read {local_path.name} ({e}); "
              "comment action markers left in place.")
        return markdown

    if _digest(on_disk) != _digest(markdown):
        print(f"  Warning: {local_path.name} changed while this push was "
              "running; comment action markers left in place (they will be "
              "re-applied on the next push).")
        return markdown

    consumed = consume_action_markers(markdown, applied)
    backup_file(local_path, tag="pre-push")
    atomic_write(local_path, consumed)
    return consumed


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _guard_flatten(doc: dict, local_path: Path, flatten: bool) -> None:
    """Refuse to collapse a multi-tab document into its first tab.

    This is the check the ``pull_only`` mark used to stand in for. It is
    better placed here: what makes a push safe is not a flag recorded at
    import time but whether the file in hand actually describes the tabs, and
    that can only be answered when the push happens.
    """
    tabs = read_tabs(doc)
    if len(tabs) <= 1 or flatten:
        return
    print(f"Refusing to push: the doc has {len(tabs)} tabs, but "
          f"{local_path.name} has no `# [TAB] <title>` headers, so this push "
          f"would flatten every tab into the first one.", file=sys.stderr)
    print(f"  Pull first to get the tab headers:  gdoc-sync pull {local_path}",
          file=sys.stderr)
    print("  Or push anyway, destroying the tabs: add --flatten",
          file=sys.stderr)
    sys.exit(3)


def _push_tabs(drive_service, docs_service, doc_id: str, sections, *,
               resource_dir: Path, font: str | None, theme: str | None,
               prune_tabs: bool) -> None:
    print(f"Writing {len(sections)} tab(s) via the Docs API...")
    write_sections(docs_service, drive_service, doc_id, sections,
                   font=font, theme=theme, resource_dir=resource_dir,
                   prune=prune_tabs, say=print)


def _push_docx(drive_service, docs_service, doc_id: str, body_md: str, *,
               resource_dir: Path, font: str | None, theme: str | None) -> None:
    # Styling goes into the docx's own style definitions where possible, so the
    # Google Doc's named styles carry the theme instead of having it painted
    # over the top (see refdoc). Falls back to API-side styling if that fails.
    reference_doc = styled_reference_docx(font, theme)
    highlight_style = highlight_theme_for(theme)

    print("Converting markdown → docx via pandoc...")
    with tempfile.TemporaryDirectory() as tmpdir:
        docx_path = Path(tmpdir) / "doc.docx"
        pandoc_to_docx(body_md, docx_path, resource_dir=resource_dir,
                       reference_doc=reference_doc,
                       highlight_style=highlight_style)
        media = MediaFileUpload(str(docx_path), mimetype=DOCX_MIME, resumable=False)
        drive_service.files().update(
            fileId=doc_id, media_body=media).execute(num_retries=NUM_RETRIES)

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
