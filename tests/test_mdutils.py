"""Frontmatter and title-derivation helpers."""

from gdoc_sync.mdutils import derive_title, strip_frontmatter


def test_strip_frontmatter_removes_leading_block():
    md = "---\ntitle: X\ntags: [a]\n---\n\nBody\n"
    assert strip_frontmatter(md) == "Body\n"


def test_strip_frontmatter_ignores_mid_document_rules():
    md = "Body\n\n---\n\nMore\n"
    assert strip_frontmatter(md) == md


def test_derive_title_prefers_h1():
    md = "---\ntitle: Yaml Title\n---\n# Heading Title\n"
    assert derive_title(md, "fallback") == "Heading Title"


def test_derive_title_falls_back_to_yaml():
    md = '---\ntitle: "Yaml Title"\n---\nNo heading here.\n'
    assert derive_title(md, "fallback") == "Yaml Title"


def test_derive_title_falls_back_to_filename():
    assert derive_title("just text\n", "my-note") == "my-note"
