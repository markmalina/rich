# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from .main import Bot
from .providers import ProviderError
from .voice_check import INVALID, SAMPLE, VoiceCheck


class VoiceCheckTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tg = Mock()
        self.model, self.sheets = Mock(), Mock()
        self.bot = Bot(self.tg, self.sheets, self.model, Path(temp.name), 2)
        self.addCleanup(self.bot.db.close)
        self.now = 10
        self.bot.voice_check = VoiceCheck(self.tg, clock=lambda: self.now)
        self.tg.api.return_value = {"voice": {"file_unique_id": "unique"}, "message_id": 41}
        self.tg.media.return_value = SAMPLE.read_bytes()
        self.update = 0

    def dispatch(self, user=3, *, kind="private", chat=None, **msg):
        self.update += 1
        update = {"update_id": self.update, "message": {
            "from": {"id": user}, "chat": {"type": kind, "id": user if chat is None else chat}, **msg}}
        self.bot.receive([update])
        self.bot.process_update(update)

    def issue(self, user=3):
        self.dispatch(user, text="/voice_check")
        method, payload = self.tg.api.call_args.args
        self.assertEqual(method, "sendVoice")
        self.assertEqual(self.tg.api.call_args.kwargs["files"]["voice"][1], SAMPLE.read_bytes())
        return {"voice": {"file_unique_id": "unique", "file_id": "download", "file_size": 7155},
                "caption": payload["caption"]}

    def test_private_forward_roundtrip_replay_and_no_data_writes(self):
        sample = self.issue()
        self.dispatch(text="/voice_check")
        self.assertEqual(self.tg.api.call_count, 1)
        self.dispatch(**sample, forward_origin={"type": "user"})
        receipt = self.tg.message.call_args.args[1]
        self.assertIn("входящее голосовое Telegram", receipt)
        self.assertIn("7155 байт", receipt)
        self.assertIn("2bc4371e5359d2a4d447992ca19ca548c78795c6bf29abfc4cfbd366126b844f", receipt)
        self.dispatch(**sample)
        self.tg.media.assert_called_once_with("download", 7155)
        self.assertEqual(self.tg.message.call_args.args[1], receipt)
        self.assertEqual(self.sheets.mock_calls, [])
        self.assertEqual(self.model.mock_calls, [])
        self.assertEqual(self.bot.db.execute("SELECT count(*) FROM state").fetchone(), (0,))

    def test_reply_checks_issued_message_and_reports_reply_evidence(self):
        sample = self.issue()
        self.dispatch(text="/voice_verify")
        self.dispatch(text="/voice_verify", reply_to_message={**sample, "message_id": 42})
        self.tg.media.assert_not_called()
        self.dispatch(text="/voice_verify", reply_to_message={**sample, "message_id": 41})
        self.assertIn("файл голосового из ответа", self.tg.message.call_args.args[1])
        self.assertNotIn("входящее голосовое", self.tg.message.call_args.args[1])

    def test_foreign_expired_changed_voice_and_unrelated_media(self):
        first = self.issue()
        self.issue(4)
        self.dispatch(4, **first)
        self.dispatch(5, **first)
        self.dispatch(3, kind="group", **first)
        self.dispatch(3, chat=4, **first)
        self.dispatch(3, **{**first, "voice": {"file_unique_id": "another", "file_id": "other"}})
        self.assertFalse(self.bot.voice_check.handle(3, {"voice": {"file_unique_id": "ordinary", "file_id": "file"}}))
        self.now += 600
        self.dispatch(**first)
        self.tg.message.assert_called_with(3, INVALID)
        self.tg.media.assert_not_called()
        self.assertNotIn(3, self.bot.voice_check.challenges)

    def test_failed_transfer_and_hash_can_be_retried(self):
        sample = self.issue()
        self.tg.media.side_effect = ProviderError("Telegram", 503)
        self.dispatch(**sample)
        self.assertIn("Повтори", self.tg.message.call_args.args[1])
        self.tg.media.side_effect = None
        self.tg.media.return_value = b"OggSbad"
        self.dispatch(**sample)
        self.assertIn("отличается", self.tg.message.call_args.args[1])
        self.tg.media.return_value = SAMPLE.read_bytes()
        self.dispatch(**sample)
        self.assertIn("Байты совпали", self.tg.message.call_args.args[1])
        restarted = VoiceCheck(self.tg)
        self.assertTrue(restarted.handle(3, sample))
        self.tg.message.assert_called_with(3, INVALID)

    def test_lost_confirmation_replays_the_original_receipt_without_download(self):
        sample = self.issue()
        self.tg.message.side_effect = ProviderError("Telegram", "connection")
        with self.assertRaises(ProviderError):
            self.dispatch(**sample)
        first = self.tg.message.call_args.args[1]
        self.tg.message.side_effect = None
        self.dispatch(**sample)
        self.tg.message.assert_called_with(3, first)
        self.assertIn("входящее голосовое Telegram", first)
        self.tg.media.assert_called_once()

    def test_reply_and_forward_of_one_sample_receive_separate_evidence(self):
        sample = self.issue()
        reply = {"text": "/voice_verify", "reply_to_message": {**sample, "message_id": 41}}
        self.dispatch(**reply)
        reply_receipt = self.tg.message.call_args.args[1]
        self.dispatch(**sample)
        forward_receipt = self.tg.message.call_args.args[1]
        self.assertIn("файл голосового из ответа", reply_receipt)
        self.assertIn("входящее голосовое Telegram", forward_receipt)
        self.dispatch(**reply)
        self.tg.message.assert_called_with(3, reply_receipt)
        self.dispatch(**sample)
        self.tg.message.assert_called_with(3, forward_receipt)
        self.assertEqual(self.tg.media.call_count, 2)
