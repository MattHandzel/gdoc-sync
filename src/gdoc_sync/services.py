"""Authenticated Google API service builders."""

from __future__ import annotations

from googleapiclient.discovery import build

from .auth import get_credentials


def get_services():
    """Return (drive, docs) service clients sharing one credential."""
    creds = get_credentials()
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs
