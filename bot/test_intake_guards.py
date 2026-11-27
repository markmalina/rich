# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from .ai_quota import AIQuota
from .event_write import EventWrite
from .ledger import MOSCOW, PLAN_SCHEMA, QUERY_FIELDS
from .main import Bot
from .operations import Operations
from .providers import MAX_MEDIA, Model, ProviderError, Telegram
from .test_tracker import FakeGoogle, NOW, event


class MediaGoogle(FakeGoogle):
    def read(self, area):
        cell = area.removeprefix("'Входящие'!F")
        if cell.isdigit():
            index = int(cell) - 2
            value = self.inbox[index][5]
            return [[value]] if value else []
        return super().read(area)

    def update(self, area, values):
        super().update(area, values)
        sheet, _, cell = area.partition("!")
        if sheet == "'Входящие'" and cell[:1] in {"E", "F", "G"}:
            self.inbox[int(cell[1:]) - 2][ord(cell[0]) - ord("A")] = values[0][0]


def plan(decision, *, events=(), extracted=None, continues=False, message=""):
    return {"decision": decision, "continues": continues, "message": message, "extracted": extracted,
            "events": list(events), "query": dict.fromkeys(QUERY_FIELDS)}


class IntakeTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tg, self.model = Mock(), Mock()
        self.google = MediaGoogle()
        self.bot = Bot(self.tg, self.google, self.model, self.root, 7)
        self.addCleanup(self.bot.db.close)

    def submit(self, number, *, user=7, **content):
        message = {"chat": {"id": user}, "from": {"id": user}, "date": int(NOW.timestamp()),
                   "message_id": number, **content}
        self.bot.handle(number, message, NOW)
        return message

    def test_voice_and_own_original_preserve_transcript_and_bytes(self):
        media = b"OggS synthetic audio"
        self.tg.media.return_value = media
        self.model.transcribe.return_value = "Учебная запись. Сделал 10 отжиманий."
        self.model.plan.return_value = plan("record", events=[event("activity", action="Отжимание", quantity=10)])
        source = self.submit(1, voice={"file_id": "fixture-ogg"})
        self.submit(1, voice={"file_id": "fixture-ogg"})
        self.assertEqual(self.model.transcribe.call_count, 1)
        self.assertEqual(self.model.plan.call_count, 1)
        self.assertEqual(self.google.inbox[0][4:6], ["tg-7-1.ogg", self.model.transcribe.return_value])
        self.assertEqual(self.google.events[0][3:6], ["Отжимание", 10, "повт."])
        self.assertEqual((self.root / "tg-7-1.ogg").stat().st_mode & 0o777, 0o600)

        def send(method, body, *, files):
            self.assertEqual((method, body, files["document"][0]), ("sendDocument", {"chat_id": 7}, "tg-7-1.ogg"))
            self.assertEqual(files["document"][1].read(), media)

        self.tg.api.side_effect = send
        self.submit(2, text="/original", reply_to_message=source)
        self.submit(3, text="/originals")
        self.assertIn("tg-7-1.ogg", self.tg.message.call_args.args[1])
        (self.root / "tg-8-1.ogg").write_bytes(b"other owner")
        self.submit(4, text="/original tg-8-1.ogg")
        self.assertIn("не найден", self.tg.message.call_args.args[1])
        self.assertEqual(self.tg.api.call_count, 1)

    def test_complete_and_hidden_total_receipts_are_each_one_movement(self):
        self.tg.media.return_value = b"synthetic receipt bytes"
        receipt = event("purchase", summary="Учебная покупка", amount=360, currency="RUB", payment_account="Карта")
        self.model.plan.side_effect = [plan("record", events=[receipt], extracted="ИТОГО 360 RUB"),
                                       plan("clarify", message="Какая была итоговая сумма?", extracted="ИТОГО закрыто"),
                                       plan("record", events=[receipt], continues=True)]
        self.submit(1, photo=[{"file_id": "thumb"}, {"file_id": "jpeg"}])
        photo = self.submit(2, document={"file_id": "webp", "mime_type": "image/webp"})
        self.assertEqual(self.tg.message.call_args.args[1], "Какая была итоговая сумма?")
        self.submit(3, text="360 RUB, карта", reply_to_message=photo)
        self.assertEqual(self.google.inbox[0][4:6], ["tg-7-1.jpg", "ИТОГО 360 RUB"])
        self.assertEqual(self.google.inbox[1][4:6], ["tg-7-2.webp", "ИТОГО закрыто"])
        self.assertEqual([(r[4], r[5], r[8]) for r in self.google.events if r[3] == "Оплата"],
                         [(-360, "RUB", "tg-7-1"), (-360, "RUB", "tg-7-2")])
        self.assertIsNone(self.bot.get("question"))

    def test_ogg_document_with_generic_mime_roundtrips_and_rejects_other_content(self):
        # A beginning-of-stream page carrying the Opus identification packet.
        packet = b"OpusHead" + bytes([1, 1]) + bytes(9)
        data = b"OggS\x00\x02" + bytes(20) + bytes([1, len(packet)]) + packet
        self.tg.media.return_value = data
        self.model.transcribe.return_value = "Учебная запись. Сделал десять отжиманий."
        self.model.plan.return_value = plan("record", events=[event("activity", action="Отжимание", quantity=10)])
        source = self.submit(1, document={"file_name": "ExErCiSe.OGA", "file_id": "fixture", "mime_type": "application/octet-stream"})
        self.model.transcribe.assert_called_once_with("tg-7-1.ogg", data, "audio/ogg")
        self.assertEqual((self.root / "tg-7-1.ogg").read_bytes(), data)
        self.assertEqual(self.google.events[0][3:6], ["Отжимание", 10, "повт."])
        self.submit(2, text="/original", reply_to_message=source)
        self.assertEqual(self.tg.api.call_args.args, ("sendDocument", {"chat_id": 7}))

        self.tg.media.return_value = b"not an Ogg stream"
        self.submit(3, audio={"file_name": "voice.opus", "file_id": "wrong"})
        self.assertIn("Opus или Vorbis", self.tg.message.call_args.args[1])
        self.assertEqual(self.google.inbox[-1][4], "")
        self.assertFalse((self.root / "tg-7-3.ogg").exists())
        self.assertEqual(self.model.transcribe.call_count, 1)

    def test_unbound_profile_media_gets_private_status_without_downloading_or_writing(self):
        update = {"update_id": 12, "message": {"chat": {"id": 8, "type": "private"}, "from": {"id": 8},
                                               "message_id": 1, "date": int(NOW.timestamp()), "voice": {"file_id": "v"}}}
        self.bot.receive([update])
        self.bot.process_update(update)
        self.tg.media.assert_not_called()
        self.model.transcribe.assert_not_called()
        self.assertEqual(self.google.inbox, [])
        self.assertIn("нет подключённой", self.tg.message.call_args.args[1])

    def test_manual_receipt_when_ai_is_off_and_repeat_keeps_one_payment(self):
        self.bot.model = None
        self.tg.media.return_value = b"synthetic PNG receipt"
        source = self.submit(1, document={"file_id": "png", "mime_type": "image/png"})
        self.assertIn("/record", self.tg.message.call_args.args[1])
        self.assertTrue((self.root / "tg-7-1.png").is_file())
        for number in (2, 2, 3):
            self.submit(number, text="/record Приобретение | Учебный чек | 360 RUB | Карта | 2000-01-01 07:14",
                        reply_to_message=source)
        self.assertEqual(len(self.google.events), 2)
        self.assertEqual(self.google.events[-1][0:2], ["01.01.2000", "07:14:00"])
        self.assertEqual(self.google.events[-1][3:6], ["Оплата", -360, "RUB"])
        self.assertEqual(self.google.events[-1][8], "tg-7-1")
        self.submit(4, text="/record Завтрак | Гранола | 80 г")
        self.assertEqual(self.google.events[-1][3:6], ["Завтрак", 80, "г"])
        self.model.plan.assert_not_called()
        self.model.transcribe.assert_not_called()

    def test_quota_limits_attempts_keeps_attachment_and_resets_by_moscow_day(self):
        quota = AIQuota(self.bot.db, 1)
        self.bot.ai_quota = quota
        self.model.plan.side_effect = ProviderError("OpenAI", 429)
        self.submit(1, text="Синтетическая запись")
        self.tg.media.return_value = b"OggS fixture"
        source = self.submit(2, document={"file_id": "ogg", "mime_type": "application/ogg"})
        self.assertIn("Дневной лимит", self.tg.message.call_args.args[1])
        self.assertTrue((self.root / "tg-7-2.ogg").is_file())
        self.submit(3, text="/record Отжимание | Тест | 10", reply_to_message=source)
        self.assertEqual(self.google.events[0][3:6], ["Отжимание", 10, "повт."])
        self.assertEqual(self.model.plan.call_count, 1)
        self.model.transcribe.assert_not_called()
        later = dt.datetime(2040, 1, 2, 0, 0, tzinfo=MOSCOW)
        self.assertTrue(quota.take(later-dt.timedelta(seconds=1)))
        self.assertFalse(AIQuota(self.bot.db, 1).take(later-dt.timedelta(seconds=1)))
        self.assertTrue(quota.take(later))

    def test_invalid_attachment_clears_unavailable_reference_and_preserves_question(self):
        self.bot.put("question", {"origin": "fixture-question"})
        self.tg.media.side_effect = ProviderError("Telegram media", "size")
        self.submit(1, document={"file_id": "huge", "mime_type": "image/png", "file_size": MAX_MEDIA+1})
        self.assertIn("18 МиБ", self.tg.message.call_args.args[1])
        self.assertEqual(self.google.inbox[0][4], "")
        self.assertEqual(self.bot.get("question")["origin"], "fixture-question")
        self.submit(2, document={"file_id": "pdf", "mime_type": "application/pdf"})
        self.assertIn("JPG, PNG, WebP", self.tg.message.call_args.args[1])
        self.model.plan.assert_not_called()

    def test_alerts_only_for_current_incident_and_no_source_in_notification(self):
        quota = AIQuota(self.bot.db, 10)
        quota.take(NOW)
        operations = Operations(self.bot.db, self.tg, 7, self.root, ai_quota=quota, alerts=True)
        self.addCleanup(operations.ledger.close)
        self.bot.event_writes.stage(EventWrite(8, "sensitive-origin", 3, "synthetic-base", [], "", None))
        with self.bot.db:
            self.bot.db.execute("UPDATE event_writes SET queued_at=?", (int(NOW.timestamp())-901,))
        operations.last_poll = NOW
        with patch("bot.operations.shutil.disk_usage", return_value=Mock(total=10**9, free=1024)):
            operations.check_alerts(NOW)
            operations.check_alerts(NOW+dt.timedelta(minutes=2))
            self.assertEqual(self.tg.message.call_count, 1)
            self.assertEqual(self.tg.message.call_args.args[0], 7)
            self.assertIn("disk_low, queue_stale", self.tg.message.call_args.args[1])
            self.assertNotIn("sensitive-origin", self.tg.message.call_args.args[1])
            self.assertIn("15 мин", operations.health(NOW))
            self.assertIn("обращения 1 из 10", operations.health(NOW))
        self.bot.event_writes.finish(8, "sensitive-origin")
        with patch("bot.operations.shutil.disk_usage", return_value=Mock(total=10**9, free=10**8)):
            operations.check_alerts(NOW+dt.timedelta(minutes=4))
        with patch("bot.operations.shutil.disk_usage", return_value=Mock(total=10**9, free=1024)):
            operations.check_alerts(NOW+dt.timedelta(minutes=6))
        self.assertEqual(self.tg.message.call_count, 2)


