# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Operator-mediated pilot orders. All live amounts are captured in integer kopecks."""

import re

from .coaching import day_arg, valid_id
from .ledger import Clarification, MOSCOW
from .school import SCHOOL_HELP, School, clean_space

PACKAGE_PRICE = 990_000
TRACKING_PRICE = 420_000
PACK = 21
PORTION = 80
CHANNEL = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}")
ORDER_TITLES = {"package": "Школа трекера + продвижение", "tracking": "Трекинг на 21 день",
                "granola": "Гранола Малиновская"}


def rub(value):
    whole, cents = divmod(value, 100)
    label = f"{whole:,}".replace(",", " ")
    return f"{label},{cents:02d} ₽" if cents else f"{label} ₽"


def whole(text, limit=10_000):
    if not re.fullmatch(r"[1-9][0-9]{0,11}", text) or int(text)>limit:
        raise Clarification("Укажи целое положительное число в допустимых пределах.")
    return int(text)


def kopecks(amount):
    if not re.fullmatch(r"[0-9]{1,7}(?:[.,][0-9]{1,2})?", amount):
        raise Clarification("Укажи положительную сумму в рублях, например 9900 или 350,50.")
    main, _, fraction = amount.replace(",", ".").partition(".")
    value = 100 * int(main) + int(fraction.ljust(2, "0"))
    if not value:
        raise Clarification("Сумма должна быть больше нуля.")
    return value


