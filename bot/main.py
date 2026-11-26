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

from .coaching import Coaching
from .event_write import EVENTS, INBOX, EventWrite, EventWrites, inbox_index
from .group_face import GroupFace
from .group_telegram import UPDATE_TYPES, GroupTelegram
from .ledger import (
    Clarification, INBOX_HEADERS, MONEY_ACTIONS, MOSCOW, PLAN_SCHEMA, TELEGRAM_EVENT, action_catalog,
    ledger_rows, revised_quantity, sheets_timestamp, summarize_query,
)
from .onboarding import OAUTH_KEYS, PilotProvisioner, oauth_session
from .operations import Operations
from .pilot import Pilot
from .pilot_check import (
    BOT_USERNAME, GOOGLE_EMAIL, PILOT_CONFIG_REQUIRED, SCRIPT_SETTINGS, legacy_google_settings, report_config,
    verify_telegram,
)
from .providers import Model, ProviderError, ScriptSheets, Sheets, Telegram
from .sales import Sales

ROOT = Path(__file__).resolve().parents[1]
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TRACKER_DATA_DIR")
LEDGER_REQUIRED = ("OPENAI_API_KEY", "TRACKER_SPREADSHEET_ID")
LEDGER_UNAVAILABLE = "Запись в «БАЗУ» пока недоступна. Пришли сообщение заново после её подключения."
EVENT_UNCONFIRMED = "Входящее сохранено. Запись в «События» ещё проверяю."
EVENT_EXISTS = "С этим входящим уже связаны записи в «БАЗЕ»."
MODEL_UNAVAILABLE = "Входящее сохранено. ИИ сейчас недоступен. Попробуй прислать запись позже или опиши её текстом."
AMOUNT_INSTRUCTION = re.compile(r"^(?:/amount(?:\s|$)|(?:исправь|поправь|исправление)\b)", re.IGNORECASE)
AMOUNT_VALUE = re.compile(
    r"(?:/amount|(?:исправь|поправь|исправление)(?:\s+на)?)\s+"
    r"(?:(tg-[0-9]+-[0-9]+-e[0-9]+)\s+)?([0-9]{1,12}(?:[.,][0-9]{1,6})?)(?:\s+([\w.]{1,12}))?",
    re.IGNORECASE,
)


