"""Split an OpenAI multimodal `content` array into text + local files.

In the OpenAI format a single message can consist of text and files:

    "content": [
        {"type": "text", "text": "Bu rasmda nima bor?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
        {"type": "file", "file": {"filename": "hisobot.pdf", "file_data": "data:..."}}
    ]

Files are attached to the web-chat page via `DOM.setFileInputFiles`, which
needs a PATH on the machine running Chrome. So every attachment is written to
disk by this module and deleted when the request finishes.
"""
from __future__ import annotations

import base64
import binascii
import ipaddress
import mimetypes
import os
import re
import socket
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional

from . import logs
from . import settings as cfg

log = logs.get("attachments")

_DATA_URI = re.compile(r"^data:([\w.+-]+/[\w.+-]+)?(;[\w-]+=[\w-]+)*(;base64)?,", re.IGNORECASE)

# Safe extensions; fallback for unknown types
_EXT_BY_MIME = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
    "application/pdf": ".pdf", "text/plain": ".txt", "text/csv": ".csv",
    "text/markdown": ".md", "application/json": ".json",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}


class AttachmentError(ValueError):
    """Attachment rejected (size, type or address)."""


@dataclass
class Attachment:
    path: str
    mime: str
    name: str
    size: int

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


@dataclass
class ParsedContent:
    """Text and attachments extracted from one message."""
    text: str = ""
    attachments: list[Attachment] = field(default_factory=list)


class AttachmentStore:
    """Set of temporary files that lives for the duration of a request.

    All files are deleted when the `with` block exits, so no junk stays on disk
    and one user's file never ends up with another user.
    """

    def __init__(self) -> None:
        self._dir: Optional[str] = None
        self.items: list[Attachment] = []

    def __enter__(self) -> "AttachmentStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.cleanup()

    @property
    def dir(self) -> str:
        if self._dir is None:
            self._dir = tempfile.mkdtemp(prefix="ai-gateway-attach-", dir=cfg.ATTACHMENT_DIR or None)
            os.chmod(self._dir, 0o700)
        return self._dir

    def cleanup(self) -> None:
        for a in self.items:
            try:
                os.unlink(a.path)
            except OSError:
                pass
        if self._dir:
            try:
                os.rmdir(self._dir)
            except OSError:
                pass
        self.items = []
        self._dir = None

    @property
    def total_size(self) -> int:
        return sum(a.size for a in self.items)

    def add_bytes(self, data: bytes, mime: str, name: str = "") -> Attachment:
        if len(data) > cfg.MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                f"Fayl juda katta: {len(data) // 1024} KB "
                f"(ruxsat {cfg.MAX_ATTACHMENT_BYTES // 1024} KB)")
        if self.total_size + len(data) > cfg.MAX_ATTACHMENTS_TOTAL_BYTES:
            raise AttachmentError("Biriktirmalarning umumiy hajmi chegaradan oshdi")
        if len(self.items) >= cfg.MAX_ATTACHMENTS:
            raise AttachmentError(f"Bitta so'rovda ko'pi bilan {cfg.MAX_ATTACHMENTS} ta fayl")

        mime = (mime or "application/octet-stream").split(";")[0].strip().lower()
        if cfg.ALLOWED_ATTACHMENT_MIMES and mime not in cfg.ALLOWED_ATTACHMENT_MIMES:
            raise AttachmentError(f"Bu fayl turi qabul qilinmaydi: {mime}")

        ext = _EXT_BY_MIME.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        safe = _safe_name(name, ext)
        # Keep the filename as the user gave it: the web chat shows it in the
        # conversation and the model mentions it in its answer, so an internal
        # prefix like "a1b2c3d4-" would leak there. Uniqueness comes from the
        # per-request directory; duplicate names within a request get numbered.
        path = os.path.join(self.dir, safe)
        if os.path.exists(path):
            stem, dot, tail = safe.partition(".")
            path = os.path.join(self.dir, f"{stem}-{len(self.items) + 1}{dot}{tail}")
        with open(path, "wb") as f:
            f.write(data)
        os.chmod(path, 0o600)
        item = Attachment(path=path, mime=mime, name=safe, size=len(data))
        self.items.append(item)
        return item

    def add_data_uri(self, uri: str, name: str = "") -> Attachment:
        head, _, payload = uri.partition(",")
        mime = head[5:].split(";")[0] or "application/octet-stream"
        if ";base64" in head.lower():
            try:
                data = base64.b64decode(payload, validate=False)
            except (binascii.Error, ValueError) as e:
                raise AttachmentError(f"base64 ni o'qib bo'lmadi: {e}") from e
        else:
            data = urllib.parse.unquote_to_bytes(payload)
        return self.add_bytes(data, mime, name)

    def add_url(self, url: str, name: str = "") -> Attachment:
        _assert_public_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "ai-gateway/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=cfg.ATTACHMENT_FETCH_TIMEOUT) as resp:
                declared = resp.headers.get("Content-Length")
                if declared and int(declared) > cfg.MAX_ATTACHMENT_BYTES:
                    raise AttachmentError(f"Fayl juda katta: {int(declared) // 1024} KB")
                # Read 1 byte past the limit to detect oversize
                data = resp.read(cfg.MAX_ATTACHMENT_BYTES + 1)
                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
        except AttachmentError:
            raise
        except Exception as e:
            raise AttachmentError(f"Faylni yuklab bo'lmadi: {type(e).__name__}") from e
        if not mime:
            mime = mimetypes.guess_type(url)[0] or "application/octet-stream"
        return self.add_bytes(data, mime, name or os.path.basename(urllib.parse.urlparse(url).path))


