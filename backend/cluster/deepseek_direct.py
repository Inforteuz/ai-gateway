"""DeepSeek Direct API Client.

Direct, browserless HTTP integration with DeepSeek web API (chat.deepseek.com).
Supports:
  - DeepSeek-V3 (deepseek-chat, thinking_enabled=False)
  - DeepSeek-R1 (deepseek-reasoner, thinking_enabled=True)
  - Proof-of-Work (PoW) challenge solving via deepseek_pow (WebAssembly)
  - Chat session lifecycle management (create, delete)
  - File and image upload via /api/v0/file/upload_file and /fetch_files
  - Real-time incremental SSE streaming with thinking and content fragments
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import time
from typing import Any, Dict, Generator, List, Optional

from curl_cffi import requests
from curl_cffi.curl import CurlMime

from . import logs
from . import settings as cfg

log = logs.get("deepseek_direct")


class DeepSeekAPIError(RuntimeError):
    """DeepSeek API error."""
    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class DeepSeekDirectClient:
    """Browserless HTTP client for DeepSeek web chat API."""

    BASE_URL = "https://chat.deepseek.com/api/v0"

    def __init__(self, token: Optional[str] = None):
        self.token = (token or cfg.DEEPSEEK_USER_TOKEN or os.getenv("DEEPSEEK_USER_TOKEN", "")).strip()
        if not self.token:
            raise ValueError("DEEPSEEK_USER_TOKEN ko'rsatilmadi! Muhit o'zgaruvchisida yoki sozlamalarda bering.")

        self.session = requests.Session(impersonate="chrome120")
        self.headers = {
            "authorization": f"Bearer {self.token}",
            "origin": "https://chat.deepseek.com",
            "referer": "https://chat.deepseek.com/",
            "user-agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
        }
        self.current_session_id: Optional[str] = None

    def get_user_info(self) -> Dict[str, Any]:
        """Check authenticated user info."""
        url = f"{self.BASE_URL}/users/current"
        resp = self.session.get(url, headers={**self.headers, "content-type": "application/json"})
        data = resp.json()
        if data.get("code") != 0:
            raise DeepSeekAPIError(f"Avtorizatsiya xatosi: {data.get('msg')}", code=data.get("code"))
        return data.get("data", {}).get("biz_data", {})

    def create_chat_session(self) -> str:
        """Create a fresh chat session."""
        url = f"{self.BASE_URL}/chat_session/create"
        resp = self.session.post(
            url,
            headers={**self.headers, "content-type": "application/json"},
            json={"character_id": None},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise DeepSeekAPIError(f"Chat seansini yaratishda xatolik: {data.get('msg')}", code=data.get("code"))
        sess_id = data["data"]["biz_data"]["chat_session"]["id"]
        self.current_session_id = sess_id
        return sess_id

    def delete_chat_session(self, session_id: Optional[str] = None) -> bool:
        """Delete a chat session by id."""
        target_id = session_id or self.current_session_id
        if not target_id:
            return False
        url = f"{self.BASE_URL}/chat_session/delete"
        resp = self.session.post(
            url,
            headers={**self.headers, "content-type": "application/json"},
            json={"chat_session_id": target_id},
        )
        if target_id == self.current_session_id:
            self.current_session_id = None
        data = resp.json()
        return data.get("code") == 0

    def solve_pow(self, target_path: str = "/api/v0/chat/completion") -> str:
        """Solve Proof-of-Work (PoW) challenge and return base64 encoded header."""
        from deepseek_pow import Challenge, registry

        url = f"{self.BASE_URL}/chat/create_pow_challenge"
        resp = self.session.post(
            url,
            headers={**self.headers, "content-type": "application/json"},
            json={"target_path": target_path},
        )
        data = resp.json()
        if data.get("code") != 0:
            raise DeepSeekAPIError(f"PoW challenge olishda xatolik: {data.get('msg')}", code=data.get("code"))

        pow_data = data["data"]["biz_data"]["challenge"]
        ch = Challenge(
            algorithm=pow_data["algorithm"],
            challenge=pow_data["challenge"],
            salt=pow_data["salt"],
            difficulty=pow_data["difficulty"],
            expire_at=pow_data["expire_at"],
            signature=pow_data["signature"],
            target_path=target_path,
        )
        solver = registry.get(ch.algorithm)
        sol = solver.solve(ch)

        payload = {
            "algorithm": ch.algorithm,
            "challenge": ch.challenge,
            "salt": ch.salt,
            "answer": int(sol.answer),
            "signature": ch.signature,
            "target_path": target_path,
        }
        return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")

    def upload_file(self, file_path: str, timeout: float = 30.0) -> str:
        """Upload a file or image to DeepSeek and wait for it to be ready.

        Returns:
            The uploaded file's ref_file_id.
        """
        if not os.path.isfile(file_path):
            raise FileNotFoundError(f"Biriktirilgan fayl topilmadi: {file_path}")

        file_name = os.path.basename(file_path)
        mime_type, _ = mimetypes.guess_type(file_name)
        mime_type = mime_type or "application/octet-stream"

        pow_header = self.solve_pow("/api/v0/file/upload_file")
        upload_headers = {
            **self.headers,
            "x-ds-pow-response": pow_header,
        }

        mp = CurlMime()
        mp.addpart(name="file", filename=file_name, local_path=file_path, content_type=mime_type)

        url = f"{self.BASE_URL}/file/upload_file"
        resp = self.session.post(url, headers=upload_headers, multipart=mp)
        data = resp.json()
        if data.get("code") != 0:
            raise DeepSeekAPIError(f"Fayl yuklashda xatolik: {data.get('msg')}", code=data.get("code"))

        file_id = data["data"]["biz_data"]["id"]
        log.info("deepseek_direct: fayl yuklandi id=%s nomi=%s", file_id, file_name)

        # Poll /fetch_files until status is SUCCESS
        deadline = time.time() + timeout
        fetch_url = f"{self.BASE_URL}/file/fetch_files"
        while time.time() < deadline:
            f_resp = self.session.get(fetch_url, headers=self.headers, params={"file_ids": [file_id]})
            f_data = f_resp.json()
            if f_data.get("code") == 0:
                files_list = f_data.get("data", {}).get("biz_data", {}).get("files", [])
                if files_list:
                    status = files_list[0].get("status")
                    if status == "SUCCESS":
                        return file_id
                    if status in ("FAILED", "ERROR"):
                        err_code = files_list[0].get("error_code")
                        raise DeepSeekAPIError(f"Faylni qayta ishlash muvaffaqiyatsiz bo'ldi: {err_code}")
            time.sleep(0.5)

        return file_id

    def stream_chat(
        self,
        prompt: str,
        ref_file_ids: Optional[List[str]] = None,
        thinking: bool = False,
        search: bool = False,
        session_id: Optional[str] = None,
    ) -> Generator[Dict[str, str], None, None]:
        """Stream chat completion via SSE.

        Yields:
            {"type": "thinking" | "content", "text": "fragment"}
        """
        if not session_id:
            if not self.current_session_id:
                self.create_chat_session()
            session_id = self.current_session_id

        pow_header = self.solve_pow("/api/v0/chat/completion")
        req_headers = {
            **self.headers,
            "content-type": "application/json",
            "x-ds-pow-response": pow_header,
        }

        body = {
            "chat_session_id": session_id,
            "parent_message_id": None,
            "prompt": prompt,
            "ref_file_ids": list(ref_file_ids or []),
            "thinking_enabled": bool(thinking),
            "search_enabled": bool(search),
        }

        url = f"{self.BASE_URL}/chat/completion"
        resp = self.session.post(url, headers=req_headers, json=body, stream=True)
        if resp.status_code != 200:
            resp.close()
            raise DeepSeekAPIError(f"Server xatosi: HTTP {resp.status_code}")

        try:
            current_frag_type = "content"
            for line in resp.iter_lines():
                if not line:
                    continue
                decoded = line.decode("utf-8", errors="ignore")
                if not decoded.startswith("data: "):
                    continue
                data_str = decoded[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    d = json.loads(data_str)
                except Exception:
                    continue

                # API level error response in data stream
                if d.get("code") not in (None, 0):
                    msg = d.get("msg") or d.get("data", {}).get("biz_msg") or "noma'lum xatolik"
                    raise DeepSeekAPIError(f"DeepSeek javobida xatolik: {msg}", code=d.get("code"))

                # 1. Initial full response fragment
                if "v" in d and isinstance(d["v"], dict) and "response" in d["v"]:
                    frags = d["v"]["response"].get("fragments", [])
                    for f in frags:
                        ftype = f.get("type", "").lower()
                        current_frag_type = "thinking" if "think" in ftype else "content"
                        cnt = f.get("content", "")
                        if cnt:
                            yield {"type": current_frag_type, "text": cnt}

                # 2. Append fragment path: response/fragments/-1/content
                elif "p" in d:
                    val = d.get("v", "")
                    p = str(d.get("p", "")).lower()
                    if isinstance(val, list) and "fragments" in p:
                        for item in val:
                            if isinstance(item, dict):
                                if "type" in item:
                                    ftype = item.get("type", "").lower()
                                    current_frag_type = "thinking" if "think" in ftype else "content"
                                if item.get("content"):
                                    yield {"type": current_frag_type, "text": item["content"]}
                    elif val and isinstance(val, str):
                        if "status" in p:
                            continue
                        if "thinking" in p:
                            yield {"type": "thinking", "text": val}
                        elif "content" in p or "fragments" in p:
                            yield {"type": current_frag_type, "text": val}

                # 3. Direct fragment chunk {"v": "text"}
                elif "v" in d and isinstance(d["v"], str):
                    yield {"type": current_frag_type, "text": d["v"]}
        finally:
            resp.close()
