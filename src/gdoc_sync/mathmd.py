"""LaTeX math across the Google Docs round trip.

Pushing math already works and always has: pandoc's gfm reader parses ``$x$``
and ``$$x$$``, writes them into the docx as OMML, and Drive imports OMML as a
*native Google Docs equation* — a real, reviewer-editable equation, not a
picture of one.

Pulling is where it falls apart. The Docs API returns an equation as::

    {"startIndex": 12, "endIndex": 53, "equation": {}}

That is the whole object. There is no LaTeX, no MathML, no text, not even a
length hint — the API exposes nothing about an equation's contents. So a pull
cannot reconstruct the source, and before this module every equation simply
vanished: ``$I$ — count of insights`` came back as ``— count of insights``.
Silently, with `watch` running, straight into the file on disk.

The fix has two halves:

1. :func:`~gdoc_sync.convert.doc_to_markdown` emits :data:`PLACEHOLDER` for
   each equation, so an equation is *visible* in the pulled markdown even when
   nothing else can be done. Losing math loudly beats losing it silently.
2. :func:`restore_math` puts the LaTeX back by reading it out of the local
   file, which is the only copy that still has it.

That makes the round trip lossless whenever the equations themselves were not
edited in Google Docs — the overwhelmingly common case, since a doc is shared
for comments on the prose. What it cannot do is notice that someone *changed*
an equation in Docs: with an empty API object there is nothing to compare, so
such an edit is invisible and the local LaTeX wins. That is a real limitation
and it is documented in the README rather than hidden; the alternative — the
old behaviour — was to delete the equation instead, which is strictly worse.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# What a pulled equation becomes before (and, if alignment fails, instead of)
# its LaTeX. Inline code so it reads as a machine token rather than prose, and
# so it survives a re-push intact: pandoc renders it monospace, and monospace
# runs come back as inline code again.
PLACEHOLDER = "`[equation]`"

_PLACEHOLDER_RE = re.compile(re.escape(PLACEHOLDER))


@dataclass(frozen=True)
class MathSpan:
    """One ``$...$`` or ``$$...$$`` run of LaTeX in a markdown source file."""

    start: int
    end: int
    text: str  # the full source span, delimiters included
    display: bool

    @property
    def content(self) -> str:
        n = 2 if self.display else 1
        return self.text[n:-n]


def find_math_spans(markdown: str) -> list[MathSpan]:
    """Locate dollar-delimited math, matching pandoc's gfm reader.

    The rules below are not invented — they were read off pandoc 3.7 by
    feeding it a battery of edge cases, because this scanner has to agree with
    the parser that actually produced the equations in the doc. Disagreement
    means the counts differ and restoration falls back to placeholders.

    What that battery established:

    * ``$`` opens inline math only when the next character is not whitespace,
      and closes only when the previous character is not whitespace. This is
      what makes ``I paid $5 and $10`` prose rather than math, which is the
      whole reason the rule exists.
    * ``\\$`` is an escape and never a delimiter, inside math or out.
    * ``$$...$$`` is display math and has no whitespace restriction, so
      ``$$\\nx\\n$$`` is a single display equation.
    * Backslash-delimited ``\\(x\\)`` is *not* math in gfm, so it is not
      matched here either — it would push as literal text.

    Fenced code blocks and inline code spans are skipped: pandoc does not read
    math inside them, so neither may we.
    """
    spans: list[MathSpan] = []
    masked = _mask_code(markdown)
    i, n = 0, len(masked)

    while i < n:
        ch = masked[i]
        if ch == "\\":  # an escape consumes the next character, e.g. \$
            i += 2
            continue
        if ch != "$":
            i += 1
            continue

        if masked.startswith("$$", i):
            end = _find_display_close(masked, i + 2)
            if end != -1:
                spans.append(MathSpan(i, end + 2, markdown[i:end + 2], True))
                i = end + 2
                continue
            # A lone `$$` is not an opener; step over both so the second `$`
            # cannot be misread as the start of an inline span.
            i += 2
            continue

        end = _find_inline_close(masked, i)
        if end != -1:
            spans.append(MathSpan(i, end + 1, markdown[i:end + 1], False))
            i = end + 1
            continue
        i += 1

    return spans


def _find_display_close(s: str, start: int) -> int:
    i = start
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s.startswith("$$", i):
            return i
        i += 1
    return -1


def _find_inline_close(s: str, open_at: int) -> int:
    # An opener must not be followed by whitespace, and cannot be the last
    # character in the string.
    if open_at + 1 >= len(s) or s[open_at + 1].isspace():
        return -1
    i = open_at + 1
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        # Math never spans a paragraph break.
        if s.startswith("\n\n", i):
            return -1
        if s[i] == "$" and not s[i - 1].isspace():
            return i
        i += 1
    return -1


def _mask_code(markdown: str) -> str:
    """Blank out fenced blocks and inline code, preserving every offset.

    Masking rather than removing keeps the returned indices valid against the
    original string, so the caller can slice real source text out of it.
    """
    out = list(markdown)
    lines = markdown.split("\n")
    pos, in_fence = 0, False
    for line in lines:
        stripped = line.lstrip()
        is_fence = stripped.startswith("```") or stripped.startswith("~~~")
        if is_fence or in_fence:
            for k in range(pos, pos + len(line)):
                out[k] = " "
        if is_fence:
            in_fence = not in_fence
        pos += len(line) + 1

    masked = "".join(out)
    # Inline code spans, longest runs of backticks first so ``a `b` c`` pairs
    # the way markdown does.
    for m in re.finditer(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", masked, re.DOTALL):
        masked = masked[: m.start()] + " " * (m.end() - m.start()) + masked[m.end():]
    return masked


def restore_math(markdown: str, existing: str) -> tuple[str, int]:
    """Replace equation placeholders in ``markdown`` with LaTeX from ``existing``.

    Returns ``(text, unrestored)`` — the count is how many placeholders had no
    local counterpart, which the caller reports so a lossy pull is never quiet.

    When both sides have the same number of equations, they are matched
    positionally: nothing was added or removed remotely, so the Nth equation in
    the doc is certainly the Nth in the file. Otherwise the two sequences are
    aligned on the prose surrounding each equation, so that one equation added
    in Docs costs *that* equation's LaTeX rather than every equation's — which
    is what a plain count check would do.
    """
    placeholders = list(_PLACEHOLDER_RE.finditer(markdown))
    if not placeholders:
        return markdown, 0

    spans = find_math_spans(existing)
    if not spans:
        return markdown, len(placeholders)

    if len(spans) == len(placeholders):
        pairs = {i: i for i in range(len(placeholders))}
    else:
        # Not every equation in the doc became a placeholder. LaTeX that
        # pandoc cannot parse is written into the docx as literal text, so it
        # comes back as a readable `$...$` rather than an equation — and the
        # local file still has a math span facing it. Counting those too is
        # what keeps a single malformed formula from unmatching the whole
        # paragraph around it.
        normalised, is_placeholder = _normalise_pulled(markdown)
        aligned = _align(normalised, _placeholderise(existing, spans))
        pairs, nth = {}, 0
        for position, placeholder in enumerate(is_placeholder):
            if not placeholder:
                continue
            if position in aligned:
                pairs[nth] = aligned[position]
            nth += 1

    out, cursor, unrestored = [], 0, 0
    for idx, m in enumerate(placeholders):
        out.append(markdown[cursor:m.start()])
        if idx in pairs:
            out.append(spans[pairs[idx]].text)
        else:
            out.append(m.group(0))
            unrestored += 1
        cursor = m.end()
    out.append(markdown[cursor:])
    return "".join(out), unrestored


def _placeholderise(existing: str, spans: list[MathSpan]) -> str:
    """Rewrite the local file with each math span reduced to :data:`PLACEHOLDER`.

    Alignment compares the local file against pulled markdown, and the pulled
    side has already lost its LaTeX. Diffing ``$a$`` against ``[equation]``
    would score every equation as a difference, so both sides are first put
    into the same alphabet.
    """
    out, cursor = [], 0
    for span in spans:
        out.append(existing[cursor:span.start])
        out.append(PLACEHOLDER)
        cursor = span.end
    out.append(existing[cursor:])
    return "".join(out)


def _normalise_pulled(markdown: str) -> tuple[str, list[bool]]:
    """Reduce every equation in pulled markdown to :data:`PLACEHOLDER`.

    Returns the rewritten text and, for each equation in document order,
    whether it was a real placeholder (restorable) or literal ``$...$`` that
    survived as text (already correct, and present here only so the positions
    line up against the local file).
    """
    found = [(m.start(), m.end(), True) for m in _PLACEHOLDER_RE.finditer(markdown)]
    found += [(s.start, s.end, False) for s in find_math_spans(markdown)]
    found.sort()

    out, cursor = [], 0
    for start, end, _ in found:
        out.append(markdown[cursor:start])
        out.append(PLACEHOLDER)
        cursor = end
    out.append(markdown[cursor:])
    return "".join(out), [is_placeholder for _, _, is_placeholder in found]


def _paragraphs(text: str) -> list[tuple[str, list[int]]]:
    """Blank-line-separated blocks as (normalised text, equation ordinals).

    Whitespace is collapsed because the round trip rewraps freely; the words
    are what identify a paragraph, not the line breaks.
    """
    out: list[tuple[str, list[int]]] = []
    seen = 0
    for block in re.split(r"\n\s*\n", text):
        count = len(_PLACEHOLDER_RE.findall(block))
        out.append((" ".join(block.split()), list(range(seen, seen + count))))
        seen += count
    return out


def _align(pulled: str, local: str) -> dict[int, int]:
    """Map pulled-equation index → local-equation index, order preserved.

    An equation is identified by the words on either side of it, so the two
    documents are diffed **with the equations taken out**. Diffing them in
    would be worse than useless: every placeholder is textually identical, so
    a run of them is the longest common subsequence in the file and the
    greedy matcher happily anchors three equations onto the wrong three
    neighbours.

    Alignment is done on **paragraphs**, not words. Words look like the finer,
    better signal and are in fact much worse: every placeholder is textually
    identical, so a word-level diff cheerfully anchors a run of equations onto
    the wrong neighbours, and the equations themselves — the one landmark that
    genuinely marks a position — carry no distinguishing text at all. A
    paragraph carries enough prose to be unambiguous.

    Matched paragraphs then map their equations in order; regions between them
    do the same as long as both sides hold the same number, which is the
    evidence that nothing was inserted or deleted *there*. Anything else stays
    unmapped, because guessing would drop the wrong formula into the wrong
    sentence — a silent corruption, and worse than an obvious gap.

    Scoping each decision this narrowly keeps damage local: a whole-document
    count check fails an entire file over a single added equation, whereas here
    it costs only the paragraph that changed.
    """
    a, b = _paragraphs(pulled), _paragraphs(local)
    matcher = difflib.SequenceMatcher(
        a=[text for text, _ in a], b=[text for text, _ in b], autojunk=False)

    pairs: dict[int, int] = {}
    a_at = b_at = 0
    for block in matcher.get_matching_blocks():  # ends with a zero-size sentinel
        _map_equal_counts(pairs, a[a_at:block.a], b[b_at:block.b])
        for offset in range(block.size):
            _map_equal_counts(pairs, [a[block.a + offset]], [b[block.b + offset]])
        a_at, b_at = block.a + block.size, block.b + block.size
    return pairs


def _map_equal_counts(
    pairs: dict[int, int],
    a_part: list[tuple[str, list[int]]],
    b_part: list[tuple[str, list[int]]],
) -> None:
    """Pair equations across one region, but only when the counts agree."""
    pulled = [n for _, equations in a_part for n in equations]
    local = [n for _, equations in b_part for n in equations]
    if len(pulled) == len(local):
        pairs.update(zip(pulled, local))
