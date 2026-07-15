"""Markdown-file helpers: frontmatter, title derivation, pandoc, clipboard."""

from __future__ import annotations

import subprocess
from pathlib import Path


def strip_frontmatter(markdown: str) -> str:
    """Remove a leading YAML frontmatter block if present."""
    if markdown.startswith("---\n"):
        end_idx = markdown.find("\n---\n", 4)
        if end_idx != -1:
            return markdown[end_idx + 5:].lstrip("\n")
    return markdown


def derive_title(markdown_with_frontmatter: str, fallback: str) -> str:
    """Derive a doc title: first H1 in the body → YAML ``title:`` → fallback."""
    body = strip_frontmatter(markdown_with_frontmatter)
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    if markdown_with_frontmatter.startswith("---\n"):
        end_idx = markdown_with_frontmatter.find("\n---\n", 4)
        if end_idx != -1:
            header = markdown_with_frontmatter[4:end_idx]
            for line in header.splitlines():
                line = line.strip()
                if line.startswith("title:"):
                    return line.split(":", 1)[1].strip().strip('"').strip("'")
    return fallback


def copy_to_clipboard(text: str) -> tuple[bool, str]:
    """Try wl-copy (Wayland), xclip (X11), pbcopy (macOS). Returns (ok, tool_used)."""
    candidates = [
        (["wl-copy"], "wl-copy"),
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["pbcopy"], "pbcopy"),
    ]
    for cmd, name in candidates:
        try:
            proc = subprocess.run(
                cmd, input=text, text=True, capture_output=True, timeout=5
            )
            if proc.returncode == 0:
                return True, name
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return False, ""


def pandoc_to_docx(markdown_body: str, output_path: Path) -> None:
    """Convert markdown to docx via pandoc. Raises with a helpful message on failure."""
    try:
        proc = subprocess.run(
            [
                "pandoc",
                "-f", "gfm+yaml_metadata_block",
                "-t", "docx",
                "-o", str(output_path),
            ],
            input=markdown_body,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pandoc not found on PATH. Install it (https://pandoc.org/installing.html) — "
            "gdoc-sync uses pandoc for high-fidelity markdown → Google Doc conversion."
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc failed (exit {proc.returncode}):\n{proc.stderr}")
