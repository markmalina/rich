# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Versioned curriculum and private learner work."""

import datetime as dt
import re

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
SCHOOL_HELP = """Школа трекера + продвижение · 21 день
/school — мои заказы
/school_day ID [номер] — открытый урок
/school_work ID день | текст или отметка о приватном черновике
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


class School:
    def __init__(self, db):
        self.db = db
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
        """)

    def enrol(self, order_id, learner, mentor, starts):
        self.db.execute("INSERT OR IGNORE INTO school_enrolments(order_id,learner,mentor,starts,version) VALUES (?,?,?,?,?)",
                        (order_id, learner, mentor, str(starts), VERSION))

    def mark_active(self, order_id, now):
        day = now.astimezone(MOSCOW).date().isoformat()
        self.db.execute("INSERT OR IGNORE INTO school_activity VALUES (?,?)", (order_id, day))

    def cohorts(self, demo, through):
        return self.db.execute("""
            WITH checkpoints(day) AS (VALUES (7),(21)), eligible AS (
                SELECT e.order_id,e.learner,e.starts,e.version,c.day,date(e.starts,'+'||(c.day-1)||' days') AS target
                FROM school_enrolments e JOIN pilot_orders o ON o.id=e.order_id CROSS JOIN checkpoints c
                WHERE o.kind='package' AND o.state='paid' AND o.demo=?
            )
            SELECT starts,version,day,COUNT(DISTINCT learner),COUNT(DISTINCT CASE WHEN
                EXISTS (SELECT 1 FROM school_activity a WHERE a.order_id=e.order_id AND a.active_on=e.target)
                OR EXISTS (SELECT 1 FROM school_work w WHERE w.order_id=e.order_id AND substr(w.submitted,1,10)=e.target)
                THEN learner END)
            FROM eligible e WHERE target<=? GROUP BY starts,version,day
            ORDER BY starts DESC,version,day LIMIT 32
        """, (int(demo), through.isoformat())).fetchall()

    def handle(self, command, order_id, words, fields, user, now, update_id):
        item = self.db.execute("""
            SELECT learner,mentor,starts,version,finished,suspended
            FROM school_enrolments WHERE order_id=? AND (learner=? OR mentor=?)
        """, (order_id,user,user)).fetchone()
        if not item:
            raise Clarification("Зачисление не найдено или ещё ждёт подтверждения.")
        learner,mentor,starts,version,finished,suspended = item
        if suspended:
            raise Clarification("Доступ к курсу приостановлен оператором. Условия заказа доступны отдельно.")
        unlocked = school_day(starts,now)
        if command == "/school_status":
            return self.status(learner, version, unlocked, finished)
        if command == "/school_history":
            return self.history(order_id,user == mentor)
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
        if command in {"/school_work","/school_share"} and user == learner and len(fields)==1 and not finished:
            work = clean_space(fields[0])
            self.db.execute("INSERT OR IGNORE INTO school_work VALUES (?,?,?,?,?,?,?)",
                            (order_id,day,update_id,learner,work,int(command=="/school_share"),now.isoformat()))
            self.mark_active(order_id, now)
            return ("Работа передана твоему наставнику." if command=="/school_share" else
                    "Личный черновик сохранён. Для проверки наставником нужно явно отправить /school_share.")
        if command == "/school_feedback" and user == mentor and len(fields)==1:
            if not self.db.execute("SELECT 1 FROM school_work WHERE order_id=? AND day=? AND shared=1",
                                   (order_id,day)).fetchone():
                raise Clarification("Ученик пока не передал работу наставнику.")
            self.db.execute("INSERT OR IGNORE INTO school_comments VALUES (?,?,?,?,?,?)",
                            (order_id,day,update_id,mentor,clean_space(fields[0]),now.isoformat()))
            return "Комментарий доступен ученику."
        raise Clarification("Команда недоступна. Список: /school.")

    def history(self, order_id, mentor):
        rows = self.db.execute("""
            SELECT day,text,shared FROM school_work WHERE order_id=? AND (shared=1 OR ?=0)
            ORDER BY update_id DESC LIMIT 5
        """,(order_id,int(mentor))).fetchall()
        comments = self.db.execute("""
            SELECT day,text FROM school_comments WHERE order_id=? ORDER BY update_id DESC LIMIT 5
        """,(order_id,)).fetchall()
        return "\n".join([*(f"День {d} · {'передано' if shared else 'личный черновик'}: {note}" for d,note,shared in rows),
                         *(f"День {d} · наставник: {note}" for d,note in comments)]) or "Доступных работ и комментариев пока нет."

    def status(self, learner, version, unlocked, finished):
        has_coaching = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='coach_links'").fetchone()
        clients=paid=0
        if has_coaching:
            clients,paid = self.db.execute("""
                SELECT COUNT(DISTINCT l.client), COUNT(p.day) FROM coach_links l
                JOIN coach_calendar c ON c.link=l.id AND c.active=1
                LEFT JOIN coach_payments p ON p.link=l.id
                WHERE l.tracker=? AND l.demo=0 AND c.day=(SELECT MIN(day) FROM coach_calendar
                                                         WHERE link=l.id AND active=1)
            """,(learner,)).fetchone()
        return (f"{'Курс завершён' if finished else 'Обучение'} · день {unlocked}/21 · {version}. "
                f"Реальных клиентов: {clients}. Подтверждено оплаченных дней: {paid}.")
