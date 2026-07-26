"""Build a themed pandoc reference.docx so styling is *innate* to the doc.

The problem this solves
-----------------------
pandoc's default ``reference.docx`` hard-codes Word's accent-blue into the
heading styles::

    <w:style w:styleId="Heading1">
      <w:rPr><w:color w:val="0F4761" w:themeColor="accent1" .../></w:rPr>

Google Docs imports those definitions as the document's **named styles**. So
however the text is recoloured afterwards through the API, "Heading 1" *itself*
is still blue: the heading dropdown shows blue, the outline shows blue, and any
heading typed later in Google Docs comes out blue. Run-level colour is a coat
of paint over a blue wall.

Patching the reference docx paints the wall. The theme's colours and font go
into the style definitions that pandoc writes and Google Docs imports, so the
document simply *is* the right colour — new headings included.

It also removes the need to re-assert bold after setting a font over the whole
body (see :func:`gdoc_sync.style.apply_styles`): nothing has to be restyled
run-by-run afterwards, so nothing gets clobbered in the first place.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import zipfile
from pathlib import Path

# Theme key → the Word styles it colours. Each heading has a *linked character
# style* (``Heading1Char``) as well as the paragraph style; Word and Google Docs
# both consult it, so colouring only the paragraph style leaves the odd run
# blue.
_STYLE_FOR_KEY = {
    "TITLE": ("Title", "TitleChar"),
    "SUBTITLE": ("Subtitle", "SubtitleChar"),
    "HEADING_1": ("Heading1", "Heading1Char"),
    "HEADING_2": ("Heading2", "Heading2Char"),
    "HEADING_3": ("Heading3", "Heading3Char"),
    "HEADING_4": ("Heading4", "Heading4Char"),
    "HEADING_5": ("Heading5", "Heading5Char"),
    # Levels 7-9 have no theme entry of their own and follow H6.
    "HEADING_6": ("Heading6", "Heading6Char", "Heading7", "Heading7Char",
                  "Heading8", "Heading8Char", "Heading9", "Heading9Char"),
}

# Everything that should simply be in the document font. Footnotes are the
# reason this list is explicit rather than relying on `basedOn Normal`
# inheritance: pandoc's FootnoteText carries no font of its own, and what
# Google Docs resolves it to on import is the theme default, not ours. The
# API-side restyling pass could never fix that either — it walks
# ``body.content``, and footnote text is not in the body.
_BODY_FONT_STYLES = (
    "Normal", "BodyText", "BodyTextChar", "FirstParagraph", "Compact",
    "DefaultParagraphFont", "BlockText",
    "FootnoteText", "FootnoteBlockText", "FootnoteReference",
    "Author", "Date", "Abstract", "AbstractTitle", "Bibliography",
    "Caption", "TableCaption", "ImageCaption", "Figure", "CaptionedFigure",
    "DefinitionTerm", "Definition", "SectionNumber", "TOCHeading", "Hyperlink",
)

# Code keeps its monospace face — applying the prose font here would be a bug,
# not a fix.
_KEEP_FONT_STYLES = frozenset({"VerbatimChar", "SourceCode"})


class ReferenceDocError(RuntimeError):
    """Raised when a themed reference doc could not be produced."""


def _hex(color: str) -> str:
    """``#1f3864`` → ``1F3864`` (the form OOXML wants)."""
    value = str(color).lstrip("#").strip()
    if len(value) == 3:  # #abc shorthand
        value = "".join(c * 2 for c in value)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", value):
        raise ReferenceDocError(f"not a hex colour: {color!r}")
    return value.upper()


def _style_block(xml: str, style_id: str) -> tuple[int, int] | None:
    """Character span of ``<w:style ... w:styleId="X"> … </w:style>``."""
    match = re.search(
        rf'<w:style\b[^>]*w:styleId="{re.escape(style_id)}"[^>]*>.*?</w:style>',
        xml, re.S,
    )
    return match.span() if match else None


def _set_run_props(block: str, *, color: str | None, font: str | None) -> str:
    """Force colour/font onto a style's run properties (``<w:rPr>``).

    Existing ``<w:color>``/``<w:rFonts>`` are replaced outright rather than
    amended, which drops the ``w:themeColor``/``w:asciiTheme`` indirection —
    otherwise the document theme would keep winning over the explicit value.
    """
    additions = ""
    if font:
        block = re.sub(r"<w:rFonts\b[^>]*/>", "", block)
        block = re.sub(r"<w:rFonts\b[^>]*>.*?</w:rFonts>", "", block, flags=re.S)
        esc = font.replace('"', "&quot;")
        additions += f'<w:rFonts w:ascii="{esc}" w:hAnsi="{esc}" w:cs="{esc}"/>'
    if color:
        block = re.sub(r"<w:color\b[^>]*/>", "", block)
        block = re.sub(r"<w:color\b[^>]*>.*?</w:color>", "", block, flags=re.S)
        additions += f'<w:color w:val="{color}"/>'
    if not additions:
        return block

    if "<w:rPr>" in block:
        return block.replace("<w:rPr>", f"<w:rPr>{additions}", 1)
    # No run properties at all — add a block just before the closing tag.
    return block.replace("</w:style>", f"<w:rPr>{additions}</w:rPr></w:style>", 1)


