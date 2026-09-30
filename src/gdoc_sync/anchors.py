"""Find where a Google Docs comment's quoted text sits in markdown.

A comment reaches us as the *plain text* the reviewer selected in the doc
(``quotedFileContent``) and nothing else: the anchor id Drive returns is an
opaque ``kix.`` handle with no offsets behind it. The markdown we have to put
the comment into is not that plain text. It carries syntax the doc does not
show — ``**`` around bold words, ``[text](url)`` around links, ``### `` before
headings, ``- `` before list items, ``[^1]`` for footnote references — and a
selection that crosses any of them never matches with ``str.find``. Selecting
a sentence with a link in it, or a heading plus the paragraph under it, used
to produce an ``<!-- orphaned comment -->`` at the end of the file every time.

The fix is to search a *projection* of the markdown: the text a reader would
see, with syntax dropped, whitespace collapsed and typography folded, where
every projected character remembers the markdown offset it came from. The
quote is folded the same way, so both sides agree on what counts. When the
whole quote is not there (the text was edited since the comment was made)
the search narrows to the quote's lines, then to its longest surviving run,
before giving up.
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Characters that are markup in markdown and still plain text in the doc
# ("5 * 3"). Dropping them on *both* sides makes the question moot.
_IGNORED = frozenset("*_~`|\\￼﻿​")

# Typography the doc and the file may spell differently: pandoc's `smart`
# extension curls the quotes of a pushed file, Docs autocorrects dashes.
_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "–": "-", "—": "-", "−": "-", "…": "...",
}

# Whole spans that are markup through and through. A comment is never placed
# inside one of them, and one that directly follows the anchor is stepped over
# (an existing comment on the same words, a footnote marker).
_OPAQUE = (
    re.compile(r"\{>>.*?<<\}", re.S),              # CriticMarkup comments
    re.compile(r"<!--.*?-->", re.S),               # HTML comments
    re.compile(r"\[\^[^\]\s]+\](?!:)"),            # footnote references
)
_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")
_LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(([^)\n]*)\)")
_CODE = re.compile(r"(`+)([^`\n]+?)\1")
_FENCE = re.compile(r"(?m)^[ \t]*(?:```|~~~)[^\n]*$")
_LINE_PREFIX = re.compile(
    r"(?m)^(?:[ \t]*(?:>[ \t]?|#{1,6}[ \t]+|[-*+][ \t]+(?:\[[ xX]\][ \t]+)?"
    r"|\d+[.)][ \t]+))+")
_FOOTNOTE_DEF = re.compile(r"(?m)^\[\^[^\]\s]+\]:[ \t]*")
_TABLE_RULE = re.compile(r"(?m)^[ \t]*\|?(?:[ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*:?-*:?[ \t]*$")
_THEMATIC_BREAK = re.compile(r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")

# Below this many projected characters a line or a partial run is too
# unspecific to anchor on: "Yes" or "1." would match the first one it meets.
_MIN_PARTIAL = 12


def _fold_char(ch: str) -> str:
    if ch.isspace():
        return " "
    if ch in _IGNORED:
        return ""
    return _FOLD.get(ch, ch).lower()


def fold(text: str) -> str:
    """The comparable form of a piece of plain text (a quote)."""
    out = "".join(_fold_char(c) for c in text)
    return re.sub(r" +", " ", out).strip()


def plain_quote(quoted: dict | None) -> str:
    """The selected text of a comment, as plain text.

    Drive reports what the reviewer selected with ``mimeType: text/html`` and
    the entities escaped, so ``wasn't`` arrives as ``wasn&#39;t`` and
    ``"done"`` as ``&quot;done&quot;``. Nearly every sentence of prose has an
    apostrophe in it, so unescaped, nearly every selection failed to match.
    """
    if not quoted:
        return ""
    value = quoted.get("value") or ""
    mime = (quoted.get("mimeType") or "").lower()
    if mime == "text/html" or (not mime and re.search(r"&(#\d+|#x[0-9a-f]+|[a-z]+);", value, re.I)):
        value = re.sub(r"(?i)<br\s*/?>", "\n", value)
        value = re.sub(r"<[^>]+>", "", value)
        value = html.unescape(value)
    return value


_W_COMMENT = re.compile(r"<w:comment\b([^>]*)>(.*?)</w:comment>", re.S)
_W_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>|<w:tab/>|<w:br/>|</w:p>")


def _w_text(xml: str) -> str:
    out = []
    for m in _W_TEXT.finditer(xml):
        tag = m.group(0)
        if m.group(1) is not None:
            out.append(m.group(1))
        elif tag == "<w:tab/>":
            out.append("\t")
        else:
            out.append("\n")
    return html.unescape("".join(out)).strip("\n")


def docx_comment_ranges(data: bytes) -> list[tuple[str, str, str]]:
    """``(author, comment text, covered text)`` for each comment in a .docx.

    A comment that reached the doc through an imported Word file has an
    anchor but no ``quotedFileContent``: the Drive API never says what it
    covers. The doc's own .docx export does, as a ``commentRangeStart`` /
    ``commentRangeEnd`` pair around the covered runs.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            notes = z.read("word/comments.xml").decode("utf-8")
        except KeyError:
            return []
        body = z.read("word/document.xml").decode("utf-8")
    out = []
    for m in _W_COMMENT.finditer(notes):
        attrs = dict(re.findall(r'w:(\w+)="([^"]*)"', m.group(1)))
        cid = attrs.get("id")
        span = re.search(rf'<w:commentRangeStart w:id="{cid}"/>(.*?)'
                         rf'<w:commentRangeEnd w:id="{cid}"/>', body, re.S)
        if not span:
            continue
        out.append((html.unescape(attrs.get("author", "")), _w_text(m.group(2)),
                    _w_text(span.group(1))))
    return out


