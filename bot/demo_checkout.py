# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Local, explicitly simulated checkout for the private Telegram pilot."""

from dataclasses import dataclass
import datetime as dt
import re
from uuid import uuid4

from .ledger import Clarification, MOSCOW
from .pilot import ORDER_TITLES, PACK, PORTION, TRACKING_PRICE, rub
from .providers import ProviderError

NOTICE = "Учебная оплата. Деньги не списываются, чек не формируется."
JAR_START = re.compile(r"/start tracking_(GR[0-9]{6})")
CALLBACK = re.compile(r"dp:(?P<action>[a-z]+):(?P<item>[a-z0-9]+)(?::(?P<detail>[a-z0-9]+))?(?::(?P<day>[0-9]{1,2}))?")
PRACTICES = {"food": "Питание", "activity": "Активность", "money": "Финансы"}


@dataclass(frozen=True)
class Screen:
    text: str
    buttons: tuple[tuple[str, str], ...] = ()

    def telegram_body(self, user):
        rows = [[{"text": title, "callback_data": f"dp:{action}"}] for title, action in self.buttons]
        return {"chat_id": user, "text": self.text, "reply_markup": {"inline_keyboard": rows}}


class DemoCheckout:
    def __init__(self, db, telegram, pilot, coaching=None):
        self.db = db
        self.telegram = telegram
        self.pilot = pilot
        self.coaching = coaching
        db.execute("""
            CREATE TABLE IF NOT EXISTS pilot_tracking_days (
                order_id INTEGER NOT NULL, day INTEGER NOT NULL, practice TEXT NOT NULL, recorded TEXT NOT NULL,
                PRIMARY KEY(order_id,day,practice)
            )
        """)
        db.commit()

    def message(self, user, text, now):
        match = JAR_START.fullmatch(text)
        if text not in {"/pay", "/start tracking", "/tracking"} and not match:
            return False
        try:
            with self.db:
                if text == "/pay":
                    screen = self.menu(user, now)
                elif text == "/tracking":
                    record = self.tracking_order(user)
                    screen = self.order_screen(record[0], user, now) if record else self.tracking_offer()
                else:
                    code = match[1] if match else None
                    if code and code.endswith("0000"):
                        raise Clarification("Код банки неполный. Открой рабочий QR или выбери трекинг в /pay.")
                    order_id = self.offer(user, "tracking", now, code)
                    screen = self.order_screen(order_id, user, now)
        except Clarification as error:
            screen = Screen(str(error))
        self.telegram.api("sendMessage", screen.telegram_body(user))
        return True

    def callback(self, query, now):
        data = query.get("data")
        if not isinstance(data, str) or not data.startswith("dp:"):
            return False
        user = query.get("from", {}).get("id")
        message = query.get("message", {})
        chat = message.get("chat", {})
        if type(user) is not int or user <= 0 or chat.get("type") != "private" or chat.get("id") != user:
            self.acknowledge(query, "Учебная оплата доступна только в личном чате.")
            return True
        try:
            match = CALLBACK.fullmatch(data) if len(data) <= 64 else None
            if not match:
                raise Clarification("Открой актуальную кнопку через /pay.")
            with self.db:
                screen = self.apply(user, now, **match.groupdict())
        except Clarification as error:
            self.acknowledge(query, str(error))
            return True
        self.acknowledge(query)
        self.telegram.api("sendMessage", screen.telegram_body(user))
        return True

    def acknowledge(self, query, text=None):
        body = {"callback_query_id": query["id"]}
        if text:
            body.update(text=text, show_alert=True)
        try:
            self.telegram.api("answerCallbackQuery", body)
        except ProviderError as error:
            if error.status != 400:
                raise

    def apply(self, user, now, action, item, detail, day):
        if day and action != "track":
            raise Clarification("Открой актуальную кнопку через /pay.")
        if action == "offer" and item in {"package", "tracking"} and not detail:
            return self.order_screen(self.offer(user, item, now), user, now)
        if not item or not re.fullmatch(r"[1-9][0-9]{0,15}", item):
            raise Clarification("Открой актуальную кнопку через /pay.")
        key = int(item)
        if action == "order" and not detail:
            return self.order_screen(key, user, now)
        if action == "pay" and not detail:
            return self.order_screen(key, user, now, prepare=True)
        if action == "paid" and not detail:
            return self.confirm_order(key, user, now)
        if action in {"coachpay", "coachpaid"} and detail and len(detail) == 8:
            return self.coach_payment(key, detail, user, now, confirmed=action == "coachpaid")
        if action == "track" and detail in PRACTICES and day:
            return self.record_practice(key, detail, int(day), user, now)
        raise Clarification("Открой актуальную кнопку через /pay.")

    def menu(self, user, now):
        buttons = [(f"Трекинг · {rub(TRACKING_PRICE)}", "offer:tracking"),
                   (f"Школа и продвижение · {rub(self.pilot.price)}", "offer:package")]
        orders = self.db.execute("""
            SELECT id,total FROM pilot_orders
            WHERE buyer=? AND demo=1 AND kind='granola' AND state<>'cancelled' ORDER BY id DESC LIMIT 10
        """, (user,))
        buttons.extend((f"Гранола #{i} · {rub(total)}", f"order:{i}") for i, total in orders)
        today = now.astimezone(MOSCOW).date()
        if self.coaching is not None:
            links = self.db.execute("""
                SELECT id,weekdays FROM coach_links
                WHERE client=? AND state='active' AND demo=1 ORDER BY id DESC LIMIT 10
            """, (user,))
            buttons.extend((f"Оплатить день у трекера #{link} · 100 ₽", f"coachpay:{link}:{today:%Y%m%d}")
                           for link, weekdays in links if self.coaching.active_on(link, today, weekdays))
        return Screen(f"Выбери, что хочешь проверить.\n{NOTICE}", tuple(buttons))

    def tracking_offer(self):
        return Screen(f"Трекинг на 21 день · {rub(TRACKING_PRICE)}. Гранола в подарок.\n{NOTICE}",
                      (("Открыть предложение", "offer:tracking"),))

    def offer(self, user, kind, now, source=None):
        price = TRACKING_PRICE if kind == "tracking" else self.pilot.price
        self.db.execute("""
            INSERT INTO pilot_orders(buyer,kind,total,demo,source,seller,created)
            SELECT ?,?,?,1,?,'Учебная сверка',? WHERE NOT EXISTS (
                SELECT 1 FROM pilot_orders WHERE buyer=? AND kind=? AND demo=1 AND state<>'cancelled'
            )
        """, (user, kind, price, source, now.isoformat(), user, kind))
        (order_id,) = self.db.execute("""
            SELECT id FROM pilot_orders WHERE buyer=? AND kind=? AND demo=1 AND state<>'cancelled'
            ORDER BY (state='awaiting') DESC,id DESC LIMIT 1
        """, (user, kind)).fetchone()
        if source:
            self.db.execute("""
                UPDATE pilot_orders SET source=? WHERE id=? AND state='awaiting' AND source IS NULL
            """, (source, order_id))
        return order_id

    def get_order(self, order_id, user):
        order = self.pilot.order(order_id, user)
        if order[0] != user or not order[3] or order[6] == "cancelled":
            raise Clarification("Учебный заказ не найден. Открой /pay.")
        return order

    def order_screen(self, order_id, user, now, *, prepare=False):
        order = self.get_order(order_id, user)
        _, kind, total, _, _, _, state = order
        if state == "paid":
            return self.after_payment(order_id, kind, now)
        title = f"{ORDER_TITLES[kind]} · {rub(total)} · заказ #{order_id}."
        gift = f"\nГранола в подарок: {PACK} банок по {PORTION} г." if kind in {"package", "tracking"} else ""
        if prepare:
            return Screen(f"{title}{gift}\n{NOTICE}\nНажми «Я оплатил», чтобы продолжить.",
                          (("Я оплатил", f"paid:{order_id}"),))
        return Screen(f"{title}{gift}\n{NOTICE}", (("Оплатить", f"pay:{order_id}"),))

    def confirm_order(self, order_id, user, now):
        order = self.get_order(order_id, user)
        if order[6] == "awaiting":
            today = now.astimezone(MOSCOW).date()
            self.pilot.confirm(order_id, order, f"demo-self:{order_id}:{uuid4().hex}", today, user, now,
                               mentor=self.pilot.owner)
        return self.after_payment(order_id, order[1], now)

    def after_payment(self, order_id, kind, now):
        text = "Учебная оплата подтверждена. Деньги не списаны."
        if kind == "package":
            return Screen(f"{text}\nШкола открыта: /school_day {order_id}. Учебная выдача: /pilot_delivery {order_id}.")
        if kind == "tracking":
            return self.tracking_screen(order_id, now, label=text)
        return Screen(f"{text}\nУчебная выдача: /pilot_delivery {order_id}.")

    def coach_payment(self, link, target, user, now, *, confirmed):
        today = now.astimezone(MOSCOW).date()
        if self.coaching is None:
            raise Clarification("Учебное сопровождение пока недоступно.")
        if target != f"{today:%Y%m%d}":
            raise Clarification("Кнопка относится к другому дню. Открой /pay ещё раз.")
        row = self.db.execute("""
            SELECT tracker,weekdays FROM coach_links WHERE id=? AND client=? AND state='active' AND demo=1
        """, (link, user)).fetchone()
        if not row or not self.coaching.active_on(link, today, row[1]):
            raise Clarification("Доступного учебного дня в этой связи нет.")
        if confirmed:
            self.db.execute("INSERT INTO coach_payments VALUES (?,?,?,?,?) ON CONFLICT(link,day) DO NOTHING",
                            (link, str(today), f"demo-self:{target}:{uuid4().hex}", row[0], now.isoformat()))
        paid = self.db.execute("SELECT 1 FROM coach_payments WHERE link=? AND day=?", (link, str(today))).fetchone()
        if paid:
            return Screen(f"Учебная оплата дня 100 ₽ подтверждена. Деньги не списаны. Продолжить: /coach_report {link}.")
        return Screen(f"День сопровождения · 100 ₽ · {today:%d.%m.%Y} МСК.\n{NOTICE}",
                      (("Я оплатил", f"coachpaid:{link}:{target}"),))

    def tracking_order(self, user):
        return self.db.execute("""
            SELECT id FROM pilot_orders WHERE buyer=? AND kind='tracking' AND demo=1 AND state<>'cancelled'
            ORDER BY id DESC LIMIT 1
        """, (user,)).fetchone()

    def tracking_day(self, order_id, now):
        (confirmed,) = self.db.execute("SELECT confirmed FROM pilot_receipts WHERE order_id=?", (order_id,)).fetchone()
        start = dt.datetime.fromisoformat(confirmed).astimezone(MOSCOW).date()
        return (now.astimezone(MOSCOW).date() - start).days + 1

    def tracking_screen(self, order_id, now, *, label=""):
        day = self.tracking_day(order_id, now)
        count = self.db.execute("SELECT COUNT(DISTINCT day) FROM pilot_tracking_days WHERE order_id=?", (order_id,)).fetchone()[0]
        history = f"Дней с отметками: {count} из {PACK}. Учебная выдача подарка: /pilot_delivery {order_id}."
        if not 1 <= day <= PACK:
            return Screen(f"Трекинг завершён. {history}")
        checked = {p for (p,) in self.db.execute("""
            SELECT practice FROM pilot_tracking_days WHERE order_id=? AND day=?
        """, (order_id, day))}
        buttons = tuple((f"{'✓ ' if p in checked else 'Отметить: '}{title}", f"track:{order_id}:{p}:{day}")
                        for p, title in PRACTICES.items())
        return Screen(f"{label}\nТрекинг · день {day} из {PACK}. {history}\n"
                      "Отметь, за чем сегодня следил. Отметки видны только тебе. /tracking — вернуться.", buttons)

    def record_practice(self, order_id, practice, shown_day, user, now):
        order = self.get_order(order_id, user)
        if order[1] != "tracking" or order[6] != "paid":
            raise Clarification("Сначала открой свой трекинг через /pay.")
        day = self.tracking_day(order_id, now)
        if day != shown_day or not 1 <= day <= PACK:
            raise Clarification("Кнопка относится к другому дню. Открой /tracking ещё раз.")
        self.db.execute("INSERT OR IGNORE INTO pilot_tracking_days VALUES (?,?,?,?)",
                        (order_id, day, practice, now.isoformat()))
        return self.tracking_screen(order_id, now, label="Отметки за текущий день.")
