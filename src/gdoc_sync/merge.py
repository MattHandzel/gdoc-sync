"""Three-way merge primitives for two-way sync.

The sync engine never decides "which side wins". When both the local markdown
and the Google Doc have moved on, it merges them the way git does: against a
common ancestor, hunk by hunk, so edits in different parts of the document all
survive. Only genuinely overlapping edits become conflicts, and a conflict is
reported — never silently resolved.

Why a *stored ancestor* is required
-----------------------------------
``md → pandoc → docx → Google Doc → md`` is not the identity function. The
markdown that comes back out of a doc is never byte-identical to the markdown
that went in, so "local text != remote text" says nothing about whether anyone
edited anything. Comparing each side against **its own** last-synced snapshot
is what separates a real edit from round-trip noise, and it is what makes a
merge base available when both sides did move.

Two snapshots are therefore kept per linked file (see :mod:`.syncstate`):

``base_local``
    the local file's bytes at the last successful sync
``base_remote``
    the markdown the doc rendered to at that same moment

``local_changed`` compares the file against ``base_local``; ``remote_changed``
compares freshly-rendered doc markdown against ``base_remote``. Neither
comparison ever sees the other representation, so round-trip noise cannot
masquerade as an edit.

Merging remote edits into the local file is then
``merge3(local, base_remote, remote)``: take the change ``base_remote →
remote`` (what the doc's editors actually did) and replay it onto the local
file. Round-trip differences between ``local`` and ``base_remote`` read as
"our" side of the merge and are preserved untouched.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Conflict markers, deliberately identical to git's so existing editor
# tooling (diffview, conflict-marker highlighting, :Gdoc conflict) works.
MARKER_OURS = "<<<<<<<"
MARKER_BASE = "|||||||"
MARKER_SPLIT = "======="
MARKER_THEIRS = ">>>>>>>"

_CONFLICT_RE = re.compile(
    rf"^{MARKER_OURS} |^{MARKER_SPLIT}$|^{MARKER_THEIRS} ",
    re.MULTILINE,
)


def has_conflict_markers(text: str) -> bool:
    """True if ``text`` still contains unresolved merge markers."""
    return bool(_CONFLICT_RE.search(text))


# ---------------------------------------------------------------------------
# Normalization + hashing
# ---------------------------------------------------------------------------

def normalize(text: str) -> str:
    """Canonical form used for *change detection only*.

    Never written to disk or pushed — it exists so that cosmetic churn does not
    read as an edit and trigger a pointless sync. Google Docs rewrites a
    document's revisionId on autosave, cursor movement and presence changes, so
    without this the watcher would push on every keystroke someone else makes.

    Normalizes line endings, trailing whitespace, runs of blank lines, and a
    missing/extra trailing newline.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def content_hash(text: str) -> str:
    """Stable hash of ``text`` in its normalized form."""
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def same_content(a: str, b: str) -> bool:
    """True when two texts differ only by normalizable churn."""
    return normalize(a) == normalize(b)


# ---------------------------------------------------------------------------
# Three-way merge
# ---------------------------------------------------------------------------

@dataclass
class MergeResult:
    """Outcome of a three-way merge.

    ``text`` is always usable content: on conflict it carries git-style markers
    rather than having silently dropped a side.
    """

    text: str
    conflicted: bool
    conflict_count: int = 0

    @property
    def clean(self) -> bool:
        return not self.conflicted


def _ensure_trailing_newline(text: str) -> str:
    """A missing final newline makes line-based merges mangle the last line."""
    if text and not text.endswith("\n"):
        return text + "\n"
    return text


def merge3(
    ours: str,
    base: str,
    theirs: str,
    *,
    label_ours: str = "local",
    label_base: str = "last-synced",
    label_theirs: str = "google-doc",
) -> MergeResult:
    """Three-way merge ``ours`` and ``theirs`` against common ancestor ``base``.

    Prefers ``git merge-file`` (ubiquitous, and the exact semantics users
    already expect) and falls back to a pure-Python diff3 when git is absent, so
    the engine has no hard dependency on git.
    """
    # Fast paths — these are the common cases and skipping the subprocess keeps
    # a watch tick cheap.
    if same_content(ours, theirs):
        return MergeResult(ours, conflicted=False)
    if same_content(base, ours):
        return MergeResult(theirs, conflicted=False)  # only they moved
    if same_content(base, theirs):
        return MergeResult(ours, conflicted=False)  # only we moved

    ours_n = _ensure_trailing_newline(ours)
    base_n = _ensure_trailing_newline(base)
    theirs_n = _ensure_trailing_newline(theirs)

    if shutil.which("git"):
        merged = _merge_with_git(
            ours_n, base_n, theirs_n, label_ours, label_base, label_theirs
        )
        if merged is not None:
            return merged

    return _merge_pure_python(
        ours_n, base_n, theirs_n, label_ours, label_base, label_theirs
    )


