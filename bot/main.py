# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from .ai_quota import AIQuota
from .coaching import Coaching
from .demo_checkout import DemoCheckout
from .event_write import EVENTS, INBOX, EventWrite, EventWrites, inbox_index
from .group_face import GroupFace
from .group_telegram import UPDATE_TYPES, GroupTelegram
from .inbox_write import InboxPending, InboxWrites
from .ledger import (
    Clarification, INBOX_HEADERS, MONEY_ACTIONS, MOSCOW, PLAN_SCHEMA, TELEGRAM_EVENT, action_catalog,
    ledger_rows, revised_quantity, sheets_timestamp, summarize_query,
)
from .manual import HELP as MANUAL_HELP, RECORD_COMMAND, parse_manual
from .onboarding import OAUTH_KEYS, PilotProvisioner, oauth_session
from .operations import CONSENT_EPOCH, Operations
from .pilot import Pilot
from .pilot_check import (
    BOT_USERNAME, GOOGLE_EMAIL, PILOT_CONFIG_REQUIRED, SCRIPT_SETTINGS, legacy_google_settings, report_config,
    verify_telegram,
)
from .providers import Model, ProviderError, RecipientUnavailable, ScriptSheets, Sheets, Telegram
from .sales import Sales
from .voice_check import VoiceCheck

ROOT = Path(__file__).resolve().parents[1]
IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
REQUIRED = ("TELEGRAM_BOT_TOKEN", "TRACKER_DATA_DIR")
LEDGER_REQUIRED = ("TRACKER_SPREADSHEET_ID",)
LEDGER_UNAVAILABLE = "Запись в «БАЗУ» пока недоступна. Пришли сообщение заново после её подключения."
EVENT_UNCONFIRMED = "Входящее сохранено. Запись в «События» ещё проверяю."
EVENT_EXISTS = "С этим входящим уже связаны записи в «БАЗЕ»."
MODEL_UNAVAILABLE = "Входящее сохранено. ИИ сейчас недоступен. Для ручной записи ответь на исходное сообщение командой /record."
MODEL_LIMIT = "Входящее сохранено. Дневной лимит ИИ исчерпан. Для ручной записи ответь на исходное сообщение командой /record."
ADMISSION_SETTING = "TRACKER_PRIVATE_ADMISSION_IDS"
TERMS_SETTING = "TRACKER_PRIVATE_TERMS"
TERMS_REQUIRED = "Для участия сначала прочитай условия: /pilot_terms."
ONBOARDING_WAIT = "Сообщение сохранено. Когда «БАЗА» подключится, продолжу с него."
ADMISSION_REQUIRED = "Этот учебный поток закрыт. Вход доступен по согласованию с оператором."
INBOX_WAIT = "Сообщение ожидает сверки с Google. Бот продолжит с исходного сообщения."
ORIGINAL_REF = re.compile(r"tg-([0-9]+)-[0-9]+\.(ogg|jpg|png|webp)")
OGG_MIMES = {"audio/ogg", "application/ogg"}
OGG_SUFFIXES = {".ogg", ".oga", ".opus"}
AMOUNT_INSTRUCTION = re.compile(r"^(?:/amount(?:\s|$)|(?:исправь|поправь|исправление)\b)", re.IGNORECASE)
AMOUNT_VALUE = re.compile(
    r"(?:/amount|(?:исправь|поправь|исправление)(?:\s+на)?)\s+"
    r"(?:(tg-[0-9]+-[0-9]+-e[0-9]+)\s+)?([0-9]{1,12}(?:[.,][0-9]{1,6})?)(?:\s+([\w.]{1,12}))?",
    re.IGNORECASE,
)


def private_admission(env):
    raw = env.get(ADMISSION_SETTING)
    if raw is None:
        return None
    try:
        users = json.loads(raw)
    except ValueError:
        raise ProviderError("RX pilot", "invalid private admission configuration") from None
    if (not isinstance(users, list) or any(type(user) is not int or not 0 < user < 2**63
                                          for user in users)):
        raise ProviderError("RX pilot", "invalid private admission configuration")
    return frozenset(users)


def verify_ledger_schema(sheets):
    if sheets.read(f"'{INBOX}'!A1:H1") != [INBOX_HEADERS] or sheets.read(f"'{EVENTS}'!I1") != [["ID входящего"]]:
        raise ProviderError("Google Sheets", "unsupported ledger schema")


