"""Markdown → Google Docs API requests, for content that must land in a *tab*.

WHY THIS EXISTS
---------------
Everywhere else, `create` and `push` hand markdown to pandoc, take the .docx,
and let Drive's importer build the Google Doc (see :mod:`.create`). That path
has the best fidelity available and is not going anywhere — but it cannot
write a tab. A Drive import replaces an entire document, and the Docs API has
no request that moves imported content into a tab. Meanwhile *every* range and
location in the API takes a ``tabId``. So content destined for a specific tab
has to be expressed as batchUpdate requests, and something has to turn
markdown into them.

That something is this module. It reads **pandoc's own JSON AST** rather than
re-parsing markdown, which means the tab path and the docx path agree about
what the markdown *says* — same reader, same extensions, same callout
rewriting (see :mod:`.callouts`, whose ``transform_ast`` has already turned
every callout into a one-cell table by the time we get here). Only the
rendering differs.

WHAT IT AIMS AT
---------------
The target is not "what pandoc's docx writer would have produced" but "what
:func:`~gdoc_sync.convert.doc_to_markdown` can read back", because a tabbed doc
has to survive `push` → `pull` unchanged. That is why, for instance, a fenced
code block becomes one monospace paragraph with ``\\v`` line breaks: that is
exactly the shape the puller recognises as a fence.

INDEX ARITHMETIC AND ORDER
--------------------------
Docs indices count UTF-16 code units, and every insert shifts everything after
it. Three rules keep that honest here:

* Text is inserted strictly front-to-back, so a running counter is exact.
* ``createParagraphBullets`` eats the leading tabs that encode nesting, so it
  runs back-to-front and every character-style range after it is corrected by
  how many tabs were removed ahead of it.
* Paragraph styling comes **before** character styling. Applying a
  ``namedStyleType`` resets the paragraph's text styles to that style's
  defaults, so bold and monospace applied first are simply erased — which is
  how this was found: pushed code blocks arrived in the document as prose.

Tables cannot be sized by arithmetic at all (the API decides how many index
units a table occupies), so they are not attempted: a placeholder paragraph
holds the spot, and :func:`fill_tables` inserts and populates the real tables
from a re-fetch of the document. See its docstring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: Font applied to code blocks and inline code. It must be one the puller
#: recognises as monospace (see :data:`gdoc_sync.convert._MONO_FONTS`) —
#: that font is the *only* surviving signal that a paragraph was a fence.
MONO_FONT = "Courier New"

#: Points of indent per level of blockquote nesting (Google Docs' own default).
INDENT_PT = 36

#: How many requests to send in one batchUpdate.
BATCH_SIZE = 400

#: Text of the placeholder paragraph that reserves a table's position.
#: Deleted before anyone sees it; distinctive so it can be found again.
TABLE_SENTINEL = "⟦gdoc-sync:table:{i}⟧"
_SENTINEL_RE = re.compile(r"⟦gdoc-sync:table:(\d+)⟧")

# Every text-style field the compiler can set. Cleared in one sweep over the
# inserted range before per-run styles are applied: text inserted into a
# document inherits the style of the character before it, so without this a
# tab whose first paragraph used to be a bold heading would come out bold
# throughout.
_RESET_FIELDS = (
    "bold,italic,underline,strikethrough,smallCaps,link,baselineOffset,"
    "foregroundColor,backgroundColor,weightedFontFamily,fontSize"
)

_HEADINGS = {1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
             4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6"}


def u16(text: str) -> int:
    """Length of ``text`` in UTF-16 code units — the unit Docs indices count.

    An emoji or any other astral-plane character is two units, not one, so
    ``len()`` would silently misplace every style range after the first one.
    """
    return len(text.encode("utf-16-le")) // 2


# ---------------------------------------------------------------------------
# The intermediate form
# ---------------------------------------------------------------------------

@dataclass
class Run:
    """A stretch of text with one set of character styles, or one image."""

    text: str = ""
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    code: bool = False
    small_caps: bool = False
    link: str | None = None
    baseline: str | None = None   # SUPERSCRIPT | SUBSCRIPT
    image: str | None = None      # the markdown image target; text is its alt

    def style_key(self) -> tuple:
        return (self.bold, self.italic, self.underline, self.strike,
                self.code, self.small_caps, self.link, self.baseline)


@dataclass
class Para:
    """One Docs paragraph."""

    runs: list[Run] = field(default_factory=list)
    named: str = "NORMAL_TEXT"
    list_kind: str | None = None   # "ul" | "ol"
    nesting: int = 0
    indent: int = 0                # blockquote depth
    mono: bool = False             # the whole paragraph is a fenced code block


@dataclass
class Table:
    """A table as rows of cells, each cell holding its own blocks."""

    rows: list[list[list]] = field(default_factory=list)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r) for r in self.rows), default=0)


@dataclass
class Rule:
    """A horizontal rule."""


# ---------------------------------------------------------------------------
# pandoc AST → blocks
# ---------------------------------------------------------------------------

def markdown_to_blocks(markdown: str, *, resource_dir: Path | None = None,
                       say=lambda *_: None) -> list:
    """Parse markdown into the intermediate blocks, via pandoc's JSON AST.

    Goes through :func:`gdoc_sync.mdutils.pandoc_to_ast` so callouts have
    already been folded into one-cell tables — the same AST the docx writer
    would have been handed.
    """
    from .mdutils import pandoc_to_ast
    ast = pandoc_to_ast(markdown, resource_dir=resource_dir)
    return ast_to_blocks(ast, say=say)


def ast_to_blocks(ast: dict, *, say=lambda *_: None) -> list:
    """Convert a pandoc AST document to blocks."""
    warned: set[str] = set()

    def warn(what: str) -> None:
        if what not in warned:
            warned.add(what)
            say(f"  Note: {what}")

    out: list = []
    _blocks(ast.get("blocks", []), _Ctx(), out, warn)
    return out


@dataclass(frozen=True)
class _Ctx:
    list_kind: str | None = None
    nesting: int = 0
    indent: int = 0

    def at(self, **kw) -> _Ctx:
        return _Ctx(**{**self.__dict__, **kw})


def _blocks(nodes: list, ctx: _Ctx, out: list, warn) -> None:
    for node in nodes:
        _block(node, ctx, out, warn)


def _block(node, ctx: _Ctx, out: list, warn) -> None:
    if not isinstance(node, dict):
        return
    t = node.get("t")
    c = node.get("c")

    if t in ("Para", "Plain"):
        out.append(Para(runs=_runs(c or []), indent=ctx.indent))
    elif t == "Header":
        level, _attr, inlines = c
        out.append(Para(runs=_runs(inlines),
                        named=_HEADINGS.get(int(level), "HEADING_6"),
                        indent=ctx.indent))
    elif t == "CodeBlock":
        _attr, text = c
        out.append(Para(runs=[Run(text=text.rstrip("\n"))], mono=True,
                        indent=ctx.indent))
    elif t == "BlockQuote":
        _blocks(c or [], ctx.at(indent=ctx.indent + 1), out, warn)
    elif t == "BulletList":
        _list(c or [], "ul", ctx, out, warn)
    elif t == "OrderedList":
        _list((c or [None, []])[1], "ol", ctx, out, warn)
    elif t == "DefinitionList":
        # No Docs equivalent; a bold term followed by its indented definitions
        # is what the docx writer produces too.
        for term, definitions in c or []:
            out.append(Para(runs=_runs(term, {"bold": True}), indent=ctx.indent))
            for blocks in definitions:
                _blocks(blocks, ctx.at(indent=ctx.indent + 1), out, warn)
    elif t == "Table":
        out.append(_table(c, ctx, warn))
    elif t == "HorizontalRule":
        out.append(Rule())
    elif t in ("Div", "Figure"):
        # Div: [attr, blocks].  Figure: [attr, caption, blocks].
        _blocks(c[-1] if t == "Div" else c[2], ctx, out, warn)
        if t == "Figure":
            caption = (c[1] or [None, []])[1]
            for blk in caption or []:
                _block(blk, ctx, out, warn)
    elif t == "LineBlock":
        for line in c or []:
            out.append(Para(runs=_runs(line), indent=ctx.indent))
    elif t == "RawBlock":
        _fmt, text = c
        warn(f"raw {_fmt} block written as plain text")
        out.append(Para(runs=[Run(text=text)], indent=ctx.indent))
    elif t == "Null":
        pass
    else:
        warn(f"unsupported block {t!r} skipped")


def _list(items: list, kind: str, ctx: _Ctx, out: list, warn) -> None:
    for item in items or []:
        first = True
        for blk in item or []:
            t = blk.get("t") if isinstance(blk, dict) else None
            if t in ("BulletList", "OrderedList"):
                sub = blk["c"] if t == "BulletList" else blk["c"][1]
                _list(sub, "ul" if t == "BulletList" else "ol",
                      ctx.at(nesting=ctx.nesting + 1), out, warn)
            elif first and t in ("Para", "Plain"):
                out.append(Para(runs=_runs(blk.get("c") or []), list_kind=kind,
                                nesting=ctx.nesting, indent=ctx.indent))
                first = False
            else:
                # A second paragraph, code block or quote inside one list item.
                # Docs has no "continuation" concept, so it becomes an ordinary
                # paragraph indented to the item's depth.
                _block(blk, ctx.at(list_kind=None,
                                   indent=ctx.indent + ctx.nesting + 1), out, warn)


def _table(c, ctx: _Ctx, warn) -> Table:
    """A pandoc Table → :class:`Table`.

    ``c`` is ``[attr, caption, colspecs, head, bodies, foot]``. Head, body and
    foot rows are concatenated in that order because the puller reads row 0 as
    the header row and everything after it as the body.
    """
    _attr, _caption, _colspecs, head, bodies, foot = c
    rows: list[list[list]] = []

    def add(row_list) -> None:
        for _rattr, cells in row_list or []:
            row: list[list] = []
            for cell in cells or []:
                _cattr, _align, _rowspan, _colspan, blocks = cell
                cell_blocks: list = []
                _blocks(blocks or [], _Ctx(), cell_blocks, warn)
                row.append(cell_blocks)
            rows.append(row)

    add((head or ["", []])[1])
    for body in bodies or []:
        _battr, _rhc, head_rows, body_rows = body
        add(head_rows)
        add(body_rows)
    add((foot or ["", []])[1])
    return Table(rows=rows)


# ---------------------------------------------------------------------------
# pandoc inlines → runs
# ---------------------------------------------------------------------------

def _runs(inlines: list, style: dict | None = None) -> list[Run]:
    out: list[Run] = []
    _inlines(inlines or [], style or {}, out)
    return out


def _emit(out: list[Run], text: str, style: dict) -> None:
    """Append ``text``, merging into the previous run when styles match.

    pandoc splits prose into a Str per word with Spaces between; without this
    a sentence would become dozens of one-word runs and dozens of identical
    insertText requests.
    """
    if not text:
        return
    run = Run(text=text, **style)
    if out and not out[-1].image and out[-1].style_key() == run.style_key():
        out[-1].text += text
        return
    out.append(run)


def _inlines(nodes: list, style: dict, out: list[Run]) -> None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        t, c = node.get("t"), node.get("c")
        if t == "Str":
            _emit(out, c, style)
        elif t in ("Space", "SoftBreak"):
            _emit(out, " ", style)
        elif t == "LineBreak":
            # A new paragraph rather than a Docs line break: a `\v` would come
            # back out of the puller as a literal control character in the
            # markdown, which is worse than losing the hard wrap's tightness.
            _emit(out, "\n", style)
        elif t == "Strong":
            _inlines(c, {**style, "bold": True}, out)
        elif t == "Emph":
            _inlines(c, {**style, "italic": True}, out)
        elif t == "Underline":
            _inlines(c, {**style, "underline": True}, out)
        elif t == "Strikeout":
            _inlines(c, {**style, "strike": True}, out)
        elif t == "SmallCaps":
            _inlines(c, {**style, "small_caps": True}, out)
        elif t == "Superscript":
            _inlines(c, {**style, "baseline": "SUPERSCRIPT"}, out)
        elif t == "Subscript":
            _inlines(c, {**style, "baseline": "SUBSCRIPT"}, out)
        elif t == "Code":
            _emit(out, c[1], {**style, "code": True})
        elif t == "Link":
            _, inner, target = c
            _inlines(inner, {**style, "link": target[0]}, out)
        elif t == "Image":
            _, alt, target = c
            out.append(Run(image=target[0], text=_plain(alt), **style))
        elif t == "Quoted":
            kind = (c[0] or {}).get("t")
            open_q, close_q = ("‘", "’") if kind == "SingleQuote" else ("“", "”")
            _emit(out, open_q, style)
            _inlines(c[1], style, out)
            _emit(out, close_q, style)
        elif t == "Math":
            # The Docs API cannot create an equation, so the LaTeX is written
            # as text. Visible and editable beats silently absent.
            kind = (c[0] or {}).get("t")
            fence = "$$" if kind == "DisplayMath" else "$"
            _emit(out, f"{fence}{c[1]}{fence}", style)
        elif t == "RawInline":
            _emit(out, c[1], style)
        elif t in ("Span", "Cite"):
            _inlines(c[-1], style, out)
        elif t == "Note":
            # Footnotes live in their own document segment, which a tab write
            # has no way to address. Inline the text so nothing is lost.
            _emit(out, " [" + _plain_blocks(c) + "]", style)


def _plain(inlines: list) -> str:
    return "".join(r.text for r in _runs(inlines))


def _plain_blocks(blocks: list) -> str:
    out: list = []
    _blocks(blocks or [], _Ctx(), out, lambda *_: None)
    return " ".join(
        "".join(r.text for r in b.runs) for b in out if isinstance(b, Para)
    ).strip()


# ---------------------------------------------------------------------------
# blocks → requests
# ---------------------------------------------------------------------------

@dataclass
class Compiled:
    """Requests that write one run of blocks, plus what still needs a re-fetch."""

    requests: list[dict] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    end_index: int = 1
    images: int = 0


def compile_blocks(blocks: list, *, tab_id: str | None = None, start: int = 1,
                   image_uri=None, allow_tables: bool = True) -> Compiled:
    """Compile ``blocks`` into requests that insert them at ``start``.

    ``image_uri(target, alt) -> str | None`` resolves a markdown image target
    to something ``insertInlineImage`` can fetch; returning None writes the alt
    text instead. Omit it to skip images entirely.

    ``allow_tables`` is False inside a table cell: filling a cell already
    depends on a re-fetch, and a table nested in a cell would need a second
    one. Such a table is written as its rows in plain pipe syntax.
    """
    def rng(s: int, e: int) -> dict:
        r = {"startIndex": s, "endIndex": e}
        if tab_id:
            r["tabId"] = tab_id
        return r

    def loc(i: int) -> dict:
        d = {"index": i}
        if tab_id:
            d["tabId"] = tab_id
        return d

    inserts: list[dict] = []
    # (start, end, textStyle, fields) in pre-bullet coordinates; turned into
    # requests at the end, once it is known how many tabs the bullets ate.
    text_styles: list[tuple[int, int, dict, str]] = []
    para_styles: list[dict] = []
    bullets: list[dict] = []
    tabs_eaten: list[tuple[int, int]] = []   # (position, how many)
    tables: list[Table] = []
    images = 0
    idx = start

    # Consecutive list paragraphs of the same kind become one
    # createParagraphBullets request, so Docs treats them as one list.
    group: list | None = None

    def flush_group() -> None:
        nonlocal group
        if group is None:
            return
        kind, gstart, gend = group
        bullets.append({"createParagraphBullets": {
            "range": rng(gstart, gend),
            "bulletPreset": ("NUMBERED_DECIMAL_ALPHA_ROMAN" if kind == "ol"
                             else "BULLET_DISC_CIRCLE_SQUARE"),
        }})
        group = None

    def insert(text: str) -> None:
        nonlocal idx
        if not text:
            return
        inserts.append({"insertText": {"location": loc(idx), "text": text}})
        idx += u16(text)

    for blk in _expand(blocks, allow_tables):
        if isinstance(blk, Table):
            flush_group()
            sentinel = TABLE_SENTINEL.format(i=len(tables))
            p0 = idx
            insert(sentinel + "\n")
            para_styles.append(_para_style_request(rng(p0, idx), Para()))
            tables.append(blk)
            continue

        if isinstance(blk, Rule):
            flush_group()
            p0 = idx
            insert("\n")
            para_styles.append(_para_style_request(rng(p0, idx), Para(), rule=True))
            continue

        p0 = idx
        # Bullet nesting is expressed as leading tabs, which
        # createParagraphBullets consumes (and only it can — there is no
        # nestingLevel field on a create request).
        if blk.list_kind and blk.nesting:
            tabs_eaten.append((idx, blk.nesting))
            insert("\t" * blk.nesting)

        if blk.mono:
            code = blk.runs[0].text if blk.runs else ""
            # Docs represents the line breaks *inside* one paragraph as \v;
            # that is exactly what the puller translates back to newlines, so
            # a fence with blank lines in it survives as one block.
            body = code.replace("\r\n", "\n").replace("\n", "\v")
            s = idx
            insert(body)
            if idx > s:
                text_styles.append(
                    (s, idx,
                     {"weightedFontFamily": {"fontFamily": MONO_FONT}},
                     "weightedFontFamily"))
        else:
            for run in blk.runs:
                if run.image is not None:
                    uri = image_uri(run.image, run.text) if image_uri else None
                    if uri:
                        inserts.append({"insertInlineImage": {
                            "location": loc(idx), "uri": uri}})
                        idx += 1
                        images += 1
                        continue
                    # No usable URI: write the alt text rather than drop the
                    # image silently, so the reader can see something is meant
                    # to be there.
                    run = Run(text=run.text or run.image, italic=True)
                s = idx
                insert(run.text)
                if idx > s:
                    style, fields = _text_style(run)
                    if fields:
                        text_styles.append((s, idx, style, ",".join(fields)))

        insert("\n")
        para_styles.append(_para_style_request(rng(p0, idx), blk))

        if blk.list_kind:
            if group and group[0] == blk.list_kind and group[2] == p0:
                group[2] = idx
            else:
                flush_group()
                group = [blk.list_kind, p0, idx]
        else:
            flush_group()

    flush_group()

    def shifted(i: int) -> int:
        """``i`` once createParagraphBullets has eaten the nesting tabs."""
        return i - sum(n for at, n in tabs_eaten if at < i)

    # Order is not cosmetic:
    #
    # 1. inserts — front to back, so the running index is exact.
    # 2. paragraph styles — BEFORE any character styling, because applying a
    #    namedStyleType resets the paragraph's text styles to that style's
    #    defaults. Bold and monospace applied first are simply erased (which
    #    is exactly how this was found: pushed code blocks came back as prose).
    # 3. bullets, back to front — each deletes the leading tabs that encode
    #    nesting, moving every index after it.
    # 4. the style reset, then the per-run styles, at their post-bullet
    #    indices — last, so nothing above can undo them.
    requests: list[dict] = list(inserts)
    requests += para_styles
    requests += list(reversed(bullets))
    end = shifted(idx)
    if end > start:
        # Text inherits the style of the character before it, so clear that
        # before painting on what this content actually asks for.
        requests.append({"updateTextStyle": {
            "range": rng(start, end), "textStyle": {}, "fields": _RESET_FIELDS}})
    for s, e, style, fields in text_styles:
        s, e = shifted(s), shifted(e)
        if e > s:
            requests.append({"updateTextStyle": {
                "range": rng(s, e), "textStyle": style, "fields": fields}})

    return Compiled(requests=requests, tables=tables, end_index=end, images=images)


def _expand(blocks: list, allow_tables: bool) -> list:
    """Blocks as the compiler wants them, flattening tables when not allowed."""
    if allow_tables:
        return list(blocks)
    out: list = []
    for blk in blocks:
        if isinstance(blk, Table):
            for row in blk.rows:
                cells = [" ".join(
                    "".join(r.text for r in b.runs)
                    for b in cell if isinstance(b, Para)).strip() for cell in row]
                out.append(Para(runs=[Run(text="| " + " | ".join(cells) + " |")]))
        else:
            out.append(blk)
    return out


def _text_style(run: Run) -> tuple[dict, list[str]]:
    style: dict = {}
    fields: list[str] = []
    if run.bold:
        style["bold"] = True
        fields.append("bold")
    if run.italic:
        style["italic"] = True
        fields.append("italic")
    if run.underline:
        style["underline"] = True
        fields.append("underline")
    if run.strike:
        style["strikethrough"] = True
        fields.append("strikethrough")
    if run.small_caps:
        style["smallCaps"] = True
        fields.append("smallCaps")
    if run.code:
        style["weightedFontFamily"] = {"fontFamily": MONO_FONT}
        fields.append("weightedFontFamily")
    if run.link:
        style["link"] = {"url": run.link}
        fields.append("link")
    if run.baseline:
        style["baselineOffset"] = run.baseline
        fields.append("baselineOffset")
    return style, fields


def _para_style_request(range_: dict, blk: Para, *, rule: bool = False) -> dict:
    """Paragraph style for one block.

    Indentation is always stated, never left alone: an emptied tab keeps the
    paragraph style of whatever used to be there, so "no request" would mean
    "inherit last week's blockquote indent".
    """
    style: dict = {
        "namedStyleType": blk.named,
        "indentStart": {"magnitude": INDENT_PT * blk.indent, "unit": "PT"},
        "indentFirstLine": {"magnitude": INDENT_PT * blk.indent, "unit": "PT"},
    }
    fields = ["namedStyleType", "indentStart", "indentFirstLine"]
    if rule:
        style["borderBottom"] = {
            "color": {"color": {"rgbColor": {"red": 0.7, "green": 0.7, "blue": 0.7}}},
            "width": {"magnitude": 1, "unit": "PT"},
            "padding": {"magnitude": 1, "unit": "PT"},
            "dashStyle": "SOLID",
        }
        fields.append("borderBottom")
    return {"updateParagraphStyle": {"range": range_, "paragraphStyle": style,
                                     "fields": ",".join(fields)}}


# ---------------------------------------------------------------------------
# Talking to the API
# ---------------------------------------------------------------------------

def batch(docs_service, doc_id: str, requests: list[dict]) -> list[dict]:
    """Send ``requests`` in chunks, returning the concatenated replies."""
    from .services import NUM_RETRIES
    replies: list[dict] = []
    for i in range(0, len(requests), BATCH_SIZE):
        chunk = requests[i:i + BATCH_SIZE]
        if not chunk:
            continue
        result = docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": chunk}
        ).execute(num_retries=NUM_RETRIES)
        replies += result.get("replies", [])
    return replies


def clear_tab_requests(tab: dict, tab_id: str) -> list[dict]:
    """Empty a tab's body and reset the one paragraph that cannot be deleted.

    Docs will not let the final newline of a segment go, so a cleared tab still
    holds one paragraph — carrying the bullet, style and indent of whatever was
    there before. Text inserted into it inherits all three, so they are reset
    here rather than fought with afterwards.
    """
    content = tab.get("documentTab", {}).get("body", {}).get("content", [])
    end = content[-1].get("endIndex", 1) if content else 1
    requests: list[dict] = []
    if end > 2:
        requests.append({"deleteContentRange": {
            "range": {"startIndex": 1, "endIndex": end - 1, "tabId": tab_id}}})
    tail = {"startIndex": 1, "endIndex": 2, "tabId": tab_id}
    requests.append({"deleteParagraphBullets": {"range": tail}})
    requests.append({"updateParagraphStyle": {
        "range": tail,
        "paragraphStyle": {
            "namedStyleType": "NORMAL_TEXT",
            "indentStart": {"magnitude": 0, "unit": "PT"},
            "indentFirstLine": {"magnitude": 0, "unit": "PT"},
        },
        "fields": "namedStyleType,indentStart,indentFirstLine",
    }})
    return requests


def find_sentinels(tab: dict) -> dict[int, tuple[int, int]]:
    """Placeholder paragraphs left by :func:`compile_blocks`, by table number."""
    found: dict[int, tuple[int, int]] = {}
    for element in tab.get("documentTab", {}).get("body", {}).get("content", []):
        para = element.get("paragraph")
        if not para:
            continue
        text = "".join(e.get("textRun", {}).get("content", "")
                       for e in para.get("elements", []))
        m = _SENTINEL_RE.search(text)
        if m:
            found[int(m.group(1))] = (element["startIndex"], element["endIndex"])
    return found


def table_elements(tab: dict) -> list[dict]:
    """Top-level table structural elements of a tab, in document order."""
    return [el for el in tab.get("documentTab", {}).get("body", {}).get("content", [])
            if "table" in el]


def insert_table_requests(tab: dict, tables: list[Table], tab_id: str) -> list[dict]:
    """Turn each placeholder paragraph into a real table of the right shape.

    Back-to-front, so that the index shift each insertion causes only ever
    lands on placeholders that have already been dealt with.
    """
    positions = find_sentinels(tab)
    requests: list[dict] = []
    for i in reversed(range(len(tables))):
        if i not in positions:
            continue
        start, end = positions[i]
        table = tables[i]
        if table.n_rows < 1 or table.n_cols < 1:
            continue
        # Delete the sentinel text but keep its paragraph: insertTable needs a
        # paragraph boundary to sit at, and the emptied one is exactly that.
        requests.append({"deleteContentRange": {
            "range": {"startIndex": start, "endIndex": end - 1, "tabId": tab_id}}})
        requests.append({"insertTable": {
            "location": {"index": start, "tabId": tab_id},
            "rows": table.n_rows, "columns": table.n_cols}})
    return requests


def fill_table_requests(tab: dict, tables: list[Table], tab_id: str,
                        *, image_uri=None) -> list[dict]:
    """Write each cell's content into the tables created by the previous batch.

    Cells are filled back-to-front for the same reason tables are inserted that
    way: every insertion moves the indices of everything after it, and nothing
    earlier in the document has been touched yet.
    """
    elements = table_elements(tab)
    requests: list[dict] = []
    for element, table in reversed(list(zip(elements, tables))):
        rows = element.get("table", {}).get("tableRows", [])
        for row_index in reversed(range(min(len(rows), len(table.rows)))):
            cells = rows[row_index].get("tableCells", [])
            want = table.rows[row_index]
            for col in reversed(range(min(len(cells), len(want)))):
                content = cells[col].get("content", [])
                if not content:
                    continue
                start = content[0].get("startIndex")
                if start is None:
                    continue
                compiled = compile_blocks(want[col], tab_id=tab_id, start=start,
                                          image_uri=image_uri, allow_tables=False)
                requests += compiled.requests
    return requests


def write_tab(docs_service, doc_id: str, tab_id: str, tab: dict, blocks: list,
              *, image_uri=None, say=lambda *_: None) -> int:
    """Replace a tab's content with ``blocks``. Returns the images inserted.

    Three batches at most: clear + text, then the tables, then their cells.
    The second and third each need the document re-read, because only the API
    knows how many index units a table it just made occupies.
    """
    compiled = compile_blocks(blocks, tab_id=tab_id, start=1, image_uri=image_uri)
    batch(docs_service, doc_id,
          clear_tab_requests(tab, tab_id) + compiled.requests)

    if compiled.tables:
        fetched = _fetch_tab(docs_service, doc_id, tab_id)
        batch(docs_service, doc_id,
              insert_table_requests(fetched, compiled.tables, tab_id))
        fetched = _fetch_tab(docs_service, doc_id, tab_id)
        batch(docs_service, doc_id,
              fill_table_requests(fetched, compiled.tables, tab_id,
                                  image_uri=image_uri))
    return compiled.images


def _fetch_tab(docs_service, doc_id: str, tab_id: str) -> dict:
    from .services import NUM_RETRIES
    from .tabs import find_tab
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute(num_retries=NUM_RETRIES)
    tab = find_tab(doc, tab_id)
    if tab is None:
        raise RuntimeError(f"tab {tab_id} disappeared while writing to it")
    return tab


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

class ImageHost:
    """Makes a markdown image target reachable by ``insertInlineImage``.

    The docx path never needed this: pandoc embeds local images in the .docx
    and Drive imports the bytes. The Docs API takes only a URI it can fetch
    itself, so a local file has to be somewhere public for the length of one
    request. It is uploaded to Drive, link-shared, inserted (Docs copies the
    bytes into the document), and then deleted again by :meth:`cleanup` — the
    document keeps its own copy, so nothing is left behind or left shared.
    """

    MAX_BYTES = 45 * 1024 * 1024  # Docs rejects images over 50MB

    def __init__(self, drive_service, resource_dir: Path | None = None,
                 say=lambda *_: None):
        self.drive = drive_service
        self.resource_dir = resource_dir
        self.say = say
        self._uploaded: list[str] = []
        self._cache: dict[str, str | None] = {}

    def __call__(self, target: str, alt: str = "") -> str | None:
        if target in self._cache:
            return self._cache[target]
        uri = self._resolve(target)
        self._cache[target] = uri
        return uri

    def _resolve(self, target: str) -> str | None:
        if target.startswith(("http://", "https://")):
            return target
        path = Path(target).expanduser()
        if not path.is_absolute() and self.resource_dir:
            path = self.resource_dir / path
        if not path.is_file():
            self.say(f"  Warning: image not found, writing its alt text: {target}")
            return None
        try:
            if path.stat().st_size > self.MAX_BYTES:
                self.say(f"  Warning: image too large for the Docs API: {target}")
                return None
            return self._upload(path)
        except Exception as e:  # noqa: BLE001 — one bad image must not fail a push
            self.say(f"  Warning: could not stage image {target}: {e}")
            return None

    def _upload(self, path: Path) -> str:
        from googleapiclient.http import MediaFileUpload

        from .services import NUM_RETRIES
        media = MediaFileUpload(str(path), resumable=False)
        created = self.drive.files().create(
            body={"name": f"gdoc-sync-upload-{path.name}"},
            media_body=media, fields="id",
        ).execute(num_retries=NUM_RETRIES)
        file_id = created["id"]
        self._uploaded.append(file_id)
        self.drive.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}, fields="id",
        ).execute(num_retries=NUM_RETRIES)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    def cleanup(self) -> None:
        """Delete the staging copies. Safe to call more than once."""
        from .services import NUM_RETRIES
        for file_id in self._uploaded:
            try:
                self.drive.files().delete(fileId=file_id).execute(num_retries=NUM_RETRIES)
            except Exception:  # noqa: BLE001 — best effort; the doc has the bytes
                self.say(f"  Warning: left a staging image in Drive: {file_id}")
        self._uploaded.clear()
