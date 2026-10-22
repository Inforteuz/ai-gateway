"""Tests for DeepSeek direct browserless API integration and worker."""
import asyncio
import json
import unittest
from unittest.mock import MagicMock, patch

from cluster import balancer as B
from cluster import pool as pool_mod
from cluster import runmeta
from cluster import settings as cfg
from cluster.adapters.deepseek_adapter import DeepSeekAdapter
from cluster.deepseek_direct import DeepSeekDirectClient, DeepSeekAPIError


class TestDeepSeekDirectClient(unittest.TestCase):
    """Unit tests for DeepSeekDirectClient."""

    def test_missing_token_raises_error(self):
        with patch.object(cfg, "DEEPSEEK_USER_TOKEN", ""):
            with patch.dict("os.environ", {"DEEPSEEK_USER_TOKEN": ""}):
                with self.assertRaises(ValueError):
                    DeepSeekDirectClient("")

    def test_init_with_token(self):
        client = DeepSeekDirectClient("test_token_123")
        self.assertEqual(client.token, "test_token_123")
        self.assertIn("Bearer test_token_123", client.headers["authorization"])

    @patch("curl_cffi.requests.Session.get")
    def test_get_user_info_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "code": 0,
            "data": {"biz_data": {"user_id": "u123", "email": "test@example.com"}}
        }
        mock_get.return_value = mock_resp

        client = DeepSeekDirectClient("test_token")
        info = client.get_user_info()
        self.assertEqual(info["user_id"], "u123")
        self.assertEqual(info["email"], "test@example.com")

    @patch("curl_cffi.requests.Session.post")
    def test_create_and_delete_session(self, mock_post):
        # 1. Test create session
        mock_resp_create = MagicMock()
        mock_resp_create.json.return_value = {
            "code": 0,
            "data": {"biz_data": {"chat_session": {"id": "session-xyz"}}}
        }
        mock_post.return_value = mock_resp_create

        client = DeepSeekDirectClient("test_token")
        sess_id = client.create_chat_session()
        self.assertEqual(sess_id, "session-xyz")
        self.assertEqual(client.current_session_id, "session-xyz")

        # 2. Test delete session
        mock_resp_del = MagicMock()
        mock_resp_del.json.return_value = {"code": 0, "data": {"biz_data": None}}
        mock_post.return_value = mock_resp_del

        ok = client.delete_chat_session("session-xyz")
        self.assertTrue(ok)
        self.assertIsNone(client.current_session_id)

    def test_stream_chat_sse_parsing(self):
        """Test SSE lines parsing including thinking fragments and normal text."""
        lines = [
            b"event: ready\n",
            b'data: {"request_message_id":1}\n',
            b'data: {"v":{"response":{"fragments":[{"type":"THINKING","content":"O\'ylamoqdaman..."}]}}}\n',
            b'data: {"p":"response/fragments/-1/content","o":"APPEND","v":" davomi"}\n',
            b'data: {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"Salom! "}]}\n',
            b'data: {"v":"Qanday yordam bera olaman?"}\n',
            b"data: [DONE]\n",
        ]

        client = DeepSeekDirectClient("test_token")

        with patch.object(client, "solve_pow", return_value="mock_pow"):
            with patch.object(client.session, "post") as mock_post:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.iter_lines.return_value = lines
                mock_post.return_value = mock_resp

                chunks = list(client.stream_chat(prompt="Salom", session_id="test_sess"))

                # Verify parsed pieces
                self.assertEqual(len(chunks), 4)
                self.assertEqual(chunks[0], {"type": "thinking", "text": "O'ylamoqdaman..."})
                self.assertEqual(chunks[1], {"type": "thinking", "text": " davomi"})
                self.assertEqual(chunks[2], {"type": "content", "text": "Salom! "})
                self.assertEqual(chunks[3], {"type": "content", "text": "Qanday yordam bera olaman?"})


class TestDeepSeekDirectWorker(unittest.TestCase):
    """Unit tests for DeepSeekDirectWorker in pool."""

    def setUp(self):
        self.worker = pool_mod.DeepSeekDirectWorker("mock_token")

    def test_worker_attributes(self):
        self.assertEqual(self.worker.model_name, "deepseek")
        self.assertEqual(self.worker.browser_port, 0)
        self.assertEqual(self.worker.short_tab, "direct")
        self.assertTrue(self.worker.is_available())
        self.assertEqual(self.worker.state(), "ACTIVE")
        self.assertEqual(self.worker.health_score, 1.0)

    def test_cooldown_state(self):
        self.worker.set_cooldown(60, reason="rate_limit:test")
        self.assertFalse(self.worker.is_available())
        self.assertEqual(self.worker.state(), "COOLDOWN")
        self.worker.cooldown_until = 0.0
        self.assertTrue(self.worker.is_available())

    def test_execute_prompt_and_streaming(self):
        """Verify execute_prompt runs stream_chat and calls on_progress callback."""
        progress_updates = []

        def on_prog(text: str):
            progress_updates.append(text)

        mock_chunks = [
            {"type": "thinking", "text": "O'ylash bosqichi..."},
            {"type": "content", "text": "Birinchi "},
            {"type": "content", "text": "javob bo'lagi."},
        ]

        with patch.object(self.worker.client, "create_chat_session", return_value="sess_123"):
            with patch.object(self.worker.client, "stream_chat", return_value=iter(mock_chunks)):
                with patch.object(self.worker.client, "delete_chat_session", return_value=True) as mock_del:
                    meta = runmeta.start()
                    meta["on_progress"] = on_prog
                    meta["upstream_model"] = "deepseek-reasoner"

                    loop = asyncio.new_event_loop()
                    try:
                        ans, imgs = loop.run_until_complete(
                            self.worker.execute_prompt(prompt_text="Test prompt")
                        )
                    finally:
                        loop.close()

                    self.assertEqual(ans, "Birinchi javob bo'lagi.")
                    self.assertEqual(imgs, [])
                    self.assertEqual(progress_updates, ["Birinchi ", "Birinchi javob bo'lagi."])
                    mock_del.assert_called_once_with("sess_123")


class TestDeepSeekRoutingAndAliases(unittest.TestCase):
    """Test model aliases and balancer routing."""

    def setUp(self):
        self.worker = pool_mod.DeepSeekDirectWorker("mock_token")
        self.pool = MagicMock()
        self.pool.workers = [self.worker]
        self.pool.workers_for_model.side_effect = lambda m: [self.worker] if m == "deepseek" else []
        self.balancer = B.ClusterBalancer(self.pool)

    def test_alias_matching(self):
        for alias in ["deepseek", "deepseek-chat", "deepseek-reasoner", "deepseek-v3", "deepseek-r1"]:
            candidates = self.balancer.pick_candidates(alias)
            self.assertEqual(len(candidates), 1, f"Failed for alias {alias}")
            self.assertIs(candidates[0], self.worker)

            matching = self.balancer._matching(alias, require=())
            self.assertEqual(len(matching), 1, f"Failed matching for {alias}")
            self.assertIs(matching[0], self.worker)

    def test_adapter_get_direct_client(self):
        adapter = DeepSeekAdapter()
        client = adapter.get_direct_client("token_abc")
        self.assertIsInstance(client, DeepSeekDirectClient)
        self.assertEqual(client.token, "token_abc")


if __name__ == "__main__":
    unittest.main()
