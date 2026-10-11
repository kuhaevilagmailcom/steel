"""Unit tests for Huntergram's .ai feature. No real API key/network required."""
import io
import json
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import huntergram_ai as ai


class AICommandTests(unittest.TestCase):
    def test_parse_strict(self):
        self.assertTrue(ai.is_ai_command(".ai Привет"))
        self.assertTrue(ai.is_ai_command(" .AI\nПривет"))
        self.assertTrue(ai.is_ai_command(".ai"))
        self.assertFalse(ai.is_ai_command(".aide Привет"))
        self.assertFalse(ai.is_ai_command("Скажи .ai Привет"))

    def test_reply_context(self):
        self.assertIn("Что это?", ai.build_ai_prompt({
            "text": ".ai Что это?", "reply_to_message": {"text": "Hello"}
        }))
        self.assertIn("Hello", ai.build_ai_prompt({
            "text": ".ai", "reply_to_message": {"text": "Hello"}
        }))
        self.assertEqual(ai.build_ai_prompt({"text": ".ai"}), "")

    def test_openrouter_free_and_key(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self):
                return json.dumps({"choices": [{"message": {"content": "Привет!"}}]}).encode()
        def fake_open(req, timeout):
            self.assertEqual(json.loads(req.data)["model"], "openrouter/free")
            self.assertIn("Bearer secret_test", req.headers["Authorization"])
            self.assertEqual(timeout, 30)
            return Response()
        with patch.dict("os.environ", {"OPENROUTER_MODEL": "openrouter/free"}), patch.object(ai, "urlopen", fake_open):
            self.assertEqual(ai.ask_openrouter("Привет", "secret_test"), "Привет!")

    def test_http_429_friendly(self):
        def fake_open(req, timeout):
            raise HTTPError(ai.OPENROUTER_URL, 429, "Too many requests", {}, io.BytesIO(b"secret"))
        with patch.object(ai, "urlopen", fake_open):
            with self.assertRaises(ai.OpenRouterAIError) as cm:
                ai.ask_openrouter("Привет", "secret_test")
            self.assertIn("лимит", str(cm.exception))
            self.assertNotIn("secret", str(cm.exception))

    def test_queue_and_limits(self):
        blocker = threading.Event()
        result = []
        finished = threading.Event()
        def fake_ask(prompt):
            blocker.wait(2)
            return "answer:" + prompt
        def send(text, success):
            result.append((text, success))
            if success: finished.set()
        with patch.dict("os.environ", {"OPENROUTER_API_KEY": "secret", "AI_DAILY_PER_USER": "5"}), patch.object(ai, "ask_openrouter", fake_ask):
            self.assertTrue(ai.handle_ai_message({"text": ".ai test"}, 912345678, send))
            self.assertTrue(ai.handle_ai_message({"text": ".ai test2"}, 912345678, send))
            self.assertEqual(result[-1][1], False)
            blocker.set()
            self.assertTrue(finished.wait(3))
            self.assertIn(("answer:test", True), result)


if __name__ == "__main__":
    unittest.main()
