"""Callouts (admonitions) across the Google Docs round trip.

The markdown looks like this — GitHub calls them alerts, Obsidian calls them
callouts, and the syntax is the same::

    > [!NOTE]
    > This is a piece of advice that works well in an established company.

In an editor that understands them this renders as a coloured box with an icon
and a title. Reaching a Google Doc, it did not: pandoc's *reader* recognises
GitHub's five types and turns them into a ``Div``, but pandoc's **docx writer
flattens that Div completely**. The word "Note" arrived as an ordinary
paragraph indistinguishable from prose, and the marker line of every
Obsidian-only type (``[!TLDR]``, ``[!SUCCESS]``, …) arrived as the literal
text ``[!TLDR]``. Either way the reader of the shared doc saw no callout.

How they are represented in the doc
-----------------------------------
Each callout becomes a **one-row, one-column table** whose cell holds the
title paragraph followed by the callout's own blocks, styled with a tinted
background and a thick accent border down the left edge — which is what a
callout looks like in Obsidian, and what the Docs API can actually express.

A table rather than styled paragraphs, because the *extent* has to survive.
Blockquote paragraphs arrive with ``indentStart``/``indentEnd`` of 24pt, but a
list inside a blockquote arrives with a bullet's own indents and no
``indentEnd`` at all — so "consecutive indented paragraphs" silently ends a
callout at its first bullet. A table cell has exact boundaries, holds
arbitrary block content (lists, code, nested tables), and survives a reader
editing inside it.

Identification is by the leading icon of the title paragraph, which is unique
per type. That is also what lets a *custom* title round trip: everything after
the icon is the title, whatever it says.

What the round trip cannot carry by itself
------------------------------------------
The doc stores the rendered type, not the word that was written. ``[!INFO]``
and ``[!NOTE]`` render identically, as do ``[!ERROR]`` and ``[!DANGER]``, and
Obsidian's fold markers (``[!NOTE]-``) have no meaning in a Google Doc at all.
Rewriting a file's ``[!INFO]`` to ``[!NOTE]`` on every pull would be pointless
churn, so :func:`restore_callout_spellings` puts the author's exact wording
back from the local file — the same trick :mod:`.mathmd` uses for equations
and :func:`~gdoc_sync.convert.restore_fence_languages` uses for fences.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Unicode variation selectors. Emoji survive the round trip, but whether a
# U+FE0F rides along is not something to bet a match on.
_VS = re.compile("[︎️]")


@dataclass(frozen=True)
class CalloutType:
    """One rendered kind of callout."""

    kind: str
    icon: str
    title: str
    accent: str  # hex, tuned for a light page; adapted for dark themes


def _t(kind, icon, title, accent, *aliases):
    return CalloutType(kind, icon, title, accent), (kind,) + aliases


# Every callout type GitHub and Obsidian define, and every alias each accepts.
#
# GitHub has five (note/tip/important/warning/caution) and treats them as
# distinct; Obsidian has a larger set in which `important` is an alias of
# `tip` and `caution` an alias of `warning`. Where they disagree, GitHub wins:
# it is the syntax people write, and collapsing two spellings the author
# deliberately chose is a worse failure than showing two similar boxes.
#
# Icons must stay unique — the icon is how a pulled doc is read back.
_TYPES: list[tuple[CalloutType, tuple[str, ...]]] = [
    _t("note",      "ℹ️",  "Note",      "#1f6feb", "info"),
    _t("abstract",  "\U0001f4cb",    "Abstract",  "#0e7490", "summary", "tldr"),
    _t("todo",      "☑️",  "Todo",      "#1f6feb"),
    _t("tip",       "\U0001f4a1",    "Tip",       "#1a7f37", "hint"),
    _t("important", "❗",        "Important", "#8250df"),
    _t("success",   "✅",        "Success",   "#1a7f37", "check", "done"),
    _t("question",  "❓",        "Question",  "#9a6700", "help", "faq"),
    _t("warning",   "⚠️",  "Warning",   "#9a6700", "attention"),
    _t("caution",   "\U0001f6d1",    "Caution",   "#cf222e"),
    _t("failure",   "❌",        "Failure",   "#cf222e", "fail", "missing"),
    _t("danger",    "⛔",        "Danger",    "#cf222e", "error"),
    _t("bug",       "\U0001f41b",    "Bug",       "#cf222e"),
    _t("example",   "\U0001f9ea",    "Example",   "#8250df"),
    _t("quote",     "\U0001f4ac",    "Quote",     "#6e7781", "cite"),
]

CALLOUT_TYPES: dict[str, CalloutType] = {t.kind: t for t, _ in _TYPES}

#: Every accepted spelling (lowercase) → the type it renders as.
ALIASES: dict[str, CalloutType] = {
    alias: t for t, names in _TYPES for alias in names
}

#: Icon (variation selectors stripped) → type, for reading a pulled doc back.
_BY_ICON: dict[str, CalloutType] = {
    _VS.sub("", t.icon): t for t in CALLOUT_TYPES.values()
}

# `> [!NOTE]`, `> [!note]-`, `> [!TIP] with a custom title`
_HEADER = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marks>>[ \t]*(?:>[ \t]*)*)"
    r"\[!(?P<name>[A-Za-z][\w-]*)\](?P<fold>[+-]?)[ \t]*(?P<title>.*?)[ \t]*$"
)
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class CalloutHeader:
    """A ``> [!TYPE]`` line found in a markdown source file."""

    line: int
    name: str          # exactly as written, e.g. "INFO"
    fold: str          # "", "+" or "-"
    title: str         # custom title, "" when the type's default is used
    type: CalloutType


def find_callouts(markdown: str) -> list[CalloutHeader]:
    """Every callout header in ``markdown``, in document order.

    Only the header line is located. The body needs no parsing: it is
    whatever pandoc already makes of the rest of the blockquote.
    """
    found: list[CalloutHeader] = []
    in_fence = False
    for i, line in enumerate(markdown.split("\n")):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADER.match(line)
        if not m:
            continue
        spec = ALIASES.get(m.group("name").lower())
        if spec is None:  # `> [!SOMETHINGELSE]` is just a blockquote
            continue
        found.append(CalloutHeader(
            line=i, name=m.group("name"), fold=m.group("fold"),
            title=m.group("title"), type=spec,
        ))
    return found


def rewrite_for_pandoc(markdown: str) -> tuple[str, int]:
    """Turn each callout header into a title pandoc will keep. Returns
    ``(markdown, count)``.

    pandoc cannot be relied on to do this itself. Its reader recognises only
    GitHub's five types, and only in their bare form: ``[!NOTE] My title`` and
    ``[!NOTE]-`` are *not* alerts, they are blockquotes whose first words are
    the literal text ``[!NOTE]``. Normalising every spelling here — including
    the five pandoc would have handled — means one path to test rather than
    two, and the AST arrives with every callout in the same shape.
    """
    heads = {h.line: h for h in find_callouts(markdown)}
    if not heads:
        return markdown, 0

    out: list[str] = []
    for i, line in enumerate(markdown.split("\n")):
        head = heads.get(i)
        if head is None:
            out.append(line)
            continue
        m = _HEADER.match(line)
        prefix = m.group("indent") + m.group("marks")
        title = head.title or head.type.title
        out.append(f"{prefix}**{head.type.icon} {title}**")
        # A blank quoted line, so the title is its own paragraph rather than
        # the first line of the body's.
        out.append(f"{m.group('indent')}{m.group('marks').rstrip()}")
    return "\n".join(out), len(heads)


def split_title(text: str) -> tuple[CalloutType, str] | None:
    """Read a callout title paragraph back: ``"⚠️ Careful"`` → (warning, "Careful").

    Returns None when ``text`` does not start with a callout icon. The second
    element is "" when the title is the type's default, so a caller can tell a
    deliberately-renamed callout from an ordinary one.
    """
    stripped = _VS.sub("", text).strip()
    for icon, spec in _BY_ICON.items():
        if not stripped.startswith(icon):
            continue
        title = stripped[len(icon):].strip()
        return spec, ("" if title == spec.title else title)
    return None


# ---------------------------------------------------------------------------
# Push: markdown → pandoc AST → a one-cell table per callout
# ---------------------------------------------------------------------------

def _cell(blocks: list) -> list:
    return [["", [], []], {"t": "AlignDefault"}, 1, 1, blocks]


def _one_cell_table(blocks: list, kind: str) -> dict:
    """A full-width 1x1 pandoc Table holding ``blocks``.

    The ``callout`` class is carried for the benefit of any other writer; the
    docx writer drops it, which is why the doc is read back by icon instead.
    """
    return {"t": "Table", "c": [
        ["", ["callout", kind], []],
        [None, []],                                            # caption
        [[{"t": "AlignDefault"}, {"t": "ColWidth", "c": 1.0}]],  # colspecs
        [["", [], []], []],                                    # head
        [[["", [], []], 0, [], [[["", [], []], [_cell(blocks)]]]]],  # bodies
        [["", [], []], []],                                    # foot
    ]}


def _title_of(block: dict) -> CalloutType | None:
    """The callout type of a ``Para`` that is exactly one bold title, else None."""
    if block.get("t") != "Para":
        return None
    inlines = block.get("c") or []
    if len(inlines) != 1 or inlines[0].get("t") != "Strong":
        return None
    text = "".join(
        i.get("c", "") if isinstance(i.get("c"), str) else " "
        for i in inlines[0].get("c", [])
    )
    parsed = split_title(text)
    return parsed[0] if parsed else None


def fold_callouts(node):
    """Rewrite every callout blockquote in a pandoc AST into a 1x1 table.

    Recursive, so a callout nested in a list item or another blockquote is
    converted too.
    """
    if isinstance(node, list):
        return [fold_callouts(x) for x in node]
    if not isinstance(node, dict):
        return node

    if node.get("t") == "BlockQuote":
        blocks = node.get("c") or []
        if blocks:
            spec = _title_of(blocks[0])
            if spec is not None:
                return _one_cell_table(
                    [blocks[0]] + [fold_callouts(b) for b in blocks[1:]],
                    spec.kind,
                )
    return {k: fold_callouts(v) for k, v in node.items()}


def transform_ast(ast: dict) -> dict:
    """Apply :func:`fold_callouts` to a whole pandoc document."""
    return {**ast, "blocks": [fold_callouts(b) for b in ast.get("blocks", [])]}


# ---------------------------------------------------------------------------
# Reading a Google Doc back
# ---------------------------------------------------------------------------

def cell_title(cell: dict) -> tuple[CalloutType, str] | None:
    """If ``cell``'s first paragraph is a callout title, return (type, title)."""
    for element in cell.get("content", []):
        para = element.get("paragraph")
        if para is None:
            return None
        text = "".join(
            e.get("textRun", {}).get("content", "") for e in para.get("elements", [])
        )
        if not text.strip():  # a leading empty paragraph is not a verdict
            continue
        return split_title(text)
    return None