class Pilot:
    def __init__(self, db, telegram, owner, *, price=PACKAGE_PRICE, live_seller=None):
        self.db = db
        self.telegram = telegram
        self.owner = owner
        self.price = price
        self.live_seller = live_seller
        db.executescript("""
            CREATE TABLE IF NOT EXISTS pilot_orders(
                id INTEGER PRIMARY KEY, buyer INTEGER NOT NULL, kind TEXT NOT NULL, total INTEGER NOT NULL,
                demo INTEGER NOT NULL, source TEXT, seller TEXT NOT NULL, created TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'awaiting', updated TEXT
            );
            CREATE TABLE IF NOT EXISTS pilot_receipts(
                order_id INTEGER PRIMARY KEY, seller TEXT NOT NULL, reference TEXT NOT NULL,
                amount INTEGER NOT NULL, recorder INTEGER NOT NULL, confirmed TEXT NOT NULL,
                UNIQUE(seller,reference)
            );
            CREATE TABLE IF NOT EXISTS pilot_kits(
                order_id INTEGER PRIMARY KEY, units INTEGER NOT NULL, grams INTEGER NOT NULL, opened TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pilot_ops(
                update_id INTEGER PRIMARY KEY, user INTEGER NOT NULL, result TEXT NOT NULL
            );
        """)
        self.school=School(db)

    def handle(self, update_id, user, message, now):
        command, _, argument = message.get("text","").strip().partition(" ")
        if command not in {"/school","/pilot"} and not command.startswith(("/school_","/pilot_")):
            return False
        if len(argument)>1600:
            self.telegram.message(user,"Сократи команду до 1600 знаков.")
            return True
        reads={"/school","/pilot","/pilot_order","/school_day","/school_history","/school_status",
               "/pilot_delivery","/pilot_margin","/pilot_metrics","/pilot_cohorts"}
        try:
            with self.db:
                replay = self.db.execute("SELECT result FROM pilot_ops WHERE update_id=? AND user=?",
                                         (update_id,user)).fetchone() if command not in reads else None
                result = replay[0] if replay else self.execute(command, argument, user, now, update_id)
                if not replay and command not in reads:
                    self.db.execute("INSERT INTO pilot_ops VALUES (?,?,?)",(update_id,user,result))
        except Clarification as exc:
            result=str(exc)
        self.telegram.message(user,result)
        return True

    def execute(self, command, argument, user, now, update_id):
        header, *fields = [v.strip() for v in argument.split("|")]
        words = header.split()
        today = now.astimezone(MOSCOW).date()
        if command == "/school":
            rows=self.db.execute("SELECT id,total,state,demo FROM pilot_orders WHERE buyer=? AND kind='package' ORDER BY id DESC LIMIT 10",
                                 (user,))
            labels=[f"#{i} · {rub(total)} · {self.label(state)}{' · учебный' if demo else ''}" for i,total,state,demo in rows]
            return "\n".join([*labels,SCHOOL_HELP,"Черновик остаётся у ученика. /school_share ID день | текст — передать наставнику."])
        if command == "/pilot":
            if user != self.owner:
                raise Clarification("Команда оператора. Доступные команды ученика: /school.")
            return ("Оператор школы. /pilot_offer ID demo создаёт учебный заказ. "
                    "/pilot_offer ID live — рабочий заказ, когда включён продавец. "
                    "/pilot_paid ID сумма [дата старта] | основание. /pilot_order ID — сверка. "
                    "/pilot_cohorts demo или live — возвращение к учёбе на 7-й и 21-й день. "
                    "/school_finish ID, /school_feedback ID день | комментарий.")
        if command == "/pilot_cohorts":
            self.require_operator(user)
            if fields or len(words) not in {1,2} or words[0] not in {"demo","live"}:
                raise Clarification("Выбери /pilot_cohorts demo или live. После режима можно указать дату отчёта.")
            through = day_arg(words[1]) if len(words)==2 else today
            if through > today:
                raise Clarification("Дата отчёта не может быть позже сегодняшней.")
            demo=words[0]=="demo"
            lines=[f"{'Учебные' if demo else 'Рабочие'} потоки на {through:%d.%m.%Y} МСК."]
            lines.extend(f"Старт {start} · {version} · день {day}: {active} из {eligible}."
                         for start,version,day,eligible,active in self.school.cohorts(demo,through))
            if len(lines)==1:
                lines.append("Потоков, дошедших до контрольного дня, пока нет.")
            return "\n".join(lines)
        if command == "/pilot_offer":
            self.require_operator(user)
            if len(words)!=2 or not valid_id(words[0]) or words[1] not in {"demo","live"}:
                raise Clarification("Формат: /pilot_offer ID demo или /pilot_offer ID live.")
            buyer=int(words[0])
            if buyer==self.owner:
                raise Clarification("Назначь отдельного ученика.")
            demo=words[1]=="demo"
            if not demo and not self.live_seller:
                raise Clarification("Приём рабочих заказов ещё не открыт.")
            src = fields[0] if len(fields)==1 and CHANNEL.fullmatch(fields[0]) else None
            if fields and not src:
                raise Clarification("Код источника должен содержать латинские буквы, цифры, дефис или подчёркивание.")
            order=self.new_order(buyer,"package",self.price,demo,src,now)
            return f"{'Учебный заказ' if demo else 'Заказ'} #{order}: школа и продвижение, {rub(self.price)}. Ждёт сверки оплаты."
        if not words:
            raise Clarification("Укажи ID заказа. Список: /school.")
        order_id=whole(words.pop(0), 9_000_000_000)
        if command.startswith("/school_"):
            return self.school.handle(command,order_id,words,fields,user,now,update_id)
        order=self.order(order_id,user)
        if command == "/pilot_order":
            return self.order_text(order,order_id)
        if command == "/pilot_paid":
            self.require_operator(user)
            if len(words) not in {1,2} or len(fields)!=1:
                raise Clarification("Формат: /pilot_paid ID сумма [дата старта] | номер сверенного подтверждения.")
            start=day_arg(words[1]) if len(words)==2 else today
            amount=kopecks(words[0])
            if amount != order[2]:
                raise Clarification("Сверенная сумма отличается от суммы заказа. Оплату не подтвердил.")
            if not order[3] and start<today:
                raise Clarification("Рабочий поток не может стартовать в прошлом.")
            return self.confirm(order_id,order,clean_space(fields[0],100),start,user,now)
        if command == "/pilot_mentor":
            self.require_operator(user)
            if len(words)!=1 or not valid_id(words[0]) or int(words[0])==order[0]:
                raise Clarification("Формат: /pilot_mentor ID_заказа ID_наставника. Укажи отдельный профиль.")
            updated=self.db.execute("UPDATE school_enrolments SET mentor=? WHERE order_id=?", (int(words[0]),order_id))
            if not updated.rowcount:
                raise Clarification("Место в потоке пока не создано.")
            return "Назначение наставника обновлено. Предыдущий наставник теряет доступ."
        return self.extra(command,order_id,order,words,fields,user,now,update_id)

    def new_order(self,buyer,kind,total,demo,source,now):
        return self.db.execute("""
            INSERT INTO pilot_orders(buyer,kind,total,demo,source,seller,created) VALUES (?,?,?,?,?,?,?)
        """,(buyer,kind,total,int(demo),source,"Учебная сверка" if demo else self.live_seller,now.isoformat())).lastrowid

    def require_operator(self,user):
        if user!=self.owner:
            raise Clarification("Требуется роль оператора.")

    def order(self,order_id,user):
        row=self.db.execute("""
            SELECT buyer,kind,total,demo,source,seller,state FROM pilot_orders
            WHERE id=? AND (buyer=? OR ?=?)
        """,(order_id,user,user,self.owner)).fetchone()
        if not row:
            raise Clarification("Заказ не найден.")
        return row

    @staticmethod
    def label(state):
        return {"awaiting":"ждёт сверки", "paid":"оплачен", "cancelled":"отменён"}[state]

    def order_text(self,order,i):
        buyer,kind,total,demo,source,seller,state=order
        title=ORDER_TITLES[kind]
        return f"#{i} · {title} · {rub(total)} · {self.label(state)}. {'Учебный режим. ' if demo else ''}Продавец: {seller}."

    def confirm(self,i,order,reference,start,operator,now,*,mentor=None):
        buyer,kind,total,demo,source,seller,state=order
        if state=="cancelled":
            raise Clarification("Отменённый заказ нельзя оплатить.")
        known=self.db.execute("SELECT reference FROM pilot_receipts WHERE order_id=?",(i,)).fetchone()
        if known:
            if known[0]==reference:
                return "Такая оплата уже учтена. Место и набор повторно не создаются."
            raise Clarification("У заказа уже другое подтверждение. Проверь основание.")
        if self.db.execute("SELECT 1 FROM pilot_receipts WHERE seller=? AND reference=?",(seller,reference)).fetchone():
            raise Clarification("Это подтверждение уже относится к другому заказу.")
        if not demo and not self.live_seller:
            raise Clarification("Приём рабочих подтверждений ещё не открыт.")
        self.db.execute("INSERT INTO pilot_receipts VALUES (?,?,?,?,?,?)",
                        (i,seller,reference,total,operator,now.isoformat()))
        self.db.execute("UPDATE pilot_orders SET state='paid',updated=? WHERE id=?",(now.isoformat(),i))
        if kind=="package":
            self.school.enrol(i,buyer,operator if mentor is None else mentor,start)
        if kind in {"package", "tracking"}:
            self.db.execute("INSERT INTO pilot_kits VALUES (?,?,?,?)",(i,PACK,PORTION,now.isoformat()))
        if kind=="package":
            return f"{'Учебная оплата' if demo else 'Оплата'} сверена. Одно место и набор {PACK} × {PORTION} г."
        if kind=="tracking":
            return f"Учебный трекинг открыт. Подарочный набор {PACK} × {PORTION} г."
        return "Отдельная покупка подтверждена."

    def extra(self,command,order_id,order,words,fields,user,now,update_id):
        raise Clarification("Команда недоступна. Список оператора: /pilot.")
