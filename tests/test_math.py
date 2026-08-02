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
