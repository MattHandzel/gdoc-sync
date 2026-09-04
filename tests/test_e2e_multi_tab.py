"""Run the multi-tab end-to-end script from pytest, when that is safe to do.

Two gates, not one. Credentials are the obvious requirement — without a token
there is nothing to talk to. The opt-in variable is the other: this test
creates real Google Docs in the signed-in account and trashes them again, and
a suite that does that by surprise on someone's machine is a bad suite. The
rest of `pytest -q` runs entirely against fakes and always will.

    GDOC_SYNC_E2E=1 pytest tests/test_e2e_multi_tab.py
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tests" / "e2e" / "multi_tab.py"


def _token_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gdoc-sync" / "token.json"


@pytest.mark.skipif(not _token_path().exists(),
                    reason="no gdoc-sync credentials; run `gdoc-sync auth`")
@pytest.mark.skipif(os.environ.get("GDOC_SYNC_E2E") != "1",
                    reason="set GDOC_SYNC_E2E=1 — this creates real Google Docs")
def test_multi_tab_end_to_end():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": str(REPO / "src")},
        timeout=600,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