class ProviderContractTest(unittest.TestCase):
    def test_exact_stream_limit_and_one_extra_byte(self):
        pointer = Mock(ok=True)
        pointer.json.return_value = {"ok": True, "result": {"file_path": "fixtures/voice.oga"}}
        download = MagicMock(ok=True)
        download.__enter__.return_value = download
        client = Telegram(Mock(), "synthetic-token")
        data = b"x" * (64*1024)
        download.iter_content.return_value = [data] * (MAX_MEDIA//len(data))
        with patch("bot.providers.request", side_effect=[pointer, download, pointer, download]) as request:
            self.assertEqual(len(client.media("voice", MAX_MEDIA)), MAX_MEDIA)
            download.iter_content.return_value += [b"x"]
            with self.assertRaisesRegex(ProviderError, "size"):
                client.media("voice", None)
            with self.assertRaisesRegex(ProviderError, "size"):
                client.media("voice", MAX_MEDIA+1)
            self.assertEqual(request.call_count, 4)

    def test_model_rejects_nested_wrong_types_and_accepts_valid_plan(self):
        client = Model(Mock(), "synthetic-key", "fixture", "fixture")
        valid = plan("record", events=[event("fact", action="Завтрак", quantity=80)])
        response = Mock()
        with patch("bot.providers.request", return_value=response):
            for invalid in (plan("record", events=[event("fact", quantity="80")]),
                            {**valid, "continues": 1}, {**valid, "query": []}):
                response.json.return_value = {"status": "completed", "output": [{"content": [
                    {"type": "output_text", "text": json.dumps(invalid)}]}]}
                with self.assertRaisesRegex(ProviderError, "invalid response"):
                    client.plan("instructions", {}, PLAN_SCHEMA)
            response.json.return_value["output"][0]["content"][0]["text"] = json.dumps(valid)
            self.assertEqual(client.plan("instructions", {}, PLAN_SCHEMA), valid)
