"""Change sharing on an already-linked/created Google Doc."""

from __future__ import annotations

from googleapiclient.errors import HttpError

from .create import SHARE_ROLES, parse_share_with
from .services import NUM_RETRIES, get_services


def share(doc_id: str, *, with_: list[str] | None = None,
          anyone: str | None = None, private: bool = False) -> None:
    """Grant per-email permissions, set link sharing, or make private."""
    drive_service, _ = get_services()

    if private:
        perms = drive_service.permissions().list(
            fileId=doc_id, fields="permissions(id,type,role)"
        ).execute(num_retries=NUM_RETRIES).get("permissions", [])
        removed = 0
        for perm in perms:
            if perm.get("type") == "anyone":
                drive_service.permissions().delete(
                    fileId=doc_id, permissionId=perm["id"]
                ).execute(num_retries=NUM_RETRIES)
                removed += 1
        print("Removed link sharing" if removed else "No link sharing to remove")

    if anyone:
        role = SHARE_ROLES.get(anyone)
        if not role:
            raise RuntimeError(f"--anyone must be view|comment|edit, got {anyone!r}")
        drive_service.permissions().create(
            fileId=doc_id, body={"type": "anyone", "role": role}, fields="id",
        ).execute(num_retries=NUM_RETRIES)
        print(f"Shared: anyone with link can {role}")

    for entry in with_ or []:
        try:
            email, role = parse_share_with(entry)
            drive_service.permissions().create(
                fileId=doc_id,
                body={"type": "user", "role": role, "emailAddress": email},
                fields="id",
            ).execute(num_retries=NUM_RETRIES)
            print(f"Shared with {email} ({role})")
        except (ValueError, HttpError) as e:
            print(f"Warning: could not share with {entry}: {e}")

    print(f"URL: https://docs.google.com/document/d/{doc_id}/edit")
