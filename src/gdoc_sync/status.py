"""Show linked files and whether they've drifted from their Google Docs."""

from __future__ import annotations

import json as jsonlib
from pathlib import Path

from .config import all_mappings, config_path, get_revision, state_path


def status(*, remote: bool = False, json_out: bool = False) -> None:
    mappings = all_mappings()
    rows = []
    docs_service = None
    if remote and mappings:
        from .services import NUM_RETRIES, get_services
        _, docs_service = get_services()

    for local, doc_id in sorted(mappings.items()):
        row = {
            "file": local,
            "doc_id": doc_id,
            "exists": Path(local).exists(),
            "remote": "unchecked",
        }
        if docs_service is not None:
            stored = get_revision(local)
            try:
                current = docs_service.documents().get(
                    documentId=doc_id, fields="revisionId"
                ).execute(num_retries=NUM_RETRIES).get("revisionId", "")
                if not stored:
                    row["remote"] = "no-stored-revision"
                elif current == stored:
                    row["remote"] = "in-sync"
                else:
                    row["remote"] = "remote-changed"
            except Exception as e:
                row["remote"] = f"error: {e.__class__.__name__}"
        rows.append(row)

    if json_out:
        print(jsonlib.dumps({
            "config": str(config_path()),
            "state": str(state_path()),
            "links": rows,
        }, indent=2))
        return

    print(f"Config: {config_path()}")
    print(f"State:  {state_path()}")
    if not rows:
        print("No linked files. Use `gdoc-sync create <file>` or `gdoc-sync link <file> <url>`.")
        return
    print(f"{len(rows)} linked file(s):")
    for row in rows:
        marks = []
        if not row["exists"]:
            marks.append("MISSING LOCALLY")
        if row["remote"] not in ("unchecked", "in-sync"):
            marks.append(row["remote"])
        elif row["remote"] == "in-sync":
            marks.append("in-sync")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {row['file']}\n    → https://docs.google.com/document/d/{row['doc_id']}/edit{suffix}")
