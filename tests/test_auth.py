import json

from gdoc_sync import auth


def _write_secret(tmp_path, project="my-project-123"):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({"installed": {
        "client_id": "x.apps.googleusercontent.com",
        "project_id": project,
        "client_secret": "not-real",
    }}))
    return path


def test_project_id_from_installed_client(tmp_path):
    assert auth.project_id(_write_secret(tmp_path)) == "my-project-123"


def test_project_id_missing_file(tmp_path):
    assert auth.project_id(tmp_path / "nope.json") is None


def test_consent_url_preselects_project(tmp_path):
    url = auth.consent_screen_url(_write_secret(tmp_path))
    assert url == "https://console.cloud.google.com/apis/credentials/consent?project=my-project-123"


def test_consent_url_without_project(tmp_path):
    assert auth.consent_screen_url(tmp_path / "nope.json") == auth.CONSENT_URL


def test_non_interactive_never_opens_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "token_path", lambda: tmp_path / "token.json")
    monkeypatch.setattr(auth, "client_secret_path", lambda: _write_secret(tmp_path))
    import pytest

    with pytest.raises(RuntimeError, match="gdoc-sync auth"):
        auth.get_credentials(interactive=False)
