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

from googleapiclient.errors import HttpError

from .callouts import table_callout
from .convert import _is_mono
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


# Syntax colors for fenced code blocks, keyed by skylighting's token names
# (pandoc's highlighter). Without this pandoc falls back to its built-in
# `pygments` style, so a catppuccin doc got 1990s-green keywords in its code
# blocks while every other element followed the theme.
#
# The groupings mirror how a treesitter theme assigns captures, so a fence in
# the Google Doc lands on the same accent as the same fence in the editor.
def _code(keyword, string, number, comment, function, type_, operator, variable):
    return {
        # @keyword, @keyword.import, @keyword.directive
        "Keyword": keyword, "ControlFlow": keyword,
        "Import": keyword, "Preprocessor": keyword,
        # @string and its relatives
        "String": string, "VerbatimString": string,
        "SpecialString": string, "Char": string,
        # @number / @boolean / @constant
        "DecVal": number, "BaseN": number, "Float": number, "Constant": number,
        # @comment
        "Comment": comment, "Documentation": comment,
        "CommentVar": comment, "Annotation": comment,
        # @function
        "Function": function, "BuiltIn": function,
        # @type / @attribute
        "DataType": type_, "Attribute": type_,
        # @operator
        "Operator": operator, "SpecialChar": operator,
        # plain identifiers and anything unclassified
        "Variable": variable, "Normal": variable, "Other": variable,
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
        "code": _code("#7a3e9d", "#0b7261", "#9a4600", "#5f6368", "#1a4fa0", "#8a6d00", "#5f6368", "#202124"),
    },
    # Black on white, no accent color anywhere. For people who want styling
    # limited to the font.
    "minimal": {
        "background": "#ffffff",
        "text": "#000000",
        "link": "#0b57d0",
        "pageless": True,
        "headings": {k: "#000000" for k in _HEADING_KEYS + ["TITLE", "SUBTITLE"]},
        "code": _code("#000000", "#000000", "#000000", "#5f6368", "#000000", "#000000", "#000000", "#000000"),
    },
    "catppuccin-latte": {
        "background": "#ffffff",  # white page (kept white by request)
        "text": "#4c4f69",        # body text
        "link": "#1155cc",        # normal Google Docs hyperlink blue
        "pageless": True,
        "headings": _rainbow("#d20f39", "#fe640b", "#df8e1d", "#40a02b", "#1e66f5", "#8839ef"),
        "code": _code("#8839ef", "#40a02b", "#fe640b", "#9ca0b0", "#1e66f5", "#df8e1d", "#04a5e5", "#4c4f69"),
    },
    "catppuccin-mocha": {
        "background": "#1e1e2e",
        "text": "#cdd6f4",
        "link": "#89b4fa",
        "pageless": True,
        "headings": _rainbow("#f38ba8", "#fab387", "#f9e2af", "#a6e3a1", "#89b4fa", "#cba6f7"),
        "code": _code("#cba6f7", "#a6e3a1", "#fab387", "#6c7086", "#89b4fa", "#f9e2af", "#89dceb", "#cdd6f4"),
    },
    "catppuccin-frappe": {
        "background": "#303446",
        "text": "#c6d0f5",
        "link": "#8caaee",
        "pageless": True,
        "headings": _rainbow("#e78284", "#ef9f76", "#e5c890", "#a6d189", "#8caaee", "#ca9ee6"),
        "code": _code("#ca9ee6", "#a6d189", "#ef9f76", "#737994", "#8caaee", "#e5c890", "#99d1db", "#c6d0f5"),
    },
    "catppuccin-macchiato": {
        "background": "#24273a",
        "text": "#cad3f5",
        "link": "#8aadf4",
        "pageless": True,
        "headings": _rainbow("#ed8796", "#f5a97f", "#eed49f", "#a6da95", "#8aadf4", "#c6a0f6"),
        "code": _code("#c6a0f6", "#a6da95", "#f5a97f", "#6e738d", "#8aadf4", "#eed49f", "#91d7e3", "#cad3f5"),
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

    # Code-block syntax colors. A user theme may give the eight roles
    # (keyword/string/number/comment/function/type/operator/variable) and get a
    # full skylighting token map, or a complete token map directly. Omitting it
    # is fine — pandoc then keeps its default highlight style.
    code = raw.get("code")
    if isinstance(code, dict) and code:
        roles = ("keyword", "string", "number", "comment",
                 "function", "type", "operator", "variable")
        if any(r in code for r in roles):
            body = palette["text"]
            palette["code"] = _code(*[code.get(r, body) for r in roles])
        else:
            palette["code"] = {str(k): v for k, v in code.items()}
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
    baked: bool = False,
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

    ``baked`` says the font and heading/body colors already arrived in the
    document's *named styles* via a themed reference.docx (see :mod:`.refdoc`).
    That is the better outcome — the document genuinely is that color, so a
    heading typed later in Google Docs comes out right, and footnotes (which
    live outside ``body.content`` and so are unreachable from here) are covered.
    Only what a docx cannot express is left to do: page background, pageless
    layout, and link color.

    Returns True if a request was sent, False if there was nothing to style.
    """
    doc = docs_service.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)
    requests = style_requests(doc, font=font, theme=theme, baked=baked)
    if not requests:
        return False
    docs_service.documents().batchUpdate(
        documentId=doc_id, body={"requests": requests}
    ).execute(num_retries=NUM_RETRIES)
    return True


def style_requests(doc: dict, *, font: str | None = None,
                   theme: str | None = None, baked: bool = False) -> list[dict]:
    """Build :func:`apply_styles`' requests for an already-fetched ``doc``.

    Split out so a caller can merge them with other requests into one
    batchUpdate. `create` and `push` both style *and* border the same document,
    and doing that as two fetches plus two batches cost about a second of round
    trips on every push — see :func:`apply_document_styling`.
    """
    body_content = doc.get("body", {}).get("content", [])
    if not body_content:
        return []

    if baked:
        font = None  # already in the named styles; re-applying would clear bold

    doc_end = body_content[-1].get("endIndex", 1)
    # Body starts at index 1; the trailing newline at doc_end-1 can't be styled.
    if doc_end <= 2:
        return []
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
    if palette and not baked:
        requests.append({
            "updateTextStyle": {
                "range": full_range,
                "textStyle": {"foregroundColor": _optional_color(palette["text"])},
                "fields": "foregroundColor",
            }
        })

    # Collect ranges that need per-run treatment.
    bold_ranges: list[tuple[int, int]] = []
    mono_ranges: list[tuple[int, int, str]] = []  # (s, e, the run's own family)
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
        if font and _is_mono(style):
            # A monospace run is a fenced code block or an inline code span,
            # and the font is the ONLY thing that says so — the puller reads
            # code back by looking for it (see convert._is_code_paragraph).
            # Letting the document-wide font sweep over it turns every fence
            # in the document into ordinary prose on the next pull.
            mono_ranges.append(
                (s, e, style["weightedFontFamily"]["fontFamily"]))
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

    # 3b. Re-assert monospace, cleared by the same font request.
    for s, e, family in mono_ranges:
        s, e = _clamp(s, e)
        if e > s:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": s, "endIndex": e},
                    "textStyle": {"weightedFontFamily": {"fontFamily": family}},
                    "fields": "weightedFontFamily",
                }
            })

    # 4. Color headings — rainbow by level (red highest).
    if palette:
        for s, e, named in (() if baked else heading_ranges):
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
    # 6. Footnotes. These live in their own segments, not in body.content, so
    #    the walk above never reaches them and they keep the imported default
    #    font — the reason footnote text used to come out in the wrong face.
    #    Ranges into a footnote must name its segmentId.
    if not baked:
        for footnote_id, footnote in (doc.get("footnotes") or {}).items():
            for _named, run in _walk_runs(footnote.get("content", [])):
                s, e = run.get("startIndex"), run.get("endIndex")
                if s is None or e is None or e <= s:
                    continue
                text_style: dict = {}
                fields = []
                if font:
                    text_style["weightedFontFamily"] = {"fontFamily": font}
                    fields.append("weightedFontFamily")
                    if run["textRun"].get("textStyle", {}).get("bold"):
                        text_style["bold"] = True
                        fields.append("bold")
                if palette:
                    text_style["foregroundColor"] = _optional_color(palette["text"])
                    fields.append("foregroundColor")
                if not fields:
                    continue
                requests.append({
                    "updateTextStyle": {
                        "range": {"segmentId": footnote_id,
                                  "startIndex": s, "endIndex": e},
                        "textStyle": text_style,
                        "fields": ",".join(fields),
                    }
                })

    if palette:
        # 7. Page background.
        requests.append({
            "updateDocumentStyle": {
                "documentStyle": {"background": {"color": _optional_color(palette["background"])}},
                "fields": "background",
            }
        })
        # 8. Pageless layout.
        if palette.get("pageless"):
            requests.append({
                "updateDocumentStyle": {
                    "documentStyle": {"documentFormat": {"documentMode": "PAGELESS"}},
                    "fields": "documentFormat.documentMode",
                }
            })

    return requests


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
    requests, tables = table_border_requests(doc, width_pt)
    if requests:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute(num_retries=NUM_RETRIES)
    return tables


def table_border_requests(doc: dict, width_pt: float = 1.0) -> tuple[list[dict], int]:
    """Build :func:`apply_table_borders`' requests for an already-fetched ``doc``.

    Returns ``(requests, table_count)``.
    """
    body = doc.get("body", {}).get("content", [])
    border = _solid_border(width_pt)

    requests = []
    tables = 0
    for element in body:
        table = element.get("table")
        if not table:
            continue
        # A callout is a table too, and callout_requests gives it an accent
        # border of its own. Boxing it in plain black as well would undo the
        # whole look — and since both run in one batch, whichever request came
        # second would simply win.
        if table_callout(table) is not None:
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

    return requests, tables


def _luminance(hexstr: str) -> float:
    c = _rgb(hexstr)
    return 0.2126 * c["red"] + 0.7152 * c["green"] + 0.0722 * c["blue"]


def _mix(a: str, b: str, t: float) -> str:
    """Blend hex colour ``a`` toward ``b`` by fraction ``t``."""
    ca, cb = _rgb(a), _rgb(b)
    out = "".join(
        f"{round(255 * (ca[k] + (cb[k] - ca[k]) * t)):02x}"
        for k in ("red", "green", "blue")
    )
    return f"#{out}"


def callout_colors(spec, theme: dict | None) -> tuple[str, str]:
    """The (accent, background) a callout should use under ``theme``.

    Derived rather than tabulated, so every theme — including a user's own
    from the config's ``themes:`` section — gets callouts that belong to it
    without anyone maintaining fourteen colours per theme. The tint is the
    accent blended most of the way into the page, which keeps body text
    readable on top of it whatever the page happens to be.

    On a dark page the light-page accents are too dark to read, so they are
    lifted toward white first and the tint is taken a little stronger — the
    same reason a dark editor theme uses brighter syntax colours.
    """
    page = (theme or {}).get("background") or "#ffffff"
    accent = spec.accent
    if _luminance(page) < 0.5:
        accent = _mix(accent, "#ffffff", 0.45)
        return accent, _mix(page, accent, 0.18)
    return accent, _mix(page, accent, 0.12)


def callout_requests(doc: dict, theme: dict | None = None) -> tuple[list[dict], int]:
    """Style every callout table in an already-fetched ``doc``.

    Returns ``(requests, callout_count)``. Each callout gets a tinted cell, a
    thick accent rule down its left edge, breathing room inside the cell, and
    its title run in the accent colour — the Obsidian look, expressed in the
    only vocabulary the Docs API has for it.

    The other three borders are set to zero width rather than left alone:
    pandoc's imported tables arrive with visible edges, and a full box around
    a tinted panel reads as a table of one cell instead of a callout.
    """
    requests: list[dict] = []
    count = 0
    for element in doc.get("body", {}).get("content", []):
        table = element.get("table")
        start = element.get("startIndex")
        if not table or start is None:
            continue
        found = table_callout(table)
        if found is None:
            continue
        spec, _title, cell = found
        accent, tint = callout_colors(spec, theme)
        count += 1

        edge = {"color": _optional_color(accent),
                "width": {"magnitude": 3, "unit": "PT"}, "dashStyle": "SOLID"}
        blank = {"color": _optional_color(tint),
                 "width": {"magnitude": 0, "unit": "PT"}, "dashStyle": "SOLID"}
        pad = {"magnitude": 8, "unit": "PT"}
        requests.append({
            "updateTableCellStyle": {
                "tableCellStyle": {
                    "backgroundColor": _optional_color(tint),
                    "borderLeft": edge,
                    "borderTop": blank, "borderBottom": blank, "borderRight": blank,
                    "paddingLeft": pad, "paddingRight": pad,
                    "paddingTop": pad, "paddingBottom": pad,
                },
                "fields": ("backgroundColor,borderLeft,borderTop,borderBottom,"
                           "borderRight,paddingLeft,paddingRight,paddingTop,"
                           "paddingBottom"),
                "tableRange": {
                    "tableCellLocation": {
                        "tableStartLocation": {"index": start},
                        "rowIndex": 0,
                        "columnIndex": 0,
                    },
                    "rowSpan": 1,
                    "columnSpan": 1,
                },
            }
        })

        title_range = _callout_title_range(cell)
        if title_range:
            requests.append({
                "updateTextStyle": {
                    "range": {"startIndex": title_range[0], "endIndex": title_range[1]},
                    "textStyle": {"bold": True, "foregroundColor": _optional_color(accent)},
                    "fields": "bold,foregroundColor",
                }
            })

    return requests, count


def _callout_title_range(cell: dict) -> tuple[int, int] | None:
    """The index range of the callout's title paragraph, minus its newline."""
    for element in cell.get("content", []):
        para = element.get("paragraph")
        if para is None:
            return None
        text = "".join(
            e.get("textRun", {}).get("content", "") for e in para.get("elements", [])
        )
        if not text.strip():
            continue
        start = element.get("startIndex")
        end = element.get("endIndex")
        if start is None or end is None:
            return None
        # The paragraph mark is not part of the run and cannot be styled.
        return start, max(start + 1, end - 1)
    return None


def apply_document_styling(
    docs_service,
    doc_id: str,
    *,
    font: str | None = None,
    theme: str | None = None,
    baked: bool = False,
    table_width_pt: float = 1.0,
) -> tuple[bool, int, int]:
    """Style the document, border its tables and paint its callouts in ONE
    fetch and ONE batch.

    `create` and `push` both need styling and table borders applied to the same
    freshly-uploaded document. Doing that through :func:`apply_styles` and
    :func:`apply_table_borders` meant four sequential API round trips — two
    fetches of the same document, then two batches — which measured at roughly
    1.1 seconds, the single largest cost in a push after the upload itself.

    Merging them is safe because none of these requests move text: character
    styling, table-cell styling and document style all leave every index where
    it was, so the offsets read from one fetch stay valid for the whole batch.
    Ordering within the batch is preserved as well, which is what the font/bold
    interaction in :func:`style_requests` relies on.

    Returns ``(styled, tables_bordered, callouts_painted)``.
    """
    doc = docs_service.documents().get(documentId=doc_id).execute(num_retries=NUM_RETRIES)
    requests = style_requests(doc, font=font, theme=theme, baked=baked)
    border_requests, tables = table_border_requests(doc, table_width_pt)
    requests += border_requests
    # Callouts last: their accent colour must survive the document-wide text
    # colour that style_requests paints over every run.
    callouts, n_callouts = callout_requests(doc, resolve_theme(theme))
    requests += callouts

    if not requests:
        return False, 0, 0

    try:
        docs_service.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests}
        ).execute(num_retries=NUM_RETRIES)
        return True, tables, n_callouts
    except HttpError:
        # A batch is all-or-nothing, so one malformed table request would now
        # also cost the document its styling — resilience the two-call version
        # had for free. Fall back to the slow path rather than trade
        # correctness for the round trips.
        styled = apply_styles(docs_service, doc_id, font=font, theme=theme, baked=baked)
        return styled, apply_table_borders(docs_service, doc_id, table_width_pt), 0
