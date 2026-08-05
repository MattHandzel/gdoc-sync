"""Turn an existing Google Doc into a new, linked markdown file.

`pull` is the other direction of a link that already exists; `create` makes a
doc from a file. Neither covers the case of starting from a doc someone shared
with you and wanting it in your notes: `pull <url> <file>` gets close, but it
makes you invent the filename and it leaves the file with no record of where
the text came from.

:func:`import_doc` fills that gap — it names the file after the document, gives
it provenance frontmatter, registers the mapping and the merge ancestors so the
sync engine adopts it immediately, and marks docs that cannot survive a round
trip as one-way before an automatic push can damage them.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from pathlib import Path

import yaml

from .config import (
    atomic_write,
    extract_doc_id_from_url,
    set_doc_id,
    set_pull_only,
)
from .pull import render_doc
from .services import NUM_RETRIES, get_services
from .syncstate import clear_conflict, set_bases

MAX_SLUG_LEN = 80


def slugify(title: str, fallback: str = "untitled-doc") -> str:
    """A filesystem- and wiki-link-friendly stem for a document title.

    Titles routinely carry emoji, smart punctuation and accents ("Matt x Tzu 🌻
    1-1"), none of which belong in a filename that will be typed, linked and
    committed. Accents decompose to their base letters so "Café" stays readable
    as "cafe" rather than losing the character entirely.
    """
    decomposed = unicodedata.normalize("NFKD", title)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    slug = slug[:MAX_SLUG_LEN].strip("-")
    return slug or fallback


def doc_url(doc_id: str) -> str:
    return f"https://docs.google.com/document/d/{doc_id}/edit"


def fetch_title(doc_id: str) -> str:
    """The document's name, fetched on its own.

    The title is needed to choose the filename, and the filename is needed
    before the render (inline images are written to ``<stem>-assets/`` beside
    it), so this one cheap metadata request has to happen first. Drive's
    ``files().get`` is used rather than the Docs API because it returns just
    the name instead of the entire document body.
    """
    drive_service, _ = get_services()
    meta = drive_service.files().get(
        fileId=doc_id, fields="name", supportsAllDrives=True,
    ).execute(num_retries=NUM_RETRIES)
    return meta.get("name") or "Untitled"


def build_frontmatter(title: str, doc_id: str, today: datetime.date | None = None) -> str:
    """YAML frontmatter recording where this file came from.

    Written through ``yaml.safe_dump`` rather than an f-string because titles
    contain colons, quotes and emoji that would otherwise produce a file whose
    frontmatter no longer parses.
    """
    today = today or datetime.date.today()
    data = {
        "title": title,
        "source": doc_url(doc_id),
        "gdoc_id": doc_id,
        "imported": today.isoformat(),
    }
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"---\n{body}\n---\n"


def target_path(title: str, output: Path | None, dest: Path | None) -> Path:
    """Where the imported markdown should land."""
    if output is not None:
        return output.expanduser().resolve()
    directory = (dest or Path.cwd()).expanduser().resolve()
    return directory / f"{slugify(title)}.md"


def import_doc(
    target: str,
    *,
    output: Path | None = None,
    dest: Path | None = None,
    force: bool = False,
    pull_only: bool | None = None,
    frontmatter: bool = True,
    open_in_browser: bool = False,
    say=print,
) -> Path:
    """Import a Google Doc as a new markdown file and link the two.

    ``pull_only`` defaults to deciding per document: a tabbed doc is imported
    one-way, because pushing the flattened markdown back would collapse every
    tab into the first one. Pass ``False`` to override that and accept the risk,
    or ``True`` to force one-way on a single-tab doc.

    Returns the path written.
    """
    doc_id = extract_doc_id_from_url(target)
    title = fetch_title(doc_id)
    path = target_path(title, output, dest)

    if path.exists() and not force:
        raise FileExistsError(
            f"{path} already exists. Use --force to overwrite, or link the "
            f"existing file instead:\n"
            f"  gdoc-sync link {path} {doc_url(doc_id)} && gdoc-sync pull {path}"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    say(f"Importing: {title}")
    rendered = render_doc(doc_id, asset_path=path, local_text="", say=say)

    markdown = rendered.markdown
    if frontmatter:
        markdown = build_frontmatter(rendered.title or title, doc_id) + "\n" + markdown

    atomic_write(path, markdown)
    set_doc_id(str(path), doc_id, rendered.revision_id)
    # Register the merge ancestors so `sync`/`watch` treat this as an already
    # reconciled file. Without them the next tick sees a file it has no history
    # for and has to ask which side wins. `local` is what is on disk (including
    # the frontmatter the doc has nowhere to store); `remote` is what the doc
    # itself renders to.
    set_bases(path, local=markdown, remote=rendered.markdown)
    clear_conflict(path)

    if pull_only is None:
        pull_only = rendered.tabs > 1
    set_pull_only(path, pull_only)

    say(f"  Written to {path}")
    if rendered.images:
        say(f"  Downloaded {rendered.images} image(s)")
    if pull_only:
        reason = (f"{rendered.tabs} tabs — a push would flatten them into the first tab"
                  if rendered.tabs > 1 else "requested")
        say(f"  Marked PULL-ONLY ({reason}). `sync`/`watch` will bring doc edits "
            f"down but never push local edits up.")
    else:
        say("  Two-way: `gdoc-sync watch --all` keeps this in sync both ways.")

    if open_in_browser:
        import webbrowser
        webbrowser.open(doc_url(doc_id))

    return path
