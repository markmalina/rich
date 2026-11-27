# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from collections import Counter
from dataclasses import dataclass
import json
import logging

from .ledger import value_at
from .providers import ProviderError

INBOX = "Входящие"
EVENTS = "События"
EVENT_WIDTH = 15


def inbox_index(sheets, origin):
    return next((i for i, row in enumerate(sheets.read(f"'{INBOX}'!A2:A"), start=2)
                 if row and row[0] == origin), None)


def stored_events(sheets, origin):
    return [row for row in sheets.read(f"'{EVENTS}'!A2:O") if value_at(row, 8) == origin]


def row_fingerprints(rows):
    def typed(value):
        return (type(value) is bool, "" if value is None else value)
    return Counter(tuple(typed(value_at(row, column)) for column in range(EVENT_WIDTH)) for row in rows)


@dataclass(frozen=True)
class EventWrite:
    user: int
    origin: str
    update_id: int
    spreadsheet: str
    rows: list
    fallback: str
    question: dict | None
    sent: bool = False


class EventWrites:
    """Track sent requests before touching Sheets so an uncertain append cannot be repeated."""

    def __init__(self, db):
        self.db = db
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS event_writes (
                user INTEGER NOT NULL, origin TEXT NOT NULL, update_id INTEGER NOT NULL,
                spreadsheet TEXT NOT NULL, rows TEXT NOT NULL, fallback TEXT NOT NULL, question TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0, next_check INTEGER NOT NULL DEFAULT 0, queued_at INTEGER,
                PRIMARY KEY (user, origin), UNIQUE (user, update_id)
            )
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(event_writes)")}
        if "queued_at" not in columns:
            self.db.execute("ALTER TABLE event_writes ADD COLUMN queued_at INTEGER")
        self.db.execute("UPDATE event_writes SET queued_at=CAST(strftime('%s','now') AS INTEGER) WHERE queued_at IS NULL")
        self.db.commit()

    @staticmethod
    def decode(row):
        user, origin, update_id, spreadsheet, rows, fallback, question, sent = row
        return EventWrite(user, origin, update_id, spreadsheet, json.loads(rows), fallback, json.loads(question),
                          bool(sent))

    def find(self, user, update_id):
        row = self.db.execute("""
            SELECT user, origin, update_id, spreadsheet, rows, fallback, question, sent
            FROM event_writes WHERE user = ? AND update_id = ?
        """, (user, update_id)).fetchone()
        return self.decode(row) if row else None

    def pending_event(self, user, spreadsheet, key):
        batches = self.db.execute("SELECT rows FROM event_writes WHERE user = ? AND spreadsheet = ?",
                                  (user, spreadsheet))
        return any(value_at(event, 11) == key for (rows,) in batches for event in json.loads(rows))

    def stage(self, batch):
        with self.db:
            saved = self.db.execute("""
                INSERT INTO event_writes (user, origin, update_id, spreadsheet, rows, fallback, question, queued_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER)) ON CONFLICT(user, origin) DO NOTHING
            """, (batch.user, batch.origin, batch.update_id, batch.spreadsheet, json.dumps(batch.rows),
                  batch.fallback, json.dumps(batch.question)))
        return bool(saved.rowcount)

    def due(self, offset, now):
        return [self.decode(row) for row in self.db.execute("""
            SELECT user, origin, update_id, spreadsheet, rows, fallback, question, sent
            FROM event_writes WHERE update_id < ? AND next_check <= ?
            ORDER BY next_check, update_id LIMIT 2
        """, (offset, now)).fetchall()]

    def postpone(self, batch, next_check):
        with self.db:
            self.db.execute("UPDATE event_writes SET next_check = ? WHERE user = ? AND origin = ?",
                            (next_check, batch.user, batch.origin))

    def finish(self, user, origin):
        with self.db:
            self.db.execute("DELETE FROM event_writes WHERE user = ? AND origin = ?", (user, origin))

    def advance(self, batch, sheets):
        if sheets.spreadsheet_id != batch.spreadsheet:
            raise ProviderError("Запись RX", "назначение изменилось")
        lookup = inbox_index(sheets, batch.origin)
        if lookup is None:
            raise ProviderError("Google Sheets", "source missing")
        if batch.sent:
            return self.verify(batch, sheets)
        if any(row and row[0] == batch.origin for row in sheets.read(f"'{EVENTS}'!I2:L")):
            return "existing"
        sheets.update(f"'{INBOX}'!G{lookup}", [[batch.fallback]])
        with self.db:
            claimed = self.db.execute(
                "UPDATE event_writes SET sent = 1 WHERE user = ? AND origin = ? AND sent = 0",
                (batch.user, batch.origin),
            )
        if claimed.rowcount != 1:
            return None
        try:
            sheets.append(f"'{EVENTS}'!A:O", batch.rows)
        except ProviderError as error:
            logging.warning("Ответ на запись RX: %s", error)
            if error.provider == "Google Sheets" and error.status == 429:
                with self.db:
                    self.db.execute("UPDATE event_writes SET sent = 0 WHERE user = ? AND origin = ?",
                                    (batch.user, batch.origin))
                return None
        return self.verify(batch, sheets)

    @staticmethod
    def verify(batch, sheets):
        actual = stored_events(sheets, batch.origin)
        return "verified" if row_fingerprints(actual) == row_fingerprints(batch.rows) else None
