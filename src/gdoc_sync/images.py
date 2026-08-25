"""Image identity across the markdown ↔ Google Doc round trip.

The doc side and the markdown side name images differently. Locally an image
is ``![diagram](attachments/foo.png)`` — a path the author chose. In the doc
the same image is an inline object with an opaque id and a short-lived
``contentUri``, and every push (a docx re-import) mints brand-new ids for
every object in the document. Rendering the doc therefore cannot know, from
the API alone, that an object *is* ``attachments/foo.png``.

Before this module the renderer downloaded every object into
``<stem>-assets/img-001.png`` and emitted that path. The local file and the
rendered remote then disagreed on every image line forever: baselines
recorded phantom "remote edits", merges duplicated image references, and a
doc nobody touched churned on every watch tick.

The fix is the same shape as :mod:`.mathmd` uses for equations — restore
identity from the local file — but keyed on content rather than position:

1. A per-file index (in the sync data dir) remembers ``object id → (sha256,
   markdown token)``. An indexed object renders as its remembered token with
   no download at all. Ids survive between pushes, so steady-state watch
   ticks are free.
2. An unknown id is downloaded once and hashed. If the bytes match an image
   the local file already references, the object renders as the *local*
   token — alt text, path spelling and all — and is indexed. This is what
   makes render(push(md)) agree with md: after a push recreates every
   object, each one hashes back to the file it came from.
3. Genuinely new bytes are a new image someone added in the doc. It is
   written to the file's asset directory (``image_dir:`` in the config, or
   ``<stem>-assets/`` beside the file) under a content-hash name — the same
   doc state always renders the same text — and indexed.

Deletion needs no code here: a deleted object simply stops appearing in the
render, and the ordinary merge removes its line from the markdown.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .syncstate import _slug, sync_dir

_IMAGE_EXTS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}

# ![alt](path "optional title") — path stops at whitespace or the closing
# paren, which is how pandoc reads it; <angle-bracket> paths are rare enough
# to leave to the "new image" path harmlessly.
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


def _index_path(md_path: Path) -> Path:
    d = sync_dir() / "images"
    return d / f"{_slug(md_path)}.json"


def _load_index(md_path: Path) -> dict:
    try:
        return json.loads(_index_path(md_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_index(md_path: Path, index: dict) -> None:
    p = _index_path(md_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")


class ImageResolver:
    """Resolve a doc's inline objects to stable markdown image tokens.

    Callable with ``(object_id, content_uri)``; returns the full markdown
    token (``![alt](path)``) or ``None`` when the image cannot be fetched.
    Pass one instance to a whole render so tabs share the index and the
    dedup cache.
    """

    def __init__(self, md_path: Path, local_text: str | None = None,
                 image_dir: Path | None = None, session=None):
        self.md_path = Path(md_path)
        self._local_text = local_text
        self._image_dir = image_dir
        self._session = session
        self._index = _load_index(self.md_path)
        self._seen: dict[str, str] = {}
        self._candidates: dict[str, str] | None = None  # sha256 -> md token
        self.new_images = 0
        self._dirty = False

    # -- local side -------------------------------------------------------

    def _local_candidates(self) -> dict[str, str]:
        """Hash every image the local file references, once, lazily."""
        if self._candidates is not None:
            return self._candidates
        text = self._local_text
        if text is None:
            try:
                text = self.md_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
        cands: dict[str, str] = {}
        for m in _MD_IMAGE.finditer(text):
            path_str = m.group(1)
            if re.match(r"^[a-z]+://", path_str):  # remote URL: not ours to hash
                continue
            p = Path(path_str)
            if not p.is_absolute():
                p = self.md_path.parent / p
            try:
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
            # First reference wins so a twice-used image maps consistently.
            cands.setdefault(digest, m.group(0))
        self._candidates = cands
        return cands

    # -- doc side ---------------------------------------------------------

    def _fetch(self, content_uri: str) -> tuple[bytes, str] | None:
        if self._session is None:
            from google.auth.transport.requests import AuthorizedSession

            from .auth import get_credentials
            self._session = AuthorizedSession(get_credentials())
        try:
            resp = self._session.get(content_uri, timeout=30)
        except Exception:
            return None
        if getattr(resp, "status_code", 0) != 200:
            return None
        ctype = resp.headers.get("content-type", "").split(";")[0].strip()
        return resp.content, _IMAGE_EXTS.get(ctype, ".png")

    def _assets_dir(self) -> Path:
        if self._image_dir is not None:
            return self._image_dir
        return self.md_path.parent / f"{self.md_path.stem}-assets"

    def _store_new(self, data: bytes, digest: str, ext: str) -> str:
        d = self._assets_dir()
        d.mkdir(parents=True, exist_ok=True)
        fname = f"img-{digest[:10]}{ext}"
        target = d / fname
        if not target.exists():
            target.write_bytes(data)
        try:
            import os
            rel = os.path.relpath(target, self.md_path.parent)
        except ValueError:  # different drive (Windows) — absolute is honest
            rel = str(target)
        self.new_images += 1
        return f"![image]({rel})"

    # -- the resolver -----------------------------------------------------

    def __call__(self, object_id: str, content_uri: str) -> str | None:
        if object_id in self._seen:
            return self._seen[object_id]

        entry = self._index.get(object_id)
        if entry:
            token = entry["token"]
            self._seen[object_id] = token
            return token

        fetched = self._fetch(content_uri)
        if fetched is None:
            return None
        data, ext = fetched
        digest = hashlib.sha256(data).hexdigest()

        token = self._local_candidates().get(digest)
        if token is None:
            # Someone may have re-added bytes we already track under a
            # different (dead) object id — reuse that token before minting
            # a new file.
            for e in self._index.values():
                if e.get("sha256") == digest:
                    token = e["token"]
                    break
        if token is None:
            token = self._store_new(data, digest, ext)

        self._index[object_id] = {"sha256": digest, "token": token}
        self._dirty = True
        self._seen[object_id] = token
        return token

    def finish(self) -> None:
        """Persist the index, pruning ids the doc no longer contains."""
        live = {oid: e for oid, e in self._index.items() if oid in self._seen}
        if self._dirty or len(live) != len(self._index):
            _save_index(self.md_path, live)

    def count(self) -> int:
        return self.new_images
