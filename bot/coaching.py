# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Private Life Tracker agreements and manually reconciled days."""

import datetime as dt
from decimal import Decimal
import re
from secrets import token_urlsafe

from .ledger import Clarification, MOSCOW
from .providers import ProviderError

WEEK = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
STEP = dt.timedelta(days=1)
TIME = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")
AMOUNT = re.compile(r"[0-9]{1,9}(?:[.,][0-9]{1,3})?")
HELP = """Личное сопровождение в eRex
/coach — мои связи
/coach_id — свой Telegram ID для назначения трекером
Трекер: /coach_invite или /coach_invite demo
Клиент: /coach_join КОД
/coach_plan ID | практика | отчёт или минимум 10 повторений | пн,ср,пт | вариант трудного дня | 20:00–21:00
/coach_share_plan ID — передать условия трекеру
/coach_accept ID — принять условия
/coach_report ID [ГГГГ-ММ-ДД] | число или нет — предпросмотр
/coach_share ID — отправить отметку
/coach_view ID [дата] — отметки и обратная связь
/coach_pause ID, /coach_resume ID, /coach_end ID
/coach_reminder ID 20:00 или /coach_reminder ID off
Трекер: /coach_self ID [дата] | отметка
/coach_manual ID [дата] | отметка | основание — ручная отметка, клиент подтверждает /coach_share ID
/coach_feedback ID | ответ
/coach_week ID | поправка | дата следующей проверки
/coach_days ID [дата начала] [дата конца]
/coach_paid ID дата | основание сверки — отметить фактическую оплату дня
Сопровождение стоит 100 ₽ за активный день. Платёж идёт трекеру. eRex только фиксирует подтверждение."""


