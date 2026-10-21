"""VNC in the admin panel: browser (noVNC) <-> backend <-> x11vnc (127.0.0.1 only).

Why the backend and not websockify: x11vnc is password-protected (the server
also hosts other projects and users, so a passwordless localhost port is not
enough). Through websockify the password would have to be given to noVNC,
i.e. to the browser. Here the backend performs the RFB handshake with x11vnc
ITSELF (VNC Authentication, password in `SF_VNC_PASSWORD`, server-side only)
and offers the browser the "None" security type. So:

  * the VNC password is never sent to the browser;
  * access requires an admin session plus a one-time, 60-second ticket;
  * the x11vnc port is not exposed; only the backend connects to it.

RFB protocol: RFC 6143. After the handshake bytes are relayed unchanged in
both directions (ClientInit/ServerInit included).
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from dataclasses import dataclass

# --- DES (encryption only, single block) ----------------------------------------
# VNC Authentication requires DES; the `cryptography` package is not in the prod
# venv, so this is a small standard implementation for the 16-byte challenge.
# Verified against the FIPS 46 example (in tests): 133457799BBCDFF1 / 0123456789ABCDEF.

_PC1 = (57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35, 27, 19, 11, 3, 60, 52, 44, 36,
        63, 55, 47, 39, 31, 23, 15, 7, 62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 28, 20, 12, 4)
_PC2 = (14, 17, 11, 24, 1, 5, 3, 28, 15, 6, 21, 10, 23, 19, 12, 4, 26, 8, 16, 7, 27, 20, 13, 2,
        41, 52, 31, 37, 47, 55, 30, 40, 51, 45, 33, 48, 44, 49, 39, 56, 34, 53, 46, 42, 50, 36, 29, 32)
_SHIFTS = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
_IP = (58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6,
       64, 56, 48, 40, 32, 24, 16, 8, 57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3,
       61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
_FP = (40, 8, 48, 16, 56, 24, 64, 32, 39, 7, 47, 15, 55, 23, 63, 31, 38, 6, 46, 14, 54, 22, 62, 30,
       37, 5, 45, 13, 53, 21, 61, 29, 36, 4, 44, 12, 52, 20, 60, 28, 35, 3, 43, 11, 51, 19, 59, 27,
       34, 2, 42, 10, 50, 18, 58, 26, 33, 1, 41, 9, 49, 17, 57, 25)
_E = (32, 1, 2, 3, 4, 5, 4, 5, 6, 7, 8, 9, 8, 9, 10, 11, 12, 13, 12, 13, 14, 15, 16, 17,
      16, 17, 18, 19, 20, 21, 20, 21, 22, 23, 24, 25, 24, 25, 26, 27, 28, 29, 28, 29, 30, 31, 32, 1)
_P = (16, 7, 20, 21, 29, 12, 28, 17, 1, 15, 23, 26, 5, 18, 31, 10, 2, 8, 24, 14, 32, 27, 3, 9, 19, 13, 30, 6, 22, 11, 4, 25)
_S = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8,
     4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 14, 12, 0, 1, 10, 6, 9, 11, 5,
     0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1,
     13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9,
     10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 1, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6,
     4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8,
     9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6,
     1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2,
     7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)


def _perm(val: int, table: tuple, width: int) -> int:
    out = 0
    for pos in table:
        out = (out << 1) | ((val >> (width - pos)) & 1)
    return out


def _subkeys(key: bytes) -> list[int]:
    k = _perm(int.from_bytes(key, "big"), _PC1, 64)
    c, d = k >> 28, k & 0xFFFFFFF
    keys = []
    for s in _SHIFTS:
        c = ((c << s) | (c >> (28 - s))) & 0xFFFFFFF
        d = ((d << s) | (d >> (28 - s))) & 0xFFFFFFF
        keys.append(_perm((c << 28) | d, _PC2, 56))
    return keys


def des_encrypt_block(key: bytes, block: bytes) -> bytes:
    b = _perm(int.from_bytes(block, "big"), _IP, 64)
    left, right = b >> 32, b & 0xFFFFFFFF
    for k in _subkeys(key):
        x = _perm(right, _E, 32) ^ k
        f = 0
        for i in range(8):
            six = (x >> (42 - 6 * i)) & 0x3F
            f = (f << 4) | _S[i][(((six >> 4) & 2) | (six & 1)) * 16 + ((six >> 1) & 0xF)]
        left, right = right, left ^ _perm(f, _P, 32)
    return _perm((right << 32) | left, _FP, 64).to_bytes(8, "big")


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """RFC 6143 7.2.2: the first 8 bytes of the password, each byte bit-reversed, form the DES key."""
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in raw)
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:16])


# --- one-time tickets ---------------------------------------------------------

TICKET_TTL = 60


@dataclass
class Ticket:
    user_id: int
    instance: int
    expires: float


class TicketStore:
    """In memory (the backend is a single process). Stores the ticket's hash, not the ticket."""

    def __init__(self) -> None:
        self._items: dict[str, Ticket] = {}

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, user_id: int, instance: int) -> str:
        self._gc()
        token = secrets.token_urlsafe(32)
        self._items[self._h(token)] = Ticket(user_id, instance, time.time() + TICKET_TTL)
        return token

    def consume(self, token: str, user_id: int) -> Ticket | None:
        """Single use: after a lookup, found or not, the ticket no longer works."""
        self._gc()
        t = self._items.pop(self._h(token or ""), None)
        if t is None or t.user_id != user_id or t.expires < time.time():
            return None
        return t

    def _gc(self) -> None:
        now = time.time()
        for k in [k for k, t in self._items.items() if t.expires < now]:
            self._items.pop(k, None)


