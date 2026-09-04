"""LaTeX math survives the Google Docs round trip.

The bug these guard: the Docs API returns every equation as an empty
``{"equation": {}}``, so a pull used to drop the formula and leave the
surrounding sentence looking fine — with `watch` running, straight onto disk.
"""

import os
import shutil
import subprocess

import pytest

from gdoc_sync.convert import doc_to_markdown
from gdoc_sync.mathmd import PLACEHOLDER, find_math_spans, restore_math
from gdoc_sync.mdutils import unconvertible_math


def _run(text, **style):
    return {"textRun": {"content": text, "textStyle": style}}


def _eq():
    return {"equation": {}}


def _para(elements):
    return {"paragraph": {"elements": elements}, "startIndex": 0, "endIndex": 1}


# --------------------------------------------------------------------------
# Scanning local markdown for math
# --------------------------------------------------------------------------

def test_finds_inline_and_display():
    spans = find_math_spans("Let $x$ be.\n\n$$y = 2$$\n")
    assert [s.text for s in spans] == ["$x$", "$$y = 2$$"]
    assert [s.display for s in spans] == [False, True]


def test_span_text_is_verbatim_source():
    src = r"$$\n\begin{aligned}a &= b\end{aligned}$$".replace(r"\n", "\n")
    spans = find_math_spans(src)
    assert len(spans) == 1
    assert spans[0].text == src
    # The recorded offsets must slice the original string back out.
    assert src[spans[0].start:spans[0].end] == src


def test_currency_is_not_math():
    """The single most important false positive: prose about money."""
    assert find_math_spans("It costs $5 and $10 in total.") == []
    assert find_math_spans("Revenue was $5.") == []


def test_escaped_dollars_are_not_delimiters():
    assert find_math_spans(r"A \$100 fee and a \$200 fee.") == []


def test_escaped_dollar_inside_math_does_not_close_it():
    spans = find_math_spans(r"$a\$b$")
    assert [s.text for s in spans] == [r"$a\$b$"]


def test_code_is_skipped():
    assert find_math_spans("Use `$x$` literally.") == []
    assert find_math_spans("```\n$x$\n```\n") == []


def test_math_after_a_fenced_block_is_still_found():
    spans = find_math_spans("```\n$a$\n```\n\nthen $b$ here\n")
    assert [s.text for s in spans] == ["$b$"]


def test_adjacent_inline_spans():
    spans = find_math_spans("$a$$b$")
    assert [s.text for s in spans] == ["$a$", "$b$"]


def test_math_does_not_span_a_paragraph_break():
    assert find_math_spans("a $x\n\ny$ b") == []


# --------------------------------------------------------------------------
# Conversion emits a visible placeholder
# --------------------------------------------------------------------------

def test_equation_becomes_a_placeholder_not_nothing():
    doc = {"body": {"content": [
        _para([_eq(), _run(" — count of insights\n")]),
    ]}}
    md, _ = doc_to_markdown(doc)
    assert PLACEHOLDER in md
    assert "count of insights" in md


def test_display_equation_paragraph_is_preserved():
    doc = {"body": {"content": [_para([_eq(), _run("\n")])]}}
    md, _ = doc_to_markdown(doc)
    assert md.strip() == PLACEHOLDER


# --------------------------------------------------------------------------
# Restoring LaTeX from the local file
# --------------------------------------------------------------------------

def test_round_trip_is_lossless_when_nothing_changed():
    local = "Let $x$ be the rate.\n\n$$y = \\frac{a}{b}$$\n"
    pulled = f"Let {PLACEHOLDER} be the rate.\n\n{PLACEHOLDER}\n"
    out, lost = restore_math(pulled, local)
    assert out == local
    assert lost == 0


def test_restores_in_order_not_by_value():
    local = "$a$ then $b$ then $c$\n"
    pulled = f"{PLACEHOLDER} then {PLACEHOLDER} then {PLACEHOLDER}\n"
    out, lost = restore_math(pulled, local)
    assert out == local
    assert lost == 0


def test_prose_edited_around_the_math_still_restores():
    local = "Let $x$ be the rate.\n"
    pulled = f"Let {PLACEHOLDER} be the measured rate.\n"
    out, lost = restore_math(pulled, local)
    assert out == "Let $x$ be the measured rate.\n"
    assert lost == 0


def test_equation_added_remotely_costs_only_itself():
    """A plain count check would blank *every* equation here. This must not."""
    local = "First $a$ line.\n\nSecond $b$ line.\n"
    pulled = (f"First {PLACEHOLDER} line.\n\n"
              f"New {PLACEHOLDER} line.\n\n"
              f"Second {PLACEHOLDER} line.\n")
    out, lost = restore_math(pulled, local)
    assert "First $a$ line." in out
    assert "Second $b$ line." in out
    assert lost == 1
    assert out.count(PLACEHOLDER) == 1


