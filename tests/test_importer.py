"""Importing an existing Google Doc as a new, linked markdown file."""

import datetime

import pytest
import yaml

from gdoc_sync import config, importer
from gdoc_sync.pull import RenderedDoc


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GDOC_SYNC_CONFIG", raising=False)
    config.set_config_override(None)
    yield
    config.set_config_override(None)


# --- slugify ---------------------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("Simple Title", "simple-title"),
    ("Matt Handzel x Tzu \U0001f33b 1-1 Chief of Staff advisory",
     "matt-handzel-x-tzu-1-1-chief-of-staff-advisory"),
    ("Q3 Plan: goals & risks", "q3-plan-goals-risks"),
    ("Café résumé", "cafe-resume"),
    ("  leading and trailing  ", "leading-and-trailing"),
    ("...", "untitled-doc"),
    ("\U0001f600\U0001f600", "untitled-doc"),
])
def test_slugify(title, expected):
    assert importer.slugify(title) == expected


def test_slugify_truncates_without_trailing_hyphen():
    slug = importer.slugify("word " * 60)
    assert len(slug) <= importer.MAX_SLUG_LEN
    assert not slug.endswith("-")


# --- frontmatter -----------------------------------------------------------

def test_frontmatter_round_trips_through_yaml():
    """A title full of YAML metacharacters must not produce an unparseable header."""
    title = 'Plan: "phase 2" — 100% #done \U0001f33b'
    text = importer.build_frontmatter(title, "DOC123", today=datetime.date(2026, 8, 5))

    assert text.startswith("---\n") and text.endswith("---\n")
    data = yaml.safe_load(text.split("---")[1])
    assert data["title"] == title
    assert data["gdoc_id"] == "DOC123"
    assert data["source"] == "https://docs.google.com/document/d/DOC123/edit"
    assert data["imported"] == "2026-08-05"


# --- target path -----------------------------------------------------------

def test_explicit_output_wins_over_dest(tmp_path):
    out = tmp_path / "chosen.md"
    assert importer.target_path("Ignored", out, tmp_path / "elsewhere") == out.resolve()


def test_dest_plus_derived_name(tmp_path):
    assert importer.target_path("My Doc", None, tmp_path) == (tmp_path / "my-doc.md").resolve()


# --- import_doc ------------------------------------------------------------

def _stub_render(monkeypatch, *, markdown="# Body\n", tabs=1, title="My Doc"):
    monkeypatch.setattr(importer, "fetch_title", lambda _id: title)
    rendered = RenderedDoc(markdown=markdown, revision_id="rev-1", title=title,
                           tabs=tabs, comments=0, images=0)
    monkeypatch.setattr(importer, "render_doc",
                        lambda _id, **kwargs: rendered)
    return rendered


def test_import_writes_linked_file_with_frontmatter(tmp_path, monkeypatch):
    _stub_render(monkeypatch, markdown="# Body\n\ntext\n")

    path = importer.import_doc("https://docs.google.com/document/d/DOC123/edit",
                               dest=tmp_path, say=lambda *_: None)

    assert path == (tmp_path / "my-doc.md").resolve()
    text = path.read_text()
    assert text.startswith("---\n")
    assert "gdoc_id: DOC123" in text
    assert text.rstrip().endswith("text")
    assert config.get_doc_id(path) == "DOC123"
    assert config.get_revision(path) == "rev-1"


def test_import_registers_merge_ancestors(tmp_path, monkeypatch):
    """Without bases the next sync tick would treat the import as unexplained drift."""
    from gdoc_sync.syncstate import get_bases
    _stub_render(monkeypatch, markdown="# Body\n")

    path = importer.import_doc("DOC123", dest=tmp_path, say=lambda *_: None)

    bases = get_bases(path)
    assert bases.known
    # local carries the frontmatter the doc has nowhere to store; remote does not.
    assert bases.local == path.read_text()
    assert bases.remote == "# Body\n"


def test_no_frontmatter_flag(tmp_path, monkeypatch):
    _stub_render(monkeypatch, markdown="# Body\n")
    path = importer.import_doc("DOC123", dest=tmp_path, frontmatter=False,
                               say=lambda *_: None)
    assert path.read_text() == "# Body\n"


def test_multi_tab_doc_is_pull_only_by_default(tmp_path, monkeypatch):
    """Pushing a flattened tabbed doc would collapse every tab into the first."""
    _stub_render(monkeypatch, tabs=3)
    path = importer.import_doc("DOC123", dest=tmp_path, say=lambda *_: None)
    assert config.is_pull_only(path)


def test_single_tab_doc_is_two_way_by_default(tmp_path, monkeypatch):
    _stub_render(monkeypatch, tabs=1)
    path = importer.import_doc("DOC123", dest=tmp_path, say=lambda *_: None)
    assert not config.is_pull_only(path)


def test_two_way_override_on_a_tabbed_doc(tmp_path, monkeypatch):
    _stub_render(monkeypatch, tabs=3)
    path = importer.import_doc("DOC123", dest=tmp_path, pull_only=False,
                               say=lambda *_: None)
    assert not config.is_pull_only(path)


def test_refuses_to_clobber_without_force(tmp_path, monkeypatch):
    _stub_render(monkeypatch)
    existing = tmp_path / "my-doc.md"
    existing.write_text("mine\n")

    with pytest.raises(FileExistsError):
        importer.import_doc("DOC123", dest=tmp_path, say=lambda *_: None)
    assert existing.read_text() == "mine\n"


def test_force_overwrites(tmp_path, monkeypatch):
    _stub_render(monkeypatch, markdown="# Body\n")
    (tmp_path / "my-doc.md").write_text("mine\n")

    path = importer.import_doc("DOC123", dest=tmp_path, force=True, say=lambda *_: None)
    assert "mine" not in path.read_text()


def test_creates_missing_destination_directory(tmp_path, monkeypatch):
    _stub_render(monkeypatch)
    dest = tmp_path / "deep" / "nested"
    path = importer.import_doc("DOC123", dest=dest, say=lambda *_: None)
    assert path.exists()


# --- pull-only state -------------------------------------------------------

def test_pull_only_round_trip(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("x")
    assert not config.is_pull_only(f)

    config.set_pull_only(f)
    assert config.is_pull_only(f)

    config.set_pull_only(f, False)
    assert not config.is_pull_only(f)


def test_unlink_clears_pull_only(tmp_path):
    f = tmp_path / "a.md"
    f.write_text("x")
    config.set_doc_id(f, "DOC123")
    config.set_pull_only(f)

    config.remove_mapping(f)
    assert not config.is_pull_only(f)
