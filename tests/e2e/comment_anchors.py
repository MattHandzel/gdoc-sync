#!/usr/bin/env python3
"""Real-API proof that pushing keeps reviewers' comments anchored.

The bug: every push replaced the document's body, so every comment in it read
"Original content deleted" afterwards, and `watch` pushes on every save. The
fake-backed suite cannot see anchors at all, so this drives real Google Docs:

1. A doc is created with comments anchored to specific words (a .docx with
   Word comments, imported the way Drive imports any upload).
2. The markdown is pulled; every comment must land after its words, none at
   the end of the file as orphaned.
3. Prose edits, a new paragraph, a new list item, a deleted paragraph and a
   bolded word are pushed; the push must happen in place and every comment's
   anchor must still cover its words — read back from Drive's own .docx
   export, which is where anchors are visible.
4. The same via `sync`, the path `watch` takes.
5. Control: `push --replace` detaches the anchors, proving step 3's check can
   actually see a lost anchor.

Safety: isolated config and state in a temp directory, one private doc,
trashed on the way out even if a check fails.

Usage::

    python3 tests/e2e/comment_anchors.py

Requires an authenticated CLI (`gdoc-sync doctor` all green).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
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
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        [str(REPO / "src"), *filter(None, [os.environ.get("PYTHONPATH")])])}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode not in expect:
        raise SystemExit(
            f"`gdoc-sync {' '.join(args)}` exited {proc.returncode}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
    return proc.stdout + proc.stderr


# The words each reviewer comment is anchored to. Apostrophes and quotes are
# deliberate: Drive reports selections HTML-escaped, which used to orphan
# every comment on a sentence containing one.
ANCHORS = {
    "c1": "the budget wasn't approved",
    "c2": 'called it "done" too early',
    "c3": "Bravo stays exactly as written",
}

SEED = textwrap.dedent("""\
    # Anchor test

    Alpha paragraph: [Tighten this.]{{.comment-start id="1" author="Reviewer One"}}{c1}[]{{.comment-end id="1"}} and more words after it.

    Bravo paragraph. [Keep?]{{.comment-start id="2" author="Reviewer Two"}}{c3}[]{{.comment-end id="2"}}, with **bold** and a [link](https://example.com).

    - first item
    - second item that someone [Why?]{{.comment-start id="3" author="Reviewer One"}}{c2}[]{{.comment-end id="3"}} on
    - third item

    Charlie paragraph that will be deleted.

    Delta paragraph with a word to embolden later.

    Echo, the closing paragraph.
    """).format(**ANCHORS)


def anchored_texts(drive, doc_id: str) -> dict[str, str]:
    """Comment text → the text its anchor covers, from Drive's .docx export."""
    from gdoc_sync.anchors import docx_comment_ranges

    data = drive.files().export(
        fileId=doc_id,
        mimeType="application/vnd.openxmlformats-officedocument."
                 "wordprocessingml.document").execute()
    return {text: covered.replace("’", "'").replace("“", '"').replace("”", '"')
            for _, text, covered in docx_comment_ranges(data)}


def check_anchors(drive, doc_id: str, label: str) -> None:
    got = anchored_texts(drive, doc_id)
    for note, want in (("Tighten this.", ANCHORS["c1"]), ("Keep?", ANCHORS["c3"]),
                       ("Why?", ANCHORS["c2"])):
        covered = got.get(note, "")
        check(want in covered, f"{label}: '{note}' still anchored to its words",
              f"covers {covered!r}; export: {got}")


def main() -> int:
    from googleapiclient.http import MediaFileUpload

    from gdoc_sync.config import get_font, get_theme, set_config_override
    from gdoc_sync.create import DOCX_MIME
    from gdoc_sync.highlight import highlight_theme_for
    from gdoc_sync.refdoc import styled_reference_docx
    from gdoc_sync.services import get_services
    from gdoc_sync.style import apply_document_styling

    work = Path(tempfile.mkdtemp(prefix="gdoc-anchors-"))
    config = work / "config.yaml"
    config.write_text(textwrap.dedent(f"""\
        state_file: {work}/state.yaml
        defaults:
          clipboard: false
          share: private
          theme: professional
          font: Garamond
    """))
    set_config_override(config)
    drive, docs = get_services()
    note = work / "note.md"
    doc_id = ""
    try:
        print("\n1. a doc with anchored comments, built the way `create` builds one")
        font, theme = get_font(), get_theme()
        ref = styled_reference_docx(font, theme)
        docx = work / "seed.docx"
        # Straight pandoc, not `pandoc_to_docx`: the comment spans need the
        # bracketed_spans reader, which the push path deliberately leaves off.
        seed_cmd = ["pandoc", "-f", "gfm+yaml_metadata_block+bracketed_spans",
                    "-t", "docx", "-o", str(docx)]
        if ref is not None:
            seed_cmd += ["--reference-doc", str(ref)]
        seed_cmd += ["--highlight-style", str(highlight_theme_for(theme))]
        subprocess.run(seed_cmd, input=SEED, text=True, check=True)
        doc_id = drive.files().create(
            body={"name": "gdoc-sync e2e: comment anchors",
                  "mimeType": "application/vnd.google-apps.document"},
            media_body=MediaFileUpload(str(docx), mimetype=DOCX_MIME),
            fields="id").execute()["id"]
        apply_document_styling(docs, doc_id, font=font, theme=theme,
                               baked=ref is not None)
        (work / "doc_id").write_text(doc_id)
        check_anchors(drive, doc_id, "seeded")

        print("\n2. pull: every comment lands after its words")
        note.write_text("")
        run_cli(config, "link", str(note), doc_id)
        run_cli(config, "pull", str(note))
        text = note.read_text()
        check("orphaned" not in text, "no orphaned comments in the pull", text)
        check("wasn't approved{>>Reviewer One: Tighten this.<<}" in text
              or "wasn’t approved{>>Reviewer One: Tighten this.<<}" in text,
              "comment on an apostrophe sentence anchored in place", text)
        check('too early”{>>' in text or 'too early"{>>' in text
              or "too early{>>" in text,
              "comment on a quoted phrase anchored in place", text)

        print("\n3. local edits, pushed")
        edited = (text
                  .replace("and more words after it.",
                           "and a few more words after it, edited locally.")
                  .replace("Charlie paragraph that will be deleted.\n\n", "")
                  .replace("with a word to embolden later",
                           "with a **word** to embolden later")
                  .replace("Echo, the closing paragraph.",
                           "A brand new paragraph before the end.\n\n"
                           "Echo, the closing paragraph."))
        edited = re.sub(r"(- third item)", r"\1\n- a fourth item, new", edited)
        note.write_text(edited)
        out = run_cli(config, "push", str(note), "--yes")
        print(textwrap.indent(out.strip(), "     | "))
        check("Updated in place" in out, "the push edited the doc in place", out)
        check("lose their anchor" not in out, "no anchor-loss warning", out)
        time.sleep(2)
        check_anchors(drive, doc_id, "after push")

        remote = run_cli(config, "pull", doc_id)
        for bit in ("edited locally", "A brand new paragraph", "- a fourth item, new",
                    "**word**"):
            check(bit in remote, f"doc has the edit: {bit!r}", remote)
        check("Charlie paragraph" not in remote, "deleted paragraph is gone", remote)
        check("orphaned" not in remote, "still no orphaned comments", remote)

        print("\n4. the watch path: sync pushes in place too")
        run_cli(config, "sync", str(note))  # establish the sync baseline
        note.write_text(note.read_text().replace("Delta paragraph", "Delta, renamed,"))
        out = run_cli(config, "sync", str(note))
        print(textwrap.indent(out.strip(), "     | "))
        check("pushed" in out.lower(), "sync pushed the local edit", out)
        time.sleep(2)
        check_anchors(drive, doc_id, "after sync")
        out = run_cli(config, "sync", str(note))
        check("up to date" in out or "already" in out.lower(),
              "the next sync is quiet (the in-place doc renders the same)", out)

        print("\n5. control: --replace detaches the anchors")
        run_cli(config, "push", str(note), "--yes", "--replace")
        time.sleep(2)
        got = anchored_texts(drive, doc_id)
        still = [n for n, w in (("Tighten this.", ANCHORS["c1"]),
                                ("Why?", ANCHORS["c2"]))
                 if w in got.get(n, "")]
        check(not still, "a full replace does lose anchors (the check can see it)",
              f"still anchored: {still}; export: {got}")
    finally:
        if doc_id:
            try:
                drive.files().update(fileId=doc_id, body={"trashed": True}).execute()
                print(f"\n  trashed test doc {doc_id}")
            except Exception as e:  # noqa: BLE001
                print(f"\n  could not trash {doc_id}: {e}")

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