def _safe_name(name: str, fallback_ext: str) -> str:
    """Sanitize a filename (strip path characters)."""
    base = os.path.basename(name or "").strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or f"fayl{fallback_ext}"
    if "." not in base:
        base += fallback_ext
    return base[:64]


def _assert_public_url(url: str) -> None:
    """Check that the URL points to the public internet.

    Internal addresses (loopback, 10.x, 192.168.x, link-local, the metadata
    service 169.254.169.254) are rejected; otherwise a user could make our
    server send requests to internal services (SSRF).
    """
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise AttachmentError("Faqat http/https havolalar qabul qilinadi")
    host = parts.hostname
    if not host:
        raise AttachmentError("Havolada host yo'q")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise AttachmentError(f"Hostni aniqlab bo'lmadi: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise AttachmentError("Ichki tarmoq manzillariga ruxsat yo'q")


def parse_message_content(content: Any, store: AttachmentStore) -> ParsedContent:
    """Split one message's `content` into text + attachments.

    A string `content` is all text. An array is split by OpenAI part types
    (`text`, `image_url`, `file`, `input_file`).
    """
    out = ParsedContent()
    if content is None:
        return out
    if isinstance(content, str):
        out.text = content.strip()
        return out
    if not isinstance(content, Iterable):
        out.text = str(content).strip()
        return out

    texts: list[str] = []
    for part in content:
        if isinstance(part, str):
            texts.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        kind = (part.get("type") or "").lower()

        if kind in ("text", "input_text"):
            texts.append(str(part.get("text") or ""))

        elif kind in ("image_url", "input_image"):
            raw = part.get("image_url") or part.get("image") or {}
            url = raw.get("url") if isinstance(raw, Mapping) else str(raw)
            if not url:
                continue
            out.attachments.append(_ingest(store, url, name="rasm"))

        elif kind in ("file", "input_file", "document"):
            raw = part.get("file") or part.get("input_file") or {}
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("filename") or raw.get("name") or "fayl")
            url = raw.get("file_data") or raw.get("file_url") or raw.get("url")
            if not url:
                continue
            out.attachments.append(_ingest(store, str(url), name=name))

    out.text = "\n".join(t for t in texts if t).strip()
    return out


def _ingest(store: AttachmentStore, url: str, name: str) -> Attachment:
    if _DATA_URI.match(url):
        return store.add_data_uri(url, name)
    return store.add_url(url, name)


def describe(attachments: list[Attachment]) -> str:
    """Short note about the attachments, added to the prompt.

    The model should also learn from the text that files are attached: some
    sites upload the file without telling the model that a file is present.
    """
    if not attachments:
        return ""
    lines = []
    for a in attachments:
        kind = "Rasm" if a.is_image else "Fayl"
        lines.append(f"- {kind}: {a.name} ({a.mime}, {max(1, a.size // 1024)} KB)")
    return "[Biriktirilgan fayllar]:\n" + "\n".join(lines)
