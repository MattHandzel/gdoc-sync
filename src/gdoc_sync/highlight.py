"""Build a pandoc `--highlight-style` theme from a gdoc-sync color palette.

Pandoc highlights fenced code blocks with skylighting, and by default uses its
built-in ``pygments`` style. That style is fixed, so a catppuccin document got
theme-colored headings, body text and links — and then 1990s-green keywords in
its code blocks. This module emits a KDE-syntax ``.theme`` JSON carrying the
palette's ``code`` colors, so a fence in the Google Doc matches the same fence
in the editor.

The file is cached by content hash: pandoc is invoked per push, and rewriting
an identical theme every time is pointless IO.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

# Every token skylighting can emit. Anything the palette does not name falls
# back to the body text color, so a partial palette never leaves a token
# rendering in pandoc's default (off-theme) color.
_ALL_TOKENS = (
    "Alert", "Annotation", "Attribute", "BaseN", "BuiltIn", "Char", "Comment",
    "CommentVar", "Constant", "ControlFlow", "DataType", "DecVal",
    "Documentation", "Error", "Extension", "Float", "Function", "Import",
    "Information", "Keyword", "Normal", "Operator", "Other", "Preprocessor",
    "RegionMarker", "SpecialChar", "SpecialString", "String", "Variable",
    "VerbatimString", "Warning",
)

# Tokens conventionally bolded. Kept small on purpose: a code block where half
# the tokens are bold reads as noise, especially in a doc meant for sharing.
_BOLD = {"Keyword", "ControlFlow", "Import", "Preprocessor", "Error", "Alert"}
_ITALIC = {"Comment", "Documentation", "CommentVar", "Annotation"}


def _style(color: str | None, *, bold: bool, italic: bool) -> dict:
    return {
        "text-color": color,
        "background-color": None,
        "bold": bold,
        "italic": italic,
        "underline": False,
    }


def build_highlight_theme(palette: dict | None) -> Path | None:
    """Write a skylighting theme for ``palette``; ``None`` when not applicable.

    Returning ``None`` means "let pandoc use its default" — styling that cannot
    be applied is not an error, it just means the code blocks keep pandoc's
    colors, exactly as before this feature existed.
    """
    if not palette:
        return None
    code = palette.get("code")
    if not isinstance(code, dict) or not code:
        return None

    fallback = palette.get("text") or None
    text_styles = {
        tok: _style(code.get(tok, fallback), bold=tok in _BOLD, italic=tok in _ITALIC)
        for tok in _ALL_TOKENS
    }
    theme = {
        "text-color": fallback,
        "background-color": None,
        "line-number-color": code.get("Comment", fallback),
        "line-number-background-color": None,
        "text-styles": text_styles,
    }

    blob = json.dumps(theme, sort_keys=True, indent=2)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    cache_dir = Path(tempfile.gettempdir()) / "gdoc-sync-highlight"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"{digest}.theme"
        if not path.exists():
            path.write_text(blob)
        return path
    except OSError:
        return None


def highlight_theme_for(theme: str | None) -> Path | None:
    """Resolve a theme name to a highlight-style file, or ``None``."""
    from .style import resolve_theme

    return build_highlight_theme(resolve_theme(theme))
