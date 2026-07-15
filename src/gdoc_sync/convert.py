#!/usr/bin/env python3
"""Convert between Google Docs JSON and Markdown with CriticMarkup comments."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OffsetMapping:
    """Maps Google Docs character offsets to markdown character offsets."""
    entries: list[tuple[int, int, int, int]] = field(default_factory=list)
    # Each: (gdoc_start, gdoc_end, md_start, md_end)

    def add(self, gdoc_start: int, gdoc_end: int, md_start: int, md_end: int):
        self.entries.append((gdoc_start, gdoc_end, md_start, md_end))

    def gdoc_to_md(self, gdoc_offset: int) -> int | None:
        """Find the markdown offset corresponding to a Google Docs offset."""
        for gs, ge, ms, me in self.entries:
            if gs <= gdoc_offset <= ge:
                ratio = (gdoc_offset - gs) / max(ge - gs, 1)
                return int(ms + ratio * (me - ms))
        return None


def doc_to_markdown(doc: dict, image_saver=None) -> tuple[str, OffsetMapping]:
    """Convert Google Docs API document JSON to markdown string + offset map.

    ``image_saver(object_id, content_uri) -> str | None`` downloads an inline
    image and returns the (relative) path to reference in the markdown; when
    None (default), inline images are skipped.
    """
    body = doc.get("body", {}).get("content", [])
    lists_meta = doc.get("lists", {})
    inline_objects = doc.get("inlineObjects", {})
    md_parts: list[str] = []
    offset_map = OffsetMapping()
    md_pos = 0

    for element in body:
        if "paragraph" in element:
            para = element["paragraph"]
            gdoc_start = element.get("startIndex", 0)
            gdoc_end = element.get("endIndex", gdoc_start)

            prefix = _paragraph_prefix(para, lists_meta)
            text, runs_md = _convert_paragraph_elements(
                para.get("elements", []), inline_objects, image_saver)

            line = prefix + runs_md
            # Strip trailing newline from Google (we add our own)
            if line.endswith("\n"):
                line = line[:-1]

            # Block elements are separated by blank lines — real markdown
            # paragraphs. Without this, a pull → push round trip merges every
            # adjacent line into one paragraph (and breaks tables/images that
            # follow text). List items stay tight (single newline).
            is_list_item = para.get("bullet") is not None
            if not is_list_item and md_parts and not md_parts[-1].endswith("\n\n"):
                md_parts.append("\n")
                md_pos += 1
            line += "\n" if is_list_item else "\n\n"

            offset_map.add(gdoc_start, gdoc_end, md_pos, md_pos + len(line))
            md_parts.append(line)
            md_pos += len(line)

        elif "table" in element:
            if md_parts and not md_parts[-1].endswith("\n\n"):
                md_parts.append("\n")
                md_pos += 1
            table_md = _convert_table(element["table"])
            md_parts.append(table_md)
            md_pos += len(table_md)

        elif "sectionBreak" in element:
            if md_parts and not md_parts[-1].endswith("\n\n"):
                md_parts.append("\n")
                md_pos += 1

    result = "".join(md_parts)
    # Clean up excessive blank lines
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")
    return result.strip() + "\n", offset_map


def _paragraph_prefix(para: dict, lists_meta: dict) -> str:
    """Determine markdown prefix for a paragraph (heading, bullet, etc.)."""
    style_type = para.get("paragraphStyle", {}).get("namedStyleType", "")

    heading_map = {
        "HEADING_1": "# ",
        "HEADING_2": "## ",
        "HEADING_3": "### ",
        "HEADING_4": "#### ",
        "HEADING_5": "##### ",
        "HEADING_6": "###### ",
    }
    if style_type in heading_map:
        return heading_map[style_type]

    bullet = para.get("bullet")
    if bullet:
        nesting = bullet.get("nestingLevel", 0)
        indent = "  " * nesting
        list_id = bullet.get("listId", "")
        list_props = lists_meta.get(list_id, {}).get("listProperties", {})
        nesting_levels = list_props.get("nestingLevels", [])

        is_ordered = False
        if nesting_levels and len(nesting_levels) > nesting:
            glyph_type = nesting_levels[nesting].get("glyphType", "")
            if glyph_type and glyph_type != "GLYPH_TYPE_UNSPECIFIED":
                is_ordered = True

        marker = "1." if is_ordered else "-"
        return f"{indent}{marker} "

    return ""


def _convert_paragraph_elements(
    elements: list[dict],
    inline_objects: dict | None = None,
    image_saver=None,
) -> tuple[str, str]:
    """Convert paragraph elements to (plain_text, markdown_text)."""
    plain_parts = []
    md_parts = []

    for elem in elements:
        text_run = elem.get("textRun")
        if not text_run:
            obj_id = elem.get("inlineObjectElement", {}).get("inlineObjectId")
            if obj_id and image_saver and inline_objects:
                uri = (
                    inline_objects.get(obj_id, {})
                    .get("inlineObjectProperties", {})
                    .get("embeddedObject", {})
                    .get("imageProperties", {})
                    .get("contentUri")
                )
                if uri:
                    saved = image_saver(obj_id, uri)
                    if saved:
                        md_parts.append(f"![image]({saved})")
            continue

        content = text_run.get("content", "")
        style = text_run.get("textStyle", {})
        plain_parts.append(content)

        formatted = content
        # Don't format whitespace-only runs
        if formatted.strip():
            link = style.get("link", {})
            if link.get("url"):
                formatted = f"[{formatted.strip()}]({link['url']})"

            if style.get("bold"):
                stripped = formatted.strip()
                formatted = formatted.replace(stripped, f"**{stripped}**")

            if style.get("italic"):
                stripped = formatted.strip()
                formatted = formatted.replace(stripped, f"*{stripped}*")

        md_parts.append(formatted)

    return "".join(plain_parts), "".join(md_parts)


def _convert_table(table: dict) -> str:
    """Convert a Google Docs table to markdown pipe table."""
    rows = table.get("tableRows", [])
    if not rows:
        return ""

    md_rows = []
    for row in rows:
        cells = row.get("tableCells", [])
        cell_texts = []
        for cell in cells:
            cell_content = cell.get("content", [])
            text = ""
            for element in cell_content:
                if "paragraph" in element:
                    elems = element["paragraph"].get("elements", [])
                    _, cell_md = _convert_paragraph_elements(elems)
                    text += cell_md.strip()
            cell_texts.append(text.replace("|", "\\|"))
        md_rows.append("| " + " | ".join(cell_texts) + " |")

    if len(md_rows) >= 1:
        # Add separator after header row
        num_cols = md_rows[0].count("|") - 1
        separator = "|" + "|".join(["---"] * max(num_cols, 1)) + "|"
        md_rows.insert(1, separator)

    return "\n".join(md_rows) + "\n\n"


# --- Markdown to Google Docs requests ---

def markdown_to_requests(md_text: str) -> list[dict]:
    """Convert markdown to Google Docs API batchUpdate requests.

    Strategy: delete all content, insert plain text, then apply formatting.
    Requests are returned in the order they should be sent.
    """
    # Strip CriticMarkup comments
    import re
    clean = re.sub(r"\{>>.*?<<\}", "", md_text)

    # Convert markdown to plain text + collect formatting ranges
    lines = clean.split("\n")
    plain_parts = []
    format_requests = []
    current_offset = 1  # Google Docs body starts at index 1

    for line in lines:
        processed, line_formats = _process_md_line(line, current_offset)
        plain_parts.append(processed)
        format_requests.extend(line_formats)
        current_offset += len(processed) + 1  # +1 for newline

    plain_text = "\n".join(plain_parts)

    requests = []
    # Will be prepended with delete request by the caller (needs doc length)
    requests.append({
        "insertText": {
            "location": {"index": 1},
            "text": plain_text,
        }
    })
    # Formatting requests in reverse offset order (required by API)
    format_requests.sort(key=lambda r: _get_start_index(r), reverse=True)
    requests.extend(format_requests)

    return requests


def _process_md_line(line: str, offset: int) -> tuple[str, list[dict]]:
    """Process a single markdown line into plain text + formatting requests."""
    import re
    formats = []
    plain = line

    # Headings
    heading_match = re.match(r"^(#{1,6})\s+(.+)$", plain)
    if heading_match:
        level = len(heading_match.group(1))
        plain = heading_match.group(2)
        style_map = {
            1: "HEADING_1", 2: "HEADING_2", 3: "HEADING_3",
            4: "HEADING_4", 5: "HEADING_5", 6: "HEADING_6",
        }
        formats.append({
            "updateParagraphStyle": {
                "range": {"startIndex": offset, "endIndex": offset + len(plain)},
                "paragraphStyle": {"namedStyleType": style_map[level]},
                "fields": "namedStyleType",
            }
        })
        return plain, formats

    # Bold
    for m in re.finditer(r"\*\*(.+?)\*\*", plain):
        start = offset + plain.index(m.group(0))
        text = m.group(1)
        formats.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + len(text)},
                "textStyle": {"bold": True},
                "fields": "bold",
            }
        })
    plain = re.sub(r"\*\*(.+?)\*\*", r"\1", plain)

    # Italic
    for m in re.finditer(r"\*(.+?)\*", plain):
        start = offset + plain.index(m.group(0))
        text = m.group(1)
        formats.append({
            "updateTextStyle": {
                "range": {"startIndex": start, "endIndex": start + len(text)},
                "textStyle": {"italic": True},
                "fields": "italic",
            }
        })
    plain = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", plain)

    return plain, formats


def _get_start_index(req: dict) -> int:
    """Extract startIndex from a formatting request."""
    for key in req:
        r = req[key].get("range", {})
        if "startIndex" in r:
            return r["startIndex"]
    return 0
