#!/usr/bin/env python3
"""Real-API end-to-end proof that two-way sync does not lose edits.

Everything else in the suite runs against fakes. This drives the actual Google
Docs and Drive APIs through the real CLI, because the bug being guarded against
(0.5.x destroying one side of a concurrent edit) only ever showed up against
the real round trip — pandoc → docx → Google Doc → markdown — and against
Google's habit of rewriting ``revisionId`` on its own.

Safety: an isolated config/state pair in a temp directory (your real mappings
are never touched), one private doc, trashed on the way out even if a check
fails.

Usage::

    python3 tests/e2e/two_way_sync.py

Requires an authenticated CLI (`gdoc-sync doctor` all green).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

# Subprocesses write straight to the terminal; without this our own prints sit
# in a block buffer when piped and the transcript comes out in the wrong order.
sys.stdout.reconfigure(line_buffering=True)

failures: list[str] = []
checks = 0


def check(condition: bool, name: str, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}" + (f"\n       {detail}" if detail else ""))
        failures.append(name)


def run_cli(config: Path, *args: str, expect: tuple[int, ...] = (0,)) -> str:
    cmd = [sys.executable, "-m", "gdoc_sync.cli", "--config", str(config), *args]
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode not in expect:
        raise SystemExit(
            f"`gdoc-sync {' '.join(args)}` exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc.stdout


def edit_doc_remotely(docs_service, doc_id: str, find: str, replace: str) -> None:
    """Edit the doc the way a person in a browser would."""
    docs_service.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": True},
                "replaceText": replace,
            }
        }]},
    ).execute()


def main() -> int:
    from gdoc_sync.config import set_config_override
    from gdoc_sync.services import get_services

    work = Path(tempfile.mkdtemp(prefix="gdoc-e2e-"))
    config = work / "config.yaml"
    config.write_text(textwrap.dedent(f"""\
        state_file: {work}/state.yaml
        defaults:
          clipboard: false
          share: private
          theme: professional
          font: Garamond
    """))

    note = work / "note.md"
    note.write_text(textwrap.dedent("""\
        # Two-way sync test

        ALPHA the first paragraph, edited locally later.

        BRAVO the second paragraph, left alone by everyone.

        CHARLIE the third paragraph, edited in Google Docs later.

        DELTA the closing paragraph.
        """))

    doc_id = ""
    try:
        print("\n1. create the doc")
        out = run_cli(config, "create", str(note))
        set_config_override(config)
        from gdoc_sync.config import get_doc_id
        doc_id = get_doc_id(str(note)) or ""
        check(bool(doc_id), "doc created and linked", out)
        if not doc_id:
            return 1
        (work / "doc_id").write_text(doc_id)

        _drive, docs = get_services()

        print("\n2. establish the sync baseline")
        out = run_cli(config, "sync", str(note))
        check("conflict" not in out.lower(), "first sync is clean", out)

        print("\n3. edit BOTH sides, in different places")
        # Local edit — as if typed in the editor.
        note.write_text(note.read_text().replace(
            "ALPHA the first paragraph, edited locally later.",
            "ALPHA edited in NEOVIM.",
        ))
        # Remote edit — as if typed in the browser.
        edit_doc_remotely(docs, doc_id,
                          "CHARLIE the third paragraph, edited in Google Docs later.",
                          "CHARLIE edited in GOOGLE DOCS.")
        time.sleep(2)  # let Drive settle

        print("\n4. sync — the moment 0.5.x destroyed one side")
        out = run_cli(config, "sync", str(note))
        print(textwrap.indent(out.strip(), "     | "))

        merged = note.read_text()
        check("edited in NEOVIM" in merged,
              "local edit survived the merge", merged)
        check("edited in GOOGLE DOCS" in merged,
              "remote edit landed in the local file", merged)
        check("BRAVO" in merged, "untouched paragraph is intact")
        check("DELTA" in merged, "closing paragraph is intact")

        print("\n5. the doc has both edits too")
        time.sleep(2)
        remote = run_cli(config, "pull", doc_id)
        check("edited in NEOVIM" in remote, "local edit reached the doc", remote)
        check("edited in GOOGLE DOCS" in remote, "remote edit still in the doc")

        print("\n6. repeat syncs converge (no edit war)")
        quiet = True
        for i in range(3):
            out = run_cli(config, "sync", str(note))
            if "up to date" not in out and "already" not in out.lower():
                quiet = False
                print(f"     pass {i + 1}: {out.strip()}")
        check(quiet, "a settled file stays quiet on repeat syncs")

        print("\n7. an overlapping edit conflicts instead of picking a winner")
        note.write_text(note.read_text().replace("BRAVO the second paragraph, left alone by everyone.",
                                                 "BRAVO rewritten LOCALLY."))
        edit_doc_remotely(docs, doc_id,
                          "BRAVO the second paragraph, left alone by everyone.",
                          "BRAVO rewritten REMOTELY.")
        time.sleep(2)
        out = run_cli(config, "sync", str(note), expect=(0, 2))
        conflicted = note.read_text()
        check("conflict" in out.lower(), "conflict reported", out)
        check("LOCALLY" in conflicted, "local version kept in the conflict")
        check("REMOTELY" in conflicted, "remote version kept in the conflict")

        print("\n8. sync stays suspended until the conflict is resolved")
        out = run_cli(config, "sync", str(note), expect=(0, 2))
        check("conflict" in out.lower() or "resolve" in out.lower(),
              "still blocked while conflicted", out)

        print("\n9. resolving by hand resumes syncing")
        # What a person does: keep one wording, delete the markers.
        resolved = [
            line for line in conflicted.splitlines()
            if not line.startswith(("<<<<<<<", "|||||||", "=======", ">>>>>>>"))
            and "BRAVO rewritten REMOTELY." not in line
        ]
        note.write_text("\n".join(resolved).replace(
            "BRAVO rewritten LOCALLY.", "BRAVO agreed wording.") + "\n")
        out = run_cli(config, "sync", str(note))
        check("conflict" not in out.lower(), "sync resumed after resolution", out)
        time.sleep(2)
        remote = run_cli(config, "pull", doc_id)
        check("agreed wording" in remote, "resolution reached the doc", remote)

        print("\n10. backups exist for every overwrite")
        out = run_cli(config, "restore", str(note))
        check("Backups for" in out, "backups were recorded", out)

    finally:
        if doc_id:
            print(f"\nTrashing test doc {doc_id}")
            trash = Path(__file__).parent / "trash_doc.py"
            subprocess.run([sys.executable, str(trash), doc_id], check=False)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("Two-way sync E2E passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
