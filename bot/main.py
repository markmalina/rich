# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from .group_telegram import UPDATE_TYPES, GroupTelegram, configured_groups
from .ledger import (
    Clarification, INBOX_HEADERS, MOSCOW, PLAN_SCHEMA, ledger_rows, sheets_timestamp, summarize_query,
)
from .onboarding import OAUTH_KEYS, PILOT_REQUIRED, PilotProvisioner, oauth_session
from .providers import Model, ProviderError, ScriptSheets, Sheets, Telegram

ROOT = Path(__file__).resolve().parents[1]
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
INBOX = "Входящие"
EVENTS = "События"
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TRACKER_DATA_DIR")
LEDGER_REQUIRED = ("OPENAI_API_KEY", "TRACKER_SPREADSHEET_ID")
SCRIPT_SETTINGS = ("TRACKER_APPS_SCRIPT_URL", "TRACKER_APPS_SCRIPT_TOKEN")
LEDGER_UNAVAILABLE = "Запись в «БАЗУ» пока недоступна. Пришли сообщение заново после её подключения."


def attachment(message):
    if voice := message.get("voice"):
        return voice, "audio/ogg", ".ogg"
    if photos := message.get("photo"):
        return photos[-1], "image/jpeg", ".jpg"
    if image := message.get("document"):
        mime = image.get("mime_type")
        if mime in IMAGE_TYPES:
            return image, mime, IMAGE_TYPES[mime]
    return None, None, None


