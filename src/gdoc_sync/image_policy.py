"""What local files a push is allowed to read and upload.

WHY THIS EXISTS
---------------
A markdown image target is not a trustworthy string. It arrives from a shared
Google Doc: anyone with edit access can type ``![logo](../../../.ssh/id_rsa)``
into a tab, `pull` round-trips it verbatim into the operator's markdown, and the
next `push` would happily read that file and — on the tabbed path — upload it to
Drive and grant ``anyone: reader`` so the Docs API can fetch it. A collaborator
would have turned "can edit a doc" into "can read any file on your laptop".

So every local target both push paths take is checked here, once, against the
same three rules:

**Containment.** Targets must resolve inside the *project* the note belongs to
— the nearest ancestor holding ``.git`` or ``.obsidian``, falling back to the
note's own directory. The project, not the note's folder, because an Obsidian
vault legitimately says ``../attachments/diagram.png`` from a subfolder. With no
resource directory at all there is no root, and every local path is refused.

**No hidden components.** Nothing whose path below the root contains a
dot-directory. ``.ssh``, ``.git``, ``.config`` and ``.env`` live inside people's
projects, and none of them are ever an illustration.

**Real image bytes.** The first bytes must be PNG, JPEG, GIF, WebP or BMP. An
extension is a claim, not evidence: ``id_rsa`` renamed to ``logo.png`` is still
a private key. SVG is refused too — the Docs API does not accept it, so it is
all risk and no feature.

Symlinks are resolved (``realpath``) on both the candidate and the root before
comparison, so a link inside the vault cannot point out of it. ``~`` is never
expanded: nothing in a document should be able to address the home directory.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

#: Enough bytes to identify every format we accept.
_SNIFF_BYTES = 16

#: Formats the Docs API and Drive's docx import both render.
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
    (b"BM", "bmp"),
)

_REMOTE = ("http://", "https://", "data:", "//")


@dataclass(frozen=True)
class Refusal:
    """Why a target will not be read. ``missing`` is not a policy violation."""

    target: str
    reason: str
    missing: bool = False


def sniff_image(head: bytes) -> str | None:
    """The image format of these leading bytes, or None if it is not one."""
    for magic, name in _MAGIC:
        if head.startswith(magic):
            return name
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    return None


def is_remote(target: str) -> bool:
    """Whether the target is a URL rather than a path — those are not our problem."""
    return target.strip().lower().startswith(_REMOTE)


def containment_root(resource_dir: Path | None) -> Path | None:
    """The project directory a note's images must live inside.

    The nearest ancestor of ``resource_dir`` (or itself) containing ``.git`` or
    ``.obsidian`` — a repo checkout or an Obsidian vault — and otherwise the
    resource directory itself. None when there is no resource directory, which
    the caller must treat as "refuse every local path": with nothing to contain
    a path *to*, containment cannot be checked at all.
    """
    if resource_dir is None:
        return None
    start = Path(os.path.realpath(Path(resource_dir)))
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() or (candidate / ".obsidian").exists():
            return candidate
    return start


def check_target(target: str, resource_dir: Path | None) -> tuple[Path | None, Refusal | None]:
    """Resolve one markdown image target, or say why it is refused.

    Returns ``(path, None)`` for a file that may be read, and ``(None,
    Refusal)`` otherwise. Never raises, never touches the network, and reads at
    most the first few bytes of the file it is judging.
    """
    raw = target.strip()
    if not raw:
        return None, Refusal(target, "empty image target")
    if raw.startswith("~"):
        return None, Refusal(
            target, "it is a home-directory path, which a document may not address")

    root = containment_root(resource_dir)
    if root is None:
        return None, Refusal(
            target, "this push has no project directory, so no local file can be staged")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(resource_dir) / candidate  # type: ignore[arg-type]
    real = Path(os.path.realpath(candidate))

    if not real.is_relative_to(root):
        return None, Refusal(
            target, f"it resolves to {real}, outside the note's project ({root})")
    if any(part.startswith(".") for part in real.relative_to(root).parts):
        return None, Refusal(target, f"it is inside a hidden path ({real})")

    if not real.is_file():
        return None, Refusal(target, f"no such file: {real}", missing=True)
    try:
        with open(real, "rb") as fh:
            head = fh.read(_SNIFF_BYTES)
    except OSError as e:
        return None, Refusal(target, f"it could not be read ({e})")
    if sniff_image(head) is None:
        return None, Refusal(
            target, "it is not a PNG, JPEG, GIF, WebP or BMP image")
    return real, None


# ---------------------------------------------------------------------------
# Finding the targets in a markdown body (the docx path's pre-scan)
# ---------------------------------------------------------------------------

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*<?([^)>\s]+)")
_HTML_IMAGE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']?([^"'>\s]+)""", re.IGNORECASE)


