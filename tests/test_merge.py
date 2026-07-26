"""Tests for the three-way merge engine.

Every merge is exercised twice — once through ``git merge-file`` and once
through the pure-Python fallback — because the fallback is what runs on a
machine without git and a silent divergence between the two would be a
data-loss bug that only shows up on someone else's laptop.
"""

from __future__ import annotations

import pytest

from gdoc_sync import merge as m


@pytest.fixture(params=["git", "pure"])
def merge3(request, monkeypatch):
    """merge3 forced through each backend in turn."""
    if request.param == "pure":
        monkeypatch.setattr(m.shutil, "which", lambda _: None)
    return m.merge3


# ---------------------------------------------------------------------------
# Normalization / hashing
# ---------------------------------------------------------------------------

def test_normalize_collapses_cosmetic_churn():
    assert m.normalize("a\r\nb  \n\n\n\nc\n") == "a\nb\n\nc"


def test_same_content_ignores_trailing_and_blank_line_churn():
    assert m.same_content("# Title\n\nbody", "# Title\n\n\n\nbody   \n\n")
    assert not m.same_content("# Title\n\nbody", "# Title\n\nBODY")


def test_content_hash_is_stable_across_churn():
    assert m.content_hash("x\n\n\ny\n") == m.content_hash("x\n\ny")
    assert m.content_hash("x") != m.content_hash("y")


# ---------------------------------------------------------------------------
# Clean merges
# ---------------------------------------------------------------------------

def test_only_remote_changed_takes_remote(merge3):
    base = "line1\nline2\nline3\n"
    result = merge3(base, base, "line1\nCHANGED\nline3\n")
    assert result.clean
    assert "CHANGED" in result.text


def test_only_local_changed_keeps_local(merge3):
    base = "line1\nline2\nline3\n"
    result = merge3("line1\nMINE\nline3\n", base, base)
    assert result.clean
    assert "MINE" in result.text


def test_both_sides_edit_different_regions_both_survive(merge3):
    """The whole point of the engine: disjoint edits must not destroy each other."""
    base = "\n".join(f"line{i}" for i in range(1, 21)) + "\n"
    ours = base.replace("line2", "LOCAL EDIT")
    theirs = base.replace("line18", "REMOTE EDIT")

    result = merge3(ours, base, theirs)

    assert result.clean, f"expected a clean merge, got:\n{result.text}"
    assert "LOCAL EDIT" in result.text
    assert "REMOTE EDIT" in result.text
    assert not m.has_conflict_markers(result.text)


def test_identical_edits_on_both_sides_do_not_conflict(merge3):
    base = "a\nb\nc\n"
    same = "a\nSAME\nc\n"
    result = merge3(same, base, same)
    assert result.clean
    assert result.text.count("SAME") == 1


def test_append_on_both_ends_merges(merge3):
    base = "middle\n"
    result = merge3("top\nmiddle\n", base, "middle\nbottom\n")
    assert result.clean
    assert "top" in result.text and "bottom" in result.text


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def test_overlapping_edits_conflict_and_keep_both_sides(merge3):
    base = "a\nshared line\nc\n"
    result = merge3("a\nLOCAL\nc\n", base, "a\nREMOTE\nc\n")

    assert result.conflicted
    assert m.has_conflict_markers(result.text)
    # Neither side may be dropped.
    assert "LOCAL" in result.text
    assert "REMOTE" in result.text


def test_conflict_uses_git_style_markers_with_labels(merge3):
    base = "x\n"
    result = merge3("ours\n", base, "theirs\n",
                    label_ours="local", label_theirs="google-doc")
    assert result.conflicted
    assert f"{m.MARKER_OURS} local" in result.text
    assert f"{m.MARKER_THEIRS} google-doc" in result.text


def test_has_conflict_markers_false_for_clean_text():
    assert not m.has_conflict_markers("just\nsome\nmarkdown\n")
    # A markdown heredoc-ish line that merely starts with '=' is not a marker.
    assert not m.has_conflict_markers("Title\n=====\n")


# ---------------------------------------------------------------------------
# Regressions guarding the reported data loss
# ---------------------------------------------------------------------------

def test_no_side_is_silently_dropped_on_conflict(merge3):
    """The 0.5.x bug: a conflict advanced both baselines and the next
    single-sided change then overwrote the un-merged side."""
    base = "intro\n\nbody\n\noutro\n"
    ours = "intro\n\nMY LOCAL PARAGRAPH\n\noutro\n"
    theirs = "intro\n\nTHEIR REMOTE PARAGRAPH\n\noutro\n"

    result = merge3(ours, base, theirs)

    assert "MY LOCAL PARAGRAPH" in result.text
    assert "THEIR REMOTE PARAGRAPH" in result.text


def test_merge_preserves_unrelated_round_trip_noise(merge3):
    """`ours` differs from base by round-trip noise; a remote edit elsewhere
    must apply without discarding that noise."""
    base = "# Title\n\npara one\n\npara two\n\npara three\n"
    ours = "# Title\n\npara one\n\npara two\n\npara three\n\n*emphasis kept*\n"
    theirs = "# Title\n\npara one EDITED\n\npara two\n\npara three\n"

    result = merge3(ours, base, theirs)

    assert result.clean
    assert "*emphasis kept*" in result.text
    assert "para one EDITED" in result.text


def test_empty_base_with_content_on_both_sides_conflicts_not_drops(merge3):
    result = merge3("local only\n", "", "remote only\n")
    assert "local only" in result.text
    assert "remote only" in result.text


def test_deletion_on_one_side_applies_cleanly(merge3):
    base = "keep1\ndelete me\nkeep2\n"
    result = merge3(base, base, "keep1\nkeep2\n")
    assert result.clean
    assert "delete me" not in result.text


def test_large_disjoint_edits_stay_clean(merge3):
    base = "\n".join(f"para {i}\n" for i in range(100))
    ours = base.replace("para 5\n", "para 5 LOCAL\n")
    theirs = base.replace("para 90\n", "para 90 REMOTE\n")
    result = merge3(ours, base, theirs)
    assert result.clean
    assert "LOCAL" in result.text and "REMOTE" in result.text
