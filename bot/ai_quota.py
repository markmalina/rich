# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Persistent limit for outbound AI attempts across the RX installation."""

from .ledger import MOSCOW


class AIQuota:
    def __init__(self, db, limit):
        self.db = db
        self.limit = limit
        db.execute("CREATE TABLE IF NOT EXISTS ai_attempts(day TEXT PRIMARY KEY, attempts INTEGER NOT NULL)")
        db.commit()

    def take(self, now):
        if self.limit <= 0:
            return False
        day = now.astimezone(MOSCOW).date().isoformat()
        with self.db:
            result = self.db.execute("""
                INSERT INTO ai_attempts VALUES (?,1)
                ON CONFLICT(day) DO UPDATE SET attempts=attempts+1 WHERE attempts<?
            """, (day, self.limit))
        return bool(result.rowcount)

    def status(self, now):
        day = now.astimezone(MOSCOW).date().isoformat()
        result = self.db.execute("SELECT attempts FROM ai_attempts WHERE day=?", (day,)).fetchone()
        return (result[0] if result else 0), self.limit
