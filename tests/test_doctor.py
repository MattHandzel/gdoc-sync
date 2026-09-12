"""`doctor` must catch a setup that parses but cannot work on this machine."""

import os
import stat

import pytest

from gdoc_sync import config, doctor, syncstate


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


def _write_config(tmp_path, state_file: str) -> None:
    cfg = tmp_path / "config" / "gdoc-sync" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"state_file: {state_file}\n")


def test_state_file_that_cannot_be_created_is_a_failure(tmp_path):
    """A `state_file:` copied from another machine parses fine and then the
    first `create` dies making the directory. doctor says so up front."""
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    if os.access(locked, os.W_OK):  # running as root: cannot make it read-only
        pytest.skip("cannot create an unwritable directory here")
    try:
        _write_config(tmp_path, str(locked / "nowhere" / "state.yaml"))
        mark, detail = doctor._check_state()
    finally:
        locked.chmod(stat.S_IRWXU)
    assert mark == doctor.BAD
    assert "cannot be created" in detail
    assert "state_file:" in detail


def test_state_counts_links_missing_on_this_machine(tmp_path):
    state = tmp_path / "vault" / "state.yaml"
    _write_config(tmp_path, str(state))
    present = tmp_path / "here.md"
    present.write_text("x")
    config.set_doc_id(present, "doc1")
    config.set_doc_id(tmp_path / "gone.md", "doc2")
    mark, detail = doctor._check_state()
    assert mark == doctor.OK
    assert "2 linked file(s), 1 not present on this machine" in detail


def test_clipboard_reports_the_platform_tool(monkeypatch):
    monkeypatch.setattr(doctor, "_clipboard_candidates", lambda: [(["pbcopy"], "pbcopy")])
    monkeypatch.setattr(doctor.shutil, "which", lambda t: "/usr/bin/pbcopy" if t == "pbcopy" else None)
    assert doctor._check_clipboard() == (doctor.OK, "pbcopy")


def test_clipboard_honours_explicit_command(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "gdoc-sync" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("defaults:\n  clipboard_command: my-copy --flag\n")
    monkeypatch.setattr(doctor.shutil, "which", lambda t: "/usr/bin/my-copy" if t == "my-copy" else None)
    mark, detail = doctor._check_clipboard()
    assert mark == doctor.OK
    assert detail.startswith("my-copy --flag")


def test_conflict_written_on_linux_is_seen_on_mac(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "matt"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    f = home / "a.md"
    f.write_text("x")
    config.save_state({"conflicts": {"/home/matt/a.md": {"since": "t", "detail": "d",
                                                          "markers": True, "remote_copy": ""}}})
    c = syncstate.get_conflict(f)
    assert c is not None and c.detail == "d" and c.path == str(f)
    assert list(syncstate.all_conflicts()) == [str(f)]
    assert syncstate.clear_conflict(f)
    assert config.load_state()["conflicts"] == {}
