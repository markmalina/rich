# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from concurrent.futures import ThreadPoolExecutor
import datetime as dt
import json
from pathlib import Path
from queue import Queue
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from .coaching import Coaching
from .ledger import MOSCOW
from .operations import Operations, apply_privacy, copy_database
from .sales import Sales


class OperationsTest(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)
        self.db=sqlite3.connect(self.root/"main.sqlite")
        self.addCleanup(self.db.close)
        self.tg=Mock()
        self.sales=Sales(self.db,self.tg,7)
        self.ops=Operations(self.db,self.tg,7,self.root)
        self.addCleanup(self.ops.ledger.close)
        self.now=dt.datetime(2026,11,26,20,0,tzinfo=MOSCOW)
        self.update=1

    def call(self,user,text):
        self.update+=1
        self.assertTrue(self.ops.handle(self.update,user,{"text":text},self.now)
                        or self.sales.handle(self.update,user,{"text":text},self.now))
        return self.tg.message.call_args.args[1] if self.tg.message.call_args else ""

    def enrol(self,learner,ref):
        self.call(7,f"/pilot_offer {learner} demo | referral")
        order=self.db.execute("SELECT MAX(id) FROM pilot_orders").fetchone()[0]
        self.call(7,f"/pilot_paid {order} 9900 | {ref}")
        return order

    def test_scoped_export_and_older_backup_respects_privacy_ledger(self):
        a,b=self.enrol(8,"receipt-a"),self.enrol(9,"receipt-b")
        self.call(8,f"/school_work {a} 1 | only A")
        self.call(9,f"/school_work {b} 1 | only B")
        self.call(8,"/pilot_export")
        packet=self.tg.api.call_args.kwargs["files"]["document"][1].getvalue()
        self.assertIn("only A",packet.decode())
        self.assertNotIn("only B",packet.decode())
        self.assertNotIn("receipt-a",packet.decode())
        self.assertEqual(json.loads(packet)["orders"][0]["order_id"],a)

        self.ops.run_daily(self.now)
        self.assertIn("2026-11-26",self.ops.health())
        source=self.ops.snapshot(self.now.date())
        self.assertEqual(source.stat().st_mode & 0o777,0o600)
        self.assertIn("нет",self.call(7,"/pilot_purge_school 8"))
        self.call(8,"/pilot_privacy school")
        self.assertIn("права",self.call(9,"/pilot_purge_school 8"))
        self.assertIn("обработаны",self.call(7,"/pilot_purge_school 8"))
        restored_path=self.root/"restored.sqlite"
        old=sqlite3.connect(source)
        self.addCleanup(old.close)
        copy_database(old,restored_path)
        with self.assertRaises(FileExistsError):
            copy_database(old,restored_path)
        restored=sqlite3.connect(restored_path)
        self.addCleanup(restored.close)
        with restored:
            apply_privacy(restored,self.ops.ledger)
        self.assertEqual(restored.execute("SELECT learner,text FROM school_work").fetchall(),[(9,"only B")])
        self.assertEqual(restored.execute("SELECT COUNT(*) FROM pilot_receipts").fetchone(),(2,))
        self.assertEqual(restored.execute("SELECT suspended FROM school_enrolments WHERE learner=8").fetchone(),(1,))
        self.now+=dt.timedelta(days=1)
        self.call(8,"/pilot_privacy school")
        with self.db:
            apply_privacy(self.db,self.ops.ledger)
        self.assertIsNone(self.db.execute("SELECT resolved FROM privacy_requests WHERE user=8").fetchone()[0])

    def test_network_counts_unique_payors_and_hides_demo(self):
        Coaching(self.db,self.tg,7)
        with self.db:
            for tracker,client,demo in [(7,i,0) for i in range(100,110)]+[(70,i,0) for i in range(110,130)]+[(70,100,0),(7,999,1)]:
                link=self.db.execute("INSERT INTO coach_links(client,tracker,state,demo) VALUES (?,?,'active',?)",
                                     (client,tracker,demo)).lastrowid
                self.db.execute("INSERT INTO coach_payments VALUES (?,?,?,?,?)",
                                (link,str(self.now.date()),str(link),tracker,self.now.isoformat()))
        self.assertIn("Уникальных платящих 30. Оплаченных назначений 31. У владельца 10.",
                      self.call(7,"/pilot_network 2026-11-26"))
        with patch("bot.operations.shutil.disk_usage",return_value=Mock(total=10**9,free=100)):
            self.assertIn("Мало места",self.call(7,"/pilot_health"))
        self.assertIn("права",self.call(8,"/pilot_health"))

    def test_parallel_producers_and_single_worker_preserve_one_order_per_update(self):
        queue=Queue()
        incoming=list(range(100))
        with ThreadPoolExecutor(max_workers=10) as producers:
            list(producers.map(queue.put,incoming))
        while not queue.empty():
            i=queue.get_nowait()
            for _ in range(2):
                self.sales.handle(500+i,7,{"text":f"/pilot_offer {100+i%30} demo | ref"},self.now)
        count, profiles=self.db.execute("SELECT COUNT(*),COUNT(DISTINCT buyer) FROM pilot_orders").fetchone()
        self.assertEqual((count,profiles),(100,30))
        self.assertIn("Заказ не найден",self.call(555,"/pilot_order 1"))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_receipts").fetchone(),(0,))

    def test_end_to_end_referral_private_training_paid_day_and_repeat_product(self):
        order=self.enrol(8,"course-receipt")
        for day in (7,14,21):
            self.now=dt.datetime(2026,11,26,20,0,tzinfo=MOSCOW)+dt.timedelta(days=day-1)
            self.call(8,f"/school_work {order} {day} | private content {day}")
        self.assertIn("подтверждено",self.call(7,f"/school_finish {order}"))
        self.assertIn("Курс завершён",self.call(8,f"/school_status {order}"))
        coach=Coaching(self.db,self.tg,7)

        def command(user,text):
            self.update+=1
            coach.handle(self.update,user,{"text":text},self.now)
            return self.tg.message.call_args.args[1]

        command(7,"/coach_allow 8")
        code=command(8,"/coach_invite").split()[-1]
        command(9,f"/coach_join {code}")
        command(9,"/coach_plan 1 | движение | отчёт | пн,вт,ср,чт,пт,сб,вс | 5 мин | 20:00–21:00")
        command(9,"/coach_share_plan 1")
        self.assertIn("Условия приняты",command(8,"/coach_accept 1"))
        command(9,"/coach_report 1 | да")
        command(9,"/coach_share 1")
        self.assertIn("сохранено",command(8,f"/coach_paid 1 {self.now.date()} | day-receipt"))
        self.assertIn("Реальных клиентов: 1. Подтверждено оплаченных дней: 1",self.call(8,f"/school_status {order}"))
        for action in ("reserve","ship","deliver"):
            self.call(7,f"/pilot_stock {order} {action} 21 | kit-{action}")
        self.assertIn("вручено 21",self.call(8,f"/pilot_delivery {order}"))
        self.call(7,"/pilot_goods 8 demo 1 80 350 | referral")
        self.call(7,"/pilot_paid 2 350 | repeat-receipt")
        self.assertIn("Гранола: заявок 1, оплат 1",self.call(7,"/pilot_metrics demo | referral"))
        self.assertEqual(self.db.execute("SELECT count(*) FROM school_enrolments").fetchone(),(1,))
