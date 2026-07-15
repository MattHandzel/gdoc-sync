#!/usr/bin/env python3
"""Document-wide styling applied via the Google Docs API.

Used by both `create` and `push` so a doc looks the same regardless of how its
content got in:
  - one font family across the whole body (default Garamond)
  - a color theme (default: professional): page background, body text color,
    accent-colored headings, and links
  - bold preserved (setting the font resets weight, which clears bold)

Themes are the built-ins below plus any user-defined themes from the config's
``themes:`` section (see resolve_theme / _normalize_theme).
"""

from __future__ import annotations

from .services import NUM_RETRIES

# ---------------------------------------------------------------------------
# Color themes
# ---------------------------------------------------------------------------

_HEADING_KEYS = ["HEADING_1", "HEADING_2", "HEADING_3",
                 "HEADING_4", "HEADING_5", "HEADING_6"]


# Catppuccin palettes (https://catppuccin.com). Latte = light, others = dark.
#
# Headings are a rainbow by level, RED = highest (H1) descending through the
# spectrum to violet (H6) — built from each flavor's red/peach/yellow/green/
# blue/mauve accents. TITLE/SUBTITLE track H1/H2.
def _rainbow(red, peach, yellow, green, blue, mauve):
    return {
        "TITLE": red, "SUBTITLE": peach,
        "HEADING_1": red, "HEADING_2": peach, "HEADING_3": yellow,
        "HEADING_4": green, "HEADING_5": blue, "HEADING_6": mauve,
    }


def _ramp(title, subtitle, h1, h2, h3, rest):
    return {
        "TITLE": title, "SUBTITLE": subtitle,
        "HEADING_1": h1, "HEADING_2": h2, "HEADING_3": h3,
        "HEADING_4": rest, "HEADING_5": rest, "HEADING_6": rest,
    }


THEMES: dict[str, dict] = {
    # The default: what a shared business/work doc is expected to look like.
    # Paginated, near-black body text, navy heading ramp, standard link blue.
    "professional": {
        "background": "#ffffff",
        "text": "#202124",
        "link": "#0b57d0",
        "pageless": False,
        "headings": _ramp("#1f3864", "#595959",
                          "#1f3864", "#2f5496", "#4472c4", "#44546a"),
    },
    # Black on white, no accent color anywhere. For people who want styling
    # limited to the font.
    "minimal": {
        "background": "#ffffff",
        "text": "#000000",
        "link": "#0b57d0",
        "pageless": True,
        "headings": {k: "#000000" for k in _HEADING_KEYS + ["TITLE", "SUBTITLE"]},
    },
    "catppuccin-latte": {
        "background": "#ffffff",  # white page (kept white by request)
        "text": "#4c4f69",        # body text
        "link": "#1155cc",        # normal Google Docs hyperlink blue
        "pageless": True,
        "headings": _rainbow("#d20f39", "#fe640b", "#df8e1d", "#40a02b", "#1e66f5", "#8839ef"),
    },
    "catppuccin-mocha": {
        "background": "#1e1e2e",
        "text": "#cdd6f4",
        "link": "#89b4fa",
        "pageless": True,
        "headings": _rainbow("#f38ba8", "#fab387", "#f9e2af", "#a6e3a1", "#89b4fa", "#cba6f7"),
    },
    "catppuccin-frappe": {
        "background": "#303446",
        "text": "#c6d0f5",
        "link": "#8caaee",
        "pageless": True,
        "headings": _rainbow("#e78284", "#ef9f76", "#e5c890", "#a6d189", "#8caaee", "#ca9ee6"),
    },
    "catppuccin-macchiato": {
        "background": "#24273a",
        "text": "#cad3f5",
        "link": "#8aadf4",
        "pageless": True,
        "headings": _rainbow("#ed8796", "#f5a97f", "#eed49f", "#a6da95", "#8aadf4", "#c6a0f6"),
    },
}


