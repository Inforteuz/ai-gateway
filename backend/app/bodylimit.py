"""Request body size limit that also works without Content-Length (chunked uploads).

Without it, `await request.body()` buffers a chunked body of any size in memory before the
handler can compare its length with the limit.
"""
from __future__ import annotations

import json


class BodySizeLimit:
    """Pure ASGI middleware: `/v1/...` may send up to `api_limit` bytes, everything else `default_limit`."""

    def __init__(self, app, *, api_limit: int, default_limit: int):
        self.app, self.api_limit, self.default_limit = app, api_limit, default_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = self.api_limit if scope.get("path", "").startswith("/v1/") else self.default_limit
        for name, value in scope.get("headers") or []:
            if name == b"content-length" and value.isdigit() and int(value) > limit:
                return await _reject(send)
        seen = 0
        too_large = False
        replaced = False

        async def limited_receive():
            nonlocal seen, too_large
            if too_large:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Stop reading; the handler sees a disconnect and whatever it answers is replaced by 413
                    too_large = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal replaced
            if too_large:
                if message["type"] == "http.response.start" and not replaced:
                    replaced = True
                    await _reject(send)
                return
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not too_large:
                raise
        if too_large and not replaced:
            await _reject(send)


async def _reject(send) -> None:
    body = json.dumps({"error": {"message": "So'rov hajmi juda katta.", "type": "invalid_request_error",
                                 "code": "request_too_large"}}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})