class Coaching:
    def __init__(self, db, telegram, owner):
        self.db = db
        self.telegram = telegram
        self.owner = owner
        db.executescript("""
            CREATE TABLE IF NOT EXISTS coach_roles (user INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS coach_invites (
                code TEXT PRIMARY KEY, tracker INTEGER NOT NULL, expires TEXT NOT NULL, demo INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coach_links (
                id INTEGER PRIMARY KEY, client INTEGER NOT NULL, tracker INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'joined', demo INTEGER NOT NULL,
                practice TEXT, minimum TEXT, weekdays TEXT, fallback TEXT, window TEXT,
                reminder TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS active_coach_link
            ON coach_links(client, tracker, demo) WHERE state <> 'ended';
            CREATE TABLE IF NOT EXISTS coach_calendar (
                link INTEGER NOT NULL, day TEXT NOT NULL, active INTEGER NOT NULL,
                PRIMARY KEY (link, day)
            );
            CREATE TABLE IF NOT EXISTS coach_drafts (
                link INTEGER PRIMARY KEY, day TEXT NOT NULL, value TEXT NOT NULL, author INTEGER NOT NULL,
                basis TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coach_marks (
                link INTEGER NOT NULL, day TEXT NOT NULL, kind TEXT NOT NULL, value TEXT NOT NULL,
                author INTEGER NOT NULL, confirmed_by INTEGER NOT NULL, published TEXT NOT NULL, basis TEXT NOT NULL,
                update_id INTEGER NOT NULL, PRIMARY KEY (link, day, kind, update_id)
            );
            CREATE TABLE IF NOT EXISTS coach_feedback (
                link INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, review TEXT,
                author INTEGER NOT NULL, created TEXT NOT NULL, PRIMARY KEY (link, kind)
            );
            CREATE TABLE IF NOT EXISTS coach_payments (
                link INTEGER NOT NULL, day TEXT NOT NULL, basis TEXT NOT NULL,
                tracker INTEGER NOT NULL, recorded TEXT NOT NULL, PRIMARY KEY (link, day),
                UNIQUE (link, basis)
            );
            CREATE TABLE IF NOT EXISTS coach_notices (
                link INTEGER NOT NULL, day TEXT NOT NULL, delivery TEXT NOT NULL, PRIMARY KEY (link, day)
            );
            CREATE TABLE IF NOT EXISTS coach_ops (
                update_id INTEGER PRIMARY KEY, user INTEGER NOT NULL, response TEXT NOT NULL
            );
        """)

    def handle(self, update_id, user, message, received):
        raw = message.get("text", "").strip()
        command, _, args = raw.partition(" ")
        if command != "/coach" and not command.startswith("/coach_"):
            return False
        if len(raw) > 1600:
            self.telegram.message(user, "Сократи команду до 1600 знаков.")
            return True
        sent = dt.datetime.fromtimestamp(message["date"], MOSCOW) if type(message.get("date")) is int else received
        today = received.astimezone(MOSCOW).date()
        try:
            with self.db:
                replay = self.db.execute("SELECT response FROM coach_ops WHERE update_id=? AND user=?",
                                         (update_id, user)).fetchone()
                response = replay[0] if replay else self.execute(command, args.strip(), user, received, sent, today, update_id)
                if not replay and command not in {"/coach", "/coach_view", "/coach_days", "/coach_id"}:
                    self.db.execute("INSERT INTO coach_ops VALUES (?, ?, ?)", (update_id, user, response))
        except Clarification as error:
            response = str(error)
        self.telegram.message(user, response)
        return True

    def execute(self, command, args, user, now, sent, today, update_id):
        if command == "/coach":
            rows = self.db.execute("""
                SELECT id, client, tracker, state, demo FROM coach_links
                WHERE client=? OR tracker=? ORDER BY id DESC LIMIT 15
            """, (user, user))
            statuses = {"joined": "нужна практика", "draft": "условия у клиента", "offered": "нужно решение трекера",
                        "active": "согласовано", "ended": "доступ отозван"}
            links = [f"#{i} · {'клиент' if user == c else 'трекер'} · {statuses[s]}{' · учебная' if demo else ''}"
                     for i, c, _, s, demo in rows]
            return "\n".join([*links, HELP])
        if command == "/coach_id":
            return f"Твой Telegram ID: {user}. Передавай его при назначении трекером."
        if command == "/coach_allow":
            if user != self.owner or not valid_id(args):
                raise Clarification("Роль трекера назначает владелец по /coach_allow ID.")
            self.db.execute("INSERT OR IGNORE INTO coach_roles VALUES (?)", (int(args),))
            return "Роль трекера назначена. Доступ к дневникам назначается отдельно."
        if command == "/coach_invite":
            if (user != self.owner and not self.db.execute("SELECT 1 FROM coach_roles WHERE user=?", (user,)).fetchone()):
                raise Clarification("Нужна роль трекера.")
            if args not in {"", "demo"}:
                raise Clarification("Используй /coach_invite или /coach_invite demo.")
            code = token_urlsafe(18)
            self.db.execute("INSERT INTO coach_invites VALUES (?,?,?,?)",
                            (code, user, (now+dt.timedelta(days=1)).isoformat(), int(args == "demo")))
            return ("Приглашение на 24 часа, для одного человека. Передай лично:\n"
                    f"/coach_join {code}")
        if command == "/coach_join":
            invite = self.db.execute("SELECT tracker,expires,demo FROM coach_invites WHERE code=?", (args,)).fetchone()
            if not invite or dt.datetime.fromisoformat(invite[1]) <= now or invite[0] == user:
                raise Clarification("Приглашение недействительно. Возьми новое у трекера.")
            tracker, _, demo = invite
            existing = self.db.execute("""
                SELECT id FROM coach_links WHERE client=? AND tracker=? AND demo=? AND state<>'ended'
            """, (user, tracker, demo)).fetchone()
            link = existing[0] if existing else self.db.execute(
                "INSERT INTO coach_links (client,tracker,demo) VALUES (?,?,?)", (user, tracker, demo)).lastrowid
            self.db.execute("DELETE FROM coach_invites WHERE code=?", (args,))
            return f"{'Учебная связь' if demo else 'Приглашение принято'} #{link}. Задай условия через /coach_plan {link}."
        return self.link_action(command, args, user, now, sent, today, update_id)

    def link_action(self, command, args, user, now, sent, today, update_id):
        header, *fields = [v.strip() for v in args.split("|")]
        if not header:
            raise Clarification("Укажи ID своей связи. Список доступен через /coach.")
        ident, *dates = header.split()
        if not valid_id(ident):
            raise Clarification("Укажи ID своей связи. Список доступен через /coach.")
        link = int(ident)
        row = self.db.execute("""
            SELECT client,tracker,state,demo,practice,minimum,weekdays,fallback,window,reminder
            FROM coach_links WHERE id=? AND (client=? OR tracker=?)
        """, (link, user, user)).fetchone()
        if not row:
            raise Clarification("Связь не найдена.")
        client, tracker, state, demo, practice, minimum, weekdays, fallback, window, _ = row
        is_client = user == client
        if command == "/coach_end" and is_client:
            self.db.execute("UPDATE coach_links SET state='ended',reminder=NULL WHERE id=?", (link,))
            self.schedule(link, today+STEP, False)
            self.db.execute("DELETE FROM coach_drafts WHERE link=?", (link,))
            return "Доступ трекера к новым просмотрам отозван. Личная «БАЗА» по-прежнему доступна владельцу."
        finance = command in {"/coach_days", "/coach_paid"}
        if state == "ended" and command == "/coach_view" and is_client and practice:
            day = day_arg(dates[0]) if dates else today
            return self.view(link, day, True, minimum, practice, window)
        if state == "ended" and not finance:
            raise Clarification("Доступ по связи закрыт.")
        label = "Учебная связь · " if demo else ""
        if command == "/coach_plan" and is_client and state in {"joined","draft","offered"}:
            practice, minimum, weekdays, fallback, window = parse_plan(fields)
            self.db.execute("""
                UPDATE coach_links SET state='draft',practice=?,minimum=?,weekdays=?,fallback=?,window=?
                WHERE id=?
            """, (practice, minimum, weekdays, fallback, window, link))
            return (f"{label}Предпросмотр #{link}. Только для выбранного трекера: "
                    f"{terms(practice,minimum,weekdays,fallback,window)}\nОтправить: /coach_share_plan {link}.")
        if command == "/coach_share_plan" and is_client and state in {"draft", "offered"}:
            self.db.execute("UPDATE coach_links SET state='offered' WHERE id=?", (link,))
            return "Условия доступны выбранному трекеру. Условия вступят в силу после принятия."
        if command == "/coach_accept" and not is_client and state == "offered":
            self.db.execute("UPDATE coach_links SET state='active' WHERE id=?", (link,))
            self.schedule(link, today, True)
            return f"{label}Условия приняты. Активные дни считаются по московскому календарю."
        if command == "/coach_view" and state != "active":
            if not is_client and state != "offered":
                return "Клиент пока не передал условия."
            if not practice:
                return "Практика пока не задана."
            return f"{label}#{link}. {terms(practice, minimum, weekdays, fallback, window)}"
        if state != "active" and not (state == "ended" and weekdays and finance):
            raise Clarification("Сначала клиент передаёт условия, затем трекер их принимает.")
        if command in {"/coach_pause","/coach_resume"} and is_client:
            self.schedule(link, today+STEP, command == "/coach_resume")
            return f"Изменение действует с {today+STEP:%d.%m.%Y} МСК."
        if command == "/coach_reminder" and is_client:
            at = dates[0] if dates else ""
            if at != "off" and not TIME.fullmatch(at):
                raise Clarification("Укажи время МСК в формате 20:00 или off.")
            self.db.execute("UPDATE coach_links SET reminder=? WHERE id=?", (None if at == "off" else at, link))
            return "Напоминания выключены." if at == "off" else f"Напоминание включено на {at} МСК."
        day = day_arg(dates[0]) if dates else today
        if command == "/coach_view":
            return label+self.view(link, day, is_client, minimum, practice, window)
        if command == "/coach_days":
            finish = day_arg(dates[1]) if len(dates)>1 else today
            return label+self.days(link, day, finish, today, weekdays)
        if command == "/coach_paid" and not is_client:
            self.require_active_day(link, day, today, weekdays)
            basis, = limited_fields(fields, 1)
            old = self.db.execute("SELECT basis FROM coach_payments WHERE link=? AND day=?", (link, str(day))).fetchone()
            if old:
                return ("Оплата уже подтверждена." if old[0] == basis else
                        "День уже закрыт по другому основанию. Требуется ручная сверка.")
            if self.db.execute("SELECT 1 FROM coach_payments WHERE link=? AND basis=?", (link, basis)).fetchone():
                raise Clarification("Это основание уже использовано для этого назначения.")
            self.db.execute("INSERT INTO coach_payments VALUES (?,?,?,?,?)",
                            (link, str(day), basis, tracker, now.isoformat()))
            return f"{label}Подтверждение оплаты за {day:%d.%m.%Y} сохранено. 100 ₽."
        if command in {"/coach_feedback","/coach_week"} and not is_client:
            note, *review = limited_fields(fields, 2 if command == "/coach_week" else 1)
            date = day_arg(review[0]) if review else None
            if date and date <= today:
                raise Clarification("Укажи будущую дату следующей проверки.")
            kind = "week" if date else "feedback"
            self.db.execute("""
                INSERT INTO coach_feedback VALUES (?,?,?,?,?,?)
                ON CONFLICT(link,kind) DO UPDATE SET text=excluded.text,review=excluded.review,
                    author=excluded.author,created=excluded.created
            """, (link, kind, note, str(date) if date else None, tracker, now.isoformat()))
            return "Сохранена недельная поправка." if date else "Обратная связь доступна клиенту."
        if command == "/coach_share" and is_client:
            return label+self.share(link, client, sent, today, weekdays, update_id)
        if (command == "/coach_report" and is_client or
                command in {"/coach_manual","/coach_self"} and not is_client):
            self.require_active_day(link, day, today, weekdays)
            value, *basis = limited_fields(fields, 2 if command == "/coach_manual" else 1)
            value = mark_value(value, minimum)
            if command == "/coach_self":
                self.save_mark(link, day, "self", value, tracker, tracker, sent, "", update_id)
                return f"{label}Собственная отметка трекера записана отдельно."
            draft = self.db.execute("""
                INSERT INTO coach_drafts VALUES (?,?,?,?,?)
                ON CONFLICT(link) DO UPDATE SET day=excluded.day,value=excluded.value,
                    author=excluded.author,basis=excluded.basis
                WHERE coach_drafts.author=excluded.author OR excluded.author=?
            """, (link, str(day), value, user, basis[0] if basis else "", client))
            if not draft.rowcount:
                raise Clarification("У клиента уже есть свой черновик. Отметка трекера не заменила его.")
            if command == "/coach_manual":
                return "Ручная отметка ожидает подтверждения клиента. Клиент может открыть её через /coach_view."
            return f"{label}Предпросмотр для выбранного трекера за {day:%d.%m}: {value}. Отправить: /coach_share {link}."
        raise Clarification("Команда недоступна для этой роли. Список: /coach.")

    def schedule(self, link, day, active):
        self.db.execute("""
            INSERT INTO coach_calendar VALUES (?,?,?)
            ON CONFLICT(link,day) DO UPDATE SET active=excluded.active
        """, (link, str(day), int(active)))

    def active_on(self, link, day, weekdays):
        if WEEK[day.weekday()] not in weekdays.split(","):
            return False
        row = self.db.execute("""
            SELECT active FROM coach_calendar WHERE link=? AND day<=? ORDER BY day DESC LIMIT 1
        """, (link, str(day))).fetchone()
        return bool(row and row[0])

    def require_active_day(self, link, day, today, weekdays):
        if day > today or not self.active_on(link, day, weekdays):
            raise Clarification("Эта дата не является прошедшим или текущим активным днём по плану.")

    def save_mark(self, link, day, kind, value, author, confirmed, sent, basis, update_id):
        self.db.execute("INSERT OR IGNORE INTO coach_marks VALUES (?,?,?,?,?,?,?,?,?)",
                        (link, str(day), kind, value, author, confirmed, sent.isoformat(), basis, update_id))

    def share(self, link, client, sent, today, weekdays, update_id):
        draft = self.db.execute("SELECT day,value,author,basis FROM coach_drafts WHERE link=?", (link,)).fetchone()
        if not draft:
            raise Clarification("Сначала подготовь отметку через /coach_report.")
        day, value, author, basis = draft
        self.require_active_day(link, day_arg(day), today, weekdays)
        self.save_mark(link, day_arg(day), "client", value, author, client, sent, basis, update_id)
        self.db.execute("DELETE FROM coach_drafts WHERE link=?", (link,))
        return "Отметка доступна выбранному трекеру." + (" Отметка поздняя." if sent.date()>day_arg(day) else "")

    def view(self, link, day, client, minimum, practice, window):
        lines = [f"{practice} · {day:%d.%m.%Y} · окно ответа {window} МСК"]
        rows = self.db.execute("""
            SELECT value,published,basis FROM coach_marks WHERE link=? AND day=? AND kind='client'
            ORDER BY published,update_id
        """, (link, str(day))).fetchall()
        if rows:
            first, last = rows[0], rows[-1]
            timely = dt.datetime.fromisoformat(first[1]).date() == day
            lines.append(f"Первая сдача: {'вовремя' if timely else 'поздно'}. Последняя отметка: {last[0]}.")
            if minimum:
                threshold = Decimal(minimum.split(maxsplit=1)[0])
                meets = last[0] != "нет" and Decimal(last[0]) >= threshold
                first_met = timely and any(
                    dt.datetime.fromisoformat(stamp).date() == day and value != "нет" and Decimal(value) >= threshold
                    for value,stamp,_ in rows)
                lines.append(f"Минимум по последней отметке: {'да' if meets else 'нет'}. "
                             f"Выполнен вовремя: {'да' if first_met else 'нет'}.")
            if last[2]:
                lines.append(f"Ручная отметка подтверждена клиентом. Основание: {last[2]}.")
        else:
            lines.append("Подтверждённой отметки нет.")
        if client:
            pending = self.db.execute("SELECT day,value,basis FROM coach_drafts WHERE link=?", (link,)).fetchone()
            if pending:
                lines.append(f"Предпросмотр за {pending[0]}: {pending[1]}. "
                             f"{'Основание: '+pending[2]+'. ' if pending[2] else ''}Отправить: /coach_share {link}.")
        else:
            own = self.db.execute("""
                SELECT value FROM coach_marks WHERE link=? AND day=? AND kind='self'
                ORDER BY published DESC,update_id DESC LIMIT 1
            """, (link, str(day))).fetchone()
            if own:
                lines.append(f"Своя отметка трекера: {own[0]}.")
        for kind,note,review in self.db.execute("SELECT kind,text,review FROM coach_feedback WHERE link=?", (link,)):
            lines.append(f"Недельная поправка: {note}. Проверка {review}." if kind == "week" else f"Ответ трекера: {note}")
        notice = self.db.execute("SELECT delivery FROM coach_notices WHERE link=? AND day=?", (link, str(day))).fetchone()
        if notice:
            status = {"pending":"ожидается", "sent":"доставлено API", "uncertain":"результат неизвестен", "failed":"не отправлено"}
            lines.append("Напоминание: "+status[notice[0]]+".")
        return "\n".join(lines)

    def days(self, link, start, finish, today, weekdays):
        if not (start<=finish<=today) or (finish-start).days>30:
            raise Clarification("Укажи период до 31 дня с датами не позже сегодняшней.")
        obligations = [start+dt.timedelta(days=i) for i in range((finish-start).days+1)
                       if self.active_on(link,start+dt.timedelta(days=i),weekdays)]
        confirmed = self.db.execute("""
            SELECT COUNT(*) FROM coach_payments WHERE link=? AND day BETWEEN ? AND ?
        """, (link, str(start), str(finish))).fetchone()[0]
        return (f"{start:%d.%m}–{finish:%d.%m} МСК. Активных дней: {len(obligations)} · {len(obligations)*100} ₽. "
                f"Подтверждено: {confirmed} · {confirmed*100} ₽. Оплата напрямую трекеру.")

    def send_due(self, now):
        today, clock = now.date(), now.strftime("%H:%M")
        rows = self.db.execute("""
            SELECT id,client,weekdays FROM coach_links WHERE state='active' AND reminder<=?
            AND NOT EXISTS (SELECT 1 FROM coach_notices WHERE link=coach_links.id AND day=?)
            ORDER BY reminder LIMIT 25
        """, (clock, str(today))).fetchall()
        for link, client, weekdays in rows:
            if not self.active_on(link,today,weekdays):
                continue
            with self.db:
                sent = self.db.execute("INSERT OR IGNORE INTO coach_notices VALUES (?,?,'pending')", (link, str(today)))
            if not sent.rowcount:
                continue
            status = "sent"
            try:
                self.telegram.message(client, f"Напоминание eRex. Можешь подготовить личную отметку: /coach_report {link}.")
            except ProviderError as error:
                # After a lost response a retry could send the same reminder twice.
                status = "failed" if error.status in {400,401,403,429} else "uncertain"
            with self.db:
                self.db.execute("UPDATE coach_notices SET delivery=? WHERE link=? AND day=?", (status,link,str(today)))


