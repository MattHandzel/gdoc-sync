"""Config/state resolution, overrides, and legacy-format compatibility."""

import os
import stat
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

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
    assert config.get_theme() == "professional"
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


def test_remove_mapping(tmp_path, monkeypatch):
    import gdoc_sync.config as config

    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults: {font: Arial}\n")
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(cfg))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config.set_config_override(None)

    f = tmp_path / "note.md"
    f.write_text("hi")
    config.set_doc_id(str(f), "doc123", "rev1")
    assert config.get_doc_id(str(f)) == "doc123"

    assert config.remove_mapping(str(f)) is True
    assert config.get_doc_id(str(f)) is None
    assert config.get_revision(str(f)) is None
    assert config.remove_mapping(str(f)) is False


def test_parse_share_with():
    import pytest

    from gdoc_sync.create import parse_share_with

    assert parse_share_with("a@b.com") == ("a@b.com", "commenter")
    assert parse_share_with("a@b.com:edit") == ("a@b.com", "writer")
    assert parse_share_with("a@b.com:view") == ("a@b.com", "reader")
    with pytest.raises(ValueError):
        parse_share_with("nonsense")
    with pytest.raises(ValueError):
        parse_share_with("a@b.com:owner")


def test_default_theme_is_professional(tmp_path, monkeypatch):
    import gdoc_sync.config as config

    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(tmp_path / "absent.yaml"))
    config.set_config_override(None)
    assert config.get_theme() == "professional"


def test_custom_theme_from_config(tmp_path, monkeypatch):
    import gdoc_sync.config as config
    from gdoc_sync.style import UnknownThemeError, available_themes, resolve_theme

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults: {theme: acme}\n"
        "themes:\n"
        "  acme:\n"
        "    text: '#111111'\n"
        "    heading_color: '#0033aa'\n"
    )
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(cfg))
    config.set_config_override(None)

    assert config.get_theme() == "acme"
    assert "acme" in available_themes()
    palette = resolve_theme("acme")
    assert palette["text"] == "#111111"
    assert palette["headings"]["HEADING_1"] == "#0033aa"
    assert palette["headings"]["HEADING_6"] == "#0033aa"
    assert palette["background"] == "#ffffff"  # sensible fill-ins
    # built-ins still resolve; unknown names are an error, not a silent None
    # (returning None here produced an unstyled doc and said nothing)
    assert resolve_theme("professional")["pageless"] is False
    assert resolve_theme("catppuccin-latte") is not None
    with pytest.raises(UnknownThemeError, match="nope"):
        resolve_theme("nope")
    # "no theme" spellings stay valid
    assert resolve_theme(None) is None
    assert resolve_theme("none") is None


def test_custom_theme_heading_shapes():
    from gdoc_sync.style import _normalize_theme

    by_list = _normalize_theme({"headings": ["#1", "#2", "#3"]})
    assert by_list["headings"]["HEADING_1"] == "#1"
    assert by_list["headings"]["HEADING_3"] == "#3"
    assert by_list["headings"]["HEADING_6"] == "#3"  # last color extends
    assert by_list["headings"]["TITLE"] == "#1"

    by_map = _normalize_theme({"headings": {"heading_2": "#b", "TITLE": "#t"}})
    assert by_map["headings"]["HEADING_2"] == "#b"
    assert by_map["headings"]["TITLE"] == "#t"


# ---------------------------------------------------------------------------
# State locking (P0-10): concurrent mutators must not lose each other's writes
# ---------------------------------------------------------------------------

needs_fcntl = pytest.mark.skipif(
    config.fcntl is None, reason="no fcntl on this platform; state_lock is a no-op there"
)

# Run in a *separate process* so the mappings can only survive via the on-disk
# lock — an in-process lock would pass this test while the real bug remained.
_LINK_CHILD = textwrap.dedent("""
    import sys
    from gdoc_sync import config
    config.set_doc_id(sys.argv[1], sys.argv[2])
""")


