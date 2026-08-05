"""Watch-loop behaviour: event emission, quiet ticks, and failure backoff.

The reconcile decisions are covered in test_sync.py; what matters here is that
the loop reports events in the shape the editor parses, stays silent when
nothing happened, and does not hammer the API when a file keeps failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gdoc_sync import config, watch
from gdoc_sync.sync import BLOCKED, CONFLICT, MERGED, NOOP, PUSHED, SKIPPED, SyncOutcome


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


def emit_lines(capsys, json_lines, event, detail="detail", **extra):
    watch._emit(json_lines, event, Path("/tmp/note.md"), detail, **extra)
    return capsys.readouterr().out.strip()


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def test_json_events_carry_what_the_editor_needs(capsys):
    out = emit_lines(capsys, True, "merged", "merged remote changes",
                     reload=True, pushed=False, conflict=False)
    payload = json.loads(out)

    assert payload["event"] == "merged"
    assert payload["file"].endswith("note.md")
    assert payload["detail"] == "merged remote changes"
    # The plugin reloads on `reload` and shows a conflict UI on `conflict`;
    # both must be explicit rather than inferred from the text.
    assert payload["reload"] is True
    assert payload["conflict"] is False


def test_human_output_is_timestamped_and_named(capsys):
    out = emit_lines(capsys, False, "pushed", "pushed local changes")
    assert "note.md: pushed local changes" in out
    assert out.startswith("[")


def test_every_json_line_is_independently_parseable(capsys):
    watch._emit(True, "a", Path("/x.md"), "one")
    watch._emit(True, "b", Path("/y.md"), "two")
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["a", "b"]


# ---------------------------------------------------------------------------
# Quiet ticks
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("action", [NOOP, SKIPPED, BLOCKED])
def test_uneventful_ticks_stay_silent(capsys, action, monkeypatch):
    """A watcher polling every 15s must not narrate every poll — the editor
    would show a notification for a file nobody touched."""
    monkeypatch.setattr(watch, "reconcile",
                        lambda *a, **k: SyncOutcome(action, "nothing happened"))
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}
    watch._tick(Path("/tmp/note.md"), t, None, False, False, True,
                lambda p: None, lambda p: None)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("action", [PUSHED, MERGED, CONFLICT])
def test_real_events_are_reported(capsys, action, monkeypatch):
    monkeypatch.setattr(watch, "reconcile",
                        lambda *a, **k: SyncOutcome(action, "something happened"))
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}
    watch._tick(Path("/tmp/note.md"), t, None, False, False, True,
                lambda p: None, lambda p: None)
    assert json.loads(capsys.readouterr().out.strip())["event"] == action


def test_tick_records_a_new_revision(monkeypatch, tmp_path):
    monkeypatch.setattr(
        watch, "reconcile",
        lambda *a, **k: SyncOutcome(NOOP, "", revision="rev-99"))
    path = tmp_path / "note.md"
    path.write_text("x")
    t = {"doc_id": "d", "rev": "rev-1", "fails": 0, "skip": 0, "one_way": False}

    watch._tick(path, t, None, False, False, True, lambda p: None, lambda p: None)

    assert t["rev"] == "rev-99"
    assert config.get_revision(str(path)) == "rev-99"


@pytest.mark.parametrize("one_way,no_push,expected", [
    (False, False, True),   # ordinary two-way file
    (True, False, False),   # the file's own mark forbids pushing
    (False, True, False),   # --no-push forbids pushing
    (True, True, False),
])
def test_pull_only_file_is_never_pushed(monkeypatch, tmp_path, one_way, no_push, expected):
    """A tabbed doc is imported pull-only; a push would flatten its tabs away."""
    seen = {}

    def capture(*a, **k):
        seen["allow_push"] = k["allow_push"]
        return SyncOutcome(NOOP, "")

    monkeypatch.setattr(watch, "reconcile", capture)
    path = tmp_path / "note.md"
    path.write_text("x")
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": one_way}

    watch._tick(path, t, None, no_push, False, True, lambda p: None, lambda p: None)

    assert seen["allow_push"] is expected


# ---------------------------------------------------------------------------
# Failure isolation and backoff
# ---------------------------------------------------------------------------

def test_an_exception_does_not_kill_the_watcher(capsys, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network gone")

    monkeypatch.setattr(watch, "reconcile", boom)
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}

    watch._safe_tick(Path("/tmp/note.md"), t, None, False, False, True,
                     lambda p: None, lambda p: None)

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["event"] == "error"
    assert "network gone" in payload["detail"]
    assert t["fails"] == 1


def test_systemexit_from_push_is_caught(capsys, monkeypatch):
    """push()/pull() call sys.exit on API trouble; the loop must survive it."""
    def bail(*a, **k):
        raise SystemExit(2)

    monkeypatch.setattr(watch, "reconcile", bail)
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}
    watch._safe_tick(Path("/tmp/note.md"), t, None, False, False, True,
                     lambda p: None, lambda p: None)
    assert json.loads(capsys.readouterr().out.strip())["event"] == "error"


def test_repeated_failures_back_off(capsys, monkeypatch):
    monkeypatch.setattr(watch, "reconcile",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}

    for _ in range(5):
        watch._safe_tick(Path("/tmp/note.md"), t, None, False, False, True,
                         lambda p: None, lambda p: None)

    capsys.readouterr()
    assert t["skip"] > 0, "a persistently failing file should stop being retried every tick"
    assert t["skip"] <= watch._BACKOFF_MAX


def test_a_success_clears_the_backoff(monkeypatch):
    outcomes = [RuntimeError("nope"), RuntimeError("nope"), None]

    def flaky(*a, **k):
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return SyncOutcome(NOOP, "")

    monkeypatch.setattr(watch, "reconcile", flaky)
    t = {"doc_id": "d", "rev": "r", "fails": 0, "skip": 0, "one_way": False}
    for _ in range(3):
        watch._safe_tick(Path("/tmp/note.md"), t, None, False, False, True,
                         lambda p: None, lambda p: None)
    assert t["fails"] == 0