def _normalize_theme(raw: dict) -> dict:
    """Turn a user-defined config theme into a full palette.

    Headings accept three shapes:
      heading_color: "#1f3864"                       — one color for all levels
      headings: ["#a", "#b", ...]                     — H1..H6 by position
      headings: {HEADING_1: "#a", TITLE: "#b", ...}   — explicit map
    """
    palette = {
        "background": raw.get("background", "#ffffff"),
        "text": raw.get("text", "#202124"),
        "link": raw.get("link", "#0b57d0"),
        "pageless": bool(raw.get("pageless", False)),
    }
    headings = raw.get("headings", raw.get("heading_color", "#1f3864"))
    if isinstance(headings, str):
        headings = {k: headings for k in _HEADING_KEYS + ["TITLE", "SUBTITLE"]}
    elif isinstance(headings, list):
        colors = list(headings) or ["#1f3864"]
        colors += [colors[-1]] * (6 - len(colors))
        headings = dict(zip(_HEADING_KEYS, colors))
        headings["TITLE"] = colors[0]
        headings["SUBTITLE"] = colors[1] if len(colors) > 1 else colors[0]
    else:
        headings = {str(k).upper(): v for k, v in dict(headings).items()}
    palette["headings"] = headings
    return palette


def available_themes() -> list[str]:
    """Built-in theme names plus user-defined ones from the config."""
    from .config import get_custom_themes
    return list(THEMES) + [t for t in get_custom_themes() if t not in THEMES]


def resolve_theme(name: str | None) -> dict | None:
    """A palette for ``name``: user-defined config themes first, then built-ins."""
    if not name:
        return None
    from .config import get_custom_themes
    raw = get_custom_themes().get(name)
    if isinstance(raw, dict):
        return _normalize_theme(raw)
    return THEMES.get(name)


def _rgb(hexstr: str) -> dict:
    h = hexstr.lstrip("#")
    return {
        "red": int(h[0:2], 16) / 255.0,
        "green": int(h[2:4], 16) / 255.0,
        "blue": int(h[4:6], 16) / 255.0,
    }


def _optional_color(hexstr: str) -> dict:
    """An OptionalColor for foregroundColor / background.color fields."""
    return {"color": {"rgbColor": _rgb(hexstr)}}


def _walk_runs(content: list):
    """Yield (paragraph_named_style, run_element) for every text run, recursing tables."""
    for el in content:
        para = el.get("paragraph")
        if para:
            named = para.get("paragraphStyle", {}).get("namedStyleType", "")
            for r in para.get("elements", []):
                if r.get("textRun"):
                    yield named, r
        table = el.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _walk_runs(cell.get("content", []))