# --------------------------------------------------------------------------
# Equal counts are not proof of equal positions (P0-14)
#
# The pull used to match equations by ordinal whenever the doc and the file
# held the same number of them. Reordering prose in Docs keeps that number
# exactly the same, so a reviewer who dragged a paragraph up the page got each
# formula rewritten into somebody else's sentence, with `unrestored=0` — the
# corruption every one of these guards.
# --------------------------------------------------------------------------

def test_swapped_paragraphs_keep_each_equation_with_its_prose():
    local = ("The force law states that $F = ma$.\n\n"
             "The energy relation is $E = mc^2$.\n")
    pulled = (f"The energy relation is {PLACEHOLDER}.\n\n"
              f"The force law states that {PLACEHOLDER}.\n")

    out, lost = restore_math(pulled, local)

    assert out == ("The energy relation is $E = mc^2$.\n\n"
                   "The force law states that $F = ma$.\n")
    assert lost == 0


def test_swapped_and_edited_paragraph_is_flagged_rather_than_swapped():
    """The moved paragraph was also reworded, so identity cannot recover it.

    The paragraph that did survive verbatim claims its own equation, which
    leaves the other placeholder facing an already-spoken-for candidate. The
    guard refuses it: an obvious gap the pull reports beats `$F = ma$` quietly
    becoming the energy relation.
    """
    local = ("The force law states that $F = ma$.\n\n"
             "The energy relation is $E = mc^2$.\n")
    pulled = (f"The energy relation here is {PLACEHOLDER}.\n\n"
              f"The force law states that {PLACEHOLDER}.\n")

    out, lost = restore_math(pulled, local)

    assert out == (f"The energy relation here is {PLACEHOLDER}.\n\n"
                   "The force law states that $F = ma$.\n")
    assert lost == 1
    assert "$E = mc^2$" not in out


def test_edit_far_from_the_equations_still_restores_all_of_them():
    """The common case must stay lossless — caution is not an excuse to warn."""
    local = ("First $a$ line.\n\n"
             "An unrelated paragraph of prose.\n\n"
             "Second $b$ line.\n")
    pulled = (f"First {PLACEHOLDER} line.\n\n"
              "An unrelated paragraph of prose, lightly reworded.\n\n"
              f"Second {PLACEHOLDER} line.\n")

    out, lost = restore_math(pulled, local)

    assert out == ("First $a$ line.\n\n"
                   "An unrelated paragraph of prose, lightly reworded.\n\n"
                   "Second $b$ line.\n")
    assert lost == 0


def test_unrecognisable_prose_leaves_placeholders_instead_of_guessing():
    """Same equation count, but nothing around them survived to match on.

    Both formulas ended up in one rewritten paragraph, so no region of the
    document holds the same number on both sides and the alignment pairs
    nothing. With no evidence at all, ordinal position is not evidence either.
    """
    local = ("An untouched opening paragraph.\n\n"
             "Alpha alpha alpha $a$.\n\n"
             "An untouched closing paragraph.\n\n"
             "Beta beta beta $b$.\n")
    pulled = ("An untouched opening paragraph.\n\n"
              f"Nothing here resembles the source {PLACEHOLDER} at all {PLACEHOLDER}.\n\n"
              "An untouched closing paragraph.\n")

    out, lost = restore_math(pulled, local)

    assert out == pulled
    assert lost == 2
    assert "$a$" not in out and "$b$" not in out


def test_three_equations_in_a_row_still_restore_in_order():
    """No prose between them to align on — order alone has to carry it."""
    local = ("The derivation runs $a = 1$, then $b = 2$, then $c = 3$ in one breath.\n\n"
             "A closing paragraph.\n")
    pulled = (f"The derivation runs {PLACEHOLDER}, then {PLACEHOLDER}, "
              f"then {PLACEHOLDER} in one breath.\n\n"
              "A closing paragraph, reworded.\n")

    out, lost = restore_math(pulled, local)

    assert out == ("The derivation runs $a = 1$, then $b = 2$, then $c = 3$ in one breath.\n\n"
                   "A closing paragraph, reworded.\n")
    assert lost == 0


def test_positional_fallback_is_anchored_and_monotonic():
    """Both halves of the fallback rule, in one document.

    One paragraph survived verbatim and anchors its own equation to the local
    third. The rewritten paragraph *before* it may take the first local
    equation: unclaimed, and still on the correct side of the anchor. The one
    *after* it may not — every candidate left sits before the anchor, and text
    does not cross over itself — so it is left visible and counted.
    """
    local = ("Alpha alpha $a$.\n\n"
             "Beta beta $b$.\n\n"
             "A paragraph that survived untouched with $c$ in it.\n")
    pulled = (f"Gibberish gibberish {PLACEHOLDER}.\n\n"
              f"A paragraph that survived untouched with {PLACEHOLDER} in it.\n\n"
              f"More gibberish entirely {PLACEHOLDER}.\n")

    out, lost = restore_math(pulled, local)

    assert out == ("Gibberish gibberish $a$.\n\n"
                   "A paragraph that survived untouched with $c$ in it.\n\n"
                   f"More gibberish entirely {PLACEHOLDER}.\n")
    assert lost == 1


