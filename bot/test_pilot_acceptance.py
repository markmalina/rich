# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

from .group_telegram import GroupTelegram, configured_groups
from .ledger import MOSCOW
from .main import Bot
from .onboarding import DRIVE_URL, REGISTRY_HEADERS, SHEET_MIME, PilotProvisioner
from .providers import SHEETS_URL
from .test_tracker import FakeGoogle, event

CHAT = -1000012345
OWNER, TRACKER, MEMBER = 7, 10, 20
NOW = dt.datetime(2026, 11, 25, 20, 15, tzinfo=MOSCOW)
STAMP = int(NOW.timestamp())
READING = "Чтение книги"


class Grid:
    def __init__(self, gid, title, rows=None):
        self.properties = {"sheetId": gid, "title": title}
        self.rows = rows or []

    def read(self, row, col, end_row, end_col):
        values = [list(line[col:end_col]) for line in self.rows[row:end_row]]
        for line in values:
            while line and line[-1] == "":
                line.pop()
        while values and not values[-1]:
            values.pop()
        return values

    def write(self, row, col, values):
        for index, line in enumerate(values, row):
            while index >= len(self.rows):
                self.rows.append([])
            current = self.rows[index]
            current.extend([""] * max(0, col + len(line) - len(current)))
            current[col:col + len(line)] = line


class PilotGoogle:
    """An in-memory Drive and Sheets transport for the ranges exercised by the pilot."""

    def __init__(self):
        self.session = object()
        self.books = {"registry": {0: Grid(0, "Пары", [REGISTRY_HEADERS.copy()])}}
        self.created = {}

    def request(self, session, provider, method, url, params=None, json=None, **_):
        assert session is self.session and provider in {"Google pilot", "Google Sheets"}, (provider, method)
        payload = self.drive(method, params, json) if url == DRIVE_URL else self.sheets(method, url, json)
        return Mock(json=Mock(return_value=payload))

    def drive(self, method, params, body):
        if method == "GET":
            tag = re.search(r"value='([^']+)'", params["q"])[1]
            return {"files": [{"id": file} for file, value in self.created.items() if value == tag]}
        assert method == "POST" and body["mimeType"] == SHEET_MIME and "parents" not in body, body
        identifier = f"test-file-{len(self.created) + 1}"
        self.created[identifier] = body["appProperties"]["rxPilot"]
        self.books[identifier] = {0: Grid(0, "Sheet1")}
        return {"id": identifier}

    def sheets(self, method, url, body):
        assert url.startswith(SHEETS_URL + "/"), url
        file, _, operation = url.removeprefix(SHEETS_URL + "/").partition("/")
        if not operation and file.endswith(":batchUpdate"):
            book = self.books[file.removesuffix(":batchUpdate")]
            for operation in body["requests"]:
                self.bootstrap(book, operation)
            return {}
        book = self.books[file]
        if not operation and method == "GET":
            return {"sheets": [{"properties": sheet.properties} for sheet in book.values()]}
        area = unquote(operation.removeprefix("values/")).removesuffix(":append")
        match = re.fullmatch(r"'([^']+)'!([A-Z])(\d+)?(?::([A-Z])(\d+)?)?", area)
        assert match is not None, area
        title, first_col, first_row, last_col, last_row = match.groups()
        sheet = next(item for item in book.values() if item.properties["title"] == title)
        row = int(first_row or 1) - 1
        col = ord(first_col) - ord("A")
        end_row = int(last_row) if last_row else (row + 1 if not last_col else None)
        end_col = ord(last_col or first_col) - ord("A") + 1
        if method == "GET":
            return {"values": sheet.read(row, col, end_row, end_col)}
        values = body["values"]
        if method == "POST" and operation.endswith(":append"):
            row += len(sheet.read(row, col, end_row, end_col))
            sheet.write(row, col, values)
            return {"updates": {"updatedRows": len(values)}}
        assert method == "PUT", (method, operation)
        sheet.write(row, col, values)
        return {}

    @staticmethod
    def bootstrap(book, operation):
        if setup := operation.get("updateSheetProperties"):
            props = setup["properties"]
            book[props["sheetId"]].properties = props
        elif added := operation.get("addSheet"):
            props = added["properties"]
            book[props["sheetId"]] = Grid(props["sheetId"], props["title"])
        elif cells := operation.get("updateCells"):
            start = cells["start"]
            rows = [[next(iter(cell.get("userEnteredValue", {}).values()), "") for cell in row["values"]]
                    for row in cells["rows"]]
            book[start["sheetId"]].write(start["rowIndex"], start["columnIndex"], rows)
        else:
            assert "updateSpreadsheetProperties" in operation or "repeatCell" in operation, operation


class PilotTelegram:
    def __init__(self):
        self.updates = []
        self.members = set()
        self.calls = []

    def api(self, method, body):
        self.calls.append((method, body))
        if method == "getUpdates":
            if not self.updates:
                raise KeyboardInterrupt()
            batch, self.updates = self.updates, []
            assert all(item["update_id"] >= body["offset"] for item in batch)
            return batch
        if method == "getChatMember":
            return {"status": "member" if (body["chat_id"], body["user_id"]) in self.members else "left"}
        assert method in {"sendMessage", "answerInlineQuery"}, method
        return True

    def message(self, chat, text):
        return self.api("sendMessage", {"chat_id": chat, "text": text})


def message(update, user, text, *, chat=None, message_id=None, via_bot=None):
    item = {"from": {"id": user}, "chat": {"id": user if chat is None else chat,
                                          "type": "private" if chat is None else "supergroup"},
            "text": text, "message_id": message_id or update, "date": STAMP}
    if via_bot:
        item["via_bot"] = {"id": via_bot}
    return {"update_id": update, "message": item}


