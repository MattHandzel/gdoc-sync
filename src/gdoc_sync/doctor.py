"""Setup diagnostics: check every dependency of a working gdoc-sync install."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .auth import CREATE_CLIENT_URL, client_secret_path, consent_screen_url, project_id, token_path
from .config import all_mappings, config_path, get_clipboard_command, load_config, state_path
from .mdutils import _clipboard_candidates

OK, BAD, SKIP = "✓", "✗", "–"


def _check_pandoc() -> tuple[str, str]:
    path = shutil.which("pandoc")
    if not path:
        return BAD, "pandoc not on PATH — install from https://pandoc.org/installing.html"
    try:
        version = subprocess.run(["pandoc", "--version"], capture_output=True,
                                 text=True, timeout=10).stdout.splitlines()[0]
        return OK, f"{version} ({path})"
    except Exception as e:
        return BAD, f"pandoc found but not runnable: {e}"


def _check_token() -> tuple[str, str]:
    tp = token_path()
    if not tp.exists():
        return SKIP, f"no cached token at {tp} — run: gdoc-sync auth"
    try:
        from google.oauth2.credentials import Credentials

        from .auth import SCOPES
        creds = Credentials.from_authorized_user_file(str(tp), SCOPES)
        if creds.valid:
            return OK, f"valid token at {tp}"
        if creds.expired and creds.refresh_token:
            return OK, f"expired token with refresh token at {tp} (auto-refreshes)"
        return BAD, f"token at {tp} is unusable — run: gdoc-sync auth --force"
    except Exception as e:
        return BAD, f"token unreadable: {e}"


def _check_api() -> tuple[str, str]:
    try:
        from .services import NUM_RETRIES, get_services
        drive, _ = get_services(interactive=False)
        about = drive.about().get(fields="user").execute(num_retries=NUM_RETRIES)
        email = about.get("user", {}).get("emailAddress", "?")
        return OK, f"authenticated to Google as {email}"
    except Exception as e:
        return BAD, f"API call failed: {e}"


def _check_state() -> tuple[str, str]:
    """The state file must be readable now and writable later.

    A ``state_file:`` copied from another machine (``/home/me/...`` on a Mac,
    where ``/home`` is a read-only automount) parses fine and reports zero
    links, then the first ``create`` dies trying to make the directory. Walk
    up to the nearest existing ancestor and check it can take a write.
    """
    p = state_path()
    ancestor = p if p.exists() else p.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if p.exists() and not os.access(p, os.W_OK):
        return BAD, f"{p} is not writable"
    if not p.exists() and (not ancestor.is_dir() or not os.access(ancestor, os.W_OK)):
        return BAD, (f"{p}: cannot be created (nothing above it is writable; nearest existing "
                     f"parent is {ancestor}) — fix `state_file:` in the config")
    mappings = all_mappings()
    missing = sum(1 for path in mappings if not Path(path).exists())
    detail = f"{p} ({len(mappings)} linked file(s)"
    if missing:
        detail += f", {missing} not present on this machine"
    return OK, detail + ")"


def _check_clipboard() -> tuple[str, str]:
    """Report the command `create` will actually copy with on this platform."""
    explicit = get_clipboard_command()
    if explicit:
        tool = explicit.split()[0]
        if shutil.which(tool):
            return OK, f"{explicit} (clipboard_command from config)"
        return BAD, f"clipboard_command `{explicit}` not on PATH"
    for cmd, name in _clipboard_candidates():
        if shutil.which(cmd[0]):
            return OK, name
    return SKIP, "no clipboard tool found — --no-copy still works"


def doctor(online: bool = True) -> int:
    """Print a ✓/✗ report; return a shell exit code (0 = healthy)."""
    rows: list[tuple[str, str, str]] = []

    v = sys.version_info
    rows.append(("python", OK if v >= (3, 10) else BAD, f"{v.major}.{v.minor}.{v.micro}"))
    rows.append(("pandoc", *_check_pandoc()))

    cp = config_path()
    if cp.exists():
        try:
            load_config()
            rows.append(("config", OK, str(cp)))
        except Exception as e:
            rows.append(("config", BAD, f"{cp} does not parse: {e}"))
    else:
        rows.append(("config", SKIP, f"{cp} (not created — defaults in effect)"))

    try:
        rows.append(("state", *_check_state()))
    except Exception as e:
        rows.append(("state", BAD, f"{state_path()}: {e}"))

    cs = client_secret_path()
    if cs.exists():
        pid = project_id(cs)
        detail = f"{cs} (project: {pid})" if pid else str(cs)
        rows.append(("oauth client", OK, detail))
        rows.append(("consent screen", SKIP,
                     f"if tokens die weekly, publish to Production: {consent_screen_url(cs)}"))
    else:
        rows.append(("oauth client", SKIP,
                     f"none at {cs} — create one at {CREATE_CLIENT_URL}"))
    rows.append(("token", *_check_token()))

    token_usable = rows[-1][1] == OK
    if online and token_usable:
        rows.append(("api", *_check_api()))
    elif online:
        rows.append(("api", SKIP, "skipped (no usable token)"))

    rows.append(("clipboard", *_check_clipboard()))

    for name, mark, detail in rows:
        print(f" {mark} {name:<14} {detail}")

    hard_failures = [r for r in rows if r[1] == BAD]
    auth_possible = cs.exists() or token_path().exists()
    if not auth_possible:
        print("\nNo OAuth client secret AND no cached token — gdoc-sync cannot reach "
              "Google yet. Follow docs/oauth-setup.md, then run: gdoc-sync auth")
        return 1
    if hard_failures:
        print(f"\n{len(hard_failures)} problem(s) found.")
        return 1
    print("\nAll good.")
    return 0
