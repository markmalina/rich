# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from dataclasses import replace
import datetime as dt
from decimal import Decimal
import sqlite3
import unittest
from unittest.mock import Mock, patch

from .group_reports import Practice, Publication, Report, ReportJournal, build_report
from .group_telegram import GroupTelegram, configured_groups
from .ledger import MOSCOW
from .providers import ProviderError

DAY = dt.date(2026, 11, 25)


def stamp(day, hour=18, minute=0):
    return int(dt.datetime.combine(day, dt.time(hour, minute), tzinfo=MOSCOW).timestamp())


def row(action, amount, unit, op, resource, day=DAY):
    return [day.isoformat(), "12:00", "частное описание", action, amount, unit, 10, "", "origin",
            53, "частный комментарий", op, resource, "закрытый счёт", "закрытый контрагент"]


class ReportContentTest(unittest.TestCase):
    def test_groups_get_only_agreed_practices_and_distinct_transaction_count(self):
        report = build_report(DAY, [
            row("Приседание", 15, "повт.", "1", ""),
            row("Приседание", 5, "повт.", "2", ""),
            row("Чтение", 12, "мин", "3", "Книга"),
            row("Приобретение", 1, "шт.", "4", "Товар"),
            row("Оплата", -600, "RUB", "4", "Деньги"),
            row("Оплата", -300, "RUB", "4", "Деньги"),
            row("Доход", 10000, "RUB", "5", "Деньги"),
            row("Перевод денег", -2200, "RUB", "6", "Деньги"),
            row("Перевод денег", 2200, "RUB", "6", "Деньги"),
            row("Оплата", -43, "RUB", "7", "Деньги", DAY - dt.timedelta(days=1)),
        ], [Practice("Приседание", "Приседания", "повт.", Decimal(20))], finance=True)
        self.assertEqual(report.text, "Отчёт за 25.11.2026\nПриседания — 20 повт.\nФинансы — операций: 2")
        self.assertTrue(report.meets_minimum)

    def test_report_only_keeps_an_honest_zero_and_absence_is_distinct(self):
        practice = Practice("Приседание", "Приседания", "повт.")
        rows = [row("Приседание", 0, "повт.", "1", "")]
        only = build_report(DAY, rows, [practice])
        self.assertIn("0 повт.", only.text)
        self.assertTrue(only.meets_minimum)
        self.assertFalse(build_report(DAY, rows, [replace(practice, minimum=Decimal(5))]).meets_minimum)
        self.assertIn("нет записи", build_report(DAY, [], [practice]).text)


class ReportJournalTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.journal = ReportJournal(self.db)
        self.issued = stamp(DAY, 22)
        self.journal.membership(-100, 1, True, stamp(DAY, 12), update=1)

    def test_preview_needs_exact_group_viewer_text_and_bot_and_is_counted_once(self):
        report = Report("Отчёт за 25.11.2026", True)
        token = self.journal.prepare(-100, 1, DAY, self.issued, report)
        sent = Publication(-100, 1, 80, self.issued + 1, 7, report.text)
        self.assertEqual(self.journal.history(-100, 1), [])
        for wrong in (replace(sent, chat=-101), replace(sent, author=2), replace(sent, via_bot=8),
                      replace(sent, text="подменённый отчёт")):
            self.assertEqual(self.journal.confirm(token, wrong, 7), "rejected")
        self.assertEqual(self.journal.confirm(token, sent, 7), "accepted")
        self.assertEqual(self.journal.confirm(token, sent, 7), "duplicate")
        self.assertEqual(self.journal.confirm(token, replace(sent, message=81), 7), "rejected")
        self.assertFalse(self.journal.missed(-100, 1, DAY, stamp(DAY + dt.timedelta(days=1), 0)))

    def test_late_review_never_changes_missed_day_and_history_respects_join(self):
        first = Report("Сегодня не приседал", False)
        original = self.journal.prepare(-100, 1, DAY, self.issued, first)
        self.assertEqual(self.journal.confirm(original, Publication(-100, 1, 81, self.issued, 7, first.text), 7),
                         "accepted")
        next_day = DAY + dt.timedelta(days=1)
        after_midnight = stamp(next_day, 0, 1)
        self.journal.membership(-100, 2, True, after_midnight, update=9)
        self.assertEqual(self.journal.history(-100, 2), [])
        self.assertTrue(self.journal.missed(-100, 1, DAY, after_midnight))
        revise = Report("Исправляю результат", True)
        correction = self.journal.prepare(-100, 1, DAY, after_midnight, revise, correction=True)
        self.assertEqual(self.journal.confirm(correction, Publication(-100, 1, 82, after_midnight, 7, revise.text), 7),
                         "accepted")
        self.assertTrue(self.journal.missed(-100, 1, DAY, after_midnight))
        self.assertEqual(len(self.journal.history(-100, 2)), 1)
        self.journal.membership(-100, 2, False, after_midnight+1, update=10)
        self.journal.membership(-100, 2, True, after_midnight, update=9)
        self.assertEqual(self.journal.history(-100, 2), [])
        self.assertEqual(len(self.journal.history(-100, 1)), 2)