@dataclass
class _Projection:
    """What a reader sees of ``markdown``, indexed back into it."""

    markdown: str
    text: str = ""
    src: list[int] = field(default_factory=list)
    markup: bytearray = field(default_factory=bytearray)
    # (start, end, skippable): spans an anchor must not land inside. The
    # skippable ones are pure markup and are stepped over after an anchor.
    spans: list[tuple[int, int, bool]] = field(default_factory=list)

    def end_of_match(self, start: int, length: int) -> int:
        """Markdown offset just past a match of ``length`` projected chars."""
        last = start + length - 1
        # A match never ends on the collapsed space, so this is a real char.
        return self._settle(self.src[last] + 1)

    def _settle(self, pos: int) -> int:
        md = self.markdown
        moved = True
        while moved:  # spans can nest: a code span inside a link's text
            moved = False
            for s, e, _ in self.spans:
                if s < pos < e:
                    pos, moved = e, True
        starts = {s: (e, skip) for s, e, skip in self.spans}
        while pos < len(md):
            hit = starts.get(pos)
            if hit is not None:
                if not hit[1]:
                    break
                pos = hit[0]
                continue
            ch = md[pos]
            if ch in "*_~" or (self.markup[pos] and not ch.isspace()):
                pos += 1
                continue
            break
        return pos


def project(markdown: str) -> _Projection:
    proj = _Projection(markdown)
    n = len(markdown)
    markup = bytearray(n)

    def mark(s: int, e: int) -> None:
        markup[s:e] = b"\x01" * (e - s)

    def free(m: re.Match) -> bool:
        return not markup[m.start()]

    for rx in _OPAQUE:
        for m in rx.finditer(markdown):
            if free(m):
                mark(m.start(), m.end())
                proj.spans.append((m.start(), m.end(), True))
    for m in _IMAGE.finditer(markdown):
        if free(m):
            mark(m.start(), m.end())
            proj.spans.append((m.start(), m.end(), False))

    # Fenced code keeps every character; only the fence lines are markup, and
    # no list/heading prefix is looked for inside.
    in_fence = bytearray(n)
    fences = [m for m in _FENCE.finditer(markdown) if free(m)]
    for opening, closing in zip(fences[::2], fences[1::2]):
        mark(opening.start(), opening.end())
        mark(closing.start(), closing.end())
        in_fence[opening.end():closing.start()] = b"\x01" * (closing.start() - opening.end())

    for m in _LINK.finditer(markdown):
        if free(m) and not in_fence[m.start()]:
            mark(m.start(), m.start(1))           # [
            mark(m.end(1), m.end())               # ](url)
            proj.spans.append((m.start(), m.end(), False))
    for m in _CODE.finditer(markdown):
        if free(m) and not in_fence[m.start()]:
            mark(m.start(), m.start(2))
            mark(m.end(2), m.end())
            proj.spans.append((m.start(), m.end(), False))
    for rx in (_LINE_PREFIX, _FOOTNOTE_DEF, _TABLE_RULE, _THEMATIC_BREAK):
        for m in rx.finditer(markdown):
            if m.end() > m.start() and free(m) and not in_fence[m.start()]:
                mark(m.start(), m.end())

    text: list[str] = []
    src: list[int] = []
    for i, ch in enumerate(markdown):
        if markup[i]:
            continue
        folded = _fold_char(ch)
        if folded == " ":
            if text and text[-1] != " ":
                text.append(" ")
                src.append(i)
            continue
        for c in folded:
            text.append(c)
            src.append(i)

    proj.text = "".join(text)
    proj.src = src
    proj.markup = markup
    return proj


def find_anchor(proj: _Projection, quote: str) -> int | None:
    """Markdown offset right after ``quote``, or ``None`` if it cannot be placed.

    Tries, in order: the whole quote; its last line, searched after where its
    first line is (a selection spanning paragraphs whose middle was edited);
    its first line; and finally the longest run of the quote that still
    appears, provided that run is long enough to mean something.
    """
    q = fold(quote)
    if not q:
        return None

    hit = proj.text.find(q)
    if hit != -1:
        return proj.end_of_match(hit, len(q))

    lines = [fold(ln) for ln in re.split(r"[\n\r\x0b]+", quote)]
    lines = [ln for ln in lines if len(ln) >= _MIN_PARTIAL]
    if len(lines) >= 2:
        first = proj.text.find(lines[0])
        after = first + len(lines[0]) if first != -1 else 0
        for line in reversed(lines[1:]):
            at = proj.text.find(line, after)
            if at != -1:
                return proj.end_of_match(at, len(line))
        if first != -1:
            return proj.end_of_match(first, len(lines[0]))

    return _longest_run(proj, q)


def _longest_run(proj: _Projection, q: str) -> int | None:
    matcher = SequenceMatcher(None, proj.text, q, autojunk=False)
    a, _, size = matcher.find_longest_match(0, len(proj.text), 0, len(q))
    if size < max(_MIN_PARTIAL * 2, int(len(q) * 0.4)):
        return None
    # Never end on the collapsed space between words.
    while size and proj.text[a + size - 1] == " ":
        size -= 1
    return proj.end_of_match(a, size) if size else None