def membership(update, user, status):
    return {"update_id": update, "chat_member": {"chat": {"id": CHAT}, "date": STAMP - 5,
            "new_chat_member": {"user": {"id": user}, "status": status}}}


class PilotAcceptanceTest(unittest.TestCase):
    def test_two_participants_and_existing_owner_survive_restart_and_share_only_confirmed_report(self):
        google, telegram, existing, face = PilotGoogle(), PilotTelegram(), FakeGoogle(), Mock()
        model = Mock()

        def plan(instructions, info, schema, image):
            results = {
                "трекер": [event("fact", action=READING, summary="закрытая книга", quantity=5, unit="мин")],
                "участник": [event("fact", action=READING, summary="личный текст", quantity=12, unit="мин"),
                             event("income", summary="частный платёж", amount=125, currency="RUB")],
                "владелец": [event("fact", action="Яблоко", quantity=1)],
            }
            events = results[info["text"]]
            for value in events:
                value["date"] = NOW.date().isoformat()
            return {"decision": "record", "continues": False, "message": "", "events": events}

        model.plan.side_effect = plan
        practice = {"action": READING, "label": "Чтение", "unit": "мин"}
        groups = configured_groups([{"chat": CHAT, "name": "Тестовая группа", "tracker": TRACKER,
                                     "members": {str(TRACKER): {"practices": [practice], "finance": True},
                                                 str(MEMBER): {"practices": [practice], "finance": True,
                                                               "name": "Участник"}}}])

        def start(directory):
            pilot = PilotProvisioner(google.session, "registry")
            bot = Bot(telegram, existing, model, Path(directory), OWNER, pilot)
            bot.groups = GroupTelegram(telegram, {"id": 999, "username": "TestRX"}, bot.db, groups,
                                       bot.registered_sheet, {CHAT: face})
            return bot

        def run(bot, *updates):
            telegram.updates = list(updates)
            with self.assertRaises(KeyboardInterrupt):
                bot.run()

        with (tempfile.TemporaryDirectory(dir=".") as directory,
              patch("bot.onboarding.request", side_effect=google.request),
              patch("bot.providers.request", side_effect=google.request),
              patch("bot.main.time.sleep", side_effect=AssertionError("the RX polling loop hit an error")),
              patch("bot.group_telegram.time.time", return_value=STAMP)):
            first = start(directory)
            try:
                run(first, message(1, TRACKER, "трекер"), message(2, MEMBER, "участник"),
                    message(3, OWNER, "владелец"))
                a = first.registered_sheet(TRACKER)
                b = first.registered_sheet(MEMBER)
                self.assertNotEqual(a.spreadsheet_id, b.spreadsheet_id)
                self.assertEqual([r[2] for r in a.read("'События'!A2:O")], ["закрытая книга"])
                self.assertEqual([r[2] for r in b.read("'События'!A2:O")], ["личный текст", "частный платёж"])
                self.assertEqual([r[3] for r in existing.events], ["Яблоко"])
            finally:
                first.db.close()

            registered = google.books["registry"][0].rows[1:]
            self.assertEqual({row[0] for row in registered}, {a.spreadsheet_id, b.spreadsheet_id})
            self.assertEqual(len({value for base, target, _ in registered for value in (base, target)}), 4)
            for base, target, gid in registered:
                tab = next(sheet for sheet in google.books[base].values() if sheet.properties["title"] == "Faces")
                self.assertIn(f"/{target}/edit#gid={gid}", tab.rows[1][1])

            restarted = start(directory)
            try:
                telegram.members.update({(CHAT, TRACKER), (CHAT, MEMBER)})
                with patch("bot.group_telegram.time.monotonic", side_effect=iter(range(100, 2000, 100))):
                    run(restarted, message(4, MEMBER, "участник", message_id=2), membership(5, TRACKER, "member"),
                        membership(6, MEMBER, "member"), {"update_id": 7, "inline_query": {
                            "id": "preview", "from": {"id": MEMBER}, "chat_type": "supergroup",
                            "query": f"отчёт {CHAT} сегодня",
                        }})
                    self.assertEqual(len(google.created), 4)
                    self.assertEqual(len(b.read("'События'!A2:O")), 2)
                    preview = next(body for method, body in telegram.calls if method == "answerInlineQuery")
                    text = preview["results"][0]["input_message_content"]["message_text"]
                    self.assertIn("Чтение — 12 мин\nФинансы — операций: 1", text)
                    for private_value in ("закрытая книга", "личный текст", "частный платёж", "125 RUB"):
                        self.assertNotIn(private_value, text)
                    self.assertEqual(restarted.groups.journal.latest_by_day(CHAT), [])
                    run(restarted, message(8, MEMBER, text, chat=CHAT-1, via_bot=999),
                        message(9, MEMBER, text, chat=CHAT, via_bot=999),
                        message(10, MEMBER, text, chat=CHAT, message_id=9, via_bot=999),
                        message(11, TRACKER, "/reports"))
                    history = next(body["text"] for method, body in telegram.calls if method == "sendMessage"
                                   and body["chat_id"] == TRACKER and "Тестовая группа" in body["text"])
                    self.assertIn("Финансы — операций: 1", history)
                    self.assertEqual(len(restarted.groups.journal.latest_by_day(CHAT)), 1)
                    self.assertEqual(len(face.sync.call_args.args[0]), 1)
                    telegram.members.remove((CHAT, TRACKER))
                    run(restarted, membership(12, TRACKER, "left"), message(13, TRACKER, "/reports"))
                    self.assertEqual(telegram.calls[-2][1]["text"], "За эту страницу нет доступных групповых отчётов.")
            finally:
                restarted.db.close()
