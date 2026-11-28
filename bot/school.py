# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Versioned curriculum and private learner work."""

import datetime as dt
import re
from secrets import token_urlsafe

from .coaching import day_arg
from .ledger import Clarification, MOSCOW

VERSION = "pilot-2026-11-v1"
PROGRAMS = {
    VERSION: (
        ("Один факт", "Запиши реальное действие в дневник. Сверь время, величину и единицу."),
        ("Питание", "Зафиксируй один приём пищи. Проверь, что порция и вес человека записаны отдельно."),
        ("Движение", "Выбери измеримое действие и зафиксируй количество или продолжительность."),
        ("Сон и восстановление", "Запиши фактическое время. Сформулируй, чего хочешь изменить."),
        ("Уточнение", "Уточни одну прежнюю запись. Сверь, какое значение показывает дневной итог."),
        ("Минимум и трудный день", "Определи достижимый минимум и выполнимый вариант сложного дня."),
        ("Личный разбор", "Сравни план и сделанное. Сформулируй одну поправку и время следующей проверки."),
        ("Приглашение клиента", "Составь предложение о сопровождении. Объясни добровольность и договорись об одной практике."),
        ("Условия", "Уточни минимум, дни, трудный день, приватность и окно ответа."),
        ("Вечерняя отметка", "Разбери учебную отметку или отметку согласившегося человека. Проверь дату и единицу."),
        ("Пропуск", "Напиши нейтральный ответ на пропуск. Пусть человек сам предложит следующий маленький шаг."),
        ("Обратная связь", "Ответь предметно. Назови наблюдение, уточни препятствие и согласуй проверяемое действие."),
        ("День и оплата", "Разбери примеры паузы, активного дня и фактической оплаты. Отмечай их раздельно."),
        ("Практика сопровождения", "Сдай разбор первых дней. Укажи, было ли это упражнение или работа с согласившимся человеком."),
        ("Понятное предложение", "Опиши для кого, какую практику и в каком формате ты сопровождаешь."),
        ("Тема контента", "Выбери одну историю о собственной практике. Убери детали других людей."),
        ("Черновик с ИИ", "Проси ИИ построить структуру и варианты формулировок на вымышленных данных."),
        ("Материал для аудитории", "Подготовь короткий ролик, текст или сторис. Приватный черновик засчитывается."),
        ("Приглашение без давления", "Напиши приглашение на разговор и способ отказаться."),
        ("Разговор о результате", "Подготовь вопросы о цели, удобстве и предыдущих попытках."),
        ("План продолжения", "Сдай кейс и одну следующую цель. Публикуй только по собственному решению."),
    )
}
CHECKPOINTS = (7, 14, 21)
COHORT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
SCHOOL_HELP = """Школа трекера + продвижение · 21 день
/school — мои заказы
/school_day ID [номер] — открытый урок
/school_work ID день | текст или отметка о приватном черновике
/school_share ID день | текст — проверить получателя работы
/school_history ID — задания и комментарии
/school_status ID — обучение, клиенты и подтверждённая оплата сопровождения
Публикация в соцсетях добровольна. Программа засчитывает личный черновик."""


def school_day(start, now):
    return min(21, max(0, (now.astimezone(MOSCOW).date()-day_arg(start)).days+1))


def clean_space(value, limit=600):
    value = value.strip()
    if not value or len(value)>limit:
        raise Clarification(f"Нужно непустое значение до {limit} знаков.")
    return value


def create_cohort(db, cohort, starts, demo, now):
    if COHORT_ID.fullmatch(cohort) is None:
        raise Clarification("ID потока: строчные латинские буквы, цифры, дефис или подчёркивание.")
    existing = db.execute("SELECT starts,version,demo FROM school_cohorts WHERE id=?", (cohort,)).fetchone()
    expected = (str(starts), VERSION, int(demo))
    if existing:
        if existing != expected:
            raise Clarification("Поток уже существует с другими параметрами.")
        return False
    db.execute("INSERT INTO school_cohorts VALUES (?,?,?,?,?)",
                    (cohort, str(starts), VERSION, int(demo), now.isoformat()))
    return True

