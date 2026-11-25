# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from .ledger import EVENT_FIELDS, MOSCOW, QUERY_FIELDS, Clarification, ledger_rows, summarize_query
from .main import Bot, attachment

NOW = dt.datetime(2026, 11, 23, 14, 5, tzinfo=MOSCOW)
ACTIONS = {
    "Приобретение": ["Приобретение", "Финансы"],
    "Оплата": ["Оплата", "Финансы"],
    "Перевод денег": ["Перевод денег", "Финансы"],
    "Благодарность": ["Благодарность", "Отношения"],
    "Яблоко": ["Яблоко", "Приём пищи", "шт.", "", "", "", "Приём пищи"],
    "Завтрак": ["Завтрак", "Приём пищи", "", "", "", "", "Приём пищи"],
    "Отжимание": ["Отжимание", "Физические упражнения", "повт.", 3.8, "", "", "Физические упражнения"],
    "Расход калорий": ["Расход калорий", "Учёт энергии", "ккал"],
}


def event(kind, **fields):
    return {"kind": kind, **dict.fromkeys(EVENT_FIELDS), **fields}


class LedgerTest(unittest.TestCase):
    def test_purchase_total_is_only_one_cash_movement(self):
        rows, fallback = ledger_rows([event("purchase", summary="Продукты", amount=123456.78, currency="RUB",
                                            resource="Продукты", payment_account="Карта")], "tg-7-1", NOW, ACTIONS, None)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][3:6], ["Приобретение", 1, "шт."])
        self.assertIn("итог 123456.78 RUB", rows[0][2])
        self.assertEqual(rows[1][3:6], ["Оплата", -123456.78, "RUB"])
        self.assertEqual(rows[1][13], "Карта")
        self.assertEqual({row[8] for row in rows}, {"tg-7-1"})
        self.assertEqual({row[11] for row in rows}, {"tg-7-1-e1"})
        self.assertEqual(fallback, "|tg-7-1-e1|")

    def test_receipt_without_total_requires_clarification(self):
        with self.assertRaises(Clarification):
            ledger_rows([event("purchase", currency="RUB")], "tg-7-2", NOW, ACTIONS, None)

    def test_food_mass_stays_in_quantity_and_counts_as_one_meal(self):
        rows, _ = ledger_rows([
            event("fact", action="Завтрак", quantity=50, unit="г", resource="Каша", body_weight_kg=50),
        ], "tg-7-5", NOW, ACTIONS, 79)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3:6], ["Завтрак", 50, "г"])
        self.assertEqual(rows[0][9], "")
        self.assertEqual(rows[0][12], "Каша")
        query = {**dict.fromkeys(QUERY_FIELDS), "category": "Приём пищи"}
        self.assertIn("50 г", summarize_query(rows, ACTIONS, query, NOW.date()))

    def test_exercise_and_query_keep_event_time(self):
        rows, fallback = ledger_rows([
            event("fact", action="Яблоко", quantity=1, time="10:00"),
            event("activity", action="Отжимание", quantity=15, body_weight_kg=79),
        ], "tg-7-3", NOW, ACTIONS, 60)
        self.assertEqual(rows[0][0:2], ["23.11.2026", "10:00:00"])
        self.assertEqual(fallback, "|tg-7-3-e2|")
        self.assertEqual(rows[-1][3:6], ["Расход калорий", -3.9, "ккал"])
        self.assertEqual(rows[1][9], 79)
        query = {**dict.fromkeys(QUERY_FIELDS), "category": "Приём пищи"}
        text = summarize_query(rows, ACTIONS, query, NOW.date())
        self.assertIn("Яблоко", text)
        self.assertNotIn("Отжимание", text)

    def test_thank_you_only_creates_a_row_when_reported(self):
        for reported, count in [(True, 2), (False, 1)]:
            rows, _ = ledger_rows([event("transfer", amount=100, currency="BYN", party="Получатель",
                                        gratitude=reported)], "tg-7-4", NOW, ACTIONS, None)
            self.assertEqual(len(rows), count)
            self.assertEqual(rows[0][4], -100)
            if reported:
                self.assertEqual(rows[1][3:6], ["Благодарность", 1, "ед."])
                self.assertEqual(rows[0][11], rows[1][11])

    def test_media_formats(self):
        self.assertEqual(attachment({"voice": {"file_id": "v"}})[1], "audio/ogg")
        self.assertEqual(attachment({"photo": [{"file_id": "p"}]})[1], "image/jpeg")
        self.assertEqual(attachment({"document": {"file_id": "p", "mime_type": "image/png"}})[1], "image/png")


class FakeGoogle:
    def __init__(self):
        self.inbox = []
        self.events = []
        self.updates = []

    def read(self, area):
        if area == "'Входящие'!A2:A":
            return [[row[0]] for row in self.inbox]
        if area == "'События'!I2:L":
            return [row[8:12] for row in self.events]
        raise AssertionError(area)

    def append(self, area, rows):
        dest = self.inbox if "Входящие" in area else self.events
        dest.extend(rows)
        return len(rows)

    def update(self, area, values):
        self.updates.append((area, values))


class ReplayTest(unittest.TestCase):
    def test_retry_keeps_one_inbox_row_and_one_event(self):
        google = FakeGoogle()
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(None, google, None, Path(directory), 7)
            try:
                for _ in range(2):
                    bot.capture("tg-7-9", NOW, "Telegram", "Купил продукты", "")
                    rows, fallback = ledger_rows([event("purchase", amount=42, currency="RUB")],
                                                 "tg-7-9", NOW, ACTIONS, None)
                    bot.save_events("tg-7-9", rows, fallback)
            finally:
                bot.db.close()
        self.assertEqual(len(google.inbox), 1)
        self.assertEqual(len(google.events), 2)
        self.assertEqual(len(google.updates), 1)


class TelegramOnlyTest(unittest.TestCase):
    def test_start_and_report_are_answered_without_processing_the_report(self):
        telegram = Mock()
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(telegram, None, None, Path(directory), 7)
            try:
                bot.handle(1, {"chat": {"id": 7}, "text": "/start"}, NOW)
                with patch.object(bot, "capture") as capture:
                    bot.handle(2, {"chat": {"id": 7}, "voice": {"file_id": "test"}}, NOW)
                    capture.assert_not_called()
                telegram.media.assert_not_called()
                self.assertIn("Telegram подключён", telegram.message.call_args_list[0].args[1])
                self.assertIn("Пришли сообщение заново", telegram.message.call_args_list[1].args[1])
            finally:
                bot.db.close()
