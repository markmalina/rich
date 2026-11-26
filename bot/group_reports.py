# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Build public report snapshots and count only confirmed Telegram publications."""

from dataclasses import dataclass
import datetime as dt
from decimal import Decimal, InvalidOperation
from uuid import uuid4
import re

from .ledger import FINANCE_ACTIONS, MOSCOW, OUTGOING_ACTIONS, as_date, current_events, value_at


@dataclass(frozen=True)
class Practice:
    action: str
    label: str
    unit: str
    minimum: Decimal | None = None


@dataclass(frozen=True)
class Report:
    text: str
    meets_minimum: bool


def number(value):
    try:
        result = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None
    if result.is_finite() and result.adjusted() >= -9 and abs(result) < 10**15:
        return result
    return None


def date_on(row):
    try:
        return as_date(value_at(row, 0))
    except (OverflowError, ValueError, TypeError):
        return None


def build_report(day, rows, practices, *, finance=False, finance_minimum=None):
    """Practice labels and units come from an agreed group profile, never the source rows."""
    daily = [row for row in current_events(rows) if date_on(row) == day]
    lines = [f"Отчёт за {day:%d.%m.%Y}"]
    meets_minimum = True
    for practice in practices:
        measured = [
            quantity for row in daily
            if value_at(row, 3) == practice.action and value_at(row, 5) == practice.unit
            and value_at(row, 12) != "Деньги"
            and (quantity := number(value_at(row, 4))) is not None and quantity >= 0
        ]
        if measured:
            total = sum(measured)
            display = f"{total.normalize():f} {practice.unit}".rstrip()
        else:
            total = None
            display = "нет записи"
        lines.append(f"{practice.label} — {display}")
        if practice.minimum is not None and (total is None or total < practice.minimum):
            meets_minimum = False
    if finance:
        count = finance_count(daily)
        lines.append(f"Финансы — операций: {count}")
        if finance_minimum is not None and count < finance_minimum:
            meets_minimum = False
    return Report("\n".join(lines), meets_minimum)


def finance_count(rows):
    operations = set()
    for row in rows:
        action = value_at(row, 3)
        amount = number(value_at(row, 4))
        currency = value_at(row, 5)
        identifier = value_at(row, 11)
        if (not isinstance(currency, str) or re.fullmatch(r"[A-Z]{3}", currency) is None
                or not isinstance(identifier, str) or not identifier or value_at(row, 12) != "Деньги"):
            continue
        if amount is not None and ((action in OUTGOING_ACTIONS and amount < 0)
                                   or (action == FINANCE_ACTIONS["income"] and amount > 0)):
            operations.add(identifier)
    return len(operations)


@dataclass(frozen=True)
class Publication:
    chat: int
    author: int
    message: int
    sent_at: int
    via_bot: int
    text: str


