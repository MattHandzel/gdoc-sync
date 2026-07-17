"""Google Docs/Drive OAuth2 authentication.

Bring your own OAuth client (see docs/oauth-setup.md):

* Client secret: ``$GDOC_SYNC_CLIENT_SECRET`` or ``~/.config/gdoc-sync/client_secret.json``
  (``gdoc-sync auth --client <downloaded.json>`` installs it there for you).
* Token cache: ``~/.config/gdoc-sync/token.json`` — refreshes automatically; the
  client secret file is only needed again for a full re-consent.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .config import config_dir

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
]

OAUTH_DOC_URL = "https://github.com/MattHandzel/gdoc-sync/blob/main/docs/oauth-setup.md"
CREATE_CLIENT_URL = "https://console.cloud.google.com/apis/credentials/oauthclient"
CONSENT_URL = "https://console.cloud.google.com/apis/credentials/consent"
ENABLE_DOCS_URL = "https://console.cloud.google.com/apis/library/docs.googleapis.com"
ENABLE_DRIVE_URL = "https://console.cloud.google.com/apis/library/drive.googleapis.com"


def token_path() -> Path:
    return config_dir() / "token.json"


def client_secret_path() -> Path:
    env = os.environ.get("GDOC_SYNC_CLIENT_SECRET")
    if env:
        return Path(env).expanduser()
    return config_dir() / "client_secret.json"


def project_id(cs_path: Path | None = None) -> str | None:
    """The Google Cloud project the OAuth client belongs to (not a secret)."""
    import json

    path = cs_path or client_secret_path()
    try:
        data = json.loads(path.read_text())
        for key in ("installed", "web"):
            if key in data:
                return data[key].get("project_id")
    except (OSError, ValueError):
        pass
    return None


def consent_screen_url(cs_path: Path | None = None) -> str:
    """Direct link to the consent screen, preselecting the project when known."""
    pid = project_id(cs_path)
    return f"{CONSENT_URL}?project={pid}" if pid else CONSENT_URL


def install_client_secret(source: Path) -> Path:
    """Copy a downloaded OAuth client JSON into the config dir."""
    dest = config_dir() / "client_secret.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, dest)
    dest.chmod(0o600)
    return dest


def get_credentials() -> Credentials:
    """Return valid credentials, refreshing or running the OAuth flow as needed."""
    creds = None
    tp = token_path()

    if tp.exists():
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            tp.write_text(creds.to_json())
            return creds
        except RefreshError:
            # Dead refresh token (e.g. OAuth app in "Testing" mode expires them
            # after ~7 days) — fall through to a fresh browser consent instead
            # of crashing.
            print("Stored token could not be refreshed; re-running OAuth flow...")
            print(
                "If this happens every week, your OAuth app is in Testing mode\n"
                "(refresh tokens expire after 7 days). Publish it to Production:\n"
                f"  {consent_screen_url()}"
            )

    cs = client_secret_path()
    if not cs.exists():
        raise FileNotFoundError(
            f"OAuth client secret not found at {cs}.\n"
            f"Create one (takes ~2 minutes):\n"
            f"  1. Create an OAuth client (type: Desktop app) and download its JSON:\n"
            f"     {CREATE_CLIENT_URL}\n"
            f"  2. Enable the two APIs for that project:\n"
            f"     {ENABLE_DOCS_URL}\n"
            f"     {ENABLE_DRIVE_URL}\n"
            f"  3. Install it:  gdoc-sync auth --client <downloaded.json>\n"
            f"Full walkthrough: {OAUTH_DOC_URL}"
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(cs), SCOPES)
    creds = flow.run_local_server(port=0)

    tp.parent.mkdir(parents=True, exist_ok=True)
    tp.write_text(creds.to_json())
    tp.chmod(0o600)
    return creds


def run_auth(client: str | None = None, force: bool = False) -> None:
    """The ``gdoc-sync auth`` command."""
    if client:
        dest = install_client_secret(Path(client).expanduser())
        print(f"Installed client secret at {dest}")
    if force and token_path().exists():
        token_path().unlink()
        print("Removed cached token; re-running OAuth flow.")
    get_credentials()
    print(f"Authenticated. Token cached at {token_path()}")
    pid = project_id()
    if pid:
        print(
            f"OAuth client project: {pid}\n"
            f"If the app is in Testing mode this token dies in 7 days; publish it\n"
            f"to Production once to make it permanent: {consent_screen_url()}"
        )