def apply_styles(
    docs_service,
    doc_id: str,
    *,
    font: str | None = None,
    theme: str | None = None,
) -> bool:
    """Apply font + color theme over the whole document in one atomic batch.

    Setting `weightedFontFamily` resets a run's font weight to 400, which Google
    Docs treats as clearing `bold` (italic/underline/color are weight-independent
    and survive). So when a font is applied we record bold runs first and
    re-assert bold AFTER the font request in the same batch.

    The theme paints the whole body the theme text color, then re-colors heading
    runs (accent) and link runs on top, and sets the page background. Headings
    keep their sizes from their named paragraph styles; only typeface/color change.

    `theme` is a key in THEMES, or None/unknown to skip theming (font only).

    Returns True if a request was sent, False if there was nothing to style.
    """
    doc = docs_service.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)
    body_content = doc.get("body", {}).get("content", [])
    if not body_content:
        return False

    doc_end = body_content[-1].get("endIndex", 1)
    # Body starts at index 1; the trailing newline at doc_end-1 can't be styled.
    if doc_end <= 2:
        return False
    cap = doc_end - 1
    full_range = {"startIndex": 1, "endIndex": cap}

    palette = resolve_theme(theme)
    requests: list[dict] = []

    # 1. Font over the whole body (clears bold — restored below).
    if font:
        requests.append({
            "updateTextStyle": {
                "range": full_range,
                "textStyle": {"weightedFontFamily": {"fontFamily": font}},
                "fields": "weightedFontFamily",
            }
        })

    # 2. Body text color over the whole body (headings/links re-colored below).
    if palette:
        requests.append({
            "updateTextStyle": {
                "range": full_range,
                "textStyle": {"foregroundColor": _optional_color(palette["text"])},
                "fields": "foregroundColor",
            }
        })

    # Collect ranges that need per-run treatment.
    bold_ranges: list[tuple[int, int]] = []
    heading_ranges: list[tuple[int, int, str]] = []  # (s, e, named-style)
    link_ranges: list[tuple[int, int]] = []
    headings_map = palette.get("headings", {}) if palette else {}
    for named, r in _walk_runs(body_content):
        s, e = r.get("startIndex"), r.get("endIndex")
        if s is None or e is None or e <= s:
            continue
        style = r["textRun"].get("textStyle", {})
        if font and style.get("bold"):
            bold_ranges.append((s, e))
        if palette and named in headings_map:
            heading_ranges.append((s, e, named))
        if palette and style.get("link"):
            link_ranges.append((s, e))

    def _clamp(s, e):
        return s, min(e, cap)

    # 3. Re-assert bold cleared by the font request.
    for s, e in bold_ranges:
        s, e = _clamp(s, e)
        if e > s:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": s, "endIndex": e},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }
            })

    # 4. Color headings — rainbow by level (red highest).
    if palette:
        for s, e, named in heading_ranges:
            s, e = _clamp(s, e)
            if e > s:
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": s, "endIndex": e},
                        "textStyle": {"foregroundColor": _optional_color(headings_map[named])},
                        "fields": "foregroundColor",
                    }
                })
        # 5. Links to the theme's normal hyperlink color (the body paint above
        #    would otherwise have made them the body text color).
        for s, e in link_ranges:
            s, e = _clamp(s, e)
            if e > s:
                requests.append({
                    "updateTextStyle": {
                        "range": {"startIndex": s, "endIndex": e},
                        "textStyle": {"foregroundColor": _optional_color(palette["link"])},
                        "fields": "foregroundColor",
                    }
                })
        # 6. Page background.
        requests.append({
            "updateDocumentStyle": {
                "documentStyle": {"background": {"color": _optional_color(palette["background"])}},
                "fields": "background",
            }
        })
        # 7. Pageless layout.
        if palette.get("pageless"):
            requests.append({
                "updateDocumentStyle": {
                    "documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
                    "fields": "documentFormat.documentMode",
                }
            })

    if not requests:
        return False

    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute(num_retries=NUM_RETRIES)
    return True


# Backwards-compatible alias for the font-only entry point.
def apply_font(docs_service, doc_id: str, font: str) -> bool:
    return apply_styles(docs_service, doc_id, font=font)


# ---------------------------------------------------------------------------
# Table borders
# ---------------------------------------------------------------------------

def _solid_border(width_pt: float = 1.0) -> dict:
    """A solid black table-cell border of the given width."""
    return {
        "color": {"color": {"rgbColor": {}}},  # empty rgbColor == black (0,0,0)
        "width": {"magnitude": width_pt, "unit": "PT"},
        "dashStyle": "SOLID",
    }


def apply_table_borders(docs_service, doc_id: str, width_pt: float = 1.0) -> int:
    """Give every table cell in the doc visible solid borders.

    pandoc-generated docx tables import into Google Docs WITHOUT visible cell
    borders, so the grid is invisible. We set all four borders on every cell
    explicitly via the Docs API. Returns the number of tables styled.
    """
    doc = docs_service.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)
    body = doc.get("body", {}).get("content", [])
    border = _solid_border(width_pt)

    requests = []
    tables = 0
    for element in body:
        table = element.get("table")
        if not table:
            continue
        start_index = element.get("startIndex")
        rows = table.get("rows", 0)
        cols = table.get("columns", 0)
        if start_index is None or rows < 1 or cols < 1:
            continue
        tables += 1
        # One request styles the whole rectangular block of cells from (0,0).
        requests.append({
            "updateTableCellStyle": {
                "tableCellStyle": {
                    "borderTop": border,
                    "borderBottom": border,
                    "borderLeft": border,
                    "borderRight": border,
                },
                "fields": "borderTop,borderBottom,borderLeft,borderRight",
                "tableRange": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": start_index},
                        "rowIndex": 0,
                        "columnIndex": 0,
                    },
                    "rowSpan": rows,
                    "columnSpan": cols,
                },
            }
        })

    if requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute(num_retries=NUM_RETRIES)
    return tables
