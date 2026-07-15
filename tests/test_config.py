"""Config/state resolution, overrides, and legacy-format compatibility."""

import textwrap

import pytest

from gdoc_sync import config


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point every XDG dir at tmp_path and clear overrides between tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


def test_default_config_path_is_xdg(tmp_path):
    assert config.config_path() == tmp_path / "config" / "gdoc-sync" / "config.yaml"


def test_env_var_overrides_xdg(tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(custom))
    assert config.config_path() == custom


def test_cli_flag_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(tmp_path / "env.yaml"))
    config.set_config_override(tmp_path / "flag.yaml")
    assert config.config_path() == tmp_path / "flag.yaml"


def test_defaults_without_config_file():
    assert config.get_font() == "Garamond"
    assert config.get_theme() == "catppuccin-latte"
    assert config.get_share_default() == "comment"
    assert config.get_clipboard_default() is True


def test_settings_from_defaults_section(tmp_path):
    p = config.config_path()
    p.parent.mkdir(parents=True)
    p.write_text(textwrap.dedent("""
        defaults:
          font: EB Garamond
          theme: none
          share: view
          clipboard: false
    """))
    assert config.get_font() == "EB Garamond"
    assert config.get_theme() is None
    assert config.get_share_default() == "view"
    assert config.get_clipboard_default() is False


def test_legacy_top_level_settings(tmp_path):
    """The pre-0.2 format put font:/theme: at the top level."""
    p = config.config_path()
    p.parent.mkdir(parents=True)
    p.write_text("font: Arial\ntheme: catppuccin-mocha\n")
    assert config.get_font() == "Arial"
    assert config.get_theme() == "catppuccin-mocha"


def test_state_defaults_to_xdg_state(tmp_path):
    assert config.state_path() == tmp_path / "state" / "gdoc-sync" / "state.yaml"


def test_legacy_combined_file_is_its_own_state(tmp_path, monkeypatch):
    """A pre-0.2 .gdoc-sync.yaml (mappings inside the config) keeps working."""
    legacy = tmp_path / ".gdoc-sync.yaml"
    legacy.write_text("font: Garamond\nmappings:\n  /a/b.md: doc123\n")
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(legacy))
    assert config.state_path() == legacy
    assert config.load_state()["mappings"]["/a/b.md"] == "doc123"


def test_legacy_combined_write_preserves_settings(tmp_path, monkeypatch):
    legacy = tmp_path / ".gdoc-sync.yaml"
    legacy.write_text("font: Arial\nmappings: {}\n")
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(legacy))
    f = tmp_path / "note.md"
    f.write_text("# hi\n")
    config.set_doc_id(f, "docXYZ", "rev1")
    # settings survive a state write into the same file
    assert config.get_font() == "Arial"
    assert config.get_doc_id(f) == "docXYZ"
    assert config.get_revision(f) == "rev1"


def test_explicit_state_file_key(tmp_path):
    p = config.config_path()
    p.parent.mkdir(parents=True)
    vault_state = tmp_path / "vault" / "sync-state.yaml"
    p.write_text(f"state_file: {vault_state}\n")
    assert config.state_path() == vault_state
    f = tmp_path / "note.md"
    f.write_text("x")
    config.set_doc_id(f, "docA")
    assert vault_state.exists()
    assert config.get_doc_id(f) == "docA"


def test_mapping_roundtrip_and_revisions(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("x")
    assert config.get_doc_id(f) is None
    config.set_doc_id(f, "doc1", "revA")
    config.set_revision(f, "revB")
    assert config.get_doc_id(f) == "doc1"
    assert config.get_revision(f) == "revB"
    assert str(f.resolve()) in config.all_mappings()


@pytest.mark.parametrize("url,expected", [
    ("https://docs.google.com/document/d/abc123XYZ_-/edit#heading=h.x", "abc123XYZ_-"),
    ("https://docs.google.com/document/d/idOnly/", "idOnly"),
    ("bareId42", "bareId42"),
])
def test_extract_doc_id(url, expected):
    assert config.extract_doc_id_from_url(url) == expected