class Bot:
    def __init__(self, telegram, sheets, model, directory, owner, provisioner=None):
        self.telegram = telegram
        self.sheets = sheets
        self.model = model
        self.directory = directory
        self.owner = owner
        self.provisioner = provisioner
        self.groups = None
        self.instructions = (ROOT / "tracking/telegram-prompt.md").read_text(encoding="utf-8")
        self.db = sqlite3.connect(directory / "bot.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, received TEXT NOT NULL)")
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, json.dumps(value)))

    def receive(self, updates):
        with self.db:
            stamp = dt.datetime.now(MOSCOW).isoformat()
            self.db.executemany("INSERT OR IGNORE INTO receipts VALUES (?, ?)",
                                [(update["update_id"], stamp) for update in updates])

    def run(self):
        first_poll = True
        update_types = ["message"] + (UPDATE_TYPES if self.groups is not None else [])
        while True:
            try:
                updates = self.telegram.api("getUpdates", {
                    "offset": self.get("offset", 0), "timeout": 25, "allowed_updates": update_types,
                })
                if first_poll:
                    logging.info("Telegram API: опрос обновлений подтверждён")
                    first_poll = False
                self.receive(updates)
                for update in updates:
                    update_id = update["update_id"]
                    message = update.get("message", {})
                    if message.get("chat", {}).get("type") == "private":
                        saved_at = self.db.execute("SELECT received FROM receipts WHERE id = ?", (update_id,)).fetchone()[0]
                        self.dispatch(update_id, message, dt.datetime.fromisoformat(saved_at))
                    elif self.groups is not None:
                        try:
                            self.groups.handle(update)
                        except ProviderError as error:
                            if error.provider != "Telegram" or error.status != 400:
                                raise
                            logging.warning("Отчёт RX: Telegram отклонил устаревший запрос или сообщение")
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (str(update_id + 1),))
                        self.db.execute("DELETE FROM receipts WHERE id = ?", (update_id,))
            except ProviderError as error:
                logging.warning("%s", error)
                time.sleep(20)
            except Exception as error:
                logging.error("Обработка прервана: %s", type(error).__name__)
                time.sleep(30)

    def report_sheet(self, user):
        if user == self.owner:
            return self.sheets
        if self.provisioner is None:
            return None
        database = self.directory / "participants" / str(user) / "bot.sqlite"
        if not database.is_file():
            return None
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            saved = connection.execute("SELECT value FROM state WHERE key = 'files'").fetchone()
        finally:
            connection.close()
        state = json.loads(saved[0]) if saved else None
        if state and state.get("ready"):
            return Sheets(state["base"], session=self.provisioner.session)
        return None

    def dispatch(self, update_id, message, received):
        user = message.get("from", {}).get("id")
        if type(user) is not int or user <= 0 or message["chat"].get("id") != user:
            return
        if user == self.owner:
            self.handle(update_id, message, received)
        elif self.provisioner is not None:
            directory = self.directory / "participants" / str(user)
            directory.mkdir(parents=True, exist_ok=True)
            bot = Bot(self.telegram, None, self.model, directory, user)
            try:
                try:
                    bot.sheets = self.provisioner.provision(bot.get("files"), lambda state: bot.put("files", state))
                except ProviderError as error:
                    logging.warning("Подключение RX: %s", error)
                    bot.telegram.message(user, "Подготовка «БАЗЫ» пока не завершилась. Пришли сообщение ещё раз позже.")
                    return
                bot.handle(update_id, message, received)
            finally:
                bot.db.close()

    def inbox_index(self, origin):
        return next((i for i, row in enumerate(self.sheets.read(f"'{INBOX}'!A2:A"), start=2)
                     if row and row[0] == origin), None)

    def capture(self, origin, received, source, text, file_ref):
        found = self.inbox_index(origin)
        if found is None:
            self.sheets.append(f"'{INBOX}'!A:H", [[origin, sheets_timestamp(received), source, text, file_ref, "", "", ""]])
            found = self.inbox_index(origin)
        if found is None:
            raise ProviderError("Google Sheets", "inbox row not confirmed")
        return found

    def save_receipt_file(self, origin, suffix, media):
        final = self.directory / f"{origin}{suffix}"
        if not final.exists():
            payload = self.telegram.media(media["file_id"], media.get("file_size"))
            temp = final.with_suffix(final.suffix + ".part")
            temp.write_bytes(payload)
            temp.replace(final)
        return final

    def handle(self, update_id, message, received):
        chat = message["chat"]["id"]
        text = message.get("text") or message.get("caption") or ""
        if self.sheets is None:
            if text == "/start":
                self.telegram.message(chat, "Привет, я eRex. Telegram подключён. Запись в «БАЗУ» пока недоступна.")
            else:
                self.telegram.message(chat, LEDGER_UNAVAILABLE)
            return
        if text == "/start":
            self.telegram.message(chat, "Присылай текст, голосовое или фото чека. Можешь спросить «Что я сегодня ел?».")
            return
        if text.startswith("/original "):
            ref = text.removeprefix("/original ").strip()
            if re.fullmatch(r"tg-[0-9]+-[0-9]+\.(ogg|jpg|png|webp)", ref):
                file = self.directory / ref
                if file.is_file():
                    with file.open("rb") as stream:
                        self.telegram.api("sendDocument", {"chat_id": chat}, files={"document": (file.name, stream)})
                    return
            self.telegram.message(chat, "Оригинал с таким именем не найден.")
            return

        origin = f"tg-{chat}-{message['message_id']}"
        media, mime, suffix = attachment(message)
        source = "Telegram · голосовое" if mime == "audio/ogg" else "Telegram · фото" if mime else "Telegram · текст"
        file_ref = f"{origin}{suffix}" if media else ""
        idx = self.capture(origin, received, source, text, file_ref)
        cached = self.get("response")
        if cached and cached["update"] == update_id:
            self.telegram.message(chat, cached["text"])
            return
        if cached and cached["update"] != update_id:
            self.put("response", None)
        if not media and not text:
            self.reply(update_id, chat, "Поддерживаются текст, голосовые и фото или файлы JPG, PNG и WEBP.")
            return

        original = None
        extracted = ""
        if media:
            try:
                original = self.save_receipt_file(origin, suffix, media)
            except ProviderError as error:
                if error.status in {"size", 400, 404}:
                    self.reply(update_id, chat, "Входящее принято. Пришли вложение повторно, размером до 18 МБ.")
                    return
                raise
        if mime == "audio/ogg":
            existing = self.sheets.read(f"'{INBOX}'!F{idx}")
            extracted = existing[0][0] if existing else self.model.transcribe(original.name, original.read_bytes(), mime)
            self.sheets.update(f"'{INBOX}'!F{idx}", [[extracted]])

        previous = self.get("question")
        action_rows = self.sheets.read("'Действия'!A2:G")
        actions = {row[0]: row for row in action_rows if row and row[0]}
        local_rules = self.sheets.read("'Правило'!A7:D50")
        sent = dt.datetime.fromtimestamp(message["date"], MOSCOW)
        image = (original.read_bytes(), mime) if mime in IMAGE_TYPES else None
        instructions = self.instructions
        if local_rules:
            instructions += "\n\nЛичные правила этой БАЗЫ (применять в рамках поддерживаемой схемы):\n" + "\n".join(
                " ".join(str(value) for value in row if value != "") for row in local_rules
            )
        try:
            plan = self.model.plan(instructions, {
                "received_at_msk": received.isoformat(), "sent_at_msk": sent.isoformat(),
                "text": text, "transcript": extracted, "has_image": bool(image),
                "actions": [[key, *value[1:3]] for key, value in actions.items()],
                "previous_question": previous,
            }, PLAN_SCHEMA, image)
        except ProviderError as error:
            if error.status != "no plan":
                raise
            self.reply(update_id, chat, "Входящее сохранено. Уточни текстом, что нужно записать или найти.")
            return
        if image and plan["extracted"]:
            extracted = plan["extracted"]
            self.sheets.update(f"'{INBOX}'!F{idx}", [[extracted]])

        parent = previous if previous and plan["continues"] else None
        event_source = parent["origin"] if parent else origin
        event_received = dt.datetime.fromisoformat(parent["received"]) if parent else received
        context = {
            "origin": event_source, "received": event_received.isoformat(),
            "sent": parent["sent"] if parent else sent.isoformat(),
            "text": "\n".join(filter(None, [parent["text"] if parent else "", text, extracted]))[-6000:],
            "question": plan["message"],
        }
        keep_question = previous if not parent else None
        try:
            match plan["decision"]:
                case "record":
                    weight_rows = self.sheets.read("'Действия'!I2")
                    rows, fallback = ledger_rows(plan["events"], event_source, event_received, actions,
                                                 weight_rows[0][0] if weight_rows else None)
                    result = self.save_events(event_source, rows, fallback)
                case "query":
                    result = summarize_query(self.sheets.read(f"'{EVENTS}'!A2:O"), actions, plan["query"], sent.date())
                case "clarify":
                    raise Clarification(plan["message"] or "Уточни, что произошло.")
                case _:
                    result = "Входящее сохранено. Для записи в «События» опиши, что уже произошло."
        except Clarification as error:
            context["question"] = str(error)
            self.reply(update_id, chat, str(error), context)
        else:
            self.reply(update_id, chat, result, keep_question)

    def save_events(self, origin, rows, fallback):
        lookup = self.inbox_index(origin)
        if lookup is None:
            raise ProviderError("Google Sheets", "source missing")
        previous = [row for row in self.sheets.read(f"'{EVENTS}'!I2:L") if row and row[0] == origin]
        if previous:
            return "События из этого сообщения уже сохранены в «БАЗЕ»."
        self.sheets.update(f"'{INBOX}'!G{lookup}", [[fallback]])
        count = self.sheets.append(f"'{EVENTS}'!A:O", rows)
        if count != len(rows):
            raise ProviderError("Google Sheets", "unexpected number of rows")
        details = [f"{row[2]} · {row[4]} {row[5]}" if row[3] == "Оплата" else row[2] for row in rows]
        return "\n".join(["Записал в «БАЗУ».", *dict.fromkeys(details)])[:3800]

    def reply(self, update, chat, text, question=None):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO state VALUES (?, ?)", [
                ("question", json.dumps(question)), ("response", json.dumps({"update": update, "text": text})),
            ])
        self.telegram.message(chat, text)


