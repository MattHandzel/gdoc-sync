#!/usr/bin/env python3
"""Trash a Google Doc using the gdoc-sync CLI's cached token. Stdlib only.

Usage: trash_doc.py <doc_id>

Refreshes the cached OAuth token (~/.config/gdoc-sync/token.json) and PATCHes
drive/v3/files/<id> {"trashed": true}. Never prints token material.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path


def token_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gdoc-sync" / "token.json"


def access_token() -> str:
    tok = json.loads(token_file().read_text())
    body = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        tok.get("token_uri", "https://oauth2.googleapis.com/token"), data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)["access_token"]


def main() -> int:
    doc_id = sys.argv[1]
    req = urllib.request.Request(
        f"https://www.googleapis.com/drive/v3/files/{doc_id}",
        data=json.dumps({"trashed": True}).encode(),
        method="PATCH",
        headers={
            "Authorization": f"Bearer {access_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        json.load(resp)
    print(f"Trashed {doc_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