def looks_like_it_has_images(markdown: str) -> bool:
    """Cheap prefilter, so a document with no images never pays for a scan."""
    return "![" in markdown or "<img" in markdown.lower()


def _targets_from_ast(node, out: list[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _targets_from_ast(item, out)
        return
    if not isinstance(node, dict):
        return
    tag, contents = node.get("t"), node.get("c")
    if tag == "Image" and isinstance(contents, list) and len(contents) == 3:
        url = contents[2]
        if isinstance(url, list) and url and isinstance(url[0], str):
            out.append(url[0])
    elif tag in ("RawInline", "RawBlock") and isinstance(contents, list) \
            and len(contents) == 2 and isinstance(contents[1], str):
        out += _HTML_IMAGE.findall(contents[1])
    for value in node.values():
        _targets_from_ast(value, out)


def local_image_targets(markdown: str, resource_dir: Path | None = None,
                        say=lambda *_: None) -> list[str]:
    """Every local image target in ``markdown``, deduplicated, in order.

    Parsed from pandoc's own AST, so an image target written inside a fenced
    code block is not mistaken for one the push would read. If pandoc cannot be
    run the scan falls back to a deliberately over-eager regex and says so — a
    false positive there costs a confusing error, while skipping the scan would
    cost the containment guarantee.
    """
    found: list[str] = []
    try:
        from .mdutils import pandoc_to_ast
        ast = pandoc_to_ast(markdown, resource_dir=resource_dir)
        _targets_from_ast(ast, found)
    except Exception:  # noqa: BLE001 — pandoc missing or unhappy; still scan
        say("  Note: checking image paths with a conservative pattern match "
            "(pandoc could not parse the file).")
        found = _MD_IMAGE.findall(markdown) + _HTML_IMAGE.findall(markdown)

    seen: dict[str, None] = {}
    for target in found:
        if target and not is_remote(target):
            seen.setdefault(target, None)
    return list(seen)


def scan_markdown(markdown: str, resource_dir: Path | None = None,
                  say=lambda *_: None) -> list[Refusal]:
    """The policy violations in a markdown body. A missing file is not one.

    Used by the docx path, where pandoc reads and embeds local images itself
    and so has to be stopped *before* it runs. A target that simply is not
    there is left to pandoc, which warns and carries on — that is a typo, not
    an exfiltration attempt.
    """
    if not looks_like_it_has_images(markdown):
        return []
    bad: list[Refusal] = []
    for target in local_image_targets(markdown, resource_dir, say=say):
        _, refusal = check_target(target, resource_dir)
        if refusal is not None and not refusal.missing:
            bad.append(refusal)
    return bad


def enforce_markdown(markdown: str, resource_dir: Path | None = None,
                     say=lambda *_: None) -> None:
    """Raise unless every local image target in ``markdown`` is allowed.

    A hard stop, unlike the tabbed path's per-image warning: pandoc embeds the
    bytes it is given, so there is no way to write "the alt text instead" once
    it has run, and a push that quietly shipped one private file is exactly the
    outcome this module exists to prevent.
    """
    bad = scan_markdown(markdown, resource_dir, say=say)
    if not bad:
        return
    detail = "\n".join(f"  - {r.target}: {r.reason}" for r in bad)
    raise RuntimeError(
        "Refusing to push: these image targets are not files this document may "
        f"embed.\n{detail}\n"
        "Images must be real image files inside the note's project directory. "
        "If a collaborator added these to the doc, delete the links and pull again."
    )
