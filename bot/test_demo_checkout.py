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
from .main import ADMISSION_REQUIRED, ADMISSION_SETTING, Bot, private_admission
from .operations import Operations
from .providers import ProviderError
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
        self.bot.coaching = Coaching(self.db, self.tg, 7, demo_only=True,
                                     admitted=self.bot.has_private_access)
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

    def test_closed_intake_blocks_qr_and_previous_checkout_button(self):
        self.bot.admitted = frozenset({7, 8})
        self.message(8, "/start tracking_GR020010")
        button = self.click_label(8, "Оплатить")
        self.assertTrue(button.startswith("dp:"))
        confirm = self.screen()["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.bot.admitted = frozenset({7})
        self.callback(8, confirm)
        self.assertIn(ADMISSION_REQUIRED, self.tg.api.call_args.args[1]["text"])
        self.message(9, "/start tracking_GR020011")
        self.assertEqual(self.tg.message.call_args.args, (9, ADMISSION_REQUIRED))
        self.assertEqual(self.db.execute("SELECT buyer,state FROM pilot_orders").fetchall(), [(8, "awaiting")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_receipts").fetchone(), (0,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_kits").fetchone(), (0,))

    def test_revoked_tracker_hides_and_rejects_previous_day_button(self):
        self.bot.admitted = frozenset({7, 8, 9})
        self.message(7, "/coach_allow 9")
        self.message(9, "/coach_invite demo")
        code = self.tg.message.call_args.args[1].split()[-1]
        self.message(8, f"/coach_join {code}")
        self.message(8, "/coach_plan 1 | движение | отчёт | пн,вт,ср,чт,пт,сб,вс | 5 мин | 20:00–21:00")
        self.message(8, "/coach_share_plan 1")
        self.message(9, "/coach_accept 1")
        self.message(8, "/pay")
        stale = next(row[0]["callback_data"] for row in self.screen()["reply_markup"]["inline_keyboard"]
                     if "Оплатить день" in row[0]["text"])

        self.bot.admitted = frozenset({7, 8})
        self.message(8, "/pay")
        self.assertFalse(any("Оплатить день" in row[0]["text"]
                             for row in self.screen()["reply_markup"]["inline_keyboard"]))
        self.callback(8, stale)
        self.assertIn("нет", self.tg.api.call_args.args[1]["text"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coach_payments").fetchone(), (0,))

    def test_closed_configuration_distinguishes_empty_and_invalid(self):
        self.assertIsNone(private_admission({}))
        self.assertEqual(private_admission({ADMISSION_SETTING: "[]"}), frozenset())
        self.assertEqual(private_admission({ADMISSION_SETTING: "[8, 9, 8]"}), {8, 9})
        for invalid in ("", "null", "8", "[true]", '["8"]', "[-1]", "[9223372036854775808]"):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                private_admission({ADMISSION_SETTING: invalid})

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

    def test_first_known_qr_is_kept_on_an_unpaid_order_and_purchase_freezes_source(self):
        self.message(8, "/pay")
        self.click_label(8, "Трекинг · 4 200 ₽")
        self.assertEqual(self.db.execute("SELECT source FROM pilot_orders").fetchall(), [(None,)])
        self.message(8, "/start tracking_GR020010")
        self.message(8, "/start tracking_GR020011")
        self.assertEqual(self.db.execute("SELECT source FROM pilot_orders").fetchall(), [("GR020010",)])
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")

        self.message(9, "/pay")
        self.click_label(9, "Трекинг · 4 200 ₽")
        self.click_label(9, "Оплатить")
        self.click_label(9, "Я оплатил")
        self.message(9, "/start tracking_GR020011")
        self.assertEqual(self.db.execute("SELECT buyer,source,state FROM pilot_orders ORDER BY id").fetchall(),
                         [(8, "GR020010", "paid"), (9, None, "paid")])

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

    def test_first_seven_school_days_do_not_offer_a_separate_coaching_payment(self):
        self.message(7, "/pilot_cohort_create first-2026 2026-11-27 demo")
        self.bot.demo_checkout.school_cohort = "first-2026"
        self.bot.demo_checkout.consent_version = "terms-v1"
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")

        self.message(7, "/coach_invite demo")
        code = self.tg.message.call_args.args[1].split()[-1]
        self.message(8, f"/coach_join {code}")
        self.message(8, "/coach_plan 1 | движение | отчёт | пн,вт,ср,чт,пт,сб,вс | 5 мин | 20:00–21:00")
        self.message(8, "/coach_share_plan 1")
        self.message(7, "/coach_accept 1")
        self.message(8, "/pay")
        self.assertFalse(any("Оплатить день" in row[0]["text"]
                             for row in self.screen()["reply_markup"]["inline_keyboard"]))
        self.callback(8, "dp:coachpay:1:20261127")
        self.assertIn("нет", self.tg.api.call_args.args[1]["text"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coach_payments").fetchone(), (0,))

        self.now += dt.timedelta(days=7)
        self.message(8, "/pay")
        self.assertTrue(any("Оплатить день" in row[0]["text"]
                            for row in self.screen()["reply_markup"]["inline_keyboard"]))
        self.assertEqual(self.db.execute("""
            SELECT starts,ends,consent_version FROM school_included_coaching s
            JOIN school_order_cohorts c ON c.order_id=s.order_id
        """).fetchone(), ("2026-11-27", "2026-12-03", "terms-v1"))
        with self.db:
            self.db.execute("UPDATE school_enrolments SET suspended=1 WHERE order_id=1")
        self.now -= dt.timedelta(days=7)
        self.message(8, "/pay")
        self.assertTrue(any("Оплатить день" in row[0]["text"]
                            for row in self.screen()["reply_markup"]["inline_keyboard"]))

    def test_missing_configured_cohort_does_not_leave_an_orphan_order(self):
        self.bot.demo_checkout.school_cohort = "missing-cohort"
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.assertIn("не найден", self.tg.api.call_args.args[1]["text"])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_orders").fetchone(), (0,))

    def test_paid_school_screen_tracks_access_after_refund(self):
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.click_label(8, "Оплатить")
        confirm = self.click_label(8, "Я оплатил")
        self.message(8, "/pilot_refund_request 1")
        self.message(7, "/pilot_refund 1 100 | partial")
        self.message(8, "/school_share 1 1 | Тестовая работа")
        preview = self.tg.message.call_args.args[1]
        share = preview[preview.index("/school_confirm_share "):].rstrip(".")
        self.message(7, "/pilot_suspend 1 on")

        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        suspended = self.screen()["text"]
        self.assertIn("приостановлен", suspended)
        self.assertNotIn("/school_day", suspended)
        self.assertIn("/pilot_delivery 1", suspended)
        self.callback(8, confirm)
        self.assertEqual(self.screen()["text"], suspended)
        self.message(8, "/school_day 1")
        self.assertIn("приостановлен", self.tg.message.call_args.args[1])

        self.message(7, "/pilot_suspend 1 off")
        self.callback(8, confirm)
        self.assertIn("/school_day 1", self.screen()["text"])
        self.message(8, share)
        self.assertIn("устарело", self.tg.message.call_args.args[1])
        self.assertEqual(self.db.execute("SELECT shared FROM school_work").fetchall(), [(0,)])
        self.assertEqual(self.db.execute("SELECT amount FROM pilot_receipts").fetchall(), [(990000,)])
        self.assertEqual(self.db.execute("SELECT amount FROM pilot_refunds").fetchall(), [(10000,)])
        self.assertEqual(self.db.execute("SELECT units,grams FROM pilot_kits").fetchall(), [(21, 80)])

    def test_school_link_requires_own_enrolment_and_an_open_lesson(self):
        self.message(7, "/pilot_offer 8 demo")
        self.message(7, "/pilot_paid 1 9900 2026-11-28 | next-cohort")
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.assertIn("2026-11-28", self.screen()["text"])
        self.assertNotIn("/school_day", self.screen()["text"])

        self.now += dt.timedelta(days=1)
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.assertIn("/school_day 1", self.screen()["text"])

        with self.db:
            self.db.execute("DELETE FROM school_enrolments WHERE order_id=1")
        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.assertIn("сверки с оператором", self.screen()["text"])
        self.assertNotIn("/school_day", self.screen()["text"])

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

    def test_private_referral_to_school_coaching_and_delivery_in_demo_mode(self):
        self.message(8, "/start tracking_GR020010")
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")
        self.click_label(8, "Отметить: Питание")
        self.assertIn("Дней с отметками: 1 из 21", self.screen()["text"])

        self.message(8, "/pay")
        self.click_label(8, "Школа и продвижение · 9 900 ₽")
        self.click_label(8, "Оплатить")
        confirm = self.click_label(8, "Я оплатил")
        self.callback(8, confirm)
        self.assertIn("/school_day 2", self.screen()["text"])
        for day in (7, 14, 21):
            self.now = dt.datetime(2026, 11, 27, 20, 0, tzinfo=MOSCOW) + dt.timedelta(days=day - 1)
            self.message(8, f"/school_work 2 {day} | Личный пример {day}")
            self.assertIn("Личный черновик", self.tg.message.call_args.args[1])
        self.message(7, "/school_history 2")
        self.assertIn("Доступных работ", self.tg.message.call_args.args[1])
        self.message(7, "/school_finish 2")
        self.assertIn("подтверждено", self.tg.message.call_args.args[1])

        self.message(7, "/coach_allow 8")
        self.message(8, "/coach_invite demo")
        invite = self.tg.message.call_args.args[1].split()[-1]
        self.message(9, f"/coach_join {invite}")
        self.message(9, "/coach_plan 1 | движение | отчёт | пн,вт,ср,чт,пт,сб,вс | 5 мин | 20:00–21:00")
        self.message(9, "/coach_share_plan 1")
        self.message(8, "/coach_accept 1")
        self.message(9, "/pay")
        self.click_label(9, "Оплатить день у трекера #1 · 100 ₽")
        confirm = self.click_label(9, "Я оплатил")
        self.callback(9, confirm)
        self.assertIn("/coach_report 1", self.screen()["text"])
        self.message(9, "/coach_report 1 | да")
        self.message(8, "/school_status 2")
        self.assertIn("Курс завершён", self.tg.message.call_args.args[1])
        status = self.tg.message.call_args.args[1]
        self.assertIn("Подтверждённая практика: 0", status)
        self.assertIn("Фактически платящих клиентов: 0", status)
        self.assertIn("Оплаченных дней: 0", status)

        self.message(7, "/pilot_stock 2 reserve 21 | kit-reserve")
        for units in (14, 7):
            for action in ("ship", "deliver"):
                self.message(7, f"/pilot_stock 2 {action} {units} | kit-{action}-{units}")
        self.message(8, "/pilot_delivery 2")
        self.assertIn("вручено 21", self.tg.message.call_args.args[1])

        self.message(7, "/pilot_goods 8 demo 1 80 350 | GR020010")
        self.message(8, "/pay")
        self.click_label(8, "Гранола #3 · 350 ₽")
        self.click_label(8, "Оплатить")
        self.click_label(8, "Я оплатил")
        self.assertIn("/pilot_delivery 3", self.screen()["text"])
        orders = self.db.execute(
            "SELECT kind, demo, source, state FROM pilot_orders ORDER BY id"
        ).fetchall()
        self.assertEqual(orders, [("tracking", 1, "GR020010", "paid"), ("package", 1, None, "paid"),
                                  ("granola", 1, "GR020010", "paid")])
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM pilot_receipts").fetchone(), (3,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM coach_payments").fetchone(), (1,))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM school_work WHERE shared=1").fetchone(), (0,))
        self.assertIn("Гранола: заявок 1, оплат 1", self.bot.pilot_orders.metrics(True, "GR020010"))
        self.assertNotIn("оплат 1", self.bot.pilot_orders.metrics(False, None))

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
