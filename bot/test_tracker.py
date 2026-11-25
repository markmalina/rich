# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from .event_write import EventWrite
from .group_reports import build_report
from .ledger import EVENT_FIELDS, MOSCOW, QUERY_FIELDS, Clarification, action_catalog, ledger_rows, summarize_query
from .main import EVENT_UNCONFIRMED, Bot, attachment
from .providers import ProviderError

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

    def test_internal_transfer_requires_two_distinct_accounts(self):
        for destination in (None, " КАРТА "):
            with self.subTest(destination=destination), self.assertRaisesRegex(Clarification, "счёта"):
                ledger_rows([event("internal_transfer", amount=100, currency="RUB", account="карта",
                                   destination_account=destination)], "tg-7-6", NOW, ACTIONS, None)

    def test_old_actions_catalog_preserves_local_settings_and_recognizes_income(self):
        custom = ["Расход", "Финансы", "BYN"]
        catalog = action_catalog([*ACTIONS.values(), custom])
        self.assertEqual(catalog["Расход"], custom)
        rows, _ = ledger_rows([event("income", amount=100, currency="BYN")], "tg-7-8", NOW, ACTIONS, None)
        query = {**dict.fromkeys(QUERY_FIELDS), "category": "Финансы"}
        self.assertIn("Доход · 100 BYN", summarize_query(rows, catalog, query, NOW.date()))

    def test_media_formats(self):
        self.assertEqual(attachment({"voice": {"file_id": "v"}})[1], "audio/ogg")
        self.assertEqual(attachment({"photo": [{"file_id": "p"}]})[1], "image/jpeg")
        self.assertEqual(attachment({"document": {"file_id": "p", "mime_type": "image/png"}})[1], "image/png")