# --- RFB handshake ------------------------------------------------------------

class VncError(Exception):
    pass


async def connect_upstream(host: str, port: int, password: str, timeout: float = 10.0):
    """Performs the handshake with x11vnc. Returns (reader, writer), ready for ClientInit."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    try:
        async def read(n: int) -> bytes:
            return await asyncio.wait_for(reader.readexactly(n), timeout)

        version = await read(12)
        if not version.startswith(b"RFB 003.") or version[8:11] < b"007":
            raise VncError("VNC server RFB 3.7+ emas")
        writer.write(b"RFB 003.008\n")
        await writer.drain()
        n = (await read(1))[0]
        if n == 0:
            raise VncError("VNC server ulanishni rad etdi")
        types = await read(n)
        if password and 2 in types:
            writer.write(b"\x02")
            await writer.drain()
            writer.write(vnc_auth_response(password, await read(16)))
        elif not password and 1 in types:
            writer.write(b"\x01")
        else:
            raise VncError("mos xavfsizlik turi yo'q")
        await writer.drain()
        if int.from_bytes(await read(4), "big") != 0:
            raise VncError("VNC autentifikatsiyasi o'tmadi")
        return reader, writer
    except BaseException:
        writer.close()
        raise


class WsReader:
    """Reads exactly N bytes from WebSocket messages (one message may carry several RFB chunks)."""

    def __init__(self, ws) -> None:
        self.ws, self.buf = ws, b""

    async def readexactly(self, n: int) -> bytes:
        while len(self.buf) < n:
            msg = await self.ws.receive()
            if msg["type"] == "websocket.disconnect":
                raise VncError("brauzer uzildi")
            self.buf += msg.get("bytes") or (msg.get("text") or "").encode()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


async def client_handshake(ws, timeout: float = 15.0) -> bytes:
    """Performs the server-side handshake with the browser (noVNC): "None" security type only.

    Returns the bytes the browser already sent after the handshake (ClientInit).
    """
    r = WsReader(ws)
    await ws.send_bytes(b"RFB 003.008\n")
    version = await asyncio.wait_for(r.readexactly(12), timeout)
    if version != b"RFB 003.008\n":
        raise VncError("brauzer RFB 3.8 emas")
    await ws.send_bytes(b"\x01\x01")                # 1 type: None
    if await asyncio.wait_for(r.readexactly(1), timeout) != b"\x01":
        raise VncError("brauzer None turini tanlamadi")
    await ws.send_bytes(b"\x00\x00\x00\x00")        # SecurityResult: OK
    return r.buf


async def pipe(ws, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, leftover: bytes,
               max_seconds: float) -> None:
    """Relays bytes in both directions; when either side closes or time runs out, both are closed."""
    if leftover:
        writer.write(leftover)
        await writer.drain()

    async def up_to_ws():
        while True:
            data = await reader.read(65536)
            if not data:
                return
            await ws.send_bytes(data)

    async def ws_to_up():
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            data = msg.get("bytes") or (msg.get("text") or "").encode()
            if data:
                writer.write(data)
                await writer.drain()

    tasks = [asyncio.create_task(up_to_ws()), asyncio.create_task(ws_to_up())]
    try:
        await asyncio.wait(tasks, timeout=max_seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