class ReportJournal:
    def __init__(self, db):
        self.db = db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS report_membership (
                chat INTEGER NOT NULL, author INTEGER NOT NULL, joined INTEGER,
                membership_update INTEGER NOT NULL, PRIMARY KEY (chat, author)
            );
            CREATE TABLE IF NOT EXISTS report_previews (
                token TEXT PRIMARY KEY, chat INTEGER NOT NULL, author INTEGER NOT NULL,
                day TEXT NOT NULL, issued INTEGER NOT NULL, text TEXT NOT NULL,
                meets_minimum INTEGER NOT NULL, correction INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS report_publications (
                chat INTEGER NOT NULL, message INTEGER NOT NULL, token TEXT NOT NULL UNIQUE,
                sent_at INTEGER NOT NULL, PRIMARY KEY (chat, message),
                FOREIGN KEY (token) REFERENCES report_previews(token)
            );
        """)

    def membership(self, chat, author, active, sent_at, update):
        with self.db:
            self.db.execute("""
                INSERT INTO report_membership VALUES (?, ?, ?, ?)
                ON CONFLICT(chat, author) DO UPDATE SET
                    joined = CASE WHEN excluded.joined IS NULL THEN NULL
                                  ELSE COALESCE(report_membership.joined, excluded.joined) END,
                    membership_update = excluded.membership_update
                WHERE excluded.membership_update > report_membership.membership_update
            """, (chat, author, sent_at if active else None, update))

    def joined(self, chat, author):
        row = self.db.execute(
            "SELECT joined FROM report_membership WHERE chat = ? AND author = ?", (chat, author),
        ).fetchone()
        return row[0] if row else None

    def prepare(self, chat, author, day, issued, report, *, correction=False):
        joined = self.joined(chat, author)
        today = dt.datetime.fromtimestamp(issued, MOSCOW).date()
        if (joined is None or joined > issued or day < dt.datetime.fromtimestamp(joined, MOSCOW).date()
                or day not in (today, today - dt.timedelta(days=1))):
            return None
        token = uuid4().hex
        with self.db:
            self.db.execute("""
                DELETE FROM report_previews WHERE issued < ?
                AND token NOT IN (SELECT token FROM report_publications)
            """, (issued - 3 * 86400,))
            self.db.execute("INSERT INTO report_previews VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (token, chat, author, day.isoformat(), issued, report.text,
                             report.meets_minimum, correction))
        return token

    def has_report(self, chat, author, day):
        return self.db.execute("""
            SELECT 1 FROM report_publications AS publication
            JOIN report_previews AS preview ON preview.token = publication.token
            WHERE preview.chat = ? AND preview.author = ? AND preview.day = ?
        """, (chat, author, day.isoformat())).fetchone() is not None

    def confirm(self, token, published, bot_id):
        """Return accepted, duplicate or rejected for an actual Telegram group message."""
        if published.via_bot != bot_id:
            return "rejected"
        with self.db:
            result = self.db.execute("""
                SELECT chat, author, day, issued, text, correction
                FROM report_previews WHERE token = ?
            """, (token,)).fetchone()
            if not result:
                return "rejected"
            chat, author, day, issued, text, correction = result
            if (published.chat, published.author, published.text) != (chat, author, text):
                return "rejected"
            duplicate = self.db.execute(
                "SELECT token FROM report_publications WHERE chat = ? AND message = ?",
                (chat, published.message),
            ).fetchone()
            if duplicate:
                return "duplicate" if duplicate[0] == token else "rejected"
            joined = self.joined(chat, author)
            if joined is None or not joined <= issued <= published.sent_at:
                return "rejected"
            last_day = dt.date.fromisoformat(day) + dt.timedelta(days=1)
            if dt.datetime.fromtimestamp(published.sent_at, MOSCOW).date() > last_day:
                return "rejected"
            if self.has_report(chat, author, dt.date.fromisoformat(day)) != bool(correction):
                return "rejected"
            saved = self.db.execute("""
                INSERT INTO report_publications VALUES (?, ?, ?, ?) ON CONFLICT(token) DO NOTHING
            """, (chat, published.message, token, published.sent_at))
            return "accepted" if saved.rowcount else "rejected"

    def history(self, chat, viewer):
        joined = self.joined(chat, viewer)
        if joined is None:
            return []
        return self.db.execute("""
            SELECT preview.author, preview.day, preview.text, publication.sent_at
            FROM report_publications AS publication
            JOIN report_previews AS preview ON preview.token = publication.token
            WHERE preview.chat = ? AND publication.sent_at >= ?
            ORDER BY publication.sent_at, publication.message
        """, (chat, joined)).fetchall()

    def recent_history(self, chat, viewer, limit, offset):
        joined = self.joined(chat, viewer)
        if joined is None:
            return []
        return self.db.execute("""
            SELECT preview.author, preview.day, preview.text, publication.sent_at
            FROM report_publications AS publication
            JOIN report_previews AS preview ON preview.token = publication.token
            WHERE preview.chat = ? AND publication.sent_at >= ?
            ORDER BY publication.sent_at DESC, publication.message DESC
            LIMIT ? OFFSET ?
        """, (chat, joined, limit, offset)).fetchall()

    def latest_by_day(self, chat):
        return self.db.execute("""
            WITH versions AS (
                SELECT preview.author, preview.day, preview.text, publication.sent_at, publication.message,
                       ROW_NUMBER() OVER (PARTITION BY preview.author, preview.day
                                          ORDER BY publication.sent_at DESC, publication.message DESC) AS revision
                FROM report_publications AS publication
                JOIN report_previews AS preview ON preview.token = publication.token
                WHERE preview.chat = ?
            )
            SELECT author, day, text, sent_at, message
            FROM versions WHERE revision = 1 ORDER BY day, author
        """, (chat,)).fetchall()

    def missed(self, chat, author, day, now):
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(), tzinfo=MOSCOW).timestamp()
        if now < end:
            return None
        latest = self.db.execute("""
            SELECT preview.meets_minimum FROM report_publications AS publication
            JOIN report_previews AS preview ON preview.token = publication.token
            WHERE preview.chat = ? AND preview.author = ? AND preview.day = ? AND publication.sent_at < ?
            ORDER BY publication.sent_at DESC, publication.message DESC LIMIT 1
        """, (chat, author, day.isoformat(), end)).fetchone()
        return latest is None or not bool(latest[0])
