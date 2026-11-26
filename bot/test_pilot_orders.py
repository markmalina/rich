# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock, patch

from .coaching import Coaching
from .ledger import MOSCOW
from .pilot import Pilot
from .school import PROGRAMS, VERSION


class SchoolPilotTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.telegram=Mock()
        self.pilot=Pilot(self.db,self.telegram,7)
        self.now=dt.datetime(2026,11,26,20,0,tzinfo=MOSCOW)
        self.update=100

    def cmd(self,user,text,advance=0,replay=False):
        if not replay:
            self.update+=1
        self.assertTrue(self.pilot.handle(self.update,user,{"text":text},self.now+dt.timedelta(days=advance)))
        return self.telegram.message.call_args.args[1]

    def test_one_order_opens_one_seat_and_one_kit_and_preserves_quote(self):
        self.assertIn("не открыт",self.cmd(7,"/pilot_offer 8 live"))
        self.assertIn("9 900",self.cmd(7,"/pilot_offer 8 demo | trener-ref"))
        self.assertIn("Заказ не найден",self.cmd(9,"/pilot_order 1"))
        self.assertIn("ждёт сверки",self.cmd(8,"/pilot_order 1"))
        self.assertIn("ещё ждёт",self.cmd(8,"/school_day 1"))
        self.assertIn("отличается",self.cmd(7,"/pilot_paid 1 9899 | demo-a"))
        self.assertIn("Требуется",self.cmd(8,"/pilot_paid 1 9900 | demo-a"))
        paid="/pilot_paid 1 9900 | demo-a"
        self.assertIn("21 × 80",self.cmd(7,paid))
        self.assertIn("21 × 80",self.cmd(7,paid,replay=True))
        self.assertIn("уже учтена",self.cmd(7,paid))
        self.pilot.price=1_100_000
        self.assertIn("11 000",self.cmd(7,"/pilot_offer 8 demo | other"))
        self.assertIn("другому заказу",self.cmd(7,"/pilot_paid 2 11000 | demo-a"))
        self.assertIn("9 900",self.cmd(8,"/pilot_order 1"))
        self.assertEqual(self.db.execute("SELECT units,grams FROM pilot_kits").fetchall(),[(21,80)])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM school_enrolments").fetchone(),(1,))
        self.assertEqual(self.db.execute("SELECT source FROM pilot_orders WHERE id=1").fetchone(),("trener-ref",))

    def test_day_gates_privacy_curriculum_version_and_completion(self):
        self.cmd(7,"/pilot_offer 8 demo")
        self.cmd(7,"/pilot_paid 1 9900 | demo-a")
        for n,label in [(1,"Опыт клиента"),(7,"Опыт клиента"),(8,"Сопровождение"),(14,"Сопровождение"),
                        (15,"Продвижение"),(21,"Продвижение")]:
            self.assertIn(label,self.cmd(8,f"/school_day 1 {n}",n-1))
        self.assertIn("ещё не открыт",self.cmd(8,"/school_day 1 8",6))
        self.assertIn("итоговые работы",self.cmd(7,"/school_finish 1",20))
        for day in (7,14,21):
            c=f"/school_work 1 {day} | Непубличный черновик"
            self.cmd(8,c,20)
            self.cmd(8,c,20,replay=True)
        self.assertNotIn("Непубличный черновик",self.cmd(7,"/school_history 1",20))
        self.assertIn("не передал",self.cmd(7,"/school_feedback 1 14 | к практике",20))
        self.cmd(8,"/school_share 1 14 | Учебная работа для наставника",20)
        self.cmd(7,"/school_feedback 1 14 | Договорённость понятна",20)
        self.assertIn("Договорённость понятна",self.cmd(8,"/school_history 1",20))
        self.assertIn("завершение",self.cmd(7,"/school_finish 1",20).lower())
        self.assertIn("Курс завершён",self.cmd(8,"/school_status 1",20))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM school_work").fetchone(),(4,))
        self.cmd(7,"/pilot_mentor 1 10",20)
        self.assertIn("не найдено",self.cmd(7,"/school_history 1",20))
        self.assertIn("Учебная работа",self.cmd(10,"/school_history 1",20))
        with patch.dict(PROGRAMS, {"v2":PROGRAMS[VERSION]}), patch("bot.school.VERSION","v2"):
            self.cmd(7,"/pilot_offer 8 demo",20)
            self.cmd(7,"/pilot_paid 2 9900 | demo-b",20)
            self.assertIn(VERSION,self.cmd(8,"/school_day 1 21",20))
            self.assertIn("v2",self.cmd(8,"/school_day 2 1",20))

    def test_course_status_uses_collected_coaching_payments_not_promises_or_demos(self):
        self.cmd(7,"/pilot_offer 8 demo")
        self.cmd(7,"/pilot_paid 1 9900 | demo-a")
        Coaching(self.db,self.telegram,7)
        with self.db:
            for demo,client in ((1,10),(0,11)):
                link=self.db.execute("INSERT INTO coach_links(client,tracker,state,demo) VALUES (?,8,'active',?)",
                                     (client,demo)).lastrowid
                self.db.execute("INSERT INTO coach_calendar VALUES (?,?,1)",(link,"2026-11-26"))
                if demo:
                    self.db.execute("INSERT INTO coach_payments VALUES (?,?,?,?,?)",
                                    (link,"2026-11-26","simulation",8,self.now.isoformat()))
        self.assertIn("Реальных клиентов: 1. Подтверждено оплаченных дней: 0",self.cmd(8,"/school_status 1"))
        with self.db:
            self.db.execute("INSERT INTO coach_payments VALUES (2,?,?,8,?)",
                            ("2026-11-26","reconciled",self.now.isoformat()))
        self.assertIn("Подтверждено оплаченных дней: 1",self.cmd(8,"/school_status 1"))
