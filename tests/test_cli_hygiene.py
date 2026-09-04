"""CLI input validation: bad arguments must fail loudly, not do the wrong thing.

Everything here goes through ``cli.main`` at the argparse level and asserts on
the exit code, so it tests what a user actually types. Nothing may touch the
network or open a browser: ``get_services`` is stubbed and ``webbrowser.open``
is asserted never to have been called.
"""

from __future__ import annotations

import webbrowser

import pytest

from gdoc_sync import config, extras
from gdoc_sync.cli import main


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """XDG dirs in tmp_path, no config override left over between tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """Record browser opens instead of performing them."""
    opened: list[str] = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
    monkeypatch.setattr(extras.webbrowser, "open", lambda url, *a, **k: opened.append(url))
    return opened


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any Google API call is a test failure, not a network round trip."""
    def boom(*a, **k):
        raise AssertionError("get_services() was called; this test must stay offline")

    monkeypatch.setattr("gdoc_sync.services.get_services", boom)
    monkeypatch.setattr("gdoc_sync.extras.get_services", boom)


def run(argv) -> int:
    """Run the CLI, returning its exit code (0 when it returns normally)."""
    try:
        main(list(argv))
    except SystemExit as e:
        return 0 if e.code is None else int(e.code)
    return 0


# --------------------------------------------------------------------------
# 1. `open` / `share` / `auth` on garbage input
# --------------------------------------------------------------------------

def test_open_garbage_exits_2_without_opening_browser(no_browser, capsys):
    assert run(["open", "nonsense"]) == 2
    assert no_browser == []
    err = capsys.readouterr().err
    assert "neither a linked file nor a Google Docs URL/ID" in err


def test_open_missing_path_is_an_error_not_an_id(no_browser, tmp_path):
    assert run(["open", str(tmp_path / "gone.md")]) == 2
    assert no_browser == []


def test_open_accepts_a_bare_doc_id(no_browser, capsys):
    doc_id = "1A2b3C4d5E6f7G8h9I0jKlMnOpQrStUvWxYz"
    assert run(["open", doc_id]) == 0
    assert no_browser == [f"https://docs.google.com/document/d/{doc_id}/edit"]
    assert doc_id in capsys.readouterr().out


def test_open_accepts_a_docs_url(no_browser):
    url = "https://docs.google.com/document/d/1A2b3C4d5E6f7G8h9I0jKlMnOp/edit?usp=sharing"
    assert run(["open", url]) == 0
    assert no_browser == ["https://docs.google.com/document/d/1A2b3C4d5E6f7G8h9I0jKlMnOp/edit"]


def test_share_missing_file_blames_the_file_not_the_flags(no_browser, tmp_path, capsys):
    """A missing path used to be reported as 'pass --with, --anyone, or --private'."""
    assert run(["share", str(tmp_path / "gone.md"), "--anyone", "view"]) == 2
    err = capsys.readouterr().err
    assert "neither a linked file nor a Google Docs URL/ID" in err
    assert "--anyone" not in err
    assert no_browser == []


def test_share_missing_file_without_flags_still_blames_the_file(tmp_path, capsys):
    assert run(["share", str(tmp_path / "gone.md")]) == 2
    assert "neither a linked file nor a Google Docs URL/ID" in capsys.readouterr().err


def test_auth_client_missing_file_fails_before_any_flow(no_browser, tmp_path, capsys):
    assert run(["auth", "--client", str(tmp_path / "no-such-client.json")]) == 2
    assert no_browser == []
    assert "OAuth client file not found" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 2. Unknown --theme
# --------------------------------------------------------------------------