def verify_ledger_schema(sheets):
    if sheets.read(f"'{INBOX}'!A1:H1") != [INBOX_HEADERS] or sheets.read(f"'{EVENTS}'!I1") != [["ID входящего"]]:
        raise ProviderError("Google Sheets", "unsupported ledger schema")


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
    def __init__(self, telegram, sheets, model, directory, owner, provisioner=None, event_writes=None):
        self.telegram = telegram
        self.sheets = sheets
        self.model = model
        self.directory = directory
        self.owner = owner
        self.provisioner = provisioner
        self.groups = None
        self.coaching = None
        self.pilot_orders = None
        self.operations = None
        self.instructions = (ROOT / "tracking/telegram-prompt.md").read_text(encoding="utf-8")
        self.db = sqlite3.connect(directory / "bot.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, received TEXT NOT NULL)")
        self.db.commit()
        self.event_writes = event_writes if event_writes is not None else EventWrites(self.db)

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
                if self.groups is not None:
                    self.groups.sync_group_faces()
                if self.coaching is not None:
                    self.coaching.send_due(dt.datetime.now(MOSCOW))
                if self.operations is not None:
                    try:
                        self.operations.run_daily(dt.datetime.now(MOSCOW))
                    except (OSError, sqlite3.Error, RuntimeError) as error:
                        logging.warning("Локальная резервная копия RX: %s", type(error).__name__)
                offset = self.get("offset", 0)
                self.reconcile_events(offset)
                updates = self.telegram.api("getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": update_types,
                })
                if first_poll:
                    logging.info("Telegram API: опрос обновлений подтверждён")
                    first_poll = False
                self.receive(updates)
                for update in updates:
                    update_id = update["update_id"]
                    message = update.get("message", {})
                    if message.get("chat", {}).get("type") == "private":
                        if self.groups is None or not self.groups.private_history(message):
                            saved_at = self.db.execute(
                                "SELECT received FROM receipts WHERE id = ?", (update_id,),
                            ).fetchone()[0]
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

    def registered_sheet(self, user):
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
        if self.coaching is not None and self.coaching.handle(update_id, user, message, received):
            return
        if self.operations is not None and self.operations.handle(update_id, user, message, received):
            return
        if self.pilot_orders is not None and self.pilot_orders.handle(update_id, user, message, received):
            return
        if user == self.owner:
            self.handle(update_id, message, received)
        elif self.provisioner is not None:
            directory = self.directory / "participants" / str(user)
            directory.mkdir(parents=True, exist_ok=True)
            bot = Bot(self.telegram, None, self.model, directory, user, event_writes=self.event_writes)
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
        elif message.get("text") == "/status":
            self.telegram.message(user, "Этот дневник eRex привязан к другому Telegram-профилю.")

    def capture(self, origin, received, source, text, file_ref):
        found = inbox_index(self.sheets, origin)
        if found is None:
            self.sheets.append(f"'{INBOX}'!A:H", [[origin, sheets_timestamp(received), source, text, file_ref, "", "", ""]])
            found = inbox_index(self.sheets, origin)
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
        if message.get("text") == "/status":
            status = "Telegram подключён. «БАЗА» пока не подключена."
            if self.sheets is not None:
                try:
                    verify_ledger_schema(self.sheets)
                except ProviderError:
                    status = "Telegram подключён. Проверить доступ к «БАЗЕ» или её схему сейчас не удалось."
                else:
                    status = "Telegram подключён. Читаю «БАЗУ», заголовки совпадают со схемой Rich."
            self.telegram.message(chat, status)
            return
        if self.sheets is None:
            if text == "/start":
                self.telegram.message(chat, "Привет, я eRex. Telegram подключён. Запись в «БАЗУ» пока недоступна.")
            else:
                self.telegram.message(chat, LEDGER_UNAVAILABLE)
            return
        if text == "/start":
            self.telegram.message(chat, "Присылай текст, голосовое или фото чека. Можешь спросить «Что я сегодня ел?». "
                                  "Подключение к «БАЗЕ» можно проверить через /status.")
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
            if confirmed := cached.get("settled_origin"):
                self.event_writes.finish(chat, confirmed)
            self.telegram.message(chat, cached["text"])
            return
        if cached and cached["update"] != update_id:
            self.put("response", None)
        if pending := self.event_writes.find(chat, update_id):
            self.answer_event(pending)
            return
        if AMOUNT_INSTRUCTION.match(message.get("text", "")):
            self.correct_quantity(update_id, message, origin, text)
            return
        if message.get("document") and not media:
            self.reply(update_id, chat, "Пришли фото либо файл JPG, PNG или WEBP.", self.get("question"))
            return
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
            try:
                extracted = existing[0][0] if existing else self.model.transcribe(original.name, original.read_bytes(), mime)
            except ProviderError:
                self.reply(update_id, chat, MODEL_UNAVAILABLE, self.get("question"))
                return
            self.sheets.update(f"'{INBOX}'!F{idx}", [[extracted]])

        previous = self.get("question")
        action_rows = self.sheets.read("'Действия'!A2:G")
        actions = action_catalog(action_rows)
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
            response = ("Входящее сохранено. Уточни текстом, что нужно записать или найти."
                        if error.status == "no plan" else MODEL_UNAVAILABLE)
            self.reply(update_id, chat, response, previous)
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
                    batch = EventWrite(chat, event_source, update_id, self.sheets.spreadsheet_id,
                                       rows, fallback, keep_question)
                    if self.event_writes.stage(batch):
                        self.answer_event(batch)
                    else:
                        self.reply(update_id, chat, EVENT_UNCONFIRMED, keep_question)
                    return
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

    @staticmethod
    def event_confirmation(rows):
        if len(rows) == 1 and TELEGRAM_EVENT.fullmatch(str(rows[0][11])) and not rows[0][11].startswith(rows[0][8] + "-e"):
            row = rows[0]
            return f"Исправил в «БАЗЕ». {row[2]} · {row[4]} {row[5]}".rstrip()
        details = [f"{row[2]} · {row[4]} {row[5]}" if row[3] in MONEY_ACTIONS else row[2] for row in rows]
        return "\n".join(["Записал в «БАЗУ».", *dict.fromkeys(details)])[:3800]

    def correct_quantity(self, update_id, message, origin, text):
        chat = message["chat"]["id"]
        question = self.get("question")
        try:
            if os.environ.get("TRACKER_QUANTITY_CORRECTIONS") != "1":
                raise Clarification("Правка количества пока недоступна для этой установки.")
            parsed = AMOUNT_VALUE.fullmatch(text.strip().rstrip("!?"))
            target = message.get("reply_to_message") or {}
            target_id = target.get("message_id")
            if not parsed or target.get("from", {}).get("id") != chat or type(target_id) is not int:
                raise Clarification("Ответь на своё исходное сообщение. Например, «исправь на 60 г» или /amount 60.")
            selected, amount, unit = parsed.groups()
            value = float(amount.replace(",", ".")) if "," in amount or "." in amount else int(amount)
            row = revised_quantity(self.sheets.read(f"'{EVENTS}'!A2:O"), f"tg-{chat}-{target_id}", origin,
                                   value, unit, selected)
            if self.event_writes.pending_event(chat, self.sheets.spreadsheet_id, row[11]):
                raise Clarification("Предыдущая версия ещё сверяется. Дождись подтверждения и отправь правку снова.")
            batch = EventWrite(chat, origin, update_id, self.sheets.spreadsheet_id, [row], "", question)
            if self.event_writes.stage(batch):
                self.answer_event(batch)
            else:
                self.reply(update_id, chat, EVENT_UNCONFIRMED, question)
        except Clarification as error:
            self.reply(update_id, chat, str(error), question)

    def verify_event(self, batch, sheets):
        try:
            return self.event_writes.advance(batch, sheets)
        except ProviderError as error:
            logging.warning("Проверка записи RX: %s", error)
            return None

    def answer_event(self, batch):
        result = self.verify_event(batch, self.sheets)
        text = EVENT_UNCONFIRMED
        if result == "verified":
            text = self.event_confirmation(batch.rows)
        elif result == "existing":
            text = EVENT_EXISTS
        self.reply(batch.update_id, batch.user, text, batch.question, settled_origin=batch.origin if result else None)

    def reconcile_events(self, offset):
        now = int(time.time())
        for batch in self.event_writes.due(offset, now):
            self.event_writes.postpone(batch, now + 120)
            sheets = self.registered_sheet(batch.user)
            if sheets is None:
                continue
            result = self.verify_event(batch, sheets)
            if result:
                text = self.event_confirmation(batch.rows) if result == "verified" else EVENT_EXISTS
                try:
                    self.telegram.message(batch.user, text)
                except ProviderError as error:
                    logging.warning("Подтверждение записи RX: %s", error)
                else:
                    self.event_writes.finish(batch.user, batch.origin)

    def reply(self, update, chat, text, question=None, *, settled_origin=None):
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO state VALUES (?, ?)", [
                ("question", json.dumps(question)),
                ("response", json.dumps({"update": update, "text": text, "settled_origin": settled_origin})),
            ])
        if settled_origin is not None:
            self.event_writes.finish(chat, settled_origin)
        self.telegram.message(chat, text)


