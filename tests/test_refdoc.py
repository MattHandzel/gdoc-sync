"""Themed reference.docx generation (issue #4: headings imported as blue).

pandoc's stock reference.docx hard-codes Word's accent blue into the heading
styles, and Google Docs imports those as the document's named styles — so
recolouring runs through the API leaves "Heading 1" itself blue. These tests
pin the patched output: the theme lands in the style definitions, the blue is
gone, and code keeps its monospace face.
"""

from __future__ import annotations

import re
import shutil
import zipfile

import pytest

from gdoc_sync import refdoc
from gdoc_sync.style import resolve_theme

pytestmark = pytest.mark.skipif(
    shutil.which("pandoc") is None, reason="pandoc is required to supply reference.docx"
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def styles_xml(path) -> str:
    return zipfile.ZipFile(path).read("word/styles.xml").decode("utf-8")


def style_block(xml: str, style_id: str) -> str:
    m = re.search(
        rf'<w:style\b[^>]*w:styleId="{style_id}"[^>]*>.*?</w:style>', xml, re.S
    )
    assert m, f"style {style_id} missing"
    return m.group(0)


# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

def test_hex_normalizes_forms():
    assert refdoc._hex("#1f3864") == "1F3864"
    assert refdoc._hex("1F3864") == "1F3864"
    assert refdoc._hex("#abc") == "AABBCC"


def test_hex_rejects_nonsense():
    with pytest.raises(refdoc.ReferenceDocError):
        refdoc._hex("cornflower")


# ---------------------------------------------------------------------------
# The actual fix
# ---------------------------------------------------------------------------

def test_headings_carry_the_theme_colour_not_pandoc_blue():
    palette = resolve_theme("catppuccin-latte")
    doc = refdoc.build_reference_docx("Garamond", palette)
    xml = styles_xml(doc)

    # H1 is the theme's red, not Word's accent blue.
    assert '<w:color w:val="D20F39"/>' in style_block(xml, "Heading1")
    assert '<w:color w:val="FE640B"/>' in style_block(xml, "Heading2")
    assert "0F4761" not in xml, "pandoc's accent blue must be gone entirely"


def test_theme_colour_indirection_is_removed():
    """A leftover w:themeColor would let the document theme win again."""
    palette = resolve_theme("professional")
    xml = styles_xml(refdoc.build_reference_docx("Garamond", palette))
    assert "w:themeColor" not in xml
    assert "asciiTheme" not in xml


def test_linked_character_styles_are_coloured_too():
    palette = resolve_theme("catppuccin-latte")
    xml = styles_xml(refdoc.build_reference_docx("Garamond", palette))
    assert '<w:color w:val="D20F39"/>' in style_block(xml, "Heading1Char")


def test_footnotes_get_the_document_font():
    """Footnote text is outside body.content, so the API restyling pass can
    never reach it — baking the font into the style is the only fix."""
    xml = styles_xml(refdoc.build_reference_docx("Garamond", None))
    for style_id in ("FootnoteText", "FootnoteReference", "FootnoteBlockText"):
        assert 'w:ascii="Garamond"' in style_block(xml, style_id), style_id


def test_body_and_default_styles_get_the_font():
    xml = styles_xml(refdoc.build_reference_docx("EB Garamond", None))
    assert 'w:ascii="EB Garamond"' in style_block(xml, "Normal")
    assert 'w:ascii="EB Garamond"' in style_block(xml, "BodyText")
    # …and the document-wide default, for anything unstyled.
    defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>", xml, re.S).group(0)
    assert 'w:ascii="EB Garamond"' in defaults


def test_code_keeps_its_monospace_font():
    """Applying the prose font to code would be a regression, not a fix."""
    xml = styles_xml(refdoc.build_reference_docx("Garamond", None))
    assert "Consolas" in style_block(xml, "VerbatimChar")
    assert "Garamond" not in style_block(xml, "VerbatimChar")


def test_hyperlink_uses_the_theme_link_colour():
    palette = resolve_theme("catppuccin-mocha")
    xml = styles_xml(refdoc.build_reference_docx("Garamond", palette))
    assert '<w:color w:val="89B4FA"/>' in style_block(xml, "Hyperlink")


def test_body_text_colour_comes_from_the_theme():
    palette = resolve_theme("catppuccin-mocha")
    xml = styles_xml(refdoc.build_reference_docx("Garamond", palette))
    assert '<w:color w:val="CDD6F4"/>' in style_block(xml, "Normal")


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

def test_result_is_a_valid_docx_pandoc_accepts():
    """The patched zip has to survive a real pandoc round trip."""
    import subprocess

    palette = resolve_theme("catppuccin-latte")
    ref = refdoc.build_reference_docx("Garamond", palette)
    out = ref.parent / "rendered.docx"
    proc = subprocess.run(
        ["pandoc", "-f", "gfm", "-t", "docx", "--reference-doc", str(ref),
         "-o", str(out)],
        input="# Heading\n\nBody with a footnote[^1].\n\n[^1]: note text\n",
        text=True, capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert out.exists() and out.stat().st_size > 0
    assert "Garamond" in zipfile.ZipFile(out).read("word/styles.xml").decode()


def test_build_is_cached_between_calls():
    palette = resolve_theme("professional")
    first = refdoc.build_reference_docx("Garamond", palette)
    mtime = first.stat().st_mtime_ns
    second = refdoc.build_reference_docx("Garamond", palette)
    assert first == second
    assert second.stat().st_mtime_ns == mtime, "should not have been rebuilt"


def test_different_themes_get_different_cache_entries():
    a = refdoc.build_reference_docx("Garamond", resolve_theme("professional"))
    b = refdoc.build_reference_docx("Garamond", resolve_theme("catppuccin-mocha"))
    c = refdoc.build_reference_docx("Helvetica", resolve_theme("professional"))
    assert len({a, b, c}) == 3


def test_no_styling_requested_returns_none():
    assert refdoc.build_reference_docx(None, None) is None


def test_styled_reference_docx_rejects_a_bad_theme_name():
    """A typo'd theme used to fall through to an unthemed (silently plain) doc."""
    from gdoc_sync.style import UnknownThemeError

    with pytest.raises(UnknownThemeError, match="no-such-theme"):
        refdoc.styled_reference_docx("Garamond", "no-such-theme")


def test_styled_reference_docx_without_a_theme_is_font_only():
    assert refdoc.styled_reference_docx("Garamond", None) is not None
    assert refdoc.styled_reference_docx("Garamond", "none") is not None


def test_clear_cache_removes_built_docs():
    refdoc.build_reference_docx("Garamond", resolve_theme("professional"))
    assert refdoc.clear_cache() >= 1
    assert refdoc.clear_cache() == 0


def test_font_names_with_quotes_do_not_break_the_xml():
    doc = refdoc.build_reference_docx('Wei"rd', None)
    xml = styles_xml(doc)
    assert "&quot;" in xml
    # Still parseable.
    import xml.etree.ElementTree as ET
    ET.fromstring(xml)


def test_patched_xml_stays_well_formed_for_every_builtin_theme():
    import xml.etree.ElementTree as ET

    from gdoc_sync.style import THEMES

    for name in THEMES:
        doc = refdoc.build_reference_docx("Garamond", resolve_theme(name))
        ET.fromstring(styles_xml(doc))  # raises if malformed
