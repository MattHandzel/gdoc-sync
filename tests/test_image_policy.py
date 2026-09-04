"""What local files a push may read (P0-2).

The threat these tests encode: a collaborator with edit access on a shared doc
types ``![logo](../../../.ssh/id_rsa)``, `pull` brings it into the operator's
markdown verbatim, and the next `push` reads that file — uploading it publicly
on the tabbed path, embedding it in the docx on the other. Every case below is
a path that must never be read, plus the ordinary vault images that must keep
working.
"""

import os

import pytest

from gdoc_sync.image_policy import (
    check_target,
    containment_root,
    enforce_markdown,
    local_image_targets,
    scan_markdown,
    sniff_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)
JPEG = b"\xff\xd8\xff\xe0" + bytes(64)
GIF = b"GIF89a" + bytes(64)
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + bytes(64)
BMP = b"BM" + bytes(64)


@pytest.fixture
def vault(tmp_path):
    """An Obsidian vault: a note in a subfolder, attachments beside it."""
    root = tmp_path / "vault"
    (root / ".obsidian").mkdir(parents=True)
    (root / "notes").mkdir()
    (root / "attachments").mkdir()
    (root / "attachments" / "logo.png").write_bytes(PNG)
    (root / ".ssh").mkdir()
    (root / ".ssh" / "id_rsa").write_bytes(PNG)      # even if it *were* a PNG
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.png").write_bytes(PNG)
    return root


# --------------------------------------------------------------------------
# The containment root
# --------------------------------------------------------------------------

def test_the_root_is_the_vault_not_the_notes_folder(vault):
    """Why the root is not simply resource_dir: ../attachments/ is normal."""
    assert containment_root(vault / "notes") == vault


def test_a_git_checkout_is_a_root_too(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "docs").mkdir()
    assert containment_root(repo / "docs") == repo


def test_without_a_marker_the_root_is_the_resource_dir(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir()
    assert containment_root(loose) == loose


def test_no_resource_dir_means_no_local_file_at_all():
    path, refusal = check_target("logo.png", None)
    assert path is None
    assert "no project directory" in refusal.reason


# --------------------------------------------------------------------------
# What is refused
# --------------------------------------------------------------------------

def test_a_relative_path_climbing_out_of_the_vault_is_refused(vault):
    path, refusal = check_target("../../outside/secret.png", vault / "notes")
    assert path is None and not refusal.missing
    assert "outside the note's project" in refusal.reason


def test_an_absolute_system_path_is_refused(vault):
    path, refusal = check_target("/etc/hostname", vault / "notes")
    assert path is None and not refusal.missing
    assert "outside the note's project" in refusal.reason


def test_a_hidden_directory_inside_the_vault_is_refused(vault):
    path, refusal = check_target("../.ssh/id_rsa", vault / "notes")
    assert path is None and not refusal.missing
    assert "hidden path" in refusal.reason


def test_a_symlink_may_not_escape_the_vault(vault):
    link = vault / "attachments" / "innocent.png"
    os.symlink(vault.parent / "outside" / "secret.png", link)
    path, refusal = check_target("../attachments/innocent.png", vault / "notes")
    assert path is None and not refusal.missing
    assert "outside the note's project" in refusal.reason


def test_a_home_relative_path_is_never_expanded(vault):
    path, refusal = check_target("~/x.png", vault / "notes")
    assert path is None and not refusal.missing
    assert "home-directory path" in refusal.reason


def test_a_text_file_with_an_image_name_is_refused(vault):
    (vault / "attachments" / "x.png").write_text("ssh-rsa AAAAB3Nza...\n")
    path, refusal = check_target("../attachments/x.png", vault / "notes")
    assert path is None and not refusal.missing
    assert "not a PNG" in refusal.reason


def test_svg_is_refused_because_docs_cannot_render_it(vault):
    (vault / "attachments" / "d.svg").write_text("<svg xmlns='...'></svg>")
    path, refusal = check_target("../attachments/d.svg", vault / "notes")
    assert path is None and "not a PNG" in refusal.reason


def test_a_missing_file_is_a_typo_not_a_violation(vault):
    """Distinguished so the tab path can still write alt text for it."""
    path, refusal = check_target("../attachments/nope.png", vault / "notes")
    assert path is None and refusal.missing


# --------------------------------------------------------------------------
# What is allowed
# --------------------------------------------------------------------------

def test_the_ordinary_vault_attachment_is_allowed(vault):
    path, refusal = check_target("../attachments/logo.png", vault / "notes")
    assert refusal is None
    assert path == (vault / "attachments" / "logo.png")


def test_a_path_through_dot_dot_inside_the_root_is_allowed(vault):
    path, refusal = check_target("sub/../../attachments/logo.png", vault / "notes")
    assert refusal is None and path == (vault / "attachments" / "logo.png")


@pytest.mark.parametrize("data,kind", [(PNG, "png"), (JPEG, "jpeg"), (GIF, "gif"),
                                       (WEBP, "webp"), (BMP, "bmp")])
def test_every_accepted_format_is_recognised(vault, data, kind):
    target = vault / "attachments" / f"pic-{kind}"
    target.write_bytes(data)
    assert sniff_image(data) == kind
    path, refusal = check_target(f"../attachments/pic-{kind}", vault / "notes")
    assert refusal is None and path == target


# --------------------------------------------------------------------------
# The docx path's pre-scan
# --------------------------------------------------------------------------

def test_the_scan_finds_targets_in_markdown_and_html(vault):
    md = ('![a](../attachments/logo.png)\n\n'
          '<img src="../attachments/other.png" width="20">\n\n'
          '![remote](https://example.com/x.png)\n')
    found = local_image_targets(md, vault / "notes")
    assert "../attachments/logo.png" in found
    assert "../attachments/other.png" in found
    assert not any(t.startswith("http") for t in found)


def test_an_image_inside_a_code_fence_is_not_a_target(vault):
    """Documentation about the syntax must not trip the guard."""
    md = "Example:\n\n```\n![k](../../outside/secret.png)\n```\n"
    assert local_image_targets(md, vault / "notes") == []
    assert scan_markdown(md, vault / "notes") == []


def test_enforce_raises_and_names_every_bad_target(vault):
    md = ("![ok](../attachments/logo.png)\n\n"
          "![k](../../outside/secret.png)\n\n"
          "![h](/etc/hostname)\n\n"
          "![s](../.ssh/id_rsa)\n")
    with pytest.raises(RuntimeError) as excinfo:
        enforce_markdown(md, vault / "notes")
    message = str(excinfo.value)
    assert "../../outside/secret.png" in message
    assert "/etc/hostname" in message
    assert "../.ssh/id_rsa" in message
    assert "../attachments/logo.png" not in message


def test_enforce_passes_a_document_with_a_real_vault_image(vault):
    enforce_markdown("![ok](../attachments/logo.png)\n", vault / "notes")


def test_enforce_ignores_a_document_with_no_images(vault):
    enforce_markdown("# Title\n\nJust words.\n", vault / "notes")


def test_the_regex_fallback_still_catches_the_bad_target(vault, monkeypatch):
    """With pandoc unavailable the scan gets more eager, never less."""
    def boom(*_a, **_kw):
        raise RuntimeError("pandoc not found on PATH")
    monkeypatch.setattr("gdoc_sync.mdutils.pandoc_to_ast", boom)
    said: list[str] = []
    bad = scan_markdown("![k](../../outside/secret.png)\n", vault / "notes",
                        say=said.append)
    assert [r.target for r in bad] == ["../../outside/secret.png"]
    assert any("conservative pattern match" in s for s in said)
