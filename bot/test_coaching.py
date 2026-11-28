# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock

from .coaching import Coaching
from .ledger import MOSCOW
from .providers import ProviderError


class CoachingTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.telegram = Mock()
        self.app = Coaching(self.db, self.telegram, 7)
        self.update = 100
        self.now = dt.datetime(2026, 11, 26, 20, 0, tzinfo=MOSCOW)

    def cmd(self, user, text, at=None, replay=False):
        if not replay:
            self.update += 1
        at = at or self.now
        self.assertTrue(self.app.handle(self.update, user, {"text": text, "date": int(at.timestamp())}, at))
        return self.telegram.message.call_args.args[1]

    def pair(self, client=8, tracker=7, criterion="минимум 10"):
        code = self.cmd(tracker, "/coach_invite demo").split()[-1]
        accepted = self.cmd(client, f"/coach_join {code}")
        self.assertIn("Учебная связь", accepted)
        link = int(accepted.split("#")[1].split(".")[0])
        self.assertIn("Предпросмотр", self.cmd(client, f"/coach_plan {link} | приседания | {criterion} | "
                                                "пн,вт,ср,чт,пт,сб,вс | пять спокойно | 20:00–21:00"))
        self.assertIn("не передал", self.cmd(tracker, f"/coach_view {link}"))
        self.assertIn("доступны", self.cmd(client, f"/coach_share_plan {link}"))
        self.assertIn("Условия приняты", self.cmd(tracker, f"/coach_accept {link}"))
        self.assertIn("недействительно", self.cmd(11, f"/coach_join {code}"))
        return link

    def test_private_preview_feedback_revocation_and_related_link_isolation(self):
        link = self.pair()
        self.assertIn("Предпросмотр", self.cmd(8, f"/coach_report {link} | 12"))
        self.assertIn("отметки нет", self.cmd(7, f"/coach_view {link}"))
        self.assertIn("не найдена", self.cmd(9, f"/coach_view {link}"))
        self.assertIn("Отметка доступна", self.cmd(8, f"/coach_share {link}"))
        self.assertIn("Отметка доступна", self.cmd(8, f"/coach_share {link}", replay=True))
        self.assertEqual(self.db.execute("SELECT count(*) FROM coach_marks").fetchone(), (1,))
        self.assertIn("отдельно", self.cmd(7, f"/coach_self {link} | 20"))
        self.cmd(7, f"/coach_feedback {link} | Виден результат")
        self.cmd(7, f"/coach_week {link} | Начинай сразу после обеда | 2026-12-03")
        shown = self.cmd(8, f"/coach_view {link}")
        self.assertIn("Виден результат", shown)
        self.assertIn("Начинай сразу после обеда", shown)
        self.assertNotIn("Своя отметка трекера", shown)
        self.assertIn("12", self.cmd(7, f"/coach_view {link}"))
        self.assertIn("отозван", self.cmd(8, f"/coach_end {link}"))
        self.assertIn("Доступ по связи закрыт", self.cmd(7, f"/coach_view {link}"))

    def test_calendar_and_reconciliation_are_independent_of_reports(self):
        self.assertIn("роль", self.cmd(10, "/coach_invite").lower())
        self.assertIn("назначена", self.cmd(7, "/coach_allow 10"))
        a = self.pair()
        b = self.pair(tracker=10)
        self.assertIn("100 ₽. Подтверждено: 0", self.cmd(7, f"/coach_days {a}"))
        self.assertIn("100 ₽. Подтверждено: 0", self.cmd(10, f"/coach_days {b}"))
        self.assertIn("27.11.2026", self.cmd(8, f"/coach_pause {a}"))
        self.assertIn("100 ₽. Подтверждено: 0", self.cmd(8, f"/coach_days {a}"))
        next_day = self.now+dt.timedelta(days=1)
        self.assertIn("дней: 0", self.cmd(7, f"/coach_days {a}", next_day))
        self.assertIn("дней: 1", self.cmd(10, f"/coach_days {b}", next_day))
        self.assertIn("не является", self.cmd(8, f"/coach_report {a} | 10", next_day))
        self.assertIn("недоступна", self.cmd(8, f"/coach_paid {a} 2026-11-26 | X"))
        self.assertIn("сохранено", self.cmd(7, f"/coach_paid {a} 2026-11-26 | X"))
        self.assertIn("уже подтверждена", self.cmd(7, f"/coach_paid {a} 2026-11-26 | X"))
        self.assertIn("другому основанию", self.cmd(7, f"/coach_paid {a} 2026-11-26 | Y"))
        self.assertIn("Подтверждено: 1 · 100 ₽", self.cmd(8, f"/coach_days {a}"))

    def test_moscow_midnight_reminders_and_confirmed_manual_mark(self):
        link = self.pair()
        before = self.now.replace(hour=23, minute=59, second=59)
        midnight = before+dt.timedelta(seconds=1)
        self.cmd(8, f"/coach_report {link} | нет", before)
        self.cmd(8, f"/coach_share {link}", before)
        self.assertIn("Первая сдача: вовремя", self.cmd(7, f"/coach_view {link}", before))
        self.assertIn("Выполнен вовремя: нет", self.cmd(7, f"/coach_view {link}", before))
        self.cmd(7, f"/coach_manual {link} 2026-11-26 | 12 | договорились в личном разговоре", midnight)
        self.assertIn("Предпросмотр", self.cmd(8, f"/coach_view {link}", midnight))
        self.assertIn("поздняя", self.cmd(8, f"/coach_share {link}", midnight))
        history = self.cmd(7, f"/coach_view {link} 2026-11-26", midnight)
        self.assertIn("Выполнен вовремя: нет", history)
        self.assertIn("Минимум по последней отметке: да", history)
        self.assertIn("Ручная отметка подтверждена клиентом", history)
        self.assertIn("Напоминание включено", self.cmd(8, f"/coach_reminder {link} 21:00"))
        self.telegram.message.side_effect = ProviderError("Telegram", "connection")
        self.app.send_due(self.now.replace(hour=21))
        self.telegram.message.side_effect = None
        self.assertIn("результат неизвестен", self.cmd(8, f"/coach_view {link}"))
        self.cmd(8, f"/coach_reminder {link} off")
        self.telegram.message.reset_mock()
        self.app.send_due(next_day := self.now+dt.timedelta(days=1, hours=2))
        self.telegram.message.assert_not_called()
        self.cmd(8, f"/coach_reminder {link} 21:00", next_day)
        self.telegram.message.reset_mock()
        self.app.send_due(next_day)
        self.app.send_due(next_day)
        self.telegram.message.assert_called_once()

    def test_reminders_page_only_links_due_on_the_moscow_calendar(self):
        # Friday in Moscow begins while it is still Thursday in UTC.
        midnight = dt.datetime(2026, 11, 26, 21, tzinfo=dt.timezone.utc)
        with self.db:
            for client in range(100, 155):
                weekdays = "чт" if client < 115 else "пт"
                link = self.db.execute("""
                    INSERT INTO coach_links(client,tracker,state,demo,weekdays,reminder)
                    VALUES (?,7,'active',1,?,'00:00')
                """, (client, weekdays)).lastrowid
                self.app.schedule(link, dt.date(2026, 11, 25), True)
                if 115 <= client < 125:
                    self.app.schedule(link, dt.date(2026, 11, 26), False)
                    self.app.schedule(link, dt.date(2026, 11, 28), True)

        def recipients():
            return [entry.args[0] for entry in self.telegram.message.call_args_list]

        self.app.send_due(midnight.astimezone(MOSCOW))
        self.assertEqual(recipients(), list(range(125, 150)))
        self.app.send_due(midnight)
        self.assertEqual(recipients(), list(range(125, 155)))
        self.app.send_due(midnight)
        self.assertEqual(recipients(), list(range(125, 155)))
        self.assertEqual(self.db.execute("SELECT DISTINCT day FROM coach_notices").fetchall(),
                         [("2026-11-27",)])

    def test_reminders_require_admission_of_client_and_tracker_in_closed_worker(self):
        self.pair()
        self.cmd(8, "/coach_reminder 1 20:00")
        self.telegram.reset_mock()
        self.app.send_due(self.now.replace(hour=21), admitted={7})
        self.app.send_due(self.now.replace(hour=21), admitted={8})
        self.telegram.message.assert_not_called()
        self.app.send_due(self.now.replace(hour=21), admitted={7, 8})
        self.telegram.message.assert_called_once()
        self.assertEqual(self.telegram.message.call_args.args[0], 8)

    def test_reporting_without_minimum_counts_an_honest_no_and_uses_actual_share_time(self):
        link = self.pair(criterion="отчёт")
        before = self.now.replace(hour=23, minute=59, second=59)
        midnight = before+dt.timedelta(seconds=1)
        self.cmd(8, f"/coach_report {link} | нет", before)
        self.assertIn("поздняя", self.cmd(8, f"/coach_share {link}", midnight))
        result = self.cmd(7, f"/coach_view {link} 2026-11-26", midnight)
        self.assertIn("Первая сдача: поздно", result)
        self.assertIn("Последняя отметка: нет", result)
        self.assertNotIn("Минимум", result)
        self.assertIn("Укажи ID", self.cmd(8, "/coach_report", midnight))
