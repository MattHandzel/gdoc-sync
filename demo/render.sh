#!/usr/bin/env bash
# Render docs/assets/demo.gif by driving the REAL CLI against the REAL API.
# Run from the repo root, inside `nix develop` (or any env with vhs, the
# gdoc-sync deps on PYTHONPATH, and an authenticated token):
#
#   nix build .#default -o /tmp/gdoc-sync-demo-bin
#   nix develop -c demo/render.sh /tmp/gdoc-sync-demo-bin/bin
#
# The demo doc is created under a scratch config and trashed afterwards.
set -euo pipefail
BIN_DIR=${1:?usage: demo/render.sh <dir containing the gdoc-sync binary>}
REPO=$(cd "$(dirname "$0")/.." && pwd)

export DEMO_DIR=$(mktemp -d /tmp/gdoc-sync-demo.XXXXXX)
export GDOC_SYNC_CONFIG=$DEMO_DIR/config.yaml
export XDG_STATE_HOME=$DEMO_DIR/state
export PYTHONPATH=$REPO/src
export PATH=$BIN_DIR:$PATH

cp "$REPO/demo/draft.md" "$DEMO_DIR/draft.md"
cp "$REPO/demo/seed-comments.sh" "$DEMO_DIR/seed-comments.sh"
cat > "$GDOC_SYNC_CONFIG" <<CFG
defaults: {font: Garamond, theme: professional, share: comment, clipboard: false}
CFG

cd "$REPO"
vhs demo/demo.tape

# Cleanup: trash the demo doc, remove the scratch dir.
python - <<'PY'
import os
import pathlib

import yaml

from gdoc_sync.services import get_services

state_file = pathlib.Path(os.environ["XDG_STATE_HOME"]) / "gdoc-sync/state.yaml"
if state_file.exists():
    for doc_id in yaml.safe_load(state_file.read_text())["mappings"].values():
        get_services()[0].files().update(fileId=doc_id, body={"trashed": True}).execute()
        print("trashed", doc_id)
PY
rm -rf "$DEMO_DIR"
echo "Rendered docs/assets/demo.gif"
