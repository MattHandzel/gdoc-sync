"""Authenticated Google API service builders and request execution."""

from __future__ import annotations

from googleapiclient.discovery import build

from .auth import get_credentials

# googleapiclient retries with exponential backoff on 429, rate-limit 403s,
# and 5xx when num_retries is set; it does NOT retry by default.
NUM_RETRIES = 4


def get_services(interactive: bool = True):
    """Return (drive, docs) service clients sharing one credential."""
    creds = get_credentials(interactive=interactive)
    drive = build("drive", "v3", credentials=creds)
    docs = build("docs", "v1", credentials=creds)
    return drive, docs


def execute(request, retries: int = NUM_RETRIES):
    """Execute an API request with backoff on transient errors."""
    return request.execute(num_retries=retries)