def table_callout(table: dict) -> tuple[CalloutType, str, dict] | None:
    """If ``table`` is a callout, return (type, custom title, its cell).

    A 1x1 table is already a strong signal — GFM cannot express one, since a
    pipe table needs a header row and a delimiter row — but the icon is what
    actually decides, so a hand-made one-cell table stays a table.
    """
    if table.get("rows") != 1 or table.get("columns") != 1:
        return None
    rows = table.get("tableRows") or []
    if len(rows) != 1:
        return None
    cells = rows[0].get("tableCells") or []
    if len(cells) != 1:
        return None
    parsed = cell_title(cells[0])
    return None if parsed is None else (parsed[0], parsed[1], cells[0])


def to_markdown(spec: CalloutType, title: str, body: str) -> str:
    """Render a callout back to ``> [!TYPE]`` markdown.

    ``body`` is the cell's content already converted to markdown, *including*
    the title paragraph, which is dropped here in favour of the header line.
    """
    lines = body.split("\n")
    # Drop the rendered title paragraph and the blank line after it.
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and split_title(re.sub(r"\*\*", "", lines[0])):
        lines.pop(0)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    header = f"> [!{spec.kind.upper()}]" + (f" {title}" if title else "")
    quoted = [header] + [(f"> {ln}" if ln.strip() else ">") for ln in lines]
    return "\n".join(quoted) + "\n\n"