def main():
    import fcntl

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    telegram_only = os.environ.get("TRACKER_TELEGRAM_ONLY") == "1"
    pilot = os.environ.get("TRACKER_PILOT") == "1"
    groups_config = os.environ.get("TRACKER_REPORT_GROUPS")
    owner = os.environ.get("TRACKER_OWNER_ID")
    if telegram_only and pilot:
        raise ProviderError("RX pilot", "incompatible worker modes")
    if groups_config and not pilot:
        raise ProviderError("RX pilot", "group reports require pilot mode")
    try:
        groups = configured_groups(json.loads(groups_config)) if groups_config else {}
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ProviderError("RX pilot", "invalid group report configuration") from None
    google_settings = (SCRIPT_SETTINGS if any(os.environ.get(name) for name in SCRIPT_SETTINGS)
                       else ("GOOGLE_SERVICE_ACCOUNT_JSON",))
    required = list(REQUIRED)
    if pilot:
        required.extend((*PILOT_REQUIRED, "OPENAI_API_KEY"))
    else:
        required.append("TRACKER_OWNER_ID")
    if not telegram_only and (owner or not pilot):
        required.extend((*LEDGER_REQUIRED, *google_settings))
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        logging.info("Ожидание настройки: %s", ", ".join(missing))
        while True:
            time.sleep(3600)
    import requests

    data = Path(os.environ["TRACKER_DATA_DIR"])
    data.mkdir(parents=True, exist_ok=True)
    with (data / "bot.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        session = requests.Session()
        telegram = Telegram(session, os.environ["TELEGRAM_BOT_TOKEN"])
        sheets = None
        model = None
        provisioner = None
        if not telegram_only:
            model = Model(session, os.environ["OPENAI_API_KEY"], os.environ.get("TRACKER_MODEL", "gpt-5.4-mini"),
                          os.environ.get("TRACKER_TRANSCRIBE_MODEL", "gpt-transcribe"))
        if pilot:
            provisioner = PilotProvisioner(oauth_session(*(os.environ[name] for name in OAUTH_KEYS)),
                                           os.environ["TRACKER_PILOT_REGISTRY_ID"])
        if owner and not telegram_only:
            spreadsheet = os.environ["TRACKER_SPREADSHEET_ID"]
            if google_settings == SCRIPT_SETTINGS:
                sheets = ScriptSheets(session, *(os.environ[name] for name in SCRIPT_SETTINGS), spreadsheet)
            else:
                sheets = Sheets(spreadsheet, json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
            if sheets.read(f"'{INBOX}'!A1:H1") != [INBOX_HEADERS] or sheets.read(f"'{EVENTS}'!I1") != [["ID входящего"]]:
                raise ProviderError("Google Sheets", "unsupported ledger schema")
        account = telegram.api("getMe", {})
        logging.info("Telegram @%s: %s", account["username"],
                     "режим без БАЗЫ" if telegram_only else "готов принимать личные сообщения")
        bot = Bot(telegram, sheets, model, data, int(owner) if owner else None, provisioner)
        if groups:
            bot.groups = GroupTelegram(telegram, account, bot.db, groups, bot.report_sheet)
            bot.groups.verify_groups()
        bot.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.error("Запуск невозможен: %s", error if isinstance(error, ProviderError) else type(error).__name__)
        raise SystemExit(1) from None