def _markdown(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("# Note\n\nbody\n")
    return str(path)


def test_create_unknown_theme_exits_2_and_lists_themes(tmp_path, capsys):
    assert run(["create", _markdown(tmp_path), "--theme", "solarized"]) == 2
    err = capsys.readouterr().err
    assert "Unknown theme 'solarized'" in err
    assert "professional" in err and "catppuccin-latte" in err


def test_push_unknown_theme_exits_2(tmp_path, capsys):
    assert run(["push", _markdown(tmp_path), "--theme", "nope"]) == 2
    assert "Unknown theme 'nope'" in capsys.readouterr().err


def test_theme_none_stays_valid(tmp_path, monkeypatch):
    """'none' disables theming; it must not be rejected as unknown."""
    from gdoc_sync.style import check_theme
    for spelling in ("none", "off", "false", None):
        check_theme(spelling)

    called = []
    monkeypatch.setattr("gdoc_sync.create.create_doc", lambda *a, **k: called.append(k))
    assert run(["create", _markdown(tmp_path), "--theme", "none"]) == 0
    assert called and called[0]["theme"] == "none"


def test_bad_theme_in_config_file_is_rejected(tmp_path, capsys):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("defaults:\n  theme: dracula-but-typo\n")
    assert run(["--config", str(cfg), "create", _markdown(tmp_path)]) == 2
    assert "Unknown theme 'dracula-but-typo'" in capsys.readouterr().err


def test_custom_config_theme_is_accepted(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "defaults:\n  theme: acme\n"
        "themes:\n  acme:\n    text: '#111111'\n    heading_color: '#ff0000'\n"
    )
    called = []
    monkeypatch.setattr("gdoc_sync.create.create_doc", lambda *a, **k: called.append(k))
    assert run(["--config", str(cfg), "create", _markdown(tmp_path)]) == 0
    assert called


# --------------------------------------------------------------------------
# 3. Missing --config PATH
# --------------------------------------------------------------------------

def test_missing_config_path_exits_2_naming_it(tmp_path, capsys):
    missing = tmp_path / "nowhere" / "config.yaml"
    assert run(["--config", str(missing), "config"]) == 2
    err = capsys.readouterr().err
    assert "Config file not found" in err
    assert str(missing) in err


def test_absent_config_env_var_still_falls_back_to_defaults(tmp_path, monkeypatch):
    """Only --config is strict; $GDOC_SYNC_CONFIG keeps its lenient behaviour."""
    monkeypatch.setenv("GDOC_SYNC_CONFIG", str(tmp_path / "absent.yaml"))
    assert run(["config"]) == 0


# --------------------------------------------------------------------------
# 4. Help text that tells the truth
# --------------------------------------------------------------------------

def test_create_help_names_the_real_default_theme(capsys):
    assert run(["create", "--help"]) == 0
    out = capsys.readouterr().out
    assert config.DEFAULT_THEME in out
    assert "catppuccin-latte" not in out.split("--theme")[1].split("--private")[0]


def test_rainbow_help_describes_the_real_arguments(capsys):
    assert run(["rainbow", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--tab", "--words", "--dry-run", "doc"):
        assert flag in out
    assert "args" not in out.split("positional arguments")[1].split("options")[0]


def test_rainbow_without_a_doc_is_an_argparse_error(capsys):
    assert run(["rainbow"]) == 2
    assert "doc" in capsys.readouterr().err


# --------------------------------------------------------------------------
# 5. Renamed / deleted notes must not vanish from --all in silence
# --------------------------------------------------------------------------

def _link(tmp_path, name: str, *, keep: bool):
    """Create a linked markdown file, optionally deleting it afterwards."""
    path = tmp_path / name
    path.write_text("# note\n")
    config.set_doc_id(str(path), f"doc-for-{name}")
    if not keep:
        path.unlink()
    return path.resolve()


def test_sync_all_warns_about_each_missing_mapping(tmp_path, capsys):
    import argparse

    from gdoc_sync.cli import _sync_targets

    kept = _link(tmp_path, "here.md", keep=True)
    gone_a = _link(tmp_path, "renamed-away.md", keep=False)
    gone_b = _link(tmp_path, "deleted.md", keep=False)

    files = _sync_targets(argparse.Namespace(all=True, files=[]), "sync")
    assert files == [kept]

    err = capsys.readouterr().err
    for missing in (gone_a, gone_b):
        assert str(missing) in err
        assert f"gdoc-sync unlink {missing}" in err
    assert str(kept) not in err
    assert err.count("no longer exists") == 2


def test_sync_all_warning_goes_to_stderr_only(tmp_path, capsys):
    """--json consumers read stdout; warnings must not land in it."""
    import argparse

    from gdoc_sync.cli import _sync_targets

    _link(tmp_path, "here.md", keep=True)
    _link(tmp_path, "gone.md", keep=False)
    _sync_targets(argparse.Namespace(all=True, files=[]), "watch")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "not watching it" in captured.err


def test_sync_all_with_every_file_missing_still_exits_1(tmp_path, capsys):
    _link(tmp_path, "gone.md", keep=False)
    assert run(["sync", "--all"]) == 1
    err = capsys.readouterr().err
    assert "no longer exists" in err
    assert "Nothing to sync" in err


def test_status_lists_missing_files_with_the_unlink_fix(tmp_path, capsys):
    from gdoc_sync.status import status

    kept = _link(tmp_path, "here.md", keep=True)
    gone = _link(tmp_path, "gone.md", keep=False)
    status()
    out = capsys.readouterr().out
    assert "MISSING LOCALLY" in out
    assert f"gdoc-sync unlink {gone}" in out
    assert f"gdoc-sync unlink {kept}" not in out


def test_status_json_still_reports_existence(tmp_path, capsys):
    import json

    _link(tmp_path, "gone.md", keep=False)
    status_json = None
    from gdoc_sync.status import status

    status(json_out=True)
    status_json = json.loads(capsys.readouterr().out)
    assert status_json["links"][0]["exists"] is False