def restore_callout_spellings(markdown: str, existing: str) -> str:
    """Put the author's own callout wording back into freshly-pulled markdown.

    A doc records how a callout *renders*, not how it was written: ``[!INFO]``
    and ``[!NOTE]`` are the same box, and a fold marker (``[!NOTE]-``) means
    nothing outside Obsidian. Left alone, every pull would rewrite the file's
    ``[!INFO]`` to ``[!NOTE]`` and drop its fold marker — a diff on every
    sync, for a change nobody made.

    Reused positionally, and only when both sides have the same number of
    callouts *and* each pair renders as the same type. If they disagree,
    callouts were added or removed in Docs and position no longer identifies
    the same one; the pulled spelling is then correct-if-plain, which beats
    relabelling somebody's ``[!WARNING]`` as ``[!TIP]``.
    """
    old = find_callouts(existing)
    new = find_callouts(markdown)
    if not old or len(old) != len(new):
        return markdown
    if any(a.type is not b.type for a, b in zip(old, new)):
        return markdown

    lines = markdown.split("\n")
    for was, now in zip(old, new):
        if was.name.lower() == now.name.lower() and not was.fold:
            continue
        m = _HEADER.match(lines[now.line])
        title = f" {now.title}" if now.title else ""
        lines[now.line] = (
            f"{m.group('indent')}{m.group('marks')}"
            f"[!{was.name}]{was.fold}{title}"
        )
    return "\n".join(lines)