class School:
    def __init__(self, db, *, access_scope=None):
        self.db = db
        self.access_scope = access_scope or (lambda user: "open")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS school_enrolments(
                order_id INTEGER PRIMARY KEY, learner INTEGER NOT NULL, mentor INTEGER NOT NULL,
                starts TEXT NOT NULL, version TEXT NOT NULL, finished TEXT, finished_by INTEGER, suspended INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS school_work(
                order_id INTEGER NOT NULL, day INTEGER NOT NULL, update_id INTEGER NOT NULL,
                learner INTEGER NOT NULL, text TEXT NOT NULL, shared INTEGER NOT NULL, submitted TEXT NOT NULL,
                PRIMARY KEY(order_id,day,update_id)
            );
            CREATE TABLE IF NOT EXISTS school_comments(
                order_id INTEGER NOT NULL, day INTEGER NOT NULL, update_id INTEGER NOT NULL,
                mentor INTEGER NOT NULL, text TEXT NOT NULL, submitted TEXT NOT NULL,
                PRIMARY KEY(order_id,day,update_id)
            );
            CREATE TABLE IF NOT EXISTS school_activity(
                order_id INTEGER NOT NULL, active_on TEXT NOT NULL, PRIMARY KEY(order_id,active_on)
            );
            CREATE TABLE IF NOT EXISTS school_share_previews (
                token TEXT PRIMARY KEY, order_id INTEGER NOT NULL, day INTEGER NOT NULL,
                draft_update INTEGER NOT NULL, mentor INTEGER NOT NULL, revision INTEGER NOT NULL,
                learner_scope TEXT NOT NULL, mentor_scope TEXT NOT NULL, expires TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS school_cohorts(
                id TEXT PRIMARY KEY, starts TEXT NOT NULL, version TEXT NOT NULL,
                demo INTEGER NOT NULL, created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS school_order_cohorts(
                order_id INTEGER PRIMARY KEY, cohort TEXT NOT NULL, source TEXT NOT NULL,
                consent_version TEXT
            );
            CREATE TABLE IF NOT EXISTS school_memberships(
                cohort TEXT NOT NULL, learner INTEGER NOT NULL, joined TEXT NOT NULL,
                source TEXT NOT NULL, consent_version TEXT, state TEXT NOT NULL,
                initial INTEGER NOT NULL, PRIMARY KEY(cohort,learner)
            );
            CREATE TABLE IF NOT EXISTS school_included_coaching(
                order_id INTEGER PRIMARY KEY, learner INTEGER NOT NULL, tracker INTEGER NOT NULL,
                starts TEXT NOT NULL, ends TEXT NOT NULL
            );
        """)
        db.execute("""
            INSERT OR IGNORE INTO school_included_coaching(order_id,learner,tracker,starts,ends)
            SELECT order_id,learner,mentor,starts,date(starts,'+6 days') FROM school_enrolments
        """)
        self.migrate_sharing()

    def migrate_sharing(self):
        additions = {
            "school_enrolments": {"mentor_revision": "INTEGER NOT NULL DEFAULT 1"},
            "school_work": {"shared_with": "INTEGER", "mentor_revision": "INTEGER"},
            "school_comments": {"mentor_revision": "INTEGER"},
        }
        with self.db:
            for table, columns in additions.items():
                existing = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, definition in columns.items():
                    if column not in existing:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def confirm_share(self, order_id, learner, mentor, revision, token, now):
        preview = self.db.execute("""
            SELECT p.day,p.draft_update
            FROM school_share_previews p
            JOIN school_work w ON w.order_id=p.order_id AND w.day=p.day AND w.update_id=p.draft_update
            WHERE p.token=? AND p.order_id=? AND w.learner=? AND w.shared=0
              AND p.mentor=? AND p.revision=? AND p.learner_scope=? AND p.mentor_scope=?
              AND p.expires>=?
        """, (token, order_id, learner, mentor, revision, self.access_scope(learner),
              self.access_scope(mentor), now.astimezone(MOSCOW).isoformat())).fetchone()
        if preview is None:
            raise Clarification("Подтверждение устарело или уже использовано. Подготовь отправку заново.")
        day, draft_update = preview
        self.db.execute("""
            UPDATE school_work SET shared=1, shared_with=?, mentor_revision=?
            WHERE order_id=? AND day=? AND update_id=?
        """, (mentor, revision, order_id, day, draft_update))
        self.db.execute("DELETE FROM school_share_previews WHERE token=?", (token,))
        return "Работа передана подтверждённому наставнику."

    def preview_share(self, order_id, day, update_id, learner, mentor, revision, now):
        learner_scope, mentor_scope = self.access_scope(learner), self.access_scope(mentor)
        if learner_scope is None or mentor_scope is None:
            raise Clarification("Передача пока недоступна. Оператор может проверить назначение наставника.")
        token = token_urlsafe(12)
        expires = (now.astimezone(MOSCOW) + dt.timedelta(minutes=15)).isoformat()
        self.db.execute("DELETE FROM school_share_previews WHERE expires<? OR (order_id=? AND day=?)",
                        (now.astimezone(MOSCOW).isoformat(), order_id, day))
        self.db.execute("INSERT INTO school_share_previews VALUES (?,?,?,?,?,?,?,?,?)",
                        (token, order_id, day, update_id, mentor, revision, learner_scope, mentor_scope, expires))
        return (f"Получатель работы за день {day} — назначенный наставник, Telegram ID {mentor}. "
                f"Для передачи в течение 15 минут отправь /school_confirm_share {order_id} {token}.")

    def create_cohort(self, cohort, starts, demo, now):
        return create_cohort(self.db, cohort, starts, demo, now)

    def assign_order(self, order_id, cohort, source, consent_version=None):
        mode = self.db.execute("""
            SELECT c.demo,o.demo FROM school_cohorts c JOIN pilot_orders o ON o.id=? WHERE c.id=?
        """, (order_id, cohort)).fetchone()
        if not mode:
            raise Clarification("Именованный поток не найден.")
        if mode[0] != mode[1]:
            raise Clarification("Режим заказа не совпадает с режимом потока.")
        current = self.db.execute("SELECT cohort,source,consent_version FROM school_order_cohorts WHERE order_id=?",
                                  (order_id,)).fetchone()
        expected = (cohort, source, consent_version)
        if current and current != expected:
            raise Clarification("Заказ уже связан с другим потоком или основанием участия.")
        self.db.execute("INSERT OR IGNORE INTO school_order_cohorts VALUES (?,?,?,?)",
                        (order_id, cohort, source, consent_version))

    def enrol(self, order_id, learner, mentor, starts, now):
        assignment = self.db.execute("""
            SELECT c.id,c.starts,c.version,c.demo,a.source,a.consent_version
            FROM school_order_cohorts a JOIN school_cohorts c ON c.id=a.cohort WHERE a.order_id=?
        """, (order_id,)).fetchone()
        version = VERSION
        if assignment:
            cohort, cohort_start, version, _, source, consent_version = assignment
            starts = day_arg(cohort_start)
        self.db.execute("INSERT OR IGNORE INTO school_enrolments(order_id,learner,mentor,starts,version) VALUES (?,?,?,?,?)",
                        (order_id, learner, mentor, str(starts), version))
        self.db.execute("INSERT OR IGNORE INTO school_included_coaching VALUES (?,?,?,?,?)",
                        (order_id, learner, mentor, str(starts), str(starts + dt.timedelta(days=6))))
        if assignment:
            joined_day = now.astimezone(MOSCOW).date()
            self.db.execute("INSERT OR IGNORE INTO school_memberships VALUES (?,?,?,?,?,'active',?)",
                            (cohort, learner, now.isoformat(), source, consent_version,
                             int(joined_day <= starts)))

    def mark_active(self, order_id, now):
        day = now.astimezone(MOSCOW).date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO school_activity VALUES (?,?)", (order_id, day))

    def cohorts(self, demo, through):
        return self.db.execute("""
            WITH checkpoints(day) AS (VALUES (7),(21)), eligible AS (
                SELECT e.order_id,e.learner,e.starts,e.version,c.day,date(e.starts,'+'||(c.day-1)||' days') AS target
                FROM school_enrolments e JOIN pilot_orders o ON o.id=e.order_id CROSS JOIN checkpoints c
                WHERE o.kind='package' AND o.state='paid' AND o.demo=?
                  AND NOT EXISTS (SELECT 1 FROM school_order_cohorts m WHERE m.order_id=e.order_id)
            )
            SELECT starts,version,day,COUNT(DISTINCT learner),COUNT(DISTINCT CASE WHEN
                EXISTS (SELECT 1 FROM school_activity a WHERE a.order_id=e.order_id AND a.active_on=e.target)
                OR EXISTS (SELECT 1 FROM school_work w WHERE w.order_id=e.order_id AND substr(w.submitted,1,10)=e.target)
                THEN learner END)
            FROM eligible e WHERE target<=? GROUP BY starts,version,day
            ORDER BY starts DESC,version,day LIMIT 32
        """, (int(demo), through.isoformat())).fetchall()

    def named_cohorts(self, demo, through):
        reports = []
        for cohort, starts, version in self.db.execute(
                "SELECT id,starts,version FROM school_cohorts WHERE demo=? ORDER BY starts DESC,id",
                (int(demo),)):
            start = day_arg(starts)
            initial, late, withdrawn = self.db.execute("""
                SELECT SUM(initial=1),SUM(initial=0),SUM(state='withdrawn')
                FROM school_memberships WHERE cohort=?
            """, (cohort,)).fetchone()
            initial, late, withdrawn = initial or 0, late or 0, withdrawn or 0
            for day in (7, 21):
                target = start + dt.timedelta(days=day - 1)
                active_initial, active_late = self.db.execute("""
                    SELECT COUNT(DISTINCT CASE WHEN m.initial=1 THEN m.learner END),
                           COUNT(DISTINCT CASE WHEN m.initial=0 THEN m.learner END)
                    FROM school_memberships m
                    WHERE m.cohort=? AND EXISTS (
                        SELECT 1 FROM school_order_cohorts oc
                        WHERE oc.cohort=m.cohort AND EXISTS (
                            SELECT 1 FROM school_enrolments e WHERE e.order_id=oc.order_id AND e.learner=m.learner
                        ) AND (
                            EXISTS (SELECT 1 FROM school_activity a
                                    WHERE a.order_id=oc.order_id AND a.active_on=?)
                            OR EXISTS (SELECT 1 FROM school_work w
                                       WHERE w.order_id=oc.order_id AND substr(w.submitted,1,10)=?)
                        )
                    )
                """, (cohort, str(target), str(target))).fetchone()
                state = "не наступил" if through < target else "предварительно" if through == target else "итог"
                reports.append((cohort, starts, version, day, target, state, initial, active_initial,
                                late, active_late, withdrawn))
        return reports

    def member_state(self, cohort, learner, state):
        if state not in {"active", "withdrawn"}:
            raise Clarification("Состояние участника: active или withdrawn.")
        updated = self.db.execute("UPDATE school_memberships SET state=? WHERE cohort=? AND learner=?",
                                  (state, cohort, learner))
        if not updated.rowcount:
            raise Clarification("Участник в потоке не найден.")

    def handle(self, command, order_id, words, fields, user, now, update_id):
        item = self.db.execute("""
            SELECT learner,mentor,starts,version,finished,suspended,mentor_revision
            FROM school_enrolments WHERE order_id=? AND (learner=? OR mentor=?)
        """, (order_id,user,user)).fetchone()
        if not item:
            raise Clarification("Зачисление не найдено или ещё ждёт подтверждения.")
        learner,mentor,starts,version,finished,suspended,revision = item
        if suspended:
            raise Clarification("Доступ к курсу приостановлен оператором. Условия заказа доступны отдельно.")
        if user != learner and self.access_scope(learner) is None:
            raise Clarification("Доступ к работе ученика приостановлен.")
        if command == "/school_confirm_share":
            if user != learner or len(words) != 1 or fields or not re.fullmatch(r"[A-Za-z0-9_-]{16}", words[0]):
                raise Clarification("Укажи действующую команду подтверждения из своего предпросмотра.")
            return self.confirm_share(order_id, learner, mentor, revision, words[0], now)
        unlocked = school_day(starts,now)
        if command == "/school_status":
            return self.status(learner, version, unlocked, finished)
        if command == "/school_history":
            return self.history(order_id, None if user == learner else mentor, revision)
        if command == "/school_finish":
            if user != mentor or unlocked != 21:
                raise Clarification("Завершение подтверждает наставник после открытия дня 21.")
            existing = self.db.execute("SELECT DISTINCT day FROM school_work WHERE order_id=?",(order_id,)).fetchall()
            if any((day,) not in existing for day in CHECKPOINTS):
                raise Clarification("Нужны итоговые работы дней 7, 14 и 21.")
            self.db.execute("UPDATE school_enrolments SET finished=?,finished_by=? WHERE order_id=? AND finished IS NULL",
                            (now.isoformat(),mentor,order_id))
            return "Завершение курса подтверждено."
        day = int(words[0]) if words and re.fullmatch(r"[0-9]{1,2}",words[0]) else unlocked if not words else -1
        if not 1<=day<=unlocked:
            raise Clarification("Этот день ещё не открыт." if day>unlocked else "Укажи номер открытого дня.")
        if command == "/school_day":
            name, task = PROGRAMS[version][day-1]
            module = "Опыт клиента" if day<=7 else "Сопровождение" if day<=14 else "Продвижение"
            if user == learner:
                self.mark_active(order_id, now)
            return f"День {day}/21 · {module} · {name}\n{task}\nДата старта {starts} МСК. Версия {version}."
        if command in {"/school_work","/school_share"} and user == learner and len(fields) == 1:
            work = clean_space(fields[0])
            share = command == "/school_share"
            if finished:
                existing = self.db.execute("""
                    SELECT 1 FROM school_work WHERE order_id=? AND day=? AND learner=? AND text=? LIMIT 1
                """, (order_id, day, learner, work)).fetchone()
                if not share or not existing:
                    raise Clarification("После завершения можно передать наставнику только свою сохранённую работу.")
            self.db.execute("""
                INSERT OR IGNORE INTO school_work (order_id,day,update_id,learner,text,shared,submitted)
                VALUES (?,?,?,?,?,0,?)
            """, (order_id, day, update_id, learner, work, now.isoformat()))
            if not finished:
                self.mark_active(order_id, now)
            if share:
                return self.preview_share(order_id, day, update_id, learner, mentor, revision, now)
            return "Личный черновик сохранён. Для проверки наставником начни с /school_share."
        if command == "/school_feedback" and user == mentor and len(fields) == 1:
            shared = self.db.execute("""
                SELECT 1 FROM school_work WHERE order_id=? AND day=? AND shared_with=? AND mentor_revision=?
            """, (order_id, day, mentor, revision)).fetchone()
            if not shared:
                raise Clarification("Ученик пока не передал работу наставнику.")
            self.db.execute("""
                INSERT OR IGNORE INTO school_comments (order_id,day,update_id,mentor,text,submitted,mentor_revision)
                VALUES (?,?,?,?,?,?,?)
            """, (order_id, day, update_id, mentor, clean_space(fields[0]), now.isoformat(), revision))
            return "Комментарий доступен ученику."
        raise Clarification("Команда недоступна. Список: /school.")

    def history(self, order_id, recipient, revision):
        rows = self.db.execute("""
            SELECT day,text,shared FROM school_work
            WHERE order_id=? AND (? IS NULL OR (shared_with=? AND mentor_revision=?))
            ORDER BY update_id DESC LIMIT 5
        """, (order_id, recipient, recipient, revision)).fetchall()
        comments = self.db.execute("""
            SELECT day,text FROM school_comments
            WHERE order_id=? AND (? IS NULL OR (mentor=? AND mentor_revision=?))
            ORDER BY update_id DESC LIMIT 5
        """, (order_id, recipient, recipient, revision)).fetchall()
        return "\n".join([*(f"День {d} · {'передано' if shared else 'личный черновик'}: {note}" for d,note,shared in rows),
                         *(f"День {d} · наставник: {note}" for d,note in comments)]) or "Доступных работ и комментариев пока нет."

    def status(self, learner, version, unlocked, finished):
        has_coaching = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coach_links'").fetchone()
        clients=paying=paid=0
        first_type = None
        if has_coaching:
            clients,paying,paid = self.db.execute("""
                SELECT COUNT(DISTINCT l.client),COUNT(DISTINCT CASE WHEN p.day IS NOT NULL THEN l.client END),
                       COUNT(p.day) FROM coach_links l
                JOIN coach_calendar c ON c.link=l.id AND c.active=1
                LEFT JOIN coach_payments p ON p.link=l.id AND NOT EXISTS (
                    SELECT 1 FROM school_included_coaching s
                    JOIN school_enrolments e ON e.order_id=s.order_id AND e.suspended=0
                    WHERE s.learner=l.client AND s.tracker=l.tracker AND p.day BETWEEN s.starts AND s.ends
                )
                WHERE l.tracker=? AND l.demo=0 AND c.day=(SELECT MIN(day) FROM coach_calendar
                                                         WHERE link=l.id AND active=1)
            """,(learner,)).fetchone()
            first = self.db.execute("""
                SELECT l.client FROM coach_payments p JOIN coach_links l ON l.id=p.link
                WHERE l.tracker=? AND l.demo=0 AND NOT EXISTS (
                    SELECT 1 FROM school_included_coaching s
                    JOIN school_enrolments e ON e.order_id=s.order_id AND e.suspended=0
                    WHERE s.learner=l.client AND s.tracker=l.tracker AND p.day BETWEEN s.starts AND s.ends
                ) ORDER BY p.recorded,p.link,p.day LIMIT 1
            """, (learner,)).fetchone()
            if first:
                first_type = ("участник школы" if self.db.execute(
                    "SELECT 1 FROM school_enrolments WHERE learner=? LIMIT 1", first,
                ).fetchone() else "независимый клиент")
        earning = f" Первый заработок: {first_type}." if first_type else " Первый заработок ещё не подтверждён."
        return (f"{'Курс завершён' if finished else 'Обучение'} · день {unlocked}/21 · {version}. "
                f"Подтверждённая практика: {clients}. Фактически платящих клиентов: {paying}. "
                f"Оплаченных дней: {paid}.{earning}")
