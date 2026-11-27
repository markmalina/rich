# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from .coaching import Coaching
from .demo_checkout import DemoCheckout
from .ledger import MOSCOW
from .main import Bot
from .operations import Operations
from .sales import Sales


class DemoCheckoutTest(unittest.TestCase):
    def setUp(self):
        root = TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.tg = Mock()
        self.bot = Bot(self.tg, None, None, Path(root.name), 7)
        self.db = self.bot.db
        self.addCleanup(self.db.close)
        self.bot.pilot_orders = Sales(self.db, self.tg, 7)
        self.bot.coaching = Coaching(self.db, self.tg, 7, demo_only=True)
        self.bot.demo_checkout = DemoCheckout(self.db, self.tg, self.bot.pilot_orders, self.bot.coaching)
        self.update = 0
        self.now = dt.datetime(2026, 11, 27, 20, 0, tzinfo=MOSCOW)

    def update_bot(self, **payload):
        self.update += 1
        with self.db:
            self.db.execute("INSERT INTO receipts VALUES (?,?)", (self.update, self.now.isoformat()))
        self.tg.reset_mock()
        self.bot.process_update({"update_id": self.update, **payload})

    def message(self, user, text):
        self.update_bot(message={"from": {"id": user}, "chat": {"id": user, "type": "private"}, "text": text})

    def callback(self, user, data, *, chat=None, kind="private"):
        self.update_bot(callback_query={"id": str(self.update), "data": data, "from": {"id": user},
                                        "message": {"chat": {"id": user if chat is None else chat, "type": kind}}})

    def screen(self):
        method, body = self.tg.api.call_args.args
        self.assertEqual(method, "sendMessage")
        return body

    def click_label(self, user, text):
        data = next(row[0]["callback_data"] for row in self.screen()["reply_markup"]["inline_keyboard"]
                    if row[0]["text"] == text)
        self.callback(user, data)
        return data

    def test_printed_jar_to_tracking_and_private_practice(self):
        self.message(8, "/start tracking_GR020010")
        self.assertIn("4 200 ₽", self.screen()["text"])
        self.message(8, "/start tracking_GR020011")
        self.assertEqual(self.db.execute("SELECT total,source,demo FROM pilot_orders").fetchall(),
                         [(420000, "GR020010", 1)])
        self.click_label(8, "Оплатить")
        self.assertIn("чек не формируется", self.screen()["text"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_receipts").fetchone(), (0,))
        pay = self.click_label(8, "Я оплатил")
        self.callback(8, pay)
        self.assertIn("день 1 из 21", self.screen()["text"])
        [(recorder, reference)] = self.db.execute("SELECT recorder,reference FROM pilot_receipts").fetchall()
        self.assertEqual(recorder, 8)
        self.assertRegex(reference, r"^demo-self:1:[0-9a-f]{32}$")
        self.assertEqual(self.db.execute("SELECT units,grams FROM pilot_kits").fetchall(), [(21, 80)])
        self.assertEqual(self.db.execute("SELECT count(*) FROM school_enrolments").fetchone(), (0,))
        mark = self.click_label(8, "Отметить: Питание")
        self.callback(8, mark)
        self.assertIn("Дней с отметками: 1 из 21", self.screen()["text"])
        self.callback(9, mark)
        self.assertEqual(self.tg.api.call_args.args[0], "answerCallbackQuery")
        self.assertTrue(self.tg.api.call_args.args[1]["show_alert"])
        self.now += dt.timedelta(days=1)
        self.callback(8, mark)
        self.assertIn("другому дню", self.tg.api.call_args.args[1]["text"])
        self.assertEqual(self.db.execute("SELECT day,practice FROM pilot_tracking_days").fetchall(), [(1, "food")])
        ops = Operations(self.db, self.tg, 7, self.bot.directory)
        self.addCleanup(ops.ledger.close)
        self.assertEqual(ops.export(8)["tracking_days"][0]["practice"], "food")
        self.assertEqual(ops.export(9)["tracking_days"], [])
        self.now += dt.timedelta(days=20)
        self.message(8, "/tracking")
        self.assertIn("завершён", self.screen()["text"])

    def test_school_and_separate_goods_advance_with_own_buttons(self):
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")
        self.assertIn("/school_day 1", self.screen()["text"])
        self.message(8, "/school_day 1")
        self.assertIn("день 1/21", self.tg.message.call_args.args[1].lower())
        self.assertEqual(self.db.execute("SELECT learner,mentor FROM school_enrolments").fetchall(), [(8, 7)])
        self.message(7, "/pilot_goods 8 demo 1 80 350 | sample")
        self.message(8, "/pay")
        self.click_label(8, "Гранола #2 · 350 ₽")
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")
        self.assertIn("/pilot_delivery 2", self.screen()["text"])
        self.message(8, "/pilot_delivery 2")
        self.assertIn("Учебная выдача", self.tg.message.call_args.args[1])
        self.assertIn("Гранола: заявок 1, оплат 1", self.bot.pilot_orders.metrics(True, None))
        self.assertNotIn("оплат 1", self.bot.pilot_orders.metrics(False, None))

    def test_old_operator_reference_does_not_block_a_new_self_confirmation(self):
        self.message(7, "/pilot_goods 8 demo 1 80 350 | sample")
        self.message(7, "/pilot_paid 1 350 | demo-self:2")
        self.message(8, "/start tracking")
        self.click_label(8, "Оплатить")
        paid = self.click_label(8, "Я оплатил")
        self.callback(8, paid)
        self.assertIn("день 1 из 21", self.screen()["text"])
        rows = self.db.execute("SELECT order_id,reference FROM pilot_receipts ORDER BY order_id").fetchall()
        self.assertEqual(rows[0], (1, "demo-self:2"))
        self.assertEqual(len(rows), 2)
        self.assertRegex(rows[1][1], r"^demo-self:2:[0-9a-f]{32}$")

    def test_no_bypass_to_cancelled_live_foreign_or_group_order(self):
        self.message(8, "/start tracking")
        self.callback(7, "dp:paid:1")
        self.assertEqual(self.tg.api.call_args.args[0], "answerCallbackQuery")
        self.callback(8, "dp:paid:1", chat=-42, kind="group")
        self.assertIn("личном чате", self.tg.api.call_args.args[1]["text"])
        self.message(7, "/pilot_cancel 1")
        self.callback(8, "dp:paid:1")
        self.assertEqual(self.tg.api.call_args.args[0], "answerCallbackQuery")
        with self.db:
            self.db.execute("INSERT INTO pilot_orders(buyer,kind,total,demo,seller,created) VALUES (8,'package',990000,0,'Fixture',?)",
                            (self.now.isoformat(),))
        self.callback(8, "dp:paid:2")
        self.message(7, "/pilot_paid 2 9900 | evidence")
        self.assertIn("не открыт", self.tg.message.call_args.args[1])
        self.assertEqual(self.db.execute("SELECT count(*) FROM pilot_receipts").fetchone(), (0,))

    def test_coaching_day_is_simulated_and_excluded_from_live_metrics(self):
        self.message(7, "/coach_invite")
        invite = self.tg.message.call_args.args[1].split()[-1]
        self.message(8, f"/coach_join {invite}")
        self.message(8, "/coach_plan 1 | движение | отчёт | пн,вт,ср,чт,пт,сб,вс | 5 мин | 20:00–21:00")
        self.message(8, "/coach_share_plan 1")
        self.message(7, "/coach_accept 1")
        self.message(7, "/coach_paid 1 2026-11-27 | pretend")
        self.assertIn("только учебная оплата", self.tg.message.call_args.args[1])
        self.message(8, "/pay")
        self.click_label(8, "Оплатить день у трекера #1 · 100 ₽")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coach_payments").fetchone(), (0,))
        confirm = self.click_label(8, "Я оплатил")
        self.callback(8, confirm)
        self.assertIn("/coach_report 1", self.screen()["text"])
        self.assertEqual(self.db.execute("SELECT link,day FROM coach_payments").fetchall(), [(1, "2026-11-27")])
        ops = Operations(self.db, self.tg, 7, self.bot.directory)
        self.addCleanup(ops.ledger.close)
        self.assertIn("Уникальных платящих 0", ops.network(self.now.date()))

        self.now += dt.timedelta(days=1)
        with self.db:
            self.db.execute("INSERT INTO coach_payments VALUES (?,?,?,?,?)",
                            (1, "2026-11-26", "demo-self:20261128", 7, self.now.isoformat()))
        self.message(8, "/pay")
        self.click_label(8, "Оплатить день у трекера #1 · 100 ₽")
        confirm = self.click_label(8, "Я оплатил")
        self.callback(8, confirm)
        self.assertIn("подтверждена", self.screen()["text"])
        [(basis,)] = self.db.execute("SELECT basis FROM coach_payments WHERE link=1 AND day='2026-11-28'").fetchall()
        self.assertRegex(basis, r"^demo-self:20261128:[0-9a-f]{32}$")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coach_payments").fetchone(), (3,))


if __name__ == "__main__":
    unittest.main()
