"""Multi-tab Google Docs: splitting the markdown, and reconciling the tabs.

`pull` already renders a tabbed document as one markdown file whose top-level
sections are ``# [TAB] <title>`` (child tabs get ``## [TAB] <title>``, and the
sections are separated by a ``---`` rule). This module is the other direction:
it reads those headers back and makes a document's tabs match them.

WHY MATCHING BY TITLE
---------------------
A tab id is minted by Google and appears nowhere in the markdown, so the only
thing the file and the document have in common is the tab's name. Matching on
it means a `push` **rewrites** the tab a section came from rather than
replacing the document's tab structure wholesale: the tab keeps its id, so
links to it, comments anchored in it, and anyone's cursor inside it survive.

A tab the file does not mention is left alone. That is deliberate — a shared
document routinely grows a tab that was never in anyone's notes, and silently
deleting it on the next sync would be the worst thing this tool could do.
``prune=True`` (the CLI's ``--prune-tabs``) is how you ask for the other
behaviour, explicitly and per invocation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

#: ``# [TAB] Title`` — the header `pull` writes and `push` reads.
TAB_HEADER = re.compile(r"^(#{1,6})[ \t]+\[TAB\][ \t]*(.*?)[ \t]*$")

_FENCE = re.compile(r"^\s*(```|~~~)")

@dataclass(frozen=True)
class TabSection:
    """One ``# [TAB] <title>`` section of a markdown file."""

    title: str
    depth: int       # 0 for a root tab, 1 for a child, …
    markdown: str    # the section's body, without its header


@dataclass
class RemoteTab:
    """A tab as the API reports it."""

    tab_id: str
    title: str
    parent_id: str   # "" for a root tab
    index: int       # position among its siblings
    tab: dict        # the raw tab, including documentTab


# ---------------------------------------------------------------------------
# The markdown side
# ---------------------------------------------------------------------------

def split_tab_sections(markdown: str, say=lambda *_: None) -> list[TabSection]:
    """Split markdown into its ``[TAB]`` sections, in document order.

    Returns an empty list for a file with no tab headers, which is how every
    caller decides whether this is a tabbed document at all — a file without
    them must keep going through the ordinary docx path untouched.

    Headers inside a fenced code block are content, not structure, so fences
    are tracked and skipped.
    """
    lines = markdown.split("\n")
    heads: list[tuple[int, int, str]] = []   # (line, depth, title)
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = TAB_HEADER.match(line)
        if m:
            heads.append((i, len(m.group(1)) - 1, m.group(2) or "Untitled tab"))

    if not heads:
        return []

    preamble = "\n".join(lines[: heads[0][0]]).strip()
    if preamble:
        say("  Note: text above the first [TAB] header has been folded into "
            "the first tab — a Google Doc has nowhere else to put it.")

    sections: list[TabSection] = []
    for n, (line_no, depth, title) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        body = "\n".join(lines[line_no + 1:end])
        if n == 0 and preamble:
            body = preamble + "\n\n" + body
        sections.append(TabSection(title=title, depth=depth,
                                   markdown=_strip_separator(body)))
    return sections


def has_tab_sections(markdown: str) -> bool:
    """Whether ``markdown`` asks for a tabbed document."""
    return bool(split_tab_sections(markdown))


def _strip_separator(body: str) -> str:
    """Drop the ``---`` rule `pull` writes between sections.

    Only the final one, and only when it is the last thing in the section:
    a rule the author put in the middle of a tab is theirs to keep.
    """
    lines = body.rstrip().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() in ("---", "***", "___"):
        lines.pop()
    return "\n".join(lines).strip() + "\n"


def tab_tree(sections: list[TabSection]) -> list[int | None]:
    """Parent index of each section, by header depth. None means a root tab."""
    parents: list[int | None] = []
    stack: list[tuple[int, int]] = []   # (depth, index)
    for i, section in enumerate(sections):
        while stack and stack[-1][0] >= section.depth:
            stack.pop()
        parents.append(stack[-1][1] if stack else None)
        stack.append((section.depth, i))
    return parents


# ---------------------------------------------------------------------------
# The document side
# ---------------------------------------------------------------------------

def read_tabs(doc: dict) -> list[RemoteTab]:
    """Every tab of a fetched document, flattened in document order.

    The document must have been fetched with ``includeTabsContent=True``;
    without it the API returns no ``tabs`` at all.
    """
    out: list[RemoteTab] = []

    def walk(tabs: list, parent_id: str) -> None:
        for i, tab in enumerate(tabs or []):
            props = tab.get("tabProperties", {})
            out.append(RemoteTab(
                tab_id=props.get("tabId", ""),
                title=props.get("title", ""),
                parent_id=parent_id,
                index=props.get("index", i),
                tab=tab,
            ))
            walk(tab.get("childTabs", []), props.get("tabId", ""))

    walk(doc.get("tabs", []), "")
    return out


def find_tab(doc: dict, tab_id: str) -> dict | None:
    """The raw tab with this id, anywhere in the tree."""
    for tab in read_tabs(doc):
        if tab.tab_id == tab_id:
            return tab.tab
    return None


