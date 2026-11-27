# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Persist uncertain source writes and resume deferred private updates in order."""

import json

from .event_write import INBOX, inbox_index
from .providers import ProviderError


class InboxPending(ProviderError):
    def __init__(self):
        super().__init__("Входящие RX", "ожидает сверки")


REJECTED = {400, 401, 403, 429}
MEDIA = ("voice", "audio", "document", "photo")
MEDIA_FIELDS = ("file_id", "file_unique_id", "file_name", "mime_type", "file_size")


def media_fields(item):
    return {field: item[field] for field in MEDIA_FIELDS if field in item}


def message_payload(message):
    """Retain only the private update fields used by the bot, without profile details."""
    result = {key: message[key] for key in ("message_id", "date", "text", "caption") if key in message}
    for key in ("from", "chat"):
        if key in message:
            result[key] = {field: message[key][field] for field in ("id", "type") if field in message[key]}
    for key in MEDIA:
        if media := message.get(key):
            result[key] = [media_fields(item) for item in media] if key == "photo" else media_fields(media)
    if reply := message.get("reply_to_message"):
        result["reply_to_message"] = message_payload({key: value for key, value in reply.items()
                                                       if key not in {"reply_to_message", "text", "caption"}})
    return result


class InboxWrites:
    def __init__(self, db):
        self.db = db
        db.executescript("""
            CREATE TABLE IF NOT EXISTS inbox_claims (
                origin TEXT PRIMARY KEY, spreadsheet TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deferred_inbox (
                update_id INTEGER PRIMARY KEY, user INTEGER NOT NULL, received TEXT NOT NULL,
                payload TEXT NOT NULL, queued_at INTEGER NOT NULL, next_check INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS deferred_inbox_by_user ON deferred_inbox (user, update_id);
        """)

    def capture(self, sheets, row):
        origin = row[0]
        saved = self.db.execute("SELECT spreadsheet FROM inbox_claims WHERE origin=?", (origin,)).fetchone()
        if saved is not None and saved[0] != sheets.spreadsheet_id:
            raise InboxPending()
        try:
            found = inbox_index(sheets, origin)
            if found is None and saved is None:
                with self.db:
                    self.db.execute("INSERT INTO inbox_claims VALUES (?, ?)", (origin, sheets.spreadsheet_id))
                try:
                    sheets.append(f"'{INBOX}'!A:H", [row])
                except ProviderError as error:
                    # An Apps Script error can come from reloading the response after a write.
                    if error.provider == "Google Sheets" and error.status in REJECTED:
                        self.clear_claim(origin)
                    raise
                found = inbox_index(sheets, origin)
        except ProviderError:
            raise InboxPending() from None
        if found is None:
            raise InboxPending()
        self.clear_claim(origin)
        return found

    def clear_claim(self, origin):
        with self.db:
            self.db.execute("DELETE FROM inbox_claims WHERE origin=?", (origin,))

    def waiting(self, user):
        return self.db.execute("SELECT 1 FROM deferred_inbox WHERE user=? LIMIT 1", (user,)).fetchone() is not None

    def defer(self, update_id, user, message, received):
        with self.db:
            self.db.execute("""
                INSERT OR IGNORE INTO deferred_inbox (update_id, user, received, payload, queued_at)
                VALUES (?, ?, ?, ?, CAST(strftime('%s','now') AS INTEGER))
            """, (update_id, user, received.isoformat(), json.dumps(message_payload(message))))

    def due(self, offset, now):
        return self.db.execute("""
            SELECT update_id, user, received, payload FROM deferred_inbox AS pending
            WHERE update_id < ? AND next_check <= ? AND NOT EXISTS (
                SELECT 1 FROM deferred_inbox AS earlier
                WHERE earlier.user = pending.user AND earlier.update_id < pending.update_id
            )
            ORDER BY next_check, update_id LIMIT 2
        """, (offset, now)).fetchall()

    def postpone(self, update_id, next_check):
        with self.db:
            self.db.execute("UPDATE deferred_inbox SET next_check=? WHERE update_id=?", (next_check, update_id))

    def finish(self, update_id):
        with self.db:
            self.db.execute("DELETE FROM deferred_inbox WHERE update_id=?", (update_id,))
