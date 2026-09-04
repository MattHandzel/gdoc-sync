#!/usr/bin/env python3
"""Fail when the project's three version strings disagree.

The version lives in three places that nothing keeps in step:

* ``pyproject.toml``            — what pip/PyPI install as
* ``src/gdoc_sync/__init__.py`` — what ``gdoc-sync --version`` prints
* ``flake.nix``                 — what ``nix build`` produces

They have drifted before (0.8.0 / 0.9.0 / 0.8.0 all at once), which makes a
bug report's "I'm on 0.9.0" mean nothing. CI runs this on every push.

Usage:  python scripts/check_version.py [REPO_ROOT]
Exit:   0 when all three agree, 1 when they do not (or a file is unreadable).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# (label, relative path, pattern whose first group is the version)
SOURCES: tuple[tuple[str, str, str], ...] = (
    ("pyproject.toml", "pyproject.toml",
     r'(?m)^\s*version\s*=\s*["\']([^"\']+)["\']'),
    ("src/gdoc_sync/__init__.py", "src/gdoc_sync/__init__.py",
     r'(?m)^\s*__version__\s*=\s*["\']([^"\']+)["\']'),
    ("flake.nix", "flake.nix",
     r'(?m)^\s*version\s*=\s*"([^"]+)"\s*;'),
)


def read_version(root: Path, label: str, relpath: str, pattern: str) -> str | None:
    path = root / relpath
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        print(f"error: cannot read {label}: {e}", file=sys.stderr)
        return None
    match = re.search(pattern, text)
    if not match:
        print(f"error: no version found in {label}", file=sys.stderr)
        return None
    return match.group(1)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = Path(args[0]).resolve() if args else Path(__file__).resolve().parent.parent

    found: dict[str, str | None] = {
        label: read_version(root, label, relpath, pattern)
        for label, relpath, pattern in SOURCES
    }
    if any(v is None for v in found.values()):
        return 1

    distinct = set(found.values())
    if len(distinct) == 1:
        print(f"version OK: {distinct.pop()} in all {len(found)} sources")
        return 0

    width = max(len(label) for label in found)
    print("version mismatch — these must all agree:", file=sys.stderr)
    for label, version in found.items():
        print(f"  {label.ljust(width)}  {version}", file=sys.stderr)
    print("\nFix them to the same value, then re-run: "
          "python scripts/check_version.py", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