class TelegramReportsTest(unittest.TestCase):
    def test_rejects_groups_with_visible_history_or_missing_bot_admin_role(self):
        telegram = Mock()
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        rx = GroupTelegram(telegram, {"id": 9, "username": "TestRX"}, db, {-100: None}, Mock())
        for group, role in [({"type": "supergroup", "has_visible_history": True}, "administrator"),
                            ({"type": "supergroup"}, "member")]:
            telegram.api.side_effect = [group, {"status": role}]
            with self.assertRaisesRegex(ProviderError, "hidden history"):
                rx.verify_groups()
        telegram.api.side_effect = [{"type": "supergroup"}, {"status": "administrator"}]
        rx.verify_groups()

    def test_inline_path_checks_membership_and_confirms_the_telegram_message(self):
        group = {"chat": -100, "name": "Тестовая группа", "tracker": 1, "members": {"1": {"practices": [
            {"action": "Приседание", "label": "Приседания", "unit": "повт."},
        ]}}}
        telegram, sheet = Mock(), Mock()
        sheet.read.return_value = [row("Приседание", 10, "повт.", "1", "")]
        telegram.api.return_value = {"status": "member"}
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        rx = GroupTelegram(telegram, {"id": 9, "username": "TestRX"}, db,
                           configured_groups([group]), lambda user: sheet)
        now = stamp(DAY)
        rx.handle({"update_id": 8, "chat_member": {
            "chat": {"id": -100}, "date": now,
            "new_chat_member": {"user": {"id": 1}, "status": "member"},
        }})
        chat = {"id": -100, "type": "supergroup"}
        rx.handle({"message": {"chat": chat, "from": {"id": 1}, "text": "/report@TestRX"}})
        buttons = telegram.api.call_args.args[1]["reply_markup"]["inline_keyboard"]
        with patch("bot.group_telegram.time.time", return_value=now+1):
            rx.handle({"inline_query": {
                "id": "query", "chat_type": "supergroup", "from": {"id": 1},
                "query": buttons[0][0]["switch_inline_query_current_chat"],
            }})
        method, response = telegram.api.call_args.args
        self.assertEqual(method, "answerInlineQuery")
        self.assertTrue(response["is_personal"])
        self.assertEqual(response["cache_time"], 0)
        self.assertEqual(rx.journal.history(-100, 1), [])
        self.assertIn("Тестовая группа", response["results"][0]["description"])
        content = response["results"][0]["input_message_content"]["message_text"]
        published = {"chat": chat, "message_id": 42, "date": now+2,
                     "from": {"id": 1}, "via_bot": {"id": 9}, "text": content}
        rx.handle({"message": {**published, "forward_origin": {"type": "user"}}})
        self.assertEqual(rx.journal.history(-100, 1), [])
        rx.handle({"message": published})
        rx.handle({"message": published})
        replies = [call for call in telegram.api.call_args_list if call.args[0] == "sendMessage"
                   and "reply_parameters" in call.args[1]]
        self.assertEqual(len(replies), 1)
        self.assertEqual(len(rx.journal.history(-100, 1)), 1)
