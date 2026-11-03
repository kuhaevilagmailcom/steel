"""Offline regression tests for Telegram Business dialog muting."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "000000:TEST_ONLY")

import deleted_message_logger_bot as bot


class BusinessMuteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.originals = {key: getattr(bot, key) for key in (
            "DATA_DIR", "DB_PATH", "LOG_PATH", "RAW_UPDATES_PATH", "telegram_call",
            "raw_log", "log", "owner_can_log",
        )}
        root = Path(self.tmp.name)
        bot.DATA_DIR = root
        bot.DB_PATH = root / "test.sqlite3"
        bot.LOG_PATH = root / "test.log"
        bot.RAW_UPDATES_PATH = root / "raw.jsonl"
        bot.raw_log = lambda *_args: None
        bot.log = lambda *_args: None
        bot.owner_can_log = lambda *_args: True
        bot.init_db()
        bot.save_business_connection({
            "id": "business-test",
            "user": {"id": 1234, "first_name": "Owner"},
            "user_chat_id": 1234,
            "is_enabled": True,
            "rights": {"can_reply": True, "can_delete_all_messages": True},
        })
        self.calls = []
        self.can_delete = True

        def fake_telegram_call(method, payload=None, timeout=30):
            if method == "getBusinessConnection":
                return {"is_enabled": True, "rights": {
                    "can_delete_all_messages": self.can_delete,
                }}
            self.calls.append((method, payload))
            return {"message_id": 777}

        bot.telegram_call = fake_telegram_call

    def tearDown(self):
        for key, value in self.originals.items():
            setattr(bot, key, value)
        self.tmp.cleanup()

    def owner_command(self, chat_id=4321, message_id=100):
        return {
            "business_connection_id": "business-test",
            "message_id": message_id,
            "from": {"id": 1234},
            "chat": {"id": chat_id, "type": "private", "first_name": "Peer"},
            "text": ".mute",
        }

    def callback(self, action, token, user_id=1234, chat_id=4321, message_id=100):
        return {
            "id": "callback-1",
            "data": f"bizmute:{action}:{token}",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id, "type": "private"}},
        }

    def test_mute_deletes_new_peer_messages_without_notifications(self):
        bot.handle_business_message(self.owner_command())
        muted = bot.get_business_mute("business-test", 4321)
        self.assertIsNotNone(muted)
        self.assertEqual(bot.count_business_mutes(1234), 1)
        status = next(payload for method, payload in self.calls if method == "editMessageText")
        self.assertIn("Размутить всех", str(status["reply_markup"]))
        self.assertIn("Размутить", str(status["reply_markup"]))

        self.calls.clear()
        bot.handle_business_message({
            "business_connection_id": "business-test",
            "message_id": 101,
            "from": {"id": 4321},
            "chat": {"id": 4321, "type": "private"},
            "text": "new message",
        })
        self.assertIn(("deleteBusinessMessages", {
            "business_connection_id": "business-test",
            "message_ids": [101],
        }), self.calls)
        self.assertIsNone(bot.get_saved_message("business:business-test", 4321, 101))

        self.calls.clear()
        bot.handle_deleted_business_messages({
            "business_connection_id": "business-test",
            "chat": {"id": 4321, "type": "private"},
            "message_ids": [101],
        })
        self.assertFalse(any(method == "sendMessage" for method, _ in self.calls))
        self.assertFalse(bot.consume_business_mute_deletion("business-test", 4321, 101))

    def test_only_owner_can_unmute(self):
        bot.handle_business_message(self.owner_command())
        token = bot.get_business_mute("business-test", 4321)["control_token"]
        bot.handle_business_mute_callback(self.callback("one", token, user_id=4321), 4321)
        self.assertIsNotNone(bot.get_business_mute("business-test", 4321))
        bot.handle_business_mute_callback(self.callback("one", token), 1234)
        self.assertIsNone(bot.get_business_mute("business-test", 4321))

    def test_unmute_all_clears_all_owner_dialogs(self):
        bot.handle_business_message(self.owner_command())
        bot.set_business_mute("business-test", 9876, 1234)
        token = bot.get_business_mute("business-test", 4321)["control_token"]
        bot.handle_business_mute_callback(self.callback("all", token), 1234)
        self.assertEqual(bot.count_business_mutes(1234), 0)
        self.assertIsNone(bot.get_business_mute("business-test", 9876))

    def test_requires_delete_all_right(self):
        self.can_delete = False
        bot.handle_business_message(self.owner_command())
        self.assertEqual(bot.count_business_mutes(1234), 0)
        self.assertTrue(any("Удаление всех сообщений" in str(p.get("text"))
                            for method, p in self.calls if method == "editMessageText"))

    def test_other_users_cannot_issue_mute_command(self):
        message = self.owner_command()
        message["from"]["id"] = 4321
        self.assertFalse(bot.handle_huntergram_command(message, 1234, "business:business-test"))
        self.assertEqual(bot.count_business_mutes(1234), 0)


if __name__ == "__main__":
    unittest.main()
