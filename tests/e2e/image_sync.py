#!/usr/bin/env python3
"""Real-API end-to-end proof that images sync both ways.

Guards the two failure modes that only show up against the real round trip:

* the phantom-drift churn — every push recreates the doc's inline objects,
  so without content matching a doc with images never agrees with its file
  and watch mutates a document nobody is editing;
* the destroy-on-push — an image added in Google Docs that the renderer
  cannot express in markdown would be silently deleted by the next push.

Safety: an isolated config/state pair in a temp directory (your real
mappings are never touched), one private doc, trashed on the way out even
if a check fails.

Usage::

    python3 tests/e2e/image_sync.py

Requires an authenticated CLI (`gdoc-sync doctor` all green).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import textwrap
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

sys.stdout.reconfigure(line_buffering=True)

failures: list[str] = []
checks = 0

# A publicly fetchable PNG for insertInlineImage (the API downloads the URI
# itself, so it must be reachable without auth).
PUBLIC_PNG = "https://www.gstatic.com/images/branding/product/1x/docs_2020q4_48dp.png"


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


def png_1x1(rgb: tuple[int, int, int]) -> bytes:
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return len(data).to_bytes(4, "big") + c + zlib.crc32(c).to_bytes(4, "big")

    ihdr = chunk(b"IHDR", (1).to_bytes(4, "big") + (1).to_bytes(4, "big")
                 + b"\x08\x02\x00\x00\x00")
    idat = chunk(b"IDAT", zlib.compress(b"\x00" + bytes(rgb)))
    return b"\x89PNG\r\n\x1a\n" + ihdr + idat + chunk(b"IEND", b"")


def main() -> int:
    from gdoc_sync.config import set_config_override

    work = Path(tempfile.mkdtemp(prefix="gdoc-e2e-img-"))
    config = work / "config.yaml"
    config.write_text(textwrap.dedent(f"""\
        state_file: {work}/state.yaml
        defaults:
          clipboard: false
          share: private
          theme: minimal
          font: Garamond
    """))

    (work / "attachments").mkdir()
    (work / "attachments" / "red.png").write_bytes(png_1x1((255, 0, 0)))
    note = work / "note.md"
    note.write_text(textwrap.dedent("""\
        # Image sync test

        Some text before.

        ![red dot](attachments/red.png)

        Some text after.
        """))

    doc_id = ""
    try:
        print("\n1. create a doc from markdown with a local image")
        run_cli(config, "create", str(note))
        set_config_override(config)
        from gdoc_sync.config import get_doc_id
        from gdoc_sync.services import get_services
        doc_id = get_doc_id(str(note)) or ""
        check(bool(doc_id), "doc created and linked")
        if not doc_id:
            return 1

        _drive, docs = get_services()

        print("\n2. first sync is calm — no phantom image drift")
        before = note.read_text()
        out = run_cli(config, "sync", str(note))
        check("conflict" not in out.lower(), "no conflict", out)
        check(note.read_text() == before, "file untouched",
              "sync rewrote a file nobody edited")

        print("\n3. add an image in Google Docs, sync brings it home")
        end = docs.documents().get(
            documentId=doc_id, fields="body(content(endIndex))").execute()
        end_index = end["body"]["content"][-1]["endIndex"]
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": [{
            "insertInlineImage": {"location": {"index": end_index - 1},
                                  "uri": PUBLIC_PNG}}]}).execute()
        run_cli(config, "sync", str(note))
        text = note.read_text()
        links = re.findall(r"!\[image\]\((note-assets/img-[0-9a-f]+\.\w+)\)", text)
        check(len(links) == 1, "doc-added image link merged into the markdown", text)
        check(bool(links) and (work / links[0]).exists(), "image file downloaded")
        check("![red dot](attachments/red.png)" in text,
              "the original image kept its own path and alt text")

        print("\n4. steady state — repeated sync changes nothing")
        before = note.read_text()
        run_cli(config, "sync", str(note))
        check(note.read_text() == before, "no churn on the file")

        print("\n5. add a local image, sync sends it up, nothing duplicates")
        (work / "attachments" / "blue.png").write_bytes(png_1x1((0, 0, 255)))
        note.write_text(note.read_text() + "\n![blue dot](attachments/blue.png)\n")
        run_cli(config, "sync", str(note))
        objects = docs.documents().get(
            documentId=doc_id, fields="inlineObjects").execute()
        check(len(objects.get("inlineObjects", {})) == 3,
              "doc holds exactly 3 images",
              f"got {len(objects.get('inlineObjects', {}))}")
        check(note.read_text().count("![") == 3, "markdown references exactly 3")

        print("\n6. and stays steady")
        before = note.read_text()
        run_cli(config, "sync", str(note))
        check(note.read_text() == before, "final steady state")

    finally:
        if doc_id:
            print(f"\nTrashing test doc {doc_id}")
            trash = Path(__file__).parent / "trash_doc.py"
            subprocess.run([sys.executable, str(trash), doc_id], check=False)

    print(f"\n{checks - len(failures)}/{checks} checks passed")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("Image sync E2E passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