def valid_id(text):
    return re.fullmatch(r"[1-9][0-9]{0,15}", text) is not None


def day_arg(text):
    try:
        day = dt.date.fromisoformat(text)
    except ValueError:
        raise Clarification("Укажи дату в формате ГГГГ-ММ-ДД.") from None
    if day.isoformat()!=text:
        raise Clarification("Укажи дату в формате ГГГГ-ММ-ДД.")
    return day


def limited_fields(fields, count):
    if len(fields)!=count or any(not val or len(val)>300 for val in fields):
        raise Clarification("Проверь число полей, разделённых |. Для каждого поля доступно до 300 знаков.")
    return fields


def parse_plan(fields):
    practice, criterion, days, fallback, window = limited_fields(fields,5)
    minimum = None
    if criterion != "отчёт":
        prefix, _, target = criterion.partition(" ")
        value, _, unit = target.strip().partition(" ")
        if prefix != "минимум" or not AMOUNT.fullmatch(value) or Decimal(value.replace(",", ".")) <= 0:
            raise Clarification("Критерий: отчёт или минимум с положительным числом.")
        minimum = f"{value.replace(',', '.')} {unit.strip()}".strip()
    days = [d.strip().lower() for d in days.split(",")]
    if any(d not in WEEK for d in days):
        raise Clarification("Дни недели: пн,вт,ср,чт,пт,сб,вс.")
    start, _, end = window.replace("–","-").partition("-")
    if not TIME.fullmatch(start.strip()) or not TIME.fullmatch(end.strip()) or start.strip()>=end.strip():
        raise Clarification("Укажи окно ответа по МСК, например 20:00–21:00.")
    return practice, minimum, ",".join(d for d in WEEK if d in days), fallback, f"{start.strip()}–{end.strip()}"


def terms(practice, minimum, days, fallback, window):
    criterion = f"минимум {minimum}" if minimum else "отчитаться"
    return f"{practice} · {criterion} · {days}. Трудный день: {fallback}. Ответ {window} МСК. 100 ₽ за активный день."


def mark_value(value, minimum):
    if value in {"нет","да"} and (not minimum or value=="нет"):
        return value
    if AMOUNT.fullmatch(value):
        return value.replace(",",".")
    raise Clarification("Укажи количество или «нет». Для обязательства отчитаться также можно написать «да».")