def media_mime(media):
    return (media.get("mime_type") or "").partition(";")[0].strip().lower()


def generic_ogg_file(media):
    mime = media_mime(media)
    return mime in {"", "application/octet-stream"} and Path(media.get("file_name") or "").suffix.lower() in OGG_SUFFIXES


def ogg_audio_header(data):
    # The first packet on the beginning-of-stream page identifies the codec.
    if len(data) < 28 or not data.startswith(b"OggS\x00") or not data[5] & 2:
        return False
    packet_start = 27 + data[26]
    return data[26] > 0 and data.startswith((b"OpusHead", b"\x01vorbis"), packet_start)


def attachment(message):
    for key in ("voice", "audio", "document"):
        if media := message.get(key):
            mime = media_mime(media)
            if mime in OGG_MIMES or (key == "voice" and not mime) or generic_ogg_file(media):
                return media, "audio/ogg", ".ogg"
    if photos := message.get("photo"):
        return photos[-1], "image/jpeg", ".jpg"
    if image := message.get("document"):
        mime = media_mime(image)
        if mime in IMAGE_TYPES:
            return image, mime, IMAGE_TYPES[mime]
    return None, None, None


class Bot:
    def __init__(self, telegram, sheets, model, directory, owner, provisioner=None, event_writes=None, inbox_writes=None,
                 *, admitted=None, terms=None):
        self.admitted = admitted if admitted is None or owner is None else admitted | {owner}
        self.terms = terms
        self.telegram = telegram
        self.sheets = sheets
        self.model = model
        self.directory = directory
        self.owner = owner
        self.provisioner = provisioner
        self.groups = None
        self.coaching = None
        self.pilot_orders = None
        self.demo_checkout = None
        self.voice_check = None
        self.operations = None
        self.ai_quota = None
        self.instructions = (ROOT / "tracking/telegram-prompt.md").read_text(encoding="utf-8")
        self.db = sqlite3.connect(directory / "bot.sqlite")
        self.db.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts (id INTEGER PRIMARY KEY, received TEXT NOT NULL)")
        self.terms_version = None
        if terms is not None:
            epoch = self.get(CONSENT_EPOCH)
            notice = terms if epoch is None else terms + "\0" + epoch
            self.terms_version = sha256(notice.encode()).hexdigest()
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS private_consents (
                    user INTEGER PRIMARY KEY, version TEXT, received TEXT NOT NULL, update_id INTEGER NOT NULL
                )
            """)
        self.db.commit()
        self.event_writes = event_writes if event_writes is not None else EventWrites(self.db)
        self.inbox_writes = inbox_writes if inbox_writes is not None else InboxWrites(self.db)

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
        if self.demo_checkout is not None:
            update_types.append("callback_query")
        while True:
            try:
                if self.groups is not None:
                    self.groups.sync_group_faces()
                if self.coaching is not None:
                    self.coaching.send_due(dt.datetime.now(MOSCOW), admitted=(
                        None if self.admitted is None else {user for user in self.admitted if self.has_private_access(user)}
                    ))
                if self.operations is not None:
                    try:
                        self.operations.run_daily(dt.datetime.now(MOSCOW))
                    except (OSError, sqlite3.Error, RuntimeError) as error:
                        logging.warning("Локальная резервная копия RX: %s", type(error).__name__)
                offset = self.get("offset", 0)
                self.reconcile_inbox(offset)
                self.reconcile_events(offset)
                if self.operations is not None:
                    try:
                        self.operations.check_alerts(dt.datetime.now(MOSCOW))
                    except (OSError, sqlite3.Error) as error:
                        logging.warning("Проверка состояния RX: %s", type(error).__name__)
                updates = self.telegram.api("getUpdates", {
                    "offset": offset, "timeout": 25, "allowed_updates": update_types,
                })
                if self.operations is not None:
                    self.operations.last_poll = dt.datetime.now(MOSCOW)
                if first_poll:
                    logging.info("Telegram API: опрос обновлений подтверждён")
                    first_poll = False
                self.receive(updates)
                for update in updates:
                    update_id = update["update_id"]
                    try:
                        self.process_update(update)
                    except RecipientUnavailable as error:
                        callback = update.get("callback_query", {})
                        message = callback.get("message", update.get("message", {}))
                        chat = message.get("chat", {})
                        sender = callback.get("from", message.get("from", {}))
                        if chat.get("type") != "private" or chat.get("id") != error.chat_id or sender.get("id") != error.chat_id:
                            raise
                        logging.warning("Telegram: адресат больше не принимает личные ответы")
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO state VALUES ('offset', ?)", (str(update_id + 1),))
                        self.db.execute("DELETE FROM receipts WHERE id = ?", (update_id,))
            except ProviderError as error:
                logging.warning("%s", error)
                time.sleep(20)
            except Exception as error:
                logging.error("Обработка прервана: %s", type(error).__name__)
                time.sleep(30)

    def admits(self, user):
        return (type(user) is int and user > 0
                and (self.admitted is None or user in self.admitted))

    def admit_private(self, message):
        user = message.get("from", {}).get("id")
        if type(user) is not int or user <= 0 or message.get("chat", {}).get("id") != user:
            return False
        if self.admits(user):
            return True
        self.telegram.message(user, ADMISSION_REQUIRED)
        return False

    def has_private_access(self, user):
        if not self.admits(user):
            return False
        if self.terms_version is None or user == self.owner:
            return True
        return self.db.execute("SELECT 1 FROM private_consents WHERE user=? AND version=?",
                               (user, self.terms_version)).fetchone() is not None

    def private_access_version(self, user):
        if not self.has_private_access(user):
            return None
        return self.terms_version or "open"

    def school_access_scope(self, user):
        version = self.private_access_version(user)
        if version is None or self.terms_version is None or user == self.owner:
            return version
        receipt = self.db.execute("SELECT received,update_id FROM private_consents WHERE user=?",
                                  (user,)).fetchone()
        return json.dumps((version, *receipt))

    def show_terms(self, user):
        self.telegram.message(user, f"{self.terms}\n\nПринять: /pilot_consent {self.terms_version}\n"
                                    "Отозвать: /pilot_withdraw")

    def handle_terms(self, user, text, update_id, received):
        if self.terms_version is None or user == self.owner:
            return False
        if text == "/pilot_terms":
            self.show_terms(user)
            return True
        if text == "/pilot_withdraw" or text == f"/pilot_consent {self.terms_version}":
            version = None if text == "/pilot_withdraw" else self.terms_version
            with self.db:
                self.db.execute("""
                    INSERT INTO private_consents(user, version, received, update_id) VALUES (?, ?, ?, ?)
                    ON CONFLICT(user) DO UPDATE SET version=excluded.version, received=excluded.received,
                        update_id=excluded.update_id
                    WHERE (excluded.received, excluded.update_id) > (private_consents.received, private_consents.update_id)
                """, (user, version, received.isoformat(), update_id))
            if self.has_private_access(user):
                self.telegram.message(user, "Условия приняты. Теперь можешь отправить первую запись.")
            else:
                self.telegram.message(user, "Обработка приостановлена. Условия доступны через /pilot_terms.")
            return True
        if text.partition(" ")[0] == "/pilot_consent" or not self.has_private_access(user):
            self.show_terms(user)
            return True
        return False

    def process_update(self, update):
        update_id = update["update_id"]
        (saved_at,) = self.db.execute("SELECT received FROM receipts WHERE id = ?", (update_id,)).fetchone()
        received = dt.datetime.fromisoformat(saved_at)
        if self.demo_checkout is not None and "callback_query" in update:
            query = update["callback_query"]
            if self.admitted is not None and not self.admits(query.get("from", {}).get("id")):
                self.demo_checkout.acknowledge(query, ADMISSION_REQUIRED)
                return
            if self.terms_version is not None and not self.has_private_access(query.get("from", {}).get("id")):
                self.demo_checkout.acknowledge(query, TERMS_REQUIRED)
                return
            if self.demo_checkout.callback(query, received):
                return
        message = update.get("message", {})
        if message.get("chat", {}).get("type") == "private":
            if not self.admit_private(message):
                return
            # Privacy requests must remain reachable while diary processing is paused.
            if (self.terms_version is not None and self.operations is not None
                    and message.get("text", "").strip().partition(" ")[0] == "/pilot_privacy"):
                self.operations.handle(update_id, message["from"]["id"], message, received)
                return
            if self.handle_terms(message["from"]["id"], message.get("text", ""), update_id, received):
                return
            if self.groups is None or not self.groups.private_history(message):
                self.process_private(update_id, message, received)
        elif self.groups is not None:
            try:
                self.groups.handle(update)
            except ProviderError as error:
                if error.provider != "Telegram" or error.status != 400:
                    raise
                logging.warning("Отчёт RX: Telegram отклонил устаревший запрос или сообщение")

    def process_private(self, update_id, message, received):
        user = message.get("from", {}).get("id")
        if not self.has_private_access(user) or message["chat"].get("id") != user:
            return
        waiting = self.inbox_writes.waiting(user)
        if not waiting or message.get("text") in {"/status", "/pilot_health"}:
            try:
                self.dispatch(update_id, message, received)
                return
            except InboxPending:
                pass
        self.inbox_writes.defer(update_id, user, message, received)
        if not waiting:
            try:
                self.telegram.message(user, INBOX_WAIT)
            except ProviderError:
                logging.warning("Входящие RX: уведомление об ожидании не доставлено")

    def reconcile_inbox(self, offset):
        now = int(time.time())
        for update_id, user, received, payload in self.inbox_writes.due(offset, now):
            self.inbox_writes.postpone(update_id, now + 120)
            if not self.has_private_access(user):
                continue
            can_prepare_pair = (self.terms_version is not None and self.provisioner is not None
                                and self.inbox_writes.needs_onboarding(update_id))
            if self.registered_sheet(user) is None and not can_prepare_pair:
                continue
            try:
                self.dispatch(update_id, json.loads(payload), dt.datetime.fromisoformat(received), recovering=True)
            except InboxPending:
                continue
            except ProviderError as error:
                if not isinstance(error, RecipientUnavailable) or error.chat_id != user:
                    logging.warning("Входящие RX: обработка отложена")
                    continue
            self.inbox_writes.finish(update_id)

    def registered_sheet(self, user):
        if not self.has_private_access(user):
            return None
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

    def dispatch(self, update_id, message, received, *, recovering=False):
        user = message.get("from", {}).get("id")
        if not self.has_private_access(user) or message["chat"].get("id") != user:
            return
        if self.voice_check is not None and self.voice_check.handle(user, message):
            return
        if self.demo_checkout is not None and self.demo_checkout.message(user, message.get("text", ""), received):
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
            if self.terms_version is not None and not recovering and self.registered_sheet(user) is None:
                self.inbox_writes.defer(update_id, user, message, received, onboarding=True)
                try:
                    self.telegram.message(user, ONBOARDING_WAIT)
                except ProviderError:
                    logging.warning("Подключение RX: уведомление об ожидании не доставлено")
                return
            directory = self.directory / "participants" / str(user)
            directory.mkdir(parents=True, exist_ok=True)
            bot = Bot(self.telegram, None, self.model, directory, user,
                      event_writes=self.event_writes, inbox_writes=self.inbox_writes)
            bot.ai_quota = self.ai_quota
            try:
                try:
                    bot.sheets = self.provisioner.provision(bot.get("files"), lambda state: bot.put("files", state))
                    if recovering:
                        self.inbox_writes.finish_onboarding(update_id)
                except ProviderError as error:
                    if recovering:
                        raise
                    logging.warning("Подключение RX: %s", error)
                    bot.telegram.message(user, "Подготовка «БАЗЫ» пока не завершилась. Пришли сообщение ещё раз позже.")
                    return
                bot.handle(update_id, message, received)
            finally:
                bot.db.close()
        elif message.get("text") == "/status":
            self.telegram.message(user, "Этот дневник eRex привязан к другому Telegram-профилю.")
        elif any(message.get(kind) for kind in ("voice", "audio", "document", "photo")):
            reply = "У этого профиля пока нет подключённой личной «БАЗЫ». Вложение осталось в чате Telegram."
            if self.demo_checkout is not None:
                reply += " Проверить передачу учебного голосового можно через /voice_check."
            self.telegram.message(user, reply)

    def capture(self, origin, received, source, text, file_ref):
        row = [origin, sheets_timestamp(received), source, text, file_ref, "", "", ""]
        return self.inbox_writes.capture(self.sheets, row)

    def save_receipt_file(self, origin, suffix, media):
        final = self.directory / f"{origin}{suffix}"
        if not final.exists():
            payload = self.telegram.media(media["file_id"], media.get("file_size"))
            if suffix == ".ogg" and generic_ogg_file(media) and not ogg_audio_header(payload):
                raise ProviderError("Telegram media", "invalid ogg")
            fd, name = tempfile.mkstemp(dir=self.directory, prefix=".attachment-")
            temp = Path(name)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                temp.replace(final)
            finally:
                temp.unlink(missing_ok=True)
        return final

    def originals(self, message, text):
        chat = message["chat"]["id"]
        if text == "/originals":
            names = [file.name for file in self.directory.glob(f"tg-{chat}-*")
                     if ORIGINAL_REF.fullmatch(file.name) and file.is_file()]
            names.sort(key=lambda name: int(name.split("-")[2].partition(".")[0]), reverse=True)
            reply = "Обращение за файлом: /original имя_файла\n"+"\n".join(names[:10]) if names else "Сохранённых оригиналов пока нет."
            self.telegram.message(chat, reply)
            return
        ref = text.removeprefix("/original").strip()
        if not ref:
            reply = message.get("reply_to_message") or {}
            origin = self.own_reply(chat, reply)
            _, _, suffix = attachment(reply)
            if origin and suffix:
                ref = f"{origin}{suffix}"
            else:
                self.telegram.message(chat, "Ответь /original на своё вложение или выбери имя в /originals.")
                return
        valid = ORIGINAL_REF.fullmatch(ref)
        if valid and valid[1] == str(chat):
            file = self.directory / ref
            if file.is_file():
                with file.open("rb") as stream:
                    self.telegram.api("sendDocument", {"chat_id": chat}, files={"document": (file.name, stream)})
                return
        self.telegram.message(chat, "Оригинал с таким именем не найден.")

    @staticmethod
    def own_reply(chat, reply):
        message_id = reply.get("message_id")
        if reply.get("from", {}).get("id") == chat and type(message_id) is int and message_id > 0:
            return f"tg-{chat}-{message_id}"
        return None

    def manual_record(self, update_id, message, origin, received, text):
        chat = message["chat"]["id"]
        previous = self.get("question")
        try:
            reply = message.get("reply_to_message") or {}
            if reply:
                parent = self.own_reply(chat, reply)
                if not parent or inbox_index(self.sheets, parent) is None:
                    raise Clarification("Ответь на своё сохранённое исходное сообщение или отправь /record отдельно.")
                origin = parent
                if previous and previous["origin"] == parent:
                    received = dt.datetime.fromisoformat(previous["received"])
                    previous = None
                elif type(reply.get("date")) is int:
                    received = dt.datetime.fromtimestamp(reply["date"], MOSCOW)
            actions = action_catalog(self.sheets.read("'Действия'!A2:G"))
            event = parse_manual(text, actions)
            self.stage_records(update_id, chat, [event], origin, received, actions, previous)
        except Clarification as error:
            self.reply(update_id, chat, str(error), self.get("question"))

    def stage_records(self, update_id, chat, events, origin, received, actions, previous):
        weights = self.sheets.read("'Действия'!I2")
        rows, fallback = ledger_rows(events, origin, received, actions, weights[0][0] if weights else None)
        batch = EventWrite(chat, origin, update_id, self.sheets.spreadsheet_id, rows, fallback, previous)
        if self.event_writes.stage(batch):
            self.answer_event(batch)
        else:
            self.reply(update_id, chat, EVENT_UNCONFIRMED, previous)

    def allow_model(self):
        if self.model is None:
            return MODEL_UNAVAILABLE
        if self.ai_quota is not None and not self.ai_quota.take(dt.datetime.now(MOSCOW)):
            return MODEL_LIMIT
        return None

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
        if text in {"/original", "/originals"} or text.startswith("/original "):
            self.originals(message, text)
            return
        if text.strip().casefold() == "/record":
            self.telegram.message(chat, MANUAL_HELP)
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
        if RECORD_COMMAND.match(message.get("text", "")):
            self.manual_record(update_id, message, origin, received, text)
            return
        if message.get("document") and not media:
            self.reply(update_id, chat, "Пришли голосовое OGG или изображение JPG, PNG, WebP.", self.get("question"))
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
                if error.status not in {"invalid ogg", "size", 400, 404}:
                    raise
                self.sheets.update(f"'{INBOX}'!E{idx}", [[""]])
                reason = ("Пришли запись в формате OGG с аудио Opus или Vorbis." if error.status == "invalid ogg"
                          else "Входящее принято. Пришли вложение повторно, размером до 18 МиБ.")
                self.reply(update_id, chat, reason, self.get("question"))
                return
        if mime == "audio/ogg":
            existing = self.sheets.read(f"'{INBOX}'!F{idx}")
            extracted = value_at(existing[0], 0) if existing else ""
            try:
                if not extracted:
                    if problem := self.allow_model():
                        self.reply(update_id, chat, problem, self.get("question"))
                        return
                    extracted = self.model.transcribe("voice.ogg", original.read_bytes(), mime)
            except ProviderError:
                self.reply(update_id, chat, MODEL_UNAVAILABLE, self.get("question"))
                return
            self.sheets.update(f"'{INBOX}'!F{idx}", [[extracted]])

        previous = self.get("question")
        if problem := self.allow_model():
            self.reply(update_id, chat, problem, previous)
            return
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
                    self.stage_records(update_id, chat, plan["events"], event_source, event_received, actions, keep_question)
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
            if not result:
                continue
            text = self.event_confirmation(batch.rows) if result == "verified" else EVENT_EXISTS
            try:
                self.telegram.message(batch.user, text)
            except ProviderError as error:
                if not isinstance(error, RecipientUnavailable) or error.chat_id != batch.user:
                    logging.warning("Подтверждение записи RX: %s", error)
                    continue
                logging.warning("Подтверждение записи RX: адресат больше не принимает личные ответы")
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
    ai_disabled = os.environ.get("TRACKER_AI_DISABLED") == "1"
    pilot = os.environ.get("TRACKER_PILOT") == "1"
    owner = os.environ.get("TRACKER_OWNER_ID")
    if telegram_only and pilot:
        raise ProviderError("RX pilot", "incompatible worker modes")
    groups, face_tokens = report_config(os.environ)
    admitted = private_admission(os.environ)
    terms = os.environ.get(TERMS_SETTING)
    if (admitted is None) != (terms is None) or (terms is not None and (not terms.strip() or len(terms) > 3000)):
        raise ProviderError("RX pilot", "private admission requires a non-empty terms notice")
    google_settings = legacy_google_settings(os.environ)
    required = list(REQUIRED)
    if pilot:
        required.extend(PILOT_CONFIG_REQUIRED)
    else:
        required.append("TRACKER_OWNER_ID")
    if not telegram_only and (owner or not pilot):
        required.extend((*LEDGER_REQUIRED, *google_settings))
    if not telegram_only and not ai_disabled:
        required.append("OPENAI_API_KEY")
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
        if not telegram_only and not ai_disabled:
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
        bot = Bot(telegram, sheets, model, data, int(owner) if owner else None, provisioner,
                  admitted=admitted, terms=terms)
        if "TRACKER_AI_DAILY_CALLS" in os.environ:
            raw_limit = os.environ["TRACKER_AI_DAILY_CALLS"]
            limit = int(raw_limit) if re.fullmatch(r"[0-9]{1,7}", raw_limit) else 0
            bot.ai_quota = AIQuota(bot.db, limit)
        demo_checkout = os.environ.get("TRACKER_DEMO_CHECKOUT") == "1"
        if os.environ.get("TRACKER_COACHING") == "1":
            bot.coaching = Coaching(bot.db, telegram, bot.owner, demo_only=demo_checkout,
                                    admitted=bot.has_private_access)
        if os.environ.get("TRACKER_SCHOOL") == "1":
            seller = (os.environ.get("TRACKER_PILOT_SELLER", "").strip()
                      if not demo_checkout and os.environ.get("TRACKER_COMMERCIAL_OPEN") == "1" else None)
            pilot_class = Sales if os.environ.get("TRACKER_FULFILLMENT") == "1" else Pilot
            bot.pilot_orders = pilot_class(bot.db, telegram, bot.owner, live_seller=seller,
                                           access_scope=bot.school_access_scope)
            if demo_checkout:
                bot.voice_check = VoiceCheck(telegram)
                bot.demo_checkout = DemoCheckout(
                    bot.db, telegram, bot.pilot_orders, bot.coaching,
                    school_cohort=os.environ.get("TRACKER_SCHOOL_COHORT"),
                    consent_version=bot.terms_version,
                )
        if os.environ.get("TRACKER_OPERATIONS") == "1":
            try:
                bot.operations = Operations(bot.db, telegram, bot.owner, data,
                                            ai_quota=bot.ai_quota, ai_disabled=model is None,
                                            alerts=os.environ.get("TRACKER_OPERATIONS_ALERTS") == "1")
            except (OSError, sqlite3.Error) as error:
                logging.warning("Эксплуатационные команды недоступны: %s", type(error).__name__)
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
                grouped = GroupTelegram(telegram, account, bot.db, groups, bot.registered_sheet, faces,
                                        admission=bot.private_access_version)
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