def fetch_tabs(docs_service, doc_id: str) -> dict:
    """Fetch a document with its tab contents."""
    from .services import NUM_RETRIES
    return docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)


def stamp_tab_id(requests: list[dict], tab_id: str) -> list[dict]:
    """Address a batch of requests at one tab.

    :mod:`.style` builds its requests against a document-shaped dict and knows
    nothing about tabs, which is exactly what makes it reusable here: a Range,
    a Location and a Location-inside-a-TableRange all take an optional
    ``tabId``, and two request types take one directly. Adding them afterwards
    keeps a single implementation of the styling itself.
    """
    direct = ("updateDocumentStyle", "updateNamedStyle")

    def walk(node):
        if isinstance(node, list):
            return [walk(x) for x in node]
        if not isinstance(node, dict):
            return node
        out = {k: walk(v) for k, v in node.items()}
        for key in ("range", "location", "tableStartLocation"):
            if isinstance(out.get(key), dict):
                out[key]["tabId"] = tab_id
        return out

    stamped = []
    for request in requests:
        new = walk(request)
        for name in direct:
            if name in new:
                new[name]["tabId"] = tab_id
        stamped.append(new)
    return stamped


# ---------------------------------------------------------------------------
# Reconciling the two
# ---------------------------------------------------------------------------

def sync_tabs(docs_service, doc_id: str, sections: list[TabSection], *,
              prune: bool = False, adopt_placeholder: bool = False,
              say=lambda *_: None) -> tuple[list[str | None], dict]:
    """Make the document's tabs match ``sections``. Returns (tab ids, doc).

    The tab ids are **one per section, in section order**, with ``None`` where
    a tab could not be created. Keeping the slot matters: a caller pairs each
    section with its id by position, and dropping a failed one would shift
    every later section into the wrong tab (which is exactly how an Appendix
    tab once got overwritten with a Plans body).

    The returned document is the state *after* any tabs were added or removed,
    so a caller can go straight on to writing content without re-fetching.

    ``adopt_placeholder`` renames the single tab a freshly-created document
    comes with instead of adding a second one beside it — otherwise every new
    document would start with an empty "Tab 1" nobody asked for.
    """
    from .mdrequests import batch

    doc = fetch_tabs(docs_service, doc_id)
    existing = read_tabs(doc)
    parents = tab_tree(sections)
    assigned: list[str | None] = [None] * len(sections)

    available: dict[tuple[str, str], list[RemoteTab]] = {}
    for tab in existing:
        available.setdefault((tab.parent_id, tab.title), []).append(tab)

    if adopt_placeholder and len(existing) == 1 and sections:
        placeholder = existing[0]
        if ("", sections[0].title) not in available:
            batch(docs_service, doc_id, [{"updateDocumentTabProperties": {
                "tabProperties": {"tabId": placeholder.tab_id,
                                  "title": sections[0].title},
                "fields": "title",
            }}])
            assigned[0] = placeholder.tab_id
            available.pop((placeholder.parent_id, placeholder.title), None)

    # Depth by depth: a child's parentTabId is only known once its parent has
    # been created, and the id of a tab added in a batch only comes back in
    # that batch's reply.
    # Where each section sits among its siblings, counted over ALL of them.
    # Counting only the ones being added would place a new tab at the position
    # of a tab that already exists, which is how the first document created
    # this way came out as Monday, Tuesday, Overview.
    position_of: list[int] = []
    seen: dict[int | None, int] = {}
    for i in range(len(sections)):
        parent = parents[i]
        position_of.append(seen.get(parent, 0))
        seen[parent] = position_of[-1] + 1

    depths = sorted({s.depth for s in sections})
    for depth in depths:
        pending: list[tuple[int, dict]] = []
        for i, section in enumerate(sections):
            if section.depth != depth or assigned[i] is not None:
                continue
            parent = parents[i]
            parent_id = assigned[parent] if parent is not None else ""
            if parent is not None and parent_id is None:
                say(f"  Warning: skipping child tab {section.title!r} — its "
                    f"parent tab could not be created.")
                continue
            parent_id = parent_id or ""
            position = position_of[i]

            candidates = available.get((parent_id, section.title))
            if candidates:
                assigned[i] = candidates.pop(0).tab_id
                continue
            props: dict = {"title": section.title, "index": position}
            if parent_id:
                props["parentTabId"] = parent_id
            pending.append((i, {"addDocumentTab": {"tabProperties": props}}))

        if pending:
            replies = batch(docs_service, doc_id, [r for _, r in pending])
            for (i, _), reply in zip(pending, replies):
                new_id = (reply.get("addDocumentTab", {})
                          .get("tabProperties", {}).get("tabId"))
                if not new_id:
                    say(f"  Warning: could not create tab {sections[i].title!r}; "
                        f"its section will not be written.")
                    continue
                assigned[i] = new_id
                say(f"  Added tab: {sections[i].title}")

    if prune:
        _prune(docs_service, doc_id, existing, {t for t in assigned if t}, say)

    return assigned, fetch_tabs(docs_service, doc_id)


