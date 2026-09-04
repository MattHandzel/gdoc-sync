#!/usr/bin/env python3
"""Real-API end-to-end proof that a tabbed document survives create/push/pull.

The unit suite replays the requests against a simulator, which catches index
arithmetic but cannot tell you whether Google accepts ``addDocumentTab``,
whether a ``\\v`` really becomes a line break inside a paragraph, or how many
index units a table it just created occupies. Those only answer themselves
against the live API.

Safety: an isolated config/state pair in a temp directory (your real mappings
are never touched), one private doc, trashed on the way out even if a check
fails. Nothing here touches a document you already had.

Usage::

    python3 tests/e2e/multi_tab.py            # create a doc, check it, trash it
    python3 tests/e2e/multi_tab.py --keep     # leave the doc behind to look at

Requires an authenticated CLI (`gdoc-sync doctor` all green).
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

sys.stdout.reconfigure(line_buffering=True)

failures: list[str] = []
checks = 0

TABBED = """\
# [TAB] Overview

The **week** ahead, with a [link](https://example.com) and `inline code`.

- one
- two
  - nested
- three

---

# [TAB] Monday

## A heading inside the tab

| Where | When |
|---|---|
| Cafe | 09:00 |

> [!NOTE]
> Bring the badge.

---

# [TAB] Tuesday

```
def hello():

    return "world"
```

Closing line.
"""


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
    return proc.stdout + proc.stderr


def tab_titles(docs_service, doc_id: str) -> list[str]:
    from gdoc_sync.tabs import read_tabs
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True).execute()
    return [t.title for t in read_tabs(doc)]


def main() -> int:
    keep = "--keep" in sys.argv
    from gdoc_sync.config import set_config_override
    from gdoc_sync.services import get_services

    work = Path(tempfile.mkdtemp(prefix="gdoc-e2e-tabs-"))
    config = work / "config.yaml"
    config.write_text(textwrap.dedent(f"""\
        state_file: {work}/state.yaml
        defaults:
          clipboard: false
          share: private
          theme: professional
          font: Garamond
    """))

    note = work / "gdoc-sync tab test.md"
    note.write_text(TABBED)

    doc_id = ""
    try:
        print("\n1. create a three-tab doc")
        out = run_cli(config, "create", str(note), "--title", "gdoc-sync tab test")
        set_config_override(config)
        from gdoc_sync.config import get_doc_id
        doc_id = get_doc_id(str(note)) or ""
        check(bool(doc_id), "doc created and linked", out)
        if not doc_id:
            return 1
        print(f"   https://docs.google.com/document/d/{doc_id}/edit")

        _drive, docs = get_services()
        titles = tab_titles(docs, doc_id)
        check(titles == ["Overview", "Monday", "Tuesday"],
              "three tabs, named and ordered", str(titles))

        print("\n2. pull it back and compare")
        pulled = work / "pulled.md"
        run_cli(config, "pull", doc_id, str(pulled))
        got = pulled.read_text()
        check("# [TAB] Overview" in got and "# [TAB] Tuesday" in got,
              "pull emits a [TAB] section per tab", got[:400])
        check("- nested" in got or "  - nested" in got,
              "nested list survived", got)
        check("```" in got and 'return "world"' in got,
              "fenced code block survived", got)
        check("[!NOTE]" in got, "callout survived", got)
        check("| Cafe | 09:00 |" in got.replace("  ", " "),
              "table survived", got)

        print("\n3. push what was pulled; the next pull must be identical")
        run_cli(config, "link", str(pulled), doc_id)
        run_cli(config, "push", str(pulled), "--yes")
        again = work / "pulled-again.md"
        run_cli(config, "pull", doc_id, str(again))
        check(again.read_text() == got,
              "pull → push → pull is a fixed point",
              "\n".join(difflib.unified_diff(
                  got.splitlines(), again.read_text().splitlines(),
                  "first pull", "second pull", lineterm="", n=1)))
        run_cli(config, "unlink", str(pulled))

        print("\n4. push an edit; tab ids and untouched tabs must survive")
        before = _tab_ids(docs, doc_id)
        note.write_text(TABBED.replace("Closing line.", "Closing line, edited."))
        run_cli(config, "push", str(note), "--yes")
        after = _tab_ids(docs, doc_id)
        check(before == after, "push rewrote tabs in place, no new ids",
              f"{before} -> {after}")
        run_cli(config, "pull", doc_id, str(pulled))
        check("Closing line, edited." in pulled.read_text(),
              "the edit reached the right tab")

        print("\n5. a hand-added tab is left alone without --prune-tabs")
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
            {"addDocumentTab": {"tabProperties": {"title": "Someone else's tab"}}}
        ]}).execute()
        run_cli(config, "push", str(note), "--yes")
        titles = tab_titles(docs, doc_id)
        check("Someone else's tab" in titles,
              "extra tab survived a push", str(titles))

        print("\n6. --prune-tabs removes it when asked")
        run_cli(config, "push", str(note), "--yes", "--prune-tabs")
        titles = tab_titles(docs, doc_id)
        check(titles == ["Overview", "Monday", "Tuesday"],
              "--prune-tabs removed the extra tab", str(titles))

        print("\n7. a file with no [TAB] headers refuses to flatten the doc")
        flat = work / "flat.md"
        flat.write_text("# Plain\n\nNo tabs here.\n")
        run_cli(config, "link", str(flat), doc_id)
        out = run_cli(config, "push", str(flat), "--yes", expect=(3,))
        check("would flatten" in out, "push refused with an explanation", out)
        check(tab_titles(docs, doc_id) == ["Overview", "Monday", "Tuesday"],
              "the doc still has its tabs")

        print("\n8. a single-tab file still takes the docx path")
        plain = work / "plain.md"
        plain.write_text("# Plain doc\n\nOne **paragraph**.\n")
        run_cli(config, "create", str(plain))
        plain_id = get_doc_id(str(plain)) or ""
        check(bool(plain_id), "ordinary create still works")
        if plain_id:
            check(len(tab_titles(docs, plain_id)) == 1, "it has one tab")
            subprocess.run([sys.executable,
                            str(Path(__file__).parent / "trash_doc.py"), plain_id],
                           check=False)

    finally:
        if doc_id and not keep:
            print(f"\nTrashing test doc {doc_id}")
            subprocess.run([sys.executable,
                            str(Path(__file__).parent / "trash_doc.py"), doc_id],
                           check=False)
        elif doc_id:
            print(f"\nKept: https://docs.google.com/document/d/{doc_id}/edit")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("Multi-tab E2E passed.")
    return 0


def _tab_ids(docs_service, doc_id: str) -> list[str]:
    from gdoc_sync.tabs import read_tabs
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True).execute()
    return [t.tab_id for t in read_tabs(doc)]


if __name__ == "__main__":
    sys.exit(main())