def main():
    import fcntl

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    telegram_only = os.environ.get("TRACKER_TELEGRAM_ONLY") == "1"
    pilot = os.environ.get("TRACKER_PILOT") == "1"
    owner = os.environ.get("TRACKER_OWNER_ID")
    if telegram_only and pilot:
        raise ProviderError("RX pilot", "incompatible worker modes")
    groups, face_tokens = report_config(os.environ)
    google_settings = legacy_google_settings(os.environ)
    required = list(REQUIRED)
    if pilot:
        required.extend((*PILOT_CONFIG_REQUIRED, "OPENAI_API_KEY"))
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
        if pilot:
            account = verify_telegram(telegram, os.environ[BOT_USERNAME])
        sheets = None
        model = None
        provisioner = None
        if not telegram_only:
            model = Model(session, os.environ["OPENAI_API_KEY"], os.environ.get("TRACKER_MODEL", "gpt-5.4-mini"),
                          os.environ.get("TRACKER_TRANSCRIBE_MODEL", "gpt-transcribe"))
        if pilot:
            provisioner = PilotProvisioner(oauth_session(*(os.environ[name] for name in OAUTH_KEYS)),
                                           os.environ["TRACKER_PILOT_REGISTRY_ID"])
            provisioner.verify_registry(os.environ[GOOGLE_EMAIL])
        if owner and not telegram_only:
            spreadsheet = os.environ["TRACKER_SPREADSHEET_ID"]
            if google_settings == SCRIPT_SETTINGS:
                sheets = ScriptSheets(session, *(os.environ[name] for name in SCRIPT_SETTINGS), spreadsheet)
            else:
                sheets = Sheets(spreadsheet, json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
            verify_ledger_schema(sheets)
            logging.info("Rich: проверены заголовки подключённой БАЗЫ")
        if not pilot:
            account = telegram.api("getMe", {})
        logging.info("Telegram @%s: %s", account["username"],
                     "режим без БАЗЫ" if telegram_only else "готов принимать личные сообщения")
        bot = Bot(telegram, sheets, model, data, int(owner) if owner else None, provisioner)
        if os.environ.get("TRACKER_COACHING") == "1":
            bot.coaching = Coaching(bot.db, telegram, bot.owner)
        if os.environ.get("TRACKER_SCHOOL") == "1":
            seller = (os.environ.get("TRACKER_PILOT_SELLER", "").strip()
                      if os.environ.get("TRACKER_COMMERCIAL_OPEN") == "1" else None)
            pilot_class = Sales if os.environ.get("TRACKER_FULFILLMENT") == "1" else Pilot
            bot.pilot_orders = pilot_class(bot.db, telegram, bot.owner, live_seller=seller)
        if os.environ.get("TRACKER_OPERATIONS") == "1":
            bot.operations = Operations(bot.db, telegram, bot.owner, data)
        if groups:
            try:
                if not account.get("supports_inline_queries"):
                    raise ProviderError("RX", "для групповых отчётов требуется inline")
                if face_tokens and not all(os.environ.get(name) for name in OAUTH_KEYS[:2]):
                    raise ProviderError("RX", "не настроен OAuth группового Face")
                faces = {
                    chat: GroupFace(oauth_session(os.environ[OAUTH_KEYS[0]], os.environ[OAUTH_KEYS[1]],
                                                  face_tokens[str(chat)]), chat, group.face)
                    for chat, group in groups.items() if group.face
                }
                grouped = GroupTelegram(telegram, account, bot.db, groups, bot.registered_sheet, faces)
                grouped.verify_groups()
                bot.groups = grouped
            except ProviderError as error:
                logging.warning("Групповые отчёты недоступны: %s", error)
        bot.run()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        logging.error("Запуск невозможен: %s", error if isinstance(error, ProviderError) else type(error).__name__)
        raise SystemExit(1) from None
