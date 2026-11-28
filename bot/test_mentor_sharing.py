# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock

from .ledger import MOSCOW
from .pilot import Pilot


class MentorSharingTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.telegram = Mock()
        self.pilot = Pilot(self.db, self.telegram, 7)
        self.now = dt.datetime(2026, 11, 28, 9, tzinfo=MOSCOW)
        self.update_id = 0
        self.command(7, "/pilot_offer 8 demo")
        self.command(7, "/pilot_paid 1 9900 | demo")

    def command(self, user, text):
        self.update_id += 1
        self.assertTrue(self.pilot.handle(self.update_id, user, {"text": text}, self.now))
        return self.telegram.message.call_args.args[1]

    def test_reassignment_limits_history_and_feedback_to_the_current_recipient(self):
        self.command(8, "/school_work 1 1 | личный текст")
        self.command(8, "/school_share 1 1 | первая работа")
        self.command(7, "/school_feedback 1 1 | первый комментарий")
        self.command(7, "/pilot_mentor 1 7")
        self.assertIn("первая работа", self.command(7, "/school_history 1"))

        self.command(7, "/pilot_mentor 1 10")
        self.assertIn("не найдено", self.command(7, "/school_history 1"))
        self.assertIn("нет", self.command(10, "/school_history 1"))
        self.assertIn("не передал", self.command(10, "/school_feedback 1 1 | ответ"))
        self.command(8, "/school_share 1 1 | новая версия")
        self.command(10, "/school_feedback 1 1 | новый комментарий")
        new_history = self.command(10, "/school_history 1")
        for visible in ("новая версия", "новый комментарий"):
            self.assertIn(visible, new_history)
        for hidden in ("личный текст", "первая работа", "первый комментарий"):
            self.assertNotIn(hidden, new_history)
            self.assertIn(hidden, self.command(8, "/school_history 1"))

        self.command(7, "/pilot_mentor 1 7")
        self.assertIn("нет", self.command(7, "/school_history 1"))
        self.assertIn("не передал", self.command(7, "/school_feedback 1 1 | новый ответ"))

    def test_legacy_shares_and_comments_survive_without_assuming_a_recipient(self):
        for table, column in (("school_work", "shared_with"), ("school_work", "mentor_revision"),
                              ("school_comments", "mentor_revision"), ("school_enrolments", "mentor_revision")):
            self.db.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        with self.db:
            self.db.execute("INSERT INTO school_work VALUES (1,1,30,8,'прежняя работа',1,?)", (self.now.isoformat(),))
            self.db.execute("INSERT INTO school_comments VALUES (1,1,31,7,'прежний отзыв',?)", (self.now.isoformat(),))
            self.db.execute("UPDATE school_enrolments SET finished=? WHERE order_id=1", (self.now.isoformat(),))
        self.pilot = Pilot(self.db, self.telegram, 7)
        self.pilot = Pilot(self.db, self.telegram, 7)
        learner_history = self.command(8, "/school_history 1")
        self.assertIn("прежняя работа", learner_history)
        self.assertIn("прежний отзыв", learner_history)
        self.assertIn("нет", self.command(7, "/school_history 1"))
        self.assertIn("не передал", self.command(7, "/school_feedback 1 1 | ответ"))
        self.assertIn("сохранённую", self.command(8, "/school_share 1 1 | совсем другая работа"))
        self.assertIn("передана", self.command(8, "/school_share 1 1 | прежняя работа"))
        mentor_history = self.command(7, "/school_history 1")
        self.assertIn("прежняя работа", mentor_history)
        self.assertNotIn("прежний отзыв", mentor_history)
