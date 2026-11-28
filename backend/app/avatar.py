"""Profile photo: upload, read and remove the signed-in user's own picture.

The browser crops and resizes the picture to a small square before sending it, so the
server keeps a short data URL's bytes. Only PNG, JPEG and WebP are accepted, and the
declared type must match the file's magic bytes (no SVG, no sniffing on the way out).
"""
from __future__ import annotations

import base64
import binascii
import re

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field

from .common import ApiError, csrf_guard, get_db, require_user
from .db import Database, now

router = APIRouter(prefix="/portal/account", dependencies=[Depends(csrf_guard)], tags=["account"])

MAX_BYTES = 256 * 1024
_DATA_URL = re.compile(r"data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/]+={0,2})")


class AvatarIn(BaseModel):
    # base64 of MAX_BYTES plus the prefix; anything longer is refused before decoding
    image: str = Field(max_length=MAX_BYTES * 4 // 3 + 64)


def sniff(data: bytes) -> str | None:
    """The image type from the first bytes, or None when it is not PNG/JPEG/WebP."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def decode(image: str) -> tuple[str, bytes]:
    m = _DATA_URL.fullmatch(image)
    if not m:
        raise ApiError(400, "invalid_image", "Faqat PNG, JPEG yoki WebP rasm.", en="Only a PNG, JPEG or WebP image.")
    try:
        data = base64.b64decode(m.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise ApiError(400, "invalid_image", "Rasm buzilgan.", en="The image is damaged.") from None
    if len(data) > MAX_BYTES:
        raise ApiError(400, "image_too_large", "Rasm 256 KB dan oshmasligi kerak.", en="The image must not exceed 256 KB.")
    if sniff(data) != m.group(1):
        raise ApiError(400, "invalid_image", "Faqat PNG, JPEG yoki WebP rasm.", en="Only a PNG, JPEG or WebP image.")
    return m.group(1), data


def avatar_version(db: Database, user_id: int) -> int | None:
    """When the user's photo last changed (cache-busting for the image URL), or None without one."""
    return db.scalar("SELECT updated_at FROM user_avatars WHERE user_id=?", (user_id,), default=None)


@router.put("/avatar")
def put_avatar(body: AvatarIn, user=Depends(require_user), db: Database = Depends(get_db)):
    mime, data = decode(body.image)
    ts = now()
    with db.tx() as c:
        c.execute("""INSERT INTO user_avatars(user_id, mime, data, updated_at) VALUES (?,?,?,?)
                     ON CONFLICT (user_id) DO UPDATE SET mime=excluded.mime, data=excluded.data,
                       updated_at=GREATEST(excluded.updated_at, user_avatars.updated_at + 1)""",
                  (user["id"], mime, data, ts))
    return {"avatar": avatar_version(db, user["id"])}


@router.delete("/avatar")
def delete_avatar(user=Depends(require_user), db: Database = Depends(get_db)):
    with db.tx() as c:
        c.execute("DELETE FROM user_avatars WHERE user_id=?", (user["id"],))
    return {"avatar": None}


@router.get("/avatar")
def get_avatar(request: Request, user=Depends(require_user), db: Database = Depends(get_db)):
    row = db.one("SELECT mime, data, updated_at FROM user_avatars WHERE user_id=?", (user["id"],))
    if row is None:
        raise ApiError(404, "no_avatar", "Profil rasmi yo'q.", en="No profile photo.")
    etag = f'"{row["updated_at"]}"'
    headers = {"Cache-Control": "private, max-age=0, must-revalidate", "ETag": etag,
               "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(bytes(row["data"]), media_type=row["mime"], headers=headers)
