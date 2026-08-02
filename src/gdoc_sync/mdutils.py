"""Markdown-file helpers: frontmatter, title derivation, pandoc, clipboard."""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
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


def _clipboard_candidates() -> list[tuple[list[str], str]]:
    """Clipboard commands to try, best-first for the platform we are on.

    Ordering matters: on a Wayland session ``xclip`` may exist but write to an
    Xwayland selection nothing else can read, and on WSL the Linux tools exist
    but the clipboard the user actually pastes from is Windows'. Probing the
    session environment rather than just ``PATH`` is what makes the copy land
    somewhere useful.
    """
    if sys.platform == "darwin":
        return [(["pbcopy"], "pbcopy")]

    if sys.platform == "win32":
        return [
            (["clip"], "clip"),
            (["powershell", "-NoProfile", "-Command", "Set-Clipboard"], "Set-Clipboard"),
        ]

    candidates: list[tuple[list[str], str]] = []

    # Termux (Android) — no X11/Wayland at all.
    if os.environ.get("PREFIX", "").startswith("/data/data/com.termux"):
        candidates.append((["termux-clipboard-set"], "termux-clipboard-set"))

    # WSL: the useful clipboard is the Windows one.
    if "microsoft" in os.environ.get("WSL_DISTRO_NAME", "").lower() or os.environ.get(
        "WSL_INTEROP"
    ):
        candidates.append((["clip.exe"], "clip.exe"))

    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append((["wl-copy"], "wl-copy"))
    if os.environ.get("DISPLAY"):
        candidates.append((["xclip", "-selection", "clipboard"], "xclip"))
        candidates.append((["xsel", "--clipboard", "--input"], "xsel"))

    # Whatever is installed, for sessions that advertise neither.
    for cmd, name in (
        (["wl-copy"], "wl-copy"),
        (["xclip", "-selection", "clipboard"], "xclip"),
        (["xsel", "--clipboard", "--input"], "xsel"),
        (["clip.exe"], "clip.exe"),
        (["pbcopy"], "pbcopy"),
    ):
        if not any(name == existing for _, existing in candidates):
            candidates.append((cmd, name))
    return candidates


def _osc52(text: str) -> bool:
    """Copy via the OSC 52 terminal escape, which works over plain SSH.

    Only attempted when stdout is a terminal — writing escape bytes into a pipe
    would corrupt whatever is reading it.
    """
    if not sys.stdout.isatty():
        return False
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    sequence = f"\033]52;c;{payload}\a"
    if os.environ.get("TMUX"):  # tmux needs the sequence wrapped to pass it through
        sequence = f"\033Ptmux;\033{sequence}\033\\"
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
        return True
    except OSError:
        return False


def copy_to_clipboard(text: str, command: str | list[str] | None = None) -> tuple[bool, str]:
    """Copy ``text`` to the system clipboard. Returns ``(ok, tool_used)``.

    ``command`` overrides the auto-detected tool (the config's
    ``clipboard_command``), for setups the probing cannot know about. Falls
    back to an OSC 52 escape so a remote session still gets the URL.
    """
    if command:
        cmd = command.split() if isinstance(command, str) else list(command)
        candidates = [(cmd, cmd[0])]
    else:
        candidates = _clipboard_candidates()

    for cmd, name in candidates:
        try:
            proc = subprocess.run(
                cmd, input=text, text=True, capture_output=True, timeout=5
            )
            if proc.returncode == 0:
                return True, name
        except (OSError, subprocess.SubprocessError):
            continue

    if not command and _osc52(text):
        return True, "OSC 52 (terminal)"
    return False, ""


_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


def ensure_table_blank_lines(markdown: str) -> str:
    """Insert the blank line GFM requires before a pipe table.

    Obsidian (and most editors Matt writes in) render a table that starts on the
    line directly after a paragraph. Strict GFM does NOT: without a preceding
    blank line the table is just part of that paragraph, so pandoc emits ZERO
    tables and the whole thing lands in the Google Doc as one flattened line of
    pipe characters. The markdown looks correct in the editor and silently
    arrives broken in the doc, which is the worst kind of failure.

    Rather than make the author remember a rule their editor does not enforce,
    normalise it here. Fenced code blocks are skipped — a table-looking line
    inside ``` is content, not a table.
    """
    lines = markdown.split("\n")
    out: list[str] = []
    in_fence = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            out.append(line)
            continue
        if (
            not in_fence
            and stripped.startswith("|")
            and i + 1 < len(lines)
            and _TABLE_SEP.match(lines[i + 1])
            and out
            and out[-1].strip()
            and not out[-1].strip().startswith("|")
        ):
            out.append("")
        out.append(line)
    return "\n".join(out)


def pandoc_to_docx(markdown_body: str, output_path: Path,
                   resource_dir: Path | None = None,
                   reference_doc: Path | None = None,
                   highlight_style: Path | None = None) -> None:
    """Convert markdown to docx via pandoc. Raises with a helpful message on failure.

    ``resource_dir`` (usually the markdown file's directory) lets pandoc
    resolve relative image paths so local images get embedded in the docx.

    ``reference_doc`` supplies the style definitions pandoc writes into the
    docx — the mechanism that makes the theme's fonts and heading colours
    *innate* to the resulting Google Doc rather than painted on afterwards
    (see :mod:`.refdoc`).
    """
    markdown_body = ensure_table_blank_lines(markdown_body)
    cmd = [
        "pandoc",
        "-f", "gfm+yaml_metadata_block",
        "-t", "docx",
        "-o", str(output_path),
    ]
    if resource_dir is not None:
        cmd += ["--resource-path", str(resource_dir)]
    if reference_doc is not None:
        cmd += ["--reference-doc", str(reference_doc)]
    # Without this pandoc highlights fenced code with its built-in `pygments`
    # style, which ignores the document theme entirely.
    if highlight_style is not None:
        cmd += ["--highlight-style", str(highlight_style)]
    try:
        proc = subprocess.run(
            cmd,
            input=markdown_body,
            text=True,
            capture_output=True,
            cwd=str(resource_dir) if resource_dir else None,
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pandoc not found on PATH. Install it (https://pandoc.org/installing.html) — "
            "gdoc-sync uses pandoc for high-fidelity markdown → Google Doc conversion."
        ) from None
    if proc.returncode != 0:
        raise RuntimeError(f"pandoc failed (exit {proc.returncode}):\n{proc.stderr}")

    # Malformed LaTeX is not an error to pandoc: it warns, writes the formula
    # into the docx as literal text, and exits 0. The doc then shows `$m^$`
    # where an equation was meant to be, which is easy to miss in a long
    # document and impossible to explain after the fact. Say so out loud.
    for formula in unconvertible_math(proc.stderr):
        print(f"  Note: {formula} is not valid LaTeX — it will appear in the "
              f"doc as literal text, not a formula.")


_MATH_WARNING = re.compile(r"^\[WARNING\] Could not convert TeX math (.*), rendering as TeX:",
                           re.MULTILINE)


def unconvertible_math(pandoc_stderr: str) -> list[str]:
    """The formulas pandoc could not turn into equations, as it reported them."""
    return _MATH_WARNING.findall(pandoc_stderr or "")
