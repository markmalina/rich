# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Stock events, separate products and verified refunds for pilot orders."""

from .coaching import valid_id
from .ledger import Clarification
from .pilot import CHANNEL, Pilot, kopecks, rub, whole
from .school import clean_space

STOCK = {"reserve", "release", "ship", "deliver", "return", "cancel"}


class Sales(Pilot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS pilot_goods(
                order_id INTEGER PRIMARY KEY, units INTEGER NOT NULL, grams INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pilot_stock(
                order_id INTEGER NOT NULL, reference TEXT NOT NULL, action TEXT NOT NULL,
                units INTEGER NOT NULL, actor INTEGER NOT NULL, recorded TEXT NOT NULL,
                PRIMARY KEY(order_id,reference)
            );
            CREATE TABLE IF NOT EXISTS pilot_refund_requests(
                id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL, buyer INTEGER NOT NULL, opened TEXT NOT NULL,
                settled TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS pilot_one_pending_refund
            ON pilot_refund_requests(order_id) WHERE settled IS NULL;
            CREATE TABLE IF NOT EXISTS pilot_refunds(
                order_id INTEGER NOT NULL, seller TEXT NOT NULL, reference TEXT NOT NULL, amount INTEGER NOT NULL,
                recorder INTEGER NOT NULL, confirmed TEXT NOT NULL, PRIMARY KEY(seller,reference)
            );
            CREATE TABLE IF NOT EXISTS pilot_costs(
                order_id INTEGER NOT NULL, category TEXT NOT NULL, reference TEXT NOT NULL, amount INTEGER NOT NULL,
                PRIMARY KEY(order_id,reference)
            );
            CREATE TABLE IF NOT EXISTS pilot_couriers(
                order_id INTEGER PRIMARY KEY, courier INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pilot_qr(
                source TEXT NOT NULL, evidence TEXT NOT NULL, views INTEGER NOT NULL, demo INTEGER NOT NULL,
                PRIMARY KEY(source,evidence,demo)
            );
            CREATE TABLE IF NOT EXISTS pilot_partners(user INTEGER PRIMARY KEY, source TEXT NOT NULL);
        """)

    def execute(self, command, argument, user, now, update_id):
        head, *fields = [part.strip() for part in argument.split("|")]
        if command == "/pilot":
            help_text = super().execute(command, argument, user, now, update_id)
            return (help_text+"\n/pilot_goods ID demo количество граммы сумма | код. "
                    "/pilot_stock ID reserve/ship/deliver/release/return/cancel количество | основание. "
                    "/pilot_delivery ID. /pilot_refund ID сумма | основание. /pilot_metrics demo или live.")
        if command == "/pilot_goods":
            self.require_operator(user)
            words = head.split()
            if len(words) != 5 or not valid_id(words[0]) or words[1] not in {"demo", "live"}:
                raise Clarification("Формат: /pilot_goods покупатель demo количество граммы сумма | источник.")
            user_id, mode, quantity, grams, total = words
            demo = mode == "demo"
            if not demo and not self.live_seller:
                raise Clarification("Приём рабочих заказов ещё не открыт.")
            src = source(fields)
            units, mass, value = whole(quantity, 1000), whole(grams, 5000), kopecks(total)
            order_id = self.new_order(int(user_id), "granola", value, demo, src, now)
            self.db.execute("INSERT INTO pilot_goods VALUES (?,?,?)", (order_id, units, mass))
            return f"{'Учебная покупка' if demo else 'Покупка'} гранолы #{order_id}. {units} × {mass} г, {rub(value)}."
        if command == "/pilot_metrics":
            if head not in {"demo","live"}:
                raise Clarification("Выбери /pilot_metrics demo или /pilot_metrics live.")
            code = source(fields)
            if user != self.owner:
                assigned = self.db.execute("SELECT source FROM pilot_partners WHERE user=?", (user,)).fetchone()
                if head != "live" or not assigned or (code and code != assigned[0]):
                    raise Clarification("Этот агрегат недоступен.")
                code = assigned[0]
            return self.metrics(head=="demo", code)
        if command == "/pilot_partner":
            self.require_operator(user)
            if not valid_id(head) or len(fields)!=1:
                raise Clarification("Формат: /pilot_partner Telegram_ID | код или off.")
            target=int(head)
            if fields[0]=="off":
                self.db.execute("DELETE FROM pilot_partners WHERE user=?",(target,))
            else:
                self.db.execute("INSERT OR REPLACE INTO pilot_partners VALUES (?,?)",(target,source(fields)))
            return "Назначение на агрегаты обновлено."
        if command == "/pilot_qr":
            self.require_operator(user)
            parts = head.split()
            if len(parts)!=3 or parts[0] not in {"demo","live"} or not CHANNEL.fullmatch(parts[1]) or len(fields)!=1:
                raise Clarification("Формат: /pilot_qr demo или live канал количество | ключ внешнего агрегата.")
            mode, code, count = parts
            demo = int(mode=="demo")
            amount, ref = whole(count, 10_000_000), clean_space(fields[0],100)
            old = self.db.execute("SELECT views FROM pilot_qr WHERE source=? AND evidence=? AND demo=?", (code,ref,demo)).fetchone()
            if old and old[0]!=amount:
                raise Clarification("Этот агрегат уже внесён с другим числом. Нужна сверка.")
            self.db.execute("INSERT OR IGNORE INTO pilot_qr VALUES (?,?,?,?)", (code,ref,amount,demo))
            return "Сводный счётчик QR сохранён. Он не влияет на оплаты."
        if command in {"/pilot_delivery","/pilot_stock"}:
            order, *rest = head.split() or [""]
            return self.shipping(command,whole(order,9_000_000_000),rest,fields,user,now)
        return super().execute(command, argument, user, now, update_id)

    def extra(self, command, i, order, words, fields, user, now, update_id):
        if command == "/pilot_refund_request":
            if order[6]!="paid":
                raise Clarification("Запрос возврата доступен после подтверждения оплаты.")
            self.db.execute("INSERT OR IGNORE INTO pilot_refund_requests(order_id,buyer,opened) VALUES (?,?,?)",
                            (i,order[0],now.isoformat()))
            return "Запрос возврата сохранён. Оператор отдельно проверяет деньги и условия выдачи."
        self.require_operator(user)
        if command == "/pilot_cancel":
            if order[6] not in {"awaiting","cancelled"}:
                raise Clarification("Для оплаченного заказа оформи запрос возврата.")
            self.db.execute("UPDATE pilot_orders SET state='cancelled',updated=? WHERE id=? AND state='awaiting'",
                            (now.isoformat(),i))
            return "Заказ отменён."
        if command == "/pilot_refund" and len(words)==1 and len(fields)==1:
            return self.refund(i,order,kopecks(words[0]),clean_space(fields[0],100),user,now)
        if command == "/pilot_suspend":
            count=self.db.execute("SELECT COUNT(*) FROM pilot_refunds WHERE order_id=?",(i,)).fetchone()[0]
            if not count or words not in (["on"],["off"]):
                raise Clarification("После сверенного возврата укажи /pilot_suspend ID on или off.")
            if order[1] != "package":
                raise Clarification("Учебный доступ по этому заказу отсутствует.")
            updated = self.db.execute("UPDATE school_enrolments SET suspended=? WHERE order_id=?",
                                      (int(words[0] == "on"), i))
            if not updated.rowcount:
                raise Clarification("Учебный доступ по этому заказу отсутствует.")
            return "Решение о доступе сохранено отдельно от возврата."
        if command == "/pilot_assign" and len(words)==1 and (valid_id(words[0]) or words[0]=="off"):
            self.db.execute("DELETE FROM pilot_couriers WHERE order_id=?",(i,))
            if words[0]!="off":
                self.db.execute("INSERT INTO pilot_couriers VALUES (?,?)", (i,int(words[0])))
            return "Назначение на выдачу обновлено."
        if command == "/pilot_cost" and len(words)==2 and words[0] in {"food","shipping","packaging","other"} and len(fields)==1:
            item=(words[0],kopecks(words[1]))
            ref=clean_space(fields[0],100)
            old=self.db.execute("SELECT category,amount FROM pilot_costs WHERE order_id=? AND reference=?",
                                (i,ref)).fetchone()
            if old and tuple(old)!=item:
                raise Clarification("Основание расхода уже учтено с другими данными.")
            self.db.execute("INSERT OR IGNORE INTO pilot_costs VALUES (?,?,?,?)",(i,item[0],ref,item[1]))
            return "Подтверждённый расход сохранён один раз."
        if command == "/pilot_margin":
            buckets=dict(self.db.execute("SELECT category,SUM(amount) FROM pilot_costs WHERE order_id=? GROUP BY category",(i,)))
            actual=" · ".join(f"{name}: {rub(buckets[code]) if code in buckets else 'неизвестно'}"
                             for code,name in (("food","продукт"),("shipping","доставка"),("packaging","упаковка"),("other","прочее")))
            received=order[2]-self.refunded(i) if order[6]=="paid" else 0
            plan="План набора 2 100 ₽, включая продукт 1 050 ₽; остаток от 9 900 ₽ до иных расходов 7 800 ₽. " if order[1]=="package" else ""
            return f"{plan}Чистые подтверждённые поступления по заказу {rub(received)}. Факты: {actual}."
        return super().extra(command,i,order,words,fields,user,now,update_id)

    def refunded(self,i):
        return self.db.execute("SELECT COALESCE(SUM(amount),0) FROM pilot_refunds WHERE order_id=?", (i,)).fetchone()[0]

    def refund(self,i,order,amount,reference,user,now):
        if not order[3] and not self.live_seller:
            raise Clarification("Рабочие возвраты сейчас недоступны.")
        old=self.db.execute("SELECT order_id, amount FROM pilot_refunds WHERE seller=? AND reference=?",
                            (order[5],reference)).fetchone()
        if old:
            if old==(i,amount):
                return "Возврат уже подтверждён."
            raise Clarification("Основание возврата уже использовано.")
        pending=self.db.execute("SELECT 1 FROM pilot_refund_requests WHERE order_id=? AND settled IS NULL",(i,)).fetchone()
        if order[6]!="paid" or not pending or self.refunded(i)+amount>order[2]:
            raise Clarification("Требуется открытый запрос и сумма в пределах остатка полученных денег.")
        self.db.execute("INSERT INTO pilot_refunds VALUES (?,?,?,?,?,?)",
                        (i,order[5],reference,amount,user,now.isoformat()))
        self.db.execute("UPDATE pilot_refund_requests SET settled=? WHERE order_id=? AND settled IS NULL",
                        (now.isoformat(),i))
        return f"{'Учебный возврат' if order[3] else 'Возврат'} {rub(amount)} подтверждён. Выдача и доступ управляются отдельно."

    def order_text(self,order,i):
        label=super().order_text(order,i)
        return f"{label} Возвращено: {rub(self.refunded(i))}."

    def quantities(self,i):
        row=self.db.execute("SELECT units,grams FROM pilot_kits WHERE order_id=? UNION ALL "
                            "SELECT units,grams FROM pilot_goods WHERE order_id=?",(i,i)).fetchone()
        if not row:
            raise Clarification("Комплект появится после подтверждения оплаты.")
        return row

    def shipping(self,command,i,words,fields,user,now):
        order=self.db.execute("SELECT buyer,state,demo FROM pilot_orders WHERE id=?", (i,)).fetchone()
        assigned=self.db.execute("SELECT courier FROM pilot_couriers WHERE order_id=?",(i,)).fetchone()
        courier=assigned is not None and assigned[0]==user
        if not order or (user not in {self.owner,order[0]} and not courier):
            raise Clarification("Выдача не найдена.")
        if order[1]!="paid":
            raise Clarification("Выдача ждёт подтверждения оплаты.")
        units,grams=self.quantities(i)
        totals=dict(self.db.execute("SELECT action,SUM(units) FROM pilot_stock WHERE order_id=? GROUP BY action",(i,)))
        reserved=totals.get("reserve",0)-totals.get("release",0)
        shipped=totals.get("ship",0)
        received=totals.get("deliver",0)
        returned=totals.get("return",0)
        cancelled=totals.get("cancel",0)
        if command=="/pilot_delivery":
            return (f"{'Учебная ' if order[2] else ''}выдача #{i}. {units} × {grams} г. "
                    f"Выделено {reserved}, отправлено {shipped}, вручено {received}, "
                    f"возвращено {returned}, отменено {cancelled}. Остаток к вручению {units-received-cancelled}.")
        if user!=self.owner and not courier:
            raise Clarification("Операцию подтверждает исполнитель выдачи.")
        if len(words)!=2 or words[0] not in STOCK or len(fields)!=1:
            raise Clarification("Формат: /pilot_stock ID операция количество | основание.")
        action,n=words[0],whole(words[1],1000)
        if courier and user!=self.owner and action not in {"ship","deliver","return"}:
            raise Clarification("Резервом и отменой управляет оператор.")
        ref=clean_space(fields[0],100)
        old=self.db.execute("SELECT action,units FROM pilot_stock WHERE order_id=? AND reference=?",(i,ref)).fetchone()
        if old:
            if old==(action,n):
                return "Движение уже учтено."
            raise Clarification("Основание уже связано с другим движением.")
        limits={"reserve":units-reserved-cancelled,"release":reserved-shipped,"ship":reserved-shipped,
                "deliver":shipped-received,"return":received-returned,"cancel":units-reserved-cancelled}
        if n>limits[action]:
            raise Clarification("Количество превышает допустимый остаток для этого действия.")
        self.db.execute("INSERT INTO pilot_stock VALUES (?,?,?,?,?,?)",(i,ref,action,n,user,now.isoformat()))
        return f"Движение сохранено. Итог: /pilot_delivery {i}."

    def metrics(self,demo,src):
        values=(int(demo),src,src)
        query="WHERE o.demo=? AND (? IS NULL OR o.source=?)"
        entries=self.db.execute(f"""
            SELECT o.kind, COUNT(*), COUNT(p.amount), COALESCE(SUM(p.amount),0),
                COALESCE(SUM((SELECT SUM(r.amount) FROM pilot_refunds r WHERE r.order_id=o.id)),0)
            FROM pilot_orders o LEFT JOIN pilot_receipts p ON p.order_id=o.id {query} GROUP BY o.kind
        """,values).fetchall()
        views=self.db.execute("SELECT COALESCE(SUM(views),0) FROM pilot_qr WHERE demo=? AND (? IS NULL OR source=?)",
                              values).fetchone()[0]
        lines=[f"{'Учебные' if demo else 'Рабочие'} заказы · QR, внешняя сверка: {views}"]
        for kind,count,paid,total,refund in entries:
            label = {"package": "Школа", "tracking": "Трекинг", "granola": "Гранола"}[kind]
            lines.append(f"{label}: заявок {count}, оплат {paid}, "
                         f"возвраты {rub(refund)}, нетто {rub(total-refund)}.")
        return "\n".join(lines)


def source(fields):
    if not fields:
        return None
    if len(fields)!=1 or not CHANNEL.fullmatch(fields[0]):
        raise Clarification("Укажи один код источника из букв, цифр, дефисов или подчёркиваний.")
    return fields[0]
