"""scripts/check_version.py — the guard that keeps the three versions in step.

pyproject.toml, src/gdoc_sync/__init__.py and flake.nix each carry the version
independently; they have silently disagreed before. CI runs this script, so it
is tested both ways: it must pass on the real repo and fail on a mismatch.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_version.py"
TRACKED = ("pyproject.toml", "flake.nix", "src/gdoc_sync/__init__.py")


def check(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True, text=True,
    )


def _fixture_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for rel in TRACKED:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / rel, dest)
    return root


def test_script_exists_and_is_the_one_ci_runs():
    assert SCRIPT.is_file()
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text()
    assert "scripts/check_version.py" in ci


def test_passes_on_this_repo():
    result = check(REPO)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "version OK" in result.stdout


def test_repo_version_is_the_package_version():
    from gdoc_sync import __version__
    assert f'version = "{__version__}"' in (REPO / "pyproject.toml").read_text()
    assert f'version = "{__version__}";' in (REPO / "flake.nix").read_text()


def test_fails_and_shows_a_diff_on_a_mismatched_fixture(tmp_path):
    root = _fixture_repo(tmp_path)
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace('version = "', 'version = "9.9.9', 1)
    )

    result = check(root)
    assert result.returncode == 1
    assert "version mismatch" in result.stderr
    # Every source is named with the value it carries, so the fix is obvious.
    for rel in TRACKED:
        assert rel in result.stderr
    assert "9.9.9" in result.stderr


def test_fails_when_a_source_is_missing(tmp_path):
    root = _fixture_repo(tmp_path)
    (root / "flake.nix").unlink()
    result = check(root)
    assert result.returncode == 1
    assert "flake.nix" in result.stderr


def test_fails_when_a_source_has_no_version(tmp_path):
    root = _fixture_repo(tmp_path)
    (root / "src" / "gdoc_sync" / "__init__.py").write_text('"""no version here."""\n')
    result = check(root)
    assert result.returncode == 1
    assert "no version found" in result.stderr