def _patch_doc_defaults(xml: str, font: str) -> str:
    """Put the font in ``<w:docDefaults>`` so unstyled runs inherit it too."""
    match = re.search(r"<w:docDefaults>.*?</w:docDefaults>", xml, re.S)
    if not match:
        return xml
    block = _set_run_props(match.group(0), color=None, font=font)
    if "<w:rPr>" not in match.group(0):
        # docDefaults nests run props one level down; add the pair if absent.
        esc = font.replace('"', "&quot;")
        block = match.group(0).replace(
            "<w:rPrDefault>",
            f'<w:rPrDefault><w:rPr><w:rFonts w:ascii="{esc}" w:hAnsi="{esc}" '
            f'w:cs="{esc}"/></w:rPr>',
            1,
        )
    return xml[: match.start()] + block + xml[match.end():]


def _patch_styles_xml(xml: str, *, font: str | None, palette: dict | None) -> str:
    """Apply the theme to every style Google Docs will import."""
    headings = (palette or {}).get("headings", {})
    body_color = _hex(palette["text"]) if palette and palette.get("text") else None
    link_color = _hex(palette["link"]) if palette and palette.get("link") else None

    if font:
        xml = _patch_doc_defaults(xml, font)

    # style id → (colour, font). Later entries win, so headings are applied
    # after the blanket body pass.
    targets: dict[str, tuple[str | None, str | None]] = {}
    for style_id in _BODY_FONT_STYLES:
        targets[style_id] = (body_color, font)
    if link_color:
        targets["Hyperlink"] = (link_color, font)
    for key, style_ids in _STYLE_FOR_KEY.items():
        color = headings.get(key)
        for style_id in style_ids:
            targets[style_id] = (_hex(color) if color else None, font)

    for style_id, (color, style_font) in targets.items():
        if style_id in _KEEP_FONT_STYLES:
            continue
        span = _style_block(xml, style_id)
        if span is None:
            continue
        start, end = span
        patched = _set_run_props(xml[start:end], color=color, font=style_font)
        xml = xml[:start] + patched + xml[end:]

    return xml


def _default_reference_docx(dest: Path) -> None:
    """Ask pandoc for its built-in reference.docx."""
    try:
        proc = subprocess.run(
            ["pandoc", "--print-default-data-file", "reference.docx"],
            capture_output=True, timeout=60,
        )
    except FileNotFoundError:
        raise ReferenceDocError("pandoc not found on PATH") from None
    except subprocess.SubprocessError as e:
        raise ReferenceDocError(f"pandoc failed: {e}") from None
    if proc.returncode != 0 or not proc.stdout:
        raise ReferenceDocError(
            f"pandoc could not supply reference.docx (exit {proc.returncode})"
        )
    dest.write_bytes(proc.stdout)


def _cache_key(font: str | None, palette: dict | None) -> str:
    material = repr((font, sorted((palette or {}).items(), key=lambda kv: kv[0])))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def build_reference_docx(font: str | None, palette: dict | None) -> Path | None:
    """A reference.docx carrying ``font`` and ``palette``, cached on disk.

    Returns ``None`` when no styling was requested. Raises
    :class:`ReferenceDocError` if one was requested but could not be built —
    callers treat that as "fall back to API-side styling", never as fatal.
    """
    if not font and not palette:
        return None

    from .syncstate import sync_dir

    cache_dir = sync_dir() / "refdocs"
    cached = cache_dir / f"ref-{_cache_key(font, palette)}.docx"
    if cached.exists():
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    source = cache_dir / f".source-{_cache_key(font, palette)}.docx"
    try:
        _default_reference_docx(source)

        with zipfile.ZipFile(source) as zin:
            entries = [(i, zin.read(i.filename)) for i in zin.infolist()]

        tmp = cached.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for info, data in entries:
                if info.filename == "word/styles.xml":
                    data = _patch_styles_xml(
                        data.decode("utf-8"), font=font, palette=palette
                    ).encode("utf-8")
                zout.writestr(info, data)
        tmp.replace(cached)
        return cached
    except ReferenceDocError:
        raise
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as e:
        raise ReferenceDocError(f"could not build a themed reference doc: {e}") from e
    finally:
        try:
            source.unlink()
        except OSError:
            pass


def clear_cache() -> int:
    """Delete cached reference docs. Returns how many were removed."""
    from .syncstate import sync_dir

    cache_dir = sync_dir() / "refdocs"
    if not cache_dir.is_dir():
        return 0
    n = 0
    for p in cache_dir.glob("*.docx"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n


def styled_reference_docx(font: str | None, theme: str | None):
    """Resolve a theme name and build its reference doc; ``None`` on failure.

    Convenience wrapper for create/push: styling that cannot be baked in is not
    an error, it just means the API pass has more to do.
    """
    from .style import resolve_theme

    palette = resolve_theme(theme)
    try:
        return build_reference_docx(font, palette)
    except ReferenceDocError:
        return None
