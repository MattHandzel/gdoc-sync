#!/usr/bin/env bash
# Hidden beat of demo.tape: an "editor" leaves two comments on the demo doc.
# Needs the devshell python (googleapiclient) and the scratch state on env.
set -euo pipefail
python - <<'PY'
import pathlib

import yaml

from gdoc_sync.services import get_services

state_file = pathlib.Path(__import__("os").environ["XDG_STATE_HOME"]) / "gdoc-sync/state.yaml"
doc_id = next(iter(yaml.safe_load(state_file.read_text())["mappings"].values()))
drive, _ = get_services()
for quoted, content in [
    ("founding-member pricing", "can we tighten the wording here"),
    ("Support docs go live the same morning.", "confirm docs are live by Wednesday"),
]:
    drive.comments().create(
        fileId=doc_id,
        body={"content": content, "quotedFileContent": {"value": quoted}},
        fields="id",
    ).execute()
print("seeded 2 comments on", doc_id)
PY
