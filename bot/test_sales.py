# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock

from .ledger import MOSCOW
from .sales import Sales


class SalesTest(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.tg=Mock()
        self.sales=Sales(self.db,self.tg,7)
        self.now=dt.datetime(2026,11,26,22,0,tzinfo=MOSCOW)
        self.update=1

    def call(self,text,user=7,replay=False):
        if not replay:
            self.update+=1
        self.assertTrue(self.sales.handle(self.update,user,{"text":text},self.now))
        return self.tg.message.call_args.args[1]

    def pack(self):
        self.call("/pilot_offer 8 demo | trainer")
        self.call("/pilot_paid 1 9900 | confirmation")

    def test_two_shipments_and_roles_keep_one_kit(self):
        self.pack()
        self.assertIn("21 × 80",self.call("/pilot_delivery 1",8))
        self.assertIn("не найдена",self.call("/pilot_delivery 1",10))
        self.call("/pilot_assign 1 10")
        self.assertIn("Выделено 0",self.call("/pilot_delivery 1",10))
        self.assertIn("Заказ не найден",self.call("/pilot_order 1",10))
        self.assertIn("превышает",self.call("/pilot_stock 1 ship 14 | ship1",10))
        self.call("/pilot_stock 1 reserve 21 | reserve1")
        self.call("/pilot_stock 1 ship 14 | ship1",10)
        self.call("/pilot_stock 1 deliver 14 | delivery1",10)
        self.assertIn("уже учтено",self.call("/pilot_stock 1 deliver 14 | delivery1",10))
        self.assertIn("к вручению 7",self.call("/pilot_delivery 1"))
        self.call("/pilot_stock 1 ship 7 | ship2",10)
        self.call("/pilot_stock 1 deliver 7 | delivery2",10)
        self.assertIn("вручено 21",self.call("/pilot_delivery 1"))
        self.assertIn("превышает",self.call("/pilot_stock 1 deliver 1 | delivery3",10))
        self.call("/pilot_assign 1 off")
        self.assertIn("не найдена",self.call("/pilot_delivery 1",10))

    def test_refund_and_cost_are_explicit_and_idempotent(self):
        self.pack()
        self.call("/pilot_stock 1 reserve 14 | reserve")
        self.call("/pilot_stock 1 ship 14 | shipped")
        self.call("/pilot_stock 1 deliver 14 | delivered")
        self.call("/pilot_refund_request 1",8)
        self.assertIn("в пределах",self.call("/pilot_refund 1 9901 | bad"))
        self.assertIn("подтверждён",self.call("/pilot_refund 1 300 | refund-a"))
        self.assertIn("уже подтверждён",self.call("/pilot_refund 1 300 | refund-a"))
        self.assertIn("остаток к вручению 7",self.call("/pilot_delivery 1").lower())
        self.assertIn("день 1/21",self.call("/school_day 1",8).lower())
        self.call("/pilot_suspend 1 on")
        self.assertIn("приостановлен",self.call("/school_day 1",8))
        self.call("/pilot_cost 1 shipping 400 | carrier")
        self.call("/pilot_cost 1 shipping 400 | carrier")
        text=self.call("/pilot_margin 1")
        for part in ("1 050 ₽","2 100 ₽","7 800 ₽","доставка: 400 ₽","упаковка: неизвестно","9 600 ₽"):
            self.assertIn(part,text)
        self.call("/pilot_goods 8 demo 1 80 350")
        self.assertIn("отменён",self.call("/pilot_cancel 2"))
        self.assertIn("Отменённый",self.call("/pilot_paid 2 350 | void"))

    def test_school_access_operation_requires_school_enrolment(self):
        self.call("/pilot_goods 8 demo 1 80 350")
        self.call("/pilot_paid 1 350 | goods-demo")
        self.call("/pilot_refund_request 1", 8)
        self.call("/pilot_refund 1 100 | goods-return")
        self.assertIn("Учебный доступ по этому заказу отсутствует", self.call("/pilot_suspend 1 on"))
        self.assertIn("350 ₽", self.call("/pilot_order 1"))
        self.assertIn("Возвращено: 100 ₽", self.call("/pilot_order 1"))
        self.assertIn("1 × 80 г", self.call("/pilot_delivery 1"))

    def test_qr_attribution_separate_product_and_partner_privacy(self):
        self.pack()
        self.call("/pilot_qr demo trainer 5 | counts")
        self.call("/pilot_qr demo trainer 5 | counts")
        self.call("/pilot_goods 8 demo 1 80 350 | trainer")
        self.call("/pilot_paid 2 350 | independent")
        self.call("/pilot_stock 2 reserve 1 | stock")
        self.call("/pilot_stock 2 ship 1 | ship")
        self.call("/pilot_stock 2 deliver 1 | received")
        self.assertIn("вручено 1",self.call("/pilot_delivery 2",8))
        summary=self.call("/pilot_metrics demo | trainer")
        for s in ("QR, внешняя сверка: 5","Гранола: заявок 1, оплат 1","Школа: заявок 1, оплат 1"):
            self.assertIn(s,summary)
        self.assertNotIn("Школа",self.call("/pilot_metrics live"))
        self.call("/pilot_partner 11 | trainer")
        self.assertIn("Рабочие заказы",self.call("/pilot_metrics live",11))
        self.assertIn("недоступен",self.call("/pilot_metrics demo",11))
        self.assertIn("недоступен",self.call("/pilot_metrics live | foreign",11))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM school_enrolments").fetchone(),(1,))