def _merge_with_git(
    ours: str, base: str, theirs: str,
    label_ours: str, label_base: str, label_theirs: str,
) -> MergeResult | None:
    """Merge via ``git merge-file``. Returns None if git could not be used."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ours_p, base_p, theirs_p = d / "ours", d / "base", d / "theirs"
        ours_p.write_text(ours, encoding="utf-8")
        base_p.write_text(base, encoding="utf-8")
        theirs_p.write_text(theirs, encoding="utf-8")
        try:
            proc = subprocess.run(
                [
                    "git", "merge-file", "--diff3",
                    "-L", label_ours, "-L", label_base, "-L", label_theirs,
                    "-p", str(ours_p), str(base_p), str(theirs_p),
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # Exit code: 0 clean, >0 = number of conflicts, <0 = error.
        if proc.returncode < 0:
            return None
        return MergeResult(
            proc.stdout,
            conflicted=proc.returncode > 0,
            conflict_count=max(proc.returncode, 0),
        )


def _merge_pure_python(
    ours: str, base: str, theirs: str,
    label_ours: str, label_base: str, label_theirs: str,
) -> MergeResult:
    """diff3 without git, so the engine still works on a bare system.

    Walks both sides' diffs against the base together. A base region touched by
    only one side takes that side; a region both sides changed differently
    becomes a marked conflict.
    """
    import difflib

    o_lines = ours.splitlines(keepends=True)
    b_lines = base.splitlines(keepends=True)
    t_lines = theirs.splitlines(keepends=True)

    # Map each side's edits onto base-line ranges.
    def changes(new: list[str]) -> list[tuple[int, int, list[str]]]:
        sm = difflib.SequenceMatcher(None, b_lines, new, autojunk=False)
        return [
            (i1, i2, new[j1:j2])
            for tag, i1, i2, j1, j2 in sm.get_opcodes()
            if tag != "equal"
        ]

    o_changes = changes(o_lines)
    t_changes = changes(t_lines)

    out: list[str] = []
    conflicts = 0
    pos = 0  # cursor into base

    while pos < len(b_lines) or o_changes or t_changes:
        o_next = o_changes[0] if o_changes else None
        t_next = t_changes[0] if t_changes else None

        if o_next is None and t_next is None:
            out.extend(b_lines[pos:])
            break

        start = min(c[0] for c in (o_next, t_next) if c is not None)
        if start > pos:
            out.extend(b_lines[pos:start])
            pos = start
            continue

        # Gather every change on each side that overlaps this base region,
        # growing the region until both sides' edits are fully contained.
        region_end = pos
        o_take: list[tuple[int, int, list[str]]] = []
        t_take: list[tuple[int, int, list[str]]] = []
        for c in (o_next, t_next):
            if c is not None and c[0] <= pos:
                region_end = max(region_end, c[1])
        grew = True
        while grew:
            grew = False
            while o_changes and o_changes[0][0] <= region_end:
                c = o_changes.pop(0)
                o_take.append(c)
                if c[1] > region_end:
                    region_end, grew = c[1], True
            while t_changes and t_changes[0][0] <= region_end:
                c = t_changes.pop(0)
                t_take.append(c)
                if c[1] > region_end:
                    region_end, grew = c[1], True

        def rendered(
            take: list[tuple[int, int, list[str]]], lo: int, hi: int
        ) -> list[str]:
            """The side's text for base region [lo, hi)."""
            result: list[str] = []
            cur = lo
            for i1, i2, repl in take:
                result.extend(b_lines[cur:i1])
                result.extend(repl)
                cur = max(cur, i2)
            result.extend(b_lines[cur:hi])
            return result

        o_text = rendered(o_take, pos, region_end)
        t_text = rendered(t_take, pos, region_end)
        base_text = b_lines[pos:region_end]

        if not o_take:
            out.extend(t_text)
        elif not t_take:
            out.extend(o_text)
        elif o_text == t_text:
            out.extend(o_text)  # both made the same edit
        else:
            conflicts += 1
            out.append(f"{MARKER_OURS} {label_ours}\n")
            out.extend(o_text)
            out.append(f"{MARKER_BASE} {label_base}\n")
            out.extend(base_text)
            out.append(f"{MARKER_SPLIT}\n")
            out.extend(t_text)
            out.append(f"{MARKER_THEIRS} {label_theirs}\n")

        pos = region_end

    return MergeResult("".join(out), conflicted=conflicts > 0, conflict_count=conflicts)