def _prune(docs_service, doc_id: str, existing: list[RemoteTab],
           keep: set[str], say) -> None:
    """Delete tabs the markdown does not mention.

    A tab is only removed when nothing under it is being kept: ``deleteTab``
    takes a tab's children with it, so pruning an untouched parent would
    silently take out a tab the file *does* own.
    """
    from .mdrequests import batch

    parent_of = {t.tab_id: t.parent_id for t in existing}

    # Every ancestor of a tab we are keeping. Deleting one would take the tab
    # we want down with it.
    protected: set[str] = set()
    for tab_id in keep:
        parent = parent_of.get(tab_id, "")
        while parent:
            protected.add(parent)
            parent = parent_of.get(parent, "")

    doomed: list[RemoteTab] = []
    going: set[str] = set()
    for tab in existing:   # depth-first, so a parent is always seen first
        if tab.tab_id in keep or tab.tab_id in protected:
            continue
        going.add(tab.tab_id)
        # A tab already going down with its parent needs no request of its
        # own — and would fail the batch, since by then it no longer exists.
        if tab.parent_id in going:
            continue
        doomed.append(tab)

    if not doomed:
        return
    if len(doomed) >= len({t.tab_id for t in existing}):
        say("  Warning: --prune-tabs would empty the document; keeping tabs.")
        return

    batch(docs_service, doc_id,
          [{"deleteTab": {"tabId": t.tab_id}} for t in doomed])
    for t in doomed:
        say(f"  Removed tab: {t.title}")


# ---------------------------------------------------------------------------
# Writing content and style into the tabs
# ---------------------------------------------------------------------------

def write_sections(docs_service, drive_service, doc_id: str,
                   sections: list[TabSection], *, font: str | None = None,
                   theme: str | None = None, resource_dir: Path | None = None,
                   prune: bool = False, adopt_placeholder: bool = False,
                   say=lambda *_: None) -> int:
    """Write every section into its tab and style each one. Returns tab count."""
    from .mdrequests import ImageHost, markdown_to_blocks, write_tab

    assigned, doc = sync_tabs(docs_service, doc_id, sections, prune=prune,
                              adopt_placeholder=adopt_placeholder, say=say)

    host = ImageHost(drive_service, resource_dir=resource_dir, say=say)
    try:
        # Paired by position, so a section whose tab could not be created
        # is skipped here rather than shifting the ones after it.
        for section, tab_id in zip(sections, assigned):
            if tab_id is None:
                say(f"  Warning: no tab for {section.title!r}; section not written.")
                continue
            tab = find_tab(doc, tab_id)
            if tab is None:
                say(f"  Warning: tab {section.title!r} vanished; skipped.")
                continue
            blocks = markdown_to_blocks(section.markdown,
                                        resource_dir=resource_dir, say=say)
            write_tab(docs_service, doc_id, tab_id, tab, blocks,
                      image_uri=host, say=say)
            say(f"  Wrote tab: {section.title}")
    finally:
        host.cleanup()

    tab_ids = [t for t in assigned if t]
    style_tabs(docs_service, doc_id, tab_ids, font=font, theme=theme, say=say)
    return len(tab_ids)


def style_tabs(docs_service, doc_id: str, tab_ids: list[str], *,
               font: str | None = None, theme: str | None = None,
               say=lambda *_: None) -> None:
    """Apply the font, theme, table borders and callout styling to each tab.

    ``baked`` is always False here: the reference-docx trick that bakes a theme
    into a document's named styles only works through a Drive import, and a
    tab's content never goes through one. So the styling is painted on, the
    same way it is on the fallback path for a document Drive imported without
    a reference (see :func:`gdoc_sync.style.style_requests`).
    """
    from .mdrequests import batch
    from .style import callout_requests, style_requests, table_border_requests

    doc = fetch_tabs(docs_service, doc_id)
    requests: list[dict] = []
    tables = callouts = 0
    for tab_id in tab_ids:
        tab = find_tab(doc, tab_id)
        if tab is None:
            continue
        shaped = _as_document(tab)
        per_tab = style_requests(shaped, font=font, theme=theme, baked=False)
        border, n_tables = table_border_requests(shaped)
        from .style import resolve_theme
        callout, n_callouts = callout_requests(shaped, resolve_theme(theme))
        tables += n_tables
        callouts += n_callouts
        requests += stamp_tab_id(per_tab + border + callout, tab_id)

    if not requests:
        return
    batch(docs_service, doc_id, requests)
    if tables:
        say(f"  Applied visible borders to {tables} table(s)")
    if callouts:
        say(f"  Styled {callouts} callout(s)")
    say(f"  Applied font: {font}" + (f" + theme: {theme}" if theme else "")
        + f" to {len(tab_ids)} tab(s)")


def _as_document(tab: dict) -> dict:
    """A tab, shaped like the document dict :mod:`.style` expects."""
    doc_tab = tab.get("documentTab", {})
    return {
        "body": doc_tab.get("body", {}),
        "lists": doc_tab.get("lists", {}),
        "inlineObjects": doc_tab.get("inlineObjects", {}),
        "footnotes": doc_tab.get("footnotes", {}),
    }