def test_malformed_latex_next_to_good_math_does_not_unmatch_it():
    """Pandoc writes unparseable LaTeX into the docx as literal text.

    So the doc holds one *fewer* equation than the file has math spans, and the
    pulled markdown carries the raw `$m^$` back as prose. Counting only
    placeholders would make the paragraph's counts disagree and strand the
    perfectly good equation beside it — which is what happened on a real
    128-equation document.
    """
    local = r"threshold $m^$, and the honest quantity is $c_{\text{int}}$ here."
    pulled = rf"threshold $m^$, and the honest quantity is {PLACEHOLDER} here."
    out, lost = restore_math(pulled, local)
    assert out == local
    assert lost == 0


def test_restoration_retires_the_spurious_merge_conflict():
    """What the lossy render actually cost on the `watch` path.

    It did not delete equations there — the three-way merge compares each side
    against its own snapshot, and an ancestor rendered just as lossily cancels
    the deletion out. What it did was conflict: with the formula missing from
    the base, *both* sides differed from it, so any prose edit on an equation's
    line stopped the watcher and demanded a hand resolution. Restoring the math
    before the comparison is what makes this an ordinary clean merge.
    """
    from gdoc_sync.merge import merge3

    local = "Let $I$ be the count of high-quality insights.\n"
    base_render = f"Let {PLACEHOLDER} be the count of high-quality insights.\n"
    their_render = f"Let {PLACEHOLDER} be the count of high quality insights.\n"

    # Pre-fix: the renders reached the merge with the equation missing.
    before = merge3(local, base_render, their_render,
                    label_ours="local", label_base="base", label_theirs="doc")
    assert before.conflicted

    # Now: every render is restored first, so only the typo fix differs.
    base, _ = restore_math(base_render, local)
    theirs, _ = restore_math(their_render, local)
    after = merge3(local, base, theirs,
                   label_ours="local", label_base="base", label_theirs="doc")
    assert not after.conflicted
    assert after.text == "Let $I$ be the count of high quality insights.\n"


def test_no_local_math_leaves_visible_placeholders():
    out, lost = restore_math(f"See {PLACEHOLDER} here.\n", "no math at all\n")
    assert out == f"See {PLACEHOLDER} here.\n"
    assert lost == 1


def test_text_without_equations_is_untouched():
    text = "Plain prose, and a $5 price tag.\n"
    out, lost = restore_math(text, text)
    assert out == text
    assert lost == 0


# --------------------------------------------------------------------------
# Conformance with the parser that actually creates the equations
# --------------------------------------------------------------------------

_PANDOC_CASES = [
    "a $x$ b",
    "a $ x$ b",
    "a $x $ b",
    "I paid $5 and $10 total.",
    "$x$5",
    r"a \$x\$ b",
    "`$x$`",
    "$a$ and $b$",
    "$a$$b$",
    r"$\text{eff}$",
    "$$x$$",
    "$$\nx\n$$",
    r"$$a \qquad b$$",
    r"$a\$b$",
    "3$ and 4$",
    "**$x$**",
    "- $x$ item",
    "> $x$ quote",
    "# heading $x$",
    r"$$Y_{\text{eff}} = \frac{I}{T}$$",
]


def _pandoc_math(text: str) -> list[str]:
    import json

    proc = subprocess.run(
        ["pandoc", "-f", "gfm", "-t", "json"],
        input=text, capture_output=True, text=True, check=True,
    )
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("t") == "Math":
                found.append(node["c"][1])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(proc.stdout))
    return found


def test_unconvertible_math_is_read_off_pandocs_warnings():
    stderr = (
        "[WARNING] Could not convert TeX math m^, rendering as TeX:\n"
        "  m^\n    ^\n  unexpected eof\n"
        "[WARNING] Could not convert TeX math \\badcmd{q}, rendering as TeX:\n"
        "  \\badcmd{q}\n"
    )
    assert unconvertible_math(stderr) == ["m^", "\\badcmd{q}"]


def test_clean_pandoc_output_reports_nothing():
    assert unconvertible_math("") == []
    assert unconvertible_math("[WARNING] Deprecated: something else\n") == []


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_pandoc_still_warns_in_the_format_we_parse():
    """Pins the warning text against the real pandoc, not against memory."""
    proc = subprocess.run(
        ["pandoc", "-f", "gfm", "-t", "docx", "-o", os.devnull],
        input="$m^$\n", capture_output=True, text=True,
    )
    assert unconvertible_math(proc.stderr) == ["m^"]


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
@pytest.mark.parametrize("case", _PANDOC_CASES)
def test_scanner_agrees_with_pandoc(case):
    """The scanner must find exactly the math pandoc turns into equations.

    Every disagreement is a miscount, and a miscount is a placeholder left in
    someone's file — so this is pinned against the real parser rather than
    against my reading of its documentation.
    """
    assert [s.content for s in find_math_spans(case)] == _pandoc_math(case)
