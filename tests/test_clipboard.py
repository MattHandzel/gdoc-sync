"""Platform-aware clipboard selection (issue #1).

The old implementation tried wl-copy → xclip → pbcopy regardless of platform,
so on WSL it wrote to a Linux selection the user could not paste from, and over
SSH it silently failed. These tests pin the ordering decisions.
"""

from __future__ import annotations

import pytest

from gdoc_sync import mdutils


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("WAYLAND_DISPLAY", "DISPLAY", "WSL_DISTRO_NAME", "WSL_INTEROP",
                "PREFIX", "TMUX"):
        monkeypatch.delenv(var, raising=False)


def names(candidates):
    return [name for _cmd, name in candidates]


def test_macos_uses_pbcopy(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "darwin")
    assert names(mdutils._clipboard_candidates()) == ["pbcopy"]


def test_windows_uses_clip(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "win32")
    assert names(mdutils._clipboard_candidates())[0] == "clip"


def test_wayland_prefers_wl_copy(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")  # Xwayland is usually present too
    assert names(mdutils._clipboard_candidates())[0] == "wl-copy"


def test_x11_without_wayland_prefers_xclip(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":0")
    assert names(mdutils._clipboard_candidates())[0] == "xclip"


def test_wsl_targets_the_windows_clipboard(monkeypatch):
    """The Linux tools may exist, but the clipboard the user pastes from is
    Windows'."""
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setenv("WSL_INTEROP", "/run/WSL/1_interop")
    monkeypatch.setenv("DISPLAY", ":0")
    assert names(mdutils._clipboard_candidates())[0] == "clip.exe"


def test_termux_uses_its_own_tool(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    assert names(mdutils._clipboard_candidates())[0] == "termux-clipboard-set"


def test_headless_linux_still_offers_every_tool(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    got = names(mdutils._clipboard_candidates())
    assert {"wl-copy", "xclip", "xsel", "pbcopy"} <= set(got)


def test_candidates_are_not_duplicated(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    got = names(mdutils._clipboard_candidates())
    assert len(got) == len(set(got))


# ---------------------------------------------------------------------------
# copy_to_clipboard behaviour
# ---------------------------------------------------------------------------

def test_explicit_command_overrides_detection(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(mdutils.subprocess, "run", fake_run)
    ok, tool = mdutils.copy_to_clipboard("hello", command="my-clip --stdin")

    assert ok and tool == "my-clip"
    assert seen["cmd"] == ["my-clip", "--stdin"]


def test_falls_through_to_the_next_tool_when_one_is_missing(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    tried = []

    def fake_run(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] != "xsel":
            raise FileNotFoundError(cmd[0])
        return type("P", (), {"returncode": 0})()

    monkeypatch.setattr(mdutils.subprocess, "run", fake_run)
    ok, tool = mdutils.copy_to_clipboard("hello")

    assert ok and tool == "xsel"
    assert "wl-copy" in tried


def test_osc52_fallback_when_nothing_is_installed(monkeypatch):
    """Over SSH there may be no clipboard binary at all; the terminal escape is
    the only route back to the user's machine."""
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setattr(mdutils.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError))
    written = []
    monkeypatch.setattr(mdutils.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(mdutils.sys.stdout, "write", written.append)
    monkeypatch.setattr(mdutils.sys.stdout, "flush", lambda: None)

    ok, tool = mdutils.copy_to_clipboard("hello")

    assert ok and "OSC 52" in tool
    assert written and written[0].startswith("\033]52;c;")


def test_osc52_is_wrapped_for_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    written = []
    monkeypatch.setattr(mdutils.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(mdutils.sys.stdout, "write", written.append)
    monkeypatch.setattr(mdutils.sys.stdout, "flush", lambda: None)

    assert mdutils._osc52("hello")
    assert written[0].startswith("\033Ptmux;")


def test_osc52_never_writes_escapes_into_a_pipe(monkeypatch):
    """Emitting escape bytes on a non-tty would corrupt piped output."""
    monkeypatch.setattr(mdutils.sys.stdout, "isatty", lambda: False)
    assert not mdutils._osc52("hello")


def test_reports_failure_when_everything_fails(monkeypatch):
    monkeypatch.setattr(mdutils.sys, "platform", "linux")
    monkeypatch.setattr(mdutils.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError))
    monkeypatch.setattr(mdutils.sys.stdout, "isatty", lambda: False)
    assert mdutils.copy_to_clipboard("hello") == (False, "")


def test_explicit_command_does_not_fall_back_to_osc52(monkeypatch):
    """If the user named a command, silently doing something else would hide
    the fact that their configuration is wrong."""
    monkeypatch.setattr(mdutils.subprocess, "run",
                        lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError))
    monkeypatch.setattr(mdutils.sys.stdout, "isatty", lambda: True)
    assert mdutils.copy_to_clipboard("x", command="nope") == (False, "")