@needs_fcntl
def test_parallel_set_doc_id_keeps_every_mapping(tmp_path):
    """24 concurrent `link`-shaped writers, 24 mappings. Unlocked this loses ~a third."""
    import gdoc_sync

    n = 24
    src_root = str(Path(gdoc_sync.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([src_root, env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env.pop("GDOC_SYNC_CONFIG", None)

    files, procs = [], []
    for i in range(n):
        f = tmp_path / f"f{i}.md"
        f.write_text("x")
        files.append(f)
        procs.append(subprocess.Popen(
            [sys.executable, "-c", _LINK_CHILD, str(f), f"doc{i}"],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ))

    for p in procs:
        _, err = p.communicate(timeout=60)
        assert p.returncode == 0, err.decode()

    mappings = config.all_mappings()
    missing = [i for i, f in enumerate(files) if str(f.resolve()) not in mappings]
    assert not missing, f"{len(missing)} of {n} mappings lost: {missing}"
    for i, f in enumerate(files):
        assert mappings[str(f.resolve())] == f"doc{i}"


@needs_fcntl
def test_state_lock_blocks_a_second_holder(tmp_path):
    acquired = threading.Event()

    def second():
        with config.state_lock(timeout=5):
            acquired.set()

    with config.state_lock():
        t = threading.Thread(target=second, daemon=True)
        t.start()
        assert not acquired.wait(0.4), "second holder got the lock while it was held"
    assert acquired.wait(5), "second holder never got the lock after release"
    t.join(timeout=5)


@needs_fcntl
def test_state_lock_times_out_with_a_clear_error(tmp_path):
    holding, release = threading.Event(), threading.Event()

    def holder():
        with config.state_lock():
            holding.set()
            release.wait(10)

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert holding.wait(5)
    try:
        with pytest.raises(RuntimeError) as excinfo:
            with config.state_lock(timeout=0.1):
                pass
        assert str(config.state_lock_path()) in str(excinfo.value)
    finally:
        release.set()
        t.join(timeout=5)


@needs_fcntl
def test_lock_file_sits_beside_state_and_is_private(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("x")
    config.set_doc_id(f, "doc1")
    lock = config.state_lock_path()
    assert lock == config.state_path().with_name("state.yaml.lock")
    assert lock.exists()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_mutate_state_falsey_result_writes_nothing(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("x")
    config.set_doc_id(f, "doc1")
    # truthy result -> written back
    assert config.mutate_state(lambda state: state.pop("mappings")) is not None
    assert config.all_mappings() == {}
    # falsey result -> the mutation is discarded, the file untouched
    before = config.state_path().read_text()

    def scribble(state):
        state["mappings"] = {"ghost": "docZ"}
        return False

    assert config.mutate_state(scribble) is False
    assert config.state_path().read_text() == before
    assert config.all_mappings() == {}


def test_corrupt_state_is_still_salvaged_under_the_lock(tmp_path):
    p = config.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("mappings: [unterminated\n")
    f = tmp_path / "note.md"
    f.write_text("x")
    config.set_doc_id(f, "doc1")
    assert p.with_suffix(p.suffix + ".corrupt").exists()
    assert config.get_doc_id(f) == "doc1"


def test_set_pull_only_is_idempotent_and_removable(tmp_path):
    f = tmp_path / "note.md"
    f.write_text("x")
    config.set_pull_only(f)
    config.set_pull_only(f)  # no-op, must not duplicate
    assert config.load_state()["pull_only"] == [str(f.resolve())]
    assert config.is_pull_only(f) is True
    config.set_pull_only(f, False)
    assert config.is_pull_only(f) is False


# ---------------------------------------------------------------------------
# Portable state keys: one state file, several machines
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """A home directory at ``/…/Users/<user>`` so foreign-home keys can be tested."""
    home = tmp_path / "Users" / "matt"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    assert Path.home() == home
    return home


def test_new_links_are_keyed_relative_to_home(fake_home):
    f = fake_home / "notes" / "a.md"
    f.parent.mkdir()
    f.write_text("x")
    config.set_doc_id(f, "doc1")
    assert config.load_state()["mappings"] == {"~/notes/a.md": "doc1"}
    assert config.get_doc_id(f) == "doc1"
    assert config.all_mappings() == {str(f): "doc1"}


def test_file_outside_home_keeps_absolute_key(fake_home, tmp_path):
    f = tmp_path / "elsewhere.md"
    f.write_text("x")
    config.set_doc_id(f, "doc1")
    assert config.load_state()["mappings"] == {str(f.resolve()): "doc1"}
    assert config.get_doc_id(f) == "doc1"


def test_legacy_key_from_another_machine_is_found(fake_home):
    """A state file written on Linux (``/home/matt/…``) works on a Mac."""
    f = fake_home / "notes" / "a.md"
    f.parent.mkdir()
    f.write_text("x")
    config.save_state({
        "mappings": {"/home/matt/notes/a.md": "doc1"},
        "revisions": {"/home/matt/notes/a.md": "rev1"},
        "pull_only": ["/home/matt/notes/a.md"],
    })
    assert config.get_doc_id(f) == "doc1"
    assert config.get_revision(f) == "rev1"
    assert config.is_pull_only(f)
    assert config.all_mappings() == {str(f): "doc1"}


def test_legacy_key_for_another_user_is_not_claimed(fake_home):
    f = fake_home / "notes" / "a.md"
    f.parent.mkdir()
    f.write_text("x")
    config.save_state({"mappings": {"/home/someone-else/notes/a.md": "doc1"}})
    assert config.get_doc_id(f) is None
    assert config.all_mappings() == {"/home/someone-else/notes/a.md": "doc1"}


def test_existing_legacy_key_is_updated_in_place(fake_home):
    """An older gdoc-sync on the other machine must still find its own keys."""
    f = fake_home / "notes" / "a.md"
    f.parent.mkdir()
    f.write_text("x")
    legacy = "/home/matt/notes/a.md"
    config.save_state({"mappings": {legacy: "doc1"}, "revisions": {legacy: "rev1"}})
    config.set_doc_id(f, "doc2", revision_id="rev2")
    config.set_revision(f, "rev3")
    config.set_pull_only(f, True)
    state = config.load_state()
    assert state["mappings"] == {legacy: "doc2"}
    assert state["revisions"] == {legacy: "rev3"}
    assert state["pull_only"] == [legacy]
    config.set_pull_only(f, False)
    assert config.load_state()["pull_only"] == []
    assert config.remove_mapping(f)
    assert config.load_state()["mappings"] == {}


def test_expand_state_key():
    home = str(Path.home())
    user = Path.home().name
    assert config.expand_state_key("~/x/y.md") == f"{home}/x/y.md"
    assert config.expand_state_key(f"/home/{user}/x.md") == f"{home}/x.md"
    assert config.expand_state_key(f"/Users/{user}/x.md") == f"{home}/x.md"
    assert config.expand_state_key("/home/other/x.md") == "/home/other/x.md"
    assert config.expand_state_key("/tmp/x.md") == "/tmp/x.md"