class FakeGoogle:
    def __init__(self):
        self.spreadsheet_id = "test-private-base"
        self.inbox = []
        self.events = []
        self.updates = []

    def read(self, area):
        if area == "'Входящие'!A2:A":
            return [[row[0]] for row in self.inbox]
        if area == "'События'!I2:L":
            return [row[8:12] for row in self.events]
        if area == "'События'!A2:O":
            return list(self.events)
        if area == "'Действия'!A2:G":
            return list(ACTIONS.values())
        if area in {"'Действия'!I2", "'Правило'!A7:D50"}:
            return []
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
            bot = Bot(Mock(), google, None, Path(directory), 7)
            try:
                for update in (1, 2):
                    bot.capture("tg-7-9", NOW, "Telegram", "Купил продукты", "")
                    rows, fallback = ledger_rows([event("purchase", amount=42, currency="RUB")],
                                                 "tg-7-9", NOW, ACTIONS, None)
                    batch = EventWrite(7, "tg-7-9", update, google.spreadsheet_id, rows, fallback, None)
                    self.assertTrue(bot.event_writes.stage(batch))
                    bot.answer_event(batch)
            finally:
                bot.db.close()
        self.assertEqual(len(google.inbox), 1)
        self.assertEqual(len(google.events), 2)
        self.assertEqual(len(google.updates), 1)

    def test_private_message_counts_real_finances_and_keeps_transfer_legs_linked(self):
        google = FakeGoogle()
        model = Mock()
        model.plan.return_value = {"decision": "record", "continues": False, "message": "", "events": [
            event("income", summary="зарплата", amount=1000, currency="RUB", account="Банк"),
            event("expense", summary="расчёт", amount=23, currency="RUB", account="Банк", party="Получатель"),
            event("internal_transfer", amount=70, currency="RUB", account="Банк", destination_account="Карман"),
            event("transfer", amount=40, currency="RUB", account="Банк"),
        ]}
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(Mock(), google, model, Path(directory), 7)
            try:
                message = {"chat": {"id": 7}, "message_id": 99, "date": int(NOW.timestamp()), "text": "Операции"}
                bot.handle(50, message, NOW)
                bot.handle(50, message, NOW)
            finally:
                bot.db.close()
        self.assertEqual(len(google.inbox), 1)
        model.plan.assert_called_once()
        income, expense, debit, credit, legacy = google.events
        self.assertEqual((income[3:6], expense[3:6]), (["Доход", 1000, "RUB"], ["Расход", -23, "RUB"]))
        self.assertEqual((debit[4], credit[4]), (-70, 70))
        self.assertEqual((debit[13], credit[13]), ("Банк", "Карман"))
        self.assertEqual(debit[11], credit[11])
        self.assertEqual(legacy[3], "Перевод денег")
        report = build_report(NOW.date(), google.events, (), finance=True)
        self.assertEqual(report.text.splitlines()[1:], ["Финансы — операций: 2"])

    def set_up_transfer(self, google):
        model = Mock()
        model.plan.return_value = {"decision": "record", "continues": False, "message": "", "events": [
            event("internal_transfer", amount=70, currency="RUB", account="Банк", destination_account="Карман"),
        ]}
        message = {"chat": {"id": 7}, "message_id": 99, "date": int(NOW.timestamp()), "text": "Между своими"}
        return model, message

    def test_accepts_confirmed_batch_if_google_lost_the_append_response(self):
        google = FakeGoogle()
        model, message = self.set_up_transfer(google)
        append = google.append

        def lose_response(area, rows):
            result = append(area, rows)
            if "События" in area:
                raise ProviderError("Apps Script", "connection")
            return result

        telegram = Mock()
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(telegram, google, model, Path(directory), 7)
            try:
                with patch.object(google, "append", side_effect=lose_response) as request:
                    bot.handle(50, message, NOW)
                    bot.handle(50, message, NOW)
                self.assertIsNone(bot.event_writes.find(7, 50))
            finally:
                bot.db.close()
        sent_batches = [call for call in request.call_args_list if "События" in call.args[0]]
        self.assertEqual(len(sent_batches), 1)
        self.assertEqual(len(google.events), 2)
        self.assertIn("Записал в «БАЗУ».", telegram.message.call_args.args[1])

    def test_late_google_commit_after_restart_is_confirmed_without_sending_a_second_append(self):
        google = FakeGoogle()
        model, message = self.set_up_transfer(google)
        append = google.append
        delayed = []

        def delay(area, rows):
            if "События" in area:
                delayed.extend(rows)
                raise ProviderError("Apps Script", "connection")
            return append(area, rows)

        telegram = Mock()
        with tempfile.TemporaryDirectory(dir=".") as directory:
            with patch.object(google, "append", side_effect=delay) as request:
                bot = Bot(telegram, google, model, Path(directory), 7)
                try:
                    bot.handle(50, message, NOW)
                finally:
                    bot.db.close()
                telegram.message.assert_called_once_with(7, EVENT_UNCONFIRMED)
                recovered = Bot(telegram, google, model, Path(directory), 7)
                try:
                    google.events.extend(delayed[:1])
                    with patch("bot.main.time.time", return_value=100):
                        recovered.reconcile_events(51)
                    telegram.message.assert_called_once()
                    self.assertTrue(recovered.event_writes.find(7, 50).sent)
                    google.events.extend(delayed[1:])
                    with patch("bot.main.time.time", return_value=280):
                        recovered.reconcile_events(51)
                    self.assertIsNone(recovered.event_writes.find(7, 50))
                finally:
                    recovered.db.close()
        self.assertEqual(len([call for call in request.call_args_list if "События" in call.args[0]]), 1)
        self.assertEqual(len(google.events), 2)
        self.assertIn("Записал в «БАЗУ».", telegram.message.call_args.args[1])
        model.plan.assert_called_once()

    def test_pending_write_cannot_follow_an_owner_to_a_different_workbook(self):
        google = FakeGoogle()
        other = FakeGoogle()
        other.spreadsheet_id = "another-private-base"
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(Mock(), google, None, Path(directory), 7)
            try:
                batch = EventWrite(7, "tg-7-9", 50, google.spreadsheet_id, [], "", None)
                bot.event_writes.stage(batch)
                with self.assertRaisesRegex(ProviderError, "назначение изменилось"):
                    bot.event_writes.advance(batch, other)
            finally:
                bot.db.close()
        self.assertEqual(other.events, [])


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
