# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from contextlib import closing
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
from .onboarding import DRIVE_URL, REGISTRY, REGISTRY_HEADERS, SHEET_MIME, PilotProvisioner
from .providers import ProviderError, SHEETS_URL
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
    def test_closed_participant_first_records_survive_provision_failure_and_restart(self):
        google, telegram = PilotGoogle(), PilotTelegram()
        attempts = 0

        def request(session, provider, method, url, **kwargs):
            nonlocal attempts
            if url == DRIVE_URL and method == "POST":
                attempts += 1
                if attempts == 1:
                    raise ProviderError("Google pilot", 429)
            return google.request(session, provider, method, url, **kwargs)

        with (tempfile.TemporaryDirectory() as directory,
              patch("bot.onboarding.request", side_effect=request),
              patch("bot.providers.request", side_effect=request),
              patch("bot.main.time.sleep", side_effect=AssertionError("onboarding interrupted the poll"))):
            def start(terms="Учебное уведомление"):
                return Bot(telegram, None, None, Path(directory), None,
                           PilotProvisioner(google.session, "registry"), admitted={MEMBER}, terms=terms)

            def run(bot, tick, *updates):
                telegram.updates = list(updates)
                with patch("bot.main.time.time", return_value=tick), self.assertRaises(KeyboardInterrupt):
                    bot.run()

            bot = start()
            with closing(bot.db):
                run(bot, 100, message(100, MEMBER, "/record Завтрак | До согласия | 80 г"))
                self.assertFalse(bot.inbox_writes.waiting(MEMBER))
                self.assertEqual(google.created, {})
                self.assertIn(bot.terms_version, telegram.calls[-2][1]["text"])
                run(bot, 110, message(101, MEMBER, f"/pilot_consent {bot.terms_version}"),
                    message(102, MEMBER, "/record Завтрак | Первый | 80 г"),
                    message(103, MEMBER, "/record Завтрак | Второй | 60 г"))
                self.assertEqual(bot.get("offset"), 104)
                self.assertEqual(attempts, 1)
                self.assertEqual(google.created, {})
                self.assertTrue(bot.inbox_writes.waiting(MEMBER))

            bot = start()
            with closing(bot.db):
                run(bot, 240)
                run(bot, 250)
                run(bot, 260)
                self.assertFalse(bot.inbox_writes.waiting(MEMBER))
                self.assertEqual(bot.db.execute("SELECT COUNT(*) FROM deferred_onboarding").fetchone(), (0,))
                sheet = bot.registered_sheet(MEMBER)
                self.assertEqual([row[0] for row in sheet.read("'Входящие'!A2:H")],
                                 [f"tg-{MEMBER}-102", f"tg-{MEMBER}-103"])
                self.assertEqual([(row[2], row[4]) for row in sheet.read("'События'!A2:O")],
                                 [("Первый", 80), ("Второй", 60)])
                self.assertEqual(len(google.created), 2)
                self.assertEqual(len(google.books["registry"][0].rows), 2)

    def test_terms_change_and_withdrawal_pause_processing(self):
        telegram = PilotTelegram()
        with tempfile.TemporaryDirectory() as directory:
            def start(notice):
                return Bot(telegram, None, None, Path(directory), None, admitted={MEMBER}, terms=notice)

            def dispatch(bot, *updates):
                bot.receive(updates)
                for update in updates:
                    bot.process_update(update)

            bot = start("Версия 1")
            with closing(bot.db):
                previous = f"/pilot_consent {bot.terms_version}"
                dispatch(bot, message(1, MEMBER, previous), message(2, MEMBER, "/pilot_withdraw"))
                bot.process_update(message(1, MEMBER, previous))
                self.assertFalse(bot.has_private_access(MEMBER))
                dispatch(bot, message(3, MEMBER, previous))
                self.assertTrue(bot.has_private_access(MEMBER))

            bot = start("Версия 2")
            with closing(bot.db):
                dispatch(bot, message(4, MEMBER, previous))
                self.assertFalse(bot.has_private_access(MEMBER))
                dispatch(bot, message(5, MEMBER, f"/pilot_consent {bot.terms_version}"))
                self.assertTrue(bot.has_private_access(MEMBER))

    def test_late_inbox_append_survives_restart_without_blocking_other_profiles(self):
        google, telegram, owner_sheet = PilotGoogle(), PilotTelegram(), FakeGoogle()
        late = []
        writes = []
        append = owner_sheet.append

        def delay_first_source(area, rows):
            if "Входящие" in area:
                origin = rows[0][0]
                writes.append(origin)
                if len(writes) == 1:
                    late.append((area, rows))
                    raise ProviderError("Google Sheets", "connection")
            return append(area, rows)

        with (tempfile.TemporaryDirectory() as directory,
              patch("bot.onboarding.request", side_effect=google.request),
              patch("bot.providers.request", side_effect=google.request),
              patch.object(owner_sheet, "append", side_effect=delay_first_source),
              patch("bot.main.time.sleep", side_effect=AssertionError("a delayed source stopped the worker"))):
            def new_bot():
                return Bot(telegram, owner_sheet, None, Path(directory), OWNER, PilotProvisioner(google.session, "registry"))

            first = message(100, OWNER, "/record Завтрак | Первый | 80 г")
            second = message(101, OWNER, "/record Завтрак | Второй | 60 г")
            telegram.updates = [first, second, message(102, MEMBER, f"/record {READING} | Другая БАЗА | 10 мин")]
            bot = new_bot()
            with closing(bot.db), patch("bot.main.time.time", return_value=100):
                with self.assertRaises(KeyboardInterrupt):
                    bot.run()
                self.assertEqual(bot.get("offset"), 103)
                self.assertEqual(owner_sheet.events, [])
                self.assertEqual(len(bot.registered_sheet(MEMBER).read("'События'!A2:O")), 1)

            bot = new_bot()
            with closing(bot.db), patch("bot.main.time.time", return_value=280):
                with self.assertRaises(KeyboardInterrupt):
                    bot.run()
                self.assertEqual(writes, [f"tg-{OWNER}-100"])
                self.assertEqual(owner_sheet.events, [])

            append(*late.pop())
            bot = new_bot()
            with closing(bot.db):
                for tick in (460, 640, 820):
                    with patch("bot.main.time.time", return_value=tick), self.assertRaises(KeyboardInterrupt):
                        bot.run()
                self.assertFalse(bot.inbox_writes.waiting(OWNER))
            self.assertEqual(writes, [f"tg-{OWNER}-100", f"tg-{OWNER}-101"])
            self.assertEqual([row[0] for row in owner_sheet.inbox], writes)
            self.assertEqual([(row[2], row[4]) for row in owner_sheet.events], [("Первый", 80), ("Второй", 60)])

    def test_inbox_waits_on_an_apps_script_response_error_with_a_numeric_status(self):
        google = FakeGoogle()
        telegram = PilotTelegram()
        append = google.append
        first = message(104, OWNER, "/record Завтрак | Мост | 80 г")
        with tempfile.TemporaryDirectory() as directory:
            bot = Bot(telegram, google, None, Path(directory), OWNER)
            with closing(bot.db), patch.object(google, "append", side_effect=ProviderError("Apps Script", 429)) as sent:
                bot.receive([first])
                bot.process_update(first)
                with patch("bot.main.time.time", return_value=100):
                    bot.reconcile_inbox(105)
                self.assertEqual(sent.call_count, 1)
                area, rows = sent.call_args.args
            append(area, rows)
            bot = Bot(telegram, google, None, Path(directory), OWNER)
            with closing(bot.db), patch("bot.main.time.time", return_value=280):
                bot.reconcile_inbox(105)
                self.assertFalse(bot.inbox_writes.waiting(OWNER))
        self.assertEqual(len(google.inbox), 1)
        self.assertEqual(len(google.events), 1)

    def test_inbox_retries_rate_limit_and_keeps_uncertain_source_in_its_original_workbook(self):
        google, telegram = FakeGoogle(), PilotTelegram()
        foreign = FakeGoogle()
        foreign.spreadsheet_id = "foreign-fixture"
        append = google.append
        late = []
        writes = []

        def fail_twice(area, rows):
            if "Входящие" in area:
                writes.append(rows[0][0])
                if len(writes) == 1:
                    raise ProviderError("Google Sheets", 429)
                late.append((area, rows))
                raise ProviderError("Google Sheets", "connection")
            return append(area, rows)

        with tempfile.TemporaryDirectory() as directory:
            bot = Bot(telegram, google, None, Path(directory), OWNER)
            with closing(bot.db), patch.object(google, "append", side_effect=fail_twice):
                update = message(103, OWNER, "/record Завтрак | После 429 | 80 г")
                bot.receive([update])
                bot.process_update(update)
                with patch("bot.main.time.time", return_value=100):
                    bot.reconcile_inbox(104)
                self.assertEqual(writes, [f"tg-{OWNER}-103"] * 2)
                bot.sheets = foreign
                for tick in (280, 460):
                    with patch("bot.main.time.time", return_value=tick):
                        bot.reconcile_inbox(104)
                self.assertEqual(foreign.inbox, [])
                self.assertTrue(bot.inbox_writes.waiting(OWNER))

                append(*late.pop())
                bot.sheets = google
                with patch("bot.main.time.time", return_value=640):
                    bot.reconcile_inbox(104)
                self.assertFalse(bot.inbox_writes.waiting(OWNER))
            self.assertEqual(len(google.inbox), 1)
            self.assertEqual(len(google.events), 1)

    @staticmethod
    def participant_status(directory, google, telegram, update_id):
        bot = Bot(telegram, None, None, Path(directory), OWNER, PilotProvisioner(google.session, "registry"))
        with closing(bot.db):
            update = message(update_id, MEMBER, "/status")
            bot.receive([update])
            bot.process_update(update)
            return bot.registered_sheet(MEMBER)

    def test_pending_registry_append_survives_restart_without_adding_a_second_pair(self):
        google, telegram = PilotGoogle(), PilotTelegram()
        registry_writes = []
        registry_pending = []

        def delay_registry(session, provider, method, url, **kwargs):
            if method == "POST" and unquote(url).endswith("/registry/values/" + REGISTRY + ":append"):
                registry_writes.append(kwargs["json"]["values"])
                if len(registry_writes) == 1:
                    registry_pending.append((session, provider, method, url, kwargs))
                    raise ProviderError("Google Sheets", "connection")
            return google.request(session, provider, method, url, **kwargs)

        with (tempfile.TemporaryDirectory() as directory,
              patch("bot.onboarding.request", side_effect=google.request),
              patch("bot.providers.request", side_effect=delay_registry)):
            self.assertIsNone(self.participant_status(directory, google, telegram, 1))
            self.assertIsNone(self.participant_status(directory, google, telegram, 2))
            self.assertEqual(google.books["registry"][0].rows, [REGISTRY_HEADERS])
            session, provider, method, url, kwargs = registry_pending.pop()
            google.request(session, provider, method, url, **kwargs)
            self.assertEqual(self.participant_status(directory, google, telegram, 3).spreadsheet_id, "test-file-2")
            self.assertEqual(self.participant_status(directory, google, telegram, 4).spreadsheet_id, "test-file-2")
            self.assertEqual(registry_writes, [[["test-file-2", "test-file-1", 0]]])
            self.assertEqual(google.books["registry"][0].rows, [REGISTRY_HEADERS, ["test-file-2", "test-file-1", 0]])
            self.assertEqual(len(google.created), 2)

    def test_reopens_after_each_provisioning_write_and_preserves_existing_cells(self):
        google, telegram = PilotGoogle(), PilotTelegram()
        writes = []

        def lose_write_reply(session, provider, method, url, **kwargs):
            result = google.request(session, provider, method, url, **kwargs)
            if method != "GET":
                writes.append((method, unquote(url)))
                raise ProviderError(provider, "connection")
            return result

        with (tempfile.TemporaryDirectory() as directory,
              patch("bot.onboarding.request", side_effect=lose_write_reply),
              patch("bot.providers.request", side_effect=lose_write_reply)):
            for step in range(1, 5):
                with self.subTest(step=step):
                    self.assertIsNone(self.participant_status(directory, google, telegram, step))
                    self.assertEqual(len(writes), step)

            base = {tab.properties["title"]: tab for tab in google.books["test-file-2"].values()}
            formulas = {title: base[title].rows[1][column]
                        for title, column in (("События", 15), ("Действия", 6), ("Экспорт Faces", 0))}
            self.assertTrue(all(formula.startswith("=") for formula in formulas.values()))
            base["Входящие"].write(1, 0, [["fixture-original"]])
            base["События"].write(1, 8, [["fixture-original", "", "", "fixture-event"]])
            base["Правило"].write(6, 0, [["Личное правило"]])

            for step in (5, 6):
                with self.subTest(step=step):
                    self.assertIsNone(self.participant_status(directory, google, telegram, step))
                    self.assertEqual(len(writes), step)
            for step in (7, 8):
                sheet = self.participant_status(directory, google, telegram, step)
                self.assertEqual(sheet.spreadsheet_id, "test-file-2")
                self.assertEqual(sheet.read("'Входящие'!A2"), [["fixture-original"]])
                self.assertEqual(sheet.read("'События'!I2:L2"), [["fixture-original", "", "", "fixture-event"]])
                self.assertEqual(sheet.read("'Правило'!A7"), [["Личное правило"]])
                for title, cell in (("События", "P2"), ("Действия", "G2"), ("Экспорт Faces", "A2")):
                    self.assertEqual(sheet.read(f"'{title}'!{cell}"), [[formulas[title]]])
            self.assertEqual(len(writes), 6)
            self.assertEqual(len(google.created), 2)
            self.assertEqual(google.books["registry"][0].rows,
                             [REGISTRY_HEADERS, ["test-file-2", "test-file-1", 0]])

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

    def test_replayed_participant_batch_recovers_after_restart_without_crossing_journals(self):
        google, telegram, model = PilotGoogle(), PilotTelegram(), Mock()
        users = list(range(100, 130))
        updates = [message(i + 1, users[i % len(users)], f"Факт {i}", message_id=i // len(users) + 1)
                   for i in range(100)]
        lost_origin = f"tg-{users[1]}-1"
        pending_book = None
        append_attempts = 0

        def sheets_request(session, provider, method, url, **kwargs):
            nonlocal pending_book, append_attempts
            address = unquote(url)
            values = kwargs.get("json", {}).get("values", [])
            if address.endswith("/'События'!A:O:append") and any(row[8] == lost_origin for row in values):
                append_attempts += 1
                google.request(session, provider, method, url, **kwargs)
                pending_book = address.split("/values/", 1)[0]
                raise ProviderError("Google Sheets", "connection")
            if pending_book and address == pending_book + "/values/'События'!A2:O":
                raise ProviderError("Google Sheets", "connection")
            return google.request(session, provider, method, url, **kwargs)

        def plan(instructions, info, schema, image):
            if info["text"] == "Факт 0":
                raise ProviderError("OpenAI", 429)
            recorded = event("fact", action=READING, summary=info["text"], quantity=1, unit="мин")
            return {"decision": "record", "continues": False, "message": "", "events": [recorded]}

        model.plan.side_effect = plan
        pilot = PilotProvisioner(google.session, "registry")
        with (tempfile.TemporaryDirectory() as directory,
              patch("bot.onboarding.request", side_effect=google.request),
              patch("bot.providers.request", side_effect=sheets_request)):
            bot = Bot(telegram, None, model, Path(directory), OWNER, pilot)
            with closing(bot.db):
                bot.receive(updates)
                for update in updates:
                    bot.process_update(update)
                    bot.process_update(update)
                pending = bot.db.execute("SELECT origin FROM event_writes").fetchall()
                self.assertCountEqual(pending, [(f"tg-{users[1]}-{i}",) for i in range(1, 5)])
                self.assertTrue(any(body["chat_id"] == users[0] and "/record" in body["text"]
                                    for method, body in telegram.calls if method == "sendMessage"))

            pending_book = None
            restarted = Bot(telegram, None, model, Path(directory), OWNER, pilot)
            with closing(restarted.db):
                for _ in range(2):  # The poller reconciles at most two batches per pass.
                    restarted.reconcile_events(101)
                manual = message(101, users[0], f"/record {READING} | Факт 0 | 1 мин", message_id=5)
                manual["message"]["reply_to_message"] = updates[0]["message"]
                restarted.receive([manual])
                restarted.process_update(manual)
                restarted.process_update(manual)
                self.assertEqual(restarted.db.execute("SELECT COUNT(*) FROM event_writes").fetchone(), (0,))
                self.assertEqual(append_attempts, 1)
                self.assertEqual(model.plan.call_count, len(updates))
                for i, user in enumerate(users):
                    rows = restarted.registered_sheet(user).read("'События'!A2:O")
                    expected = [(f"Факт {j}", f"tg-{user}-{k + 1}", f"tg-{user}-{k + 1}-e1")
                                for k, j in enumerate(range(i, len(updates), len(users)))]
                    self.assertCountEqual([(row[2], row[8], row[11]) for row in rows], expected)
