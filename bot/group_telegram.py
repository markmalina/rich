# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from dataclasses import dataclass
import datetime as dt
import logging
import re
import time

from .group_reports import Practice, Publication, Report, ReportJournal, build_report, number
from .ledger import MOSCOW
from .providers import ProviderError

QUERY = re.compile(r"отчёт (-[0-9]{1,18}) (сегодня|вчера)( исправление)?")
REPORT_CODE = "\nRX · "
FORBIDDEN = {"Финансы", "Приобретение", "Оплата", "Перевод денег", "Доход", "Расход"}
UPDATE_TYPES = ["inline_query", "chat_member"]


def active_member(member):
    return member["status"] in {"creator", "administrator", "member"} or (
        member["status"] == "restricted" and member.get("is_member") is True)


@dataclass(frozen=True)
class Profile:
    practices: tuple[Practice, ...]
    finance: bool
    finance_minimum: int | None = None


@dataclass(frozen=True)
class Group:
    name: str
    profiles: dict[int, Profile]


def configured_groups(config):
    groups = {}
    for item in config:
        chat = item["chat"]
        if type(chat) is not int or chat >= 0 or chat in groups:
            raise ValueError("invalid group")
        tracker = item["tracker"]
        if type(tracker) is not int or tracker <= 0:
            raise ValueError("invalid tracker")
        name = item["name"]
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or name.splitlines() != [name]:
            raise ValueError("invalid group name")
        profiles = {}
        for user, profile in item["members"].items():
            practices = []
            user_id = int(user)
            if user_id <= 0 or user_id in profiles:
                raise ValueError("invalid group member")
            for entry in profile.get("practices", []):
                action = entry["action"]
                label = entry["label"]
                unit = entry["unit"]
                raw = entry.get("minimum")
                minimum = number(raw) if raw is not None else None
                if (not all(isinstance(value, str) and value.strip() and len(value) <= 80
                            and value.splitlines() == [value] for value in (action, label, unit))
                        or action in FORBIDDEN or unit.upper() in {"RUB", "BYN", "USD", "EUR", "₽"}
                        or (raw is not None and (minimum is None or minimum < 0))):
                    raise ValueError("invalid agreed practice")
                practices.append(Practice(action, label, unit, minimum))
            finance = profile.get("finance", False)
            finance_minimum = profile.get("finance_minimum")
            if type(finance) is not bool or (finance_minimum is not None and
                    (not finance or type(finance_minimum) is not int or finance_minimum < 0)):
                raise ValueError("invalid finance practice")
            if len(practices) > 8 or len({p.action for p in practices}) != len(practices):
                raise ValueError("invalid practice list")
            if not practices and not finance:
                raise ValueError("no agreed practice")
            profiles[user_id] = Profile(tuple(practices), finance, finance_minimum)
        tracker_profile = profiles[tracker]
        shared = {(practice.action, practice.unit) for practice in tracker_profile.practices}
        for profile in profiles.values():
            if (any((p.action, p.unit) not in shared for p in profile.practices)
                    or (profile.finance and not tracker_profile.finance)):
                raise ValueError("practice outside the tracker profile")
        groups[chat] = Group(name, profiles)
    return groups


class GroupTelegram:
    def __init__(self, telegram, account, db, groups, ledger):
        self.telegram = telegram
        self.bot_id = account["id"]
        self.username = account["username"].casefold()
        self.journal = ReportJournal(db)
        self.groups = groups
        self.ledger = ledger

    def verify_groups(self):
        for chat in self.groups:
            state = self.telegram.api("getChat", {"chat_id": chat})
            member = self.telegram.api("getChatMember", {"chat_id": chat, "user_id": self.bot_id})
            if (state["type"] != "supergroup" or state.get("username") or state.get("active_usernames")
                    or state.get("has_visible_history") or state.get("is_forum")
                    or member["status"] != "administrator"):
                raise ProviderError("RX pilot", "reports require a private supergroup with hidden history")

    def handle(self, update):
        if query := update.get("inline_query"):
            self.query(query)
        elif event := update.get("chat_member"):
            chat = event["chat"]["id"]
            member = event["new_chat_member"]
            user = member["user"]["id"]
            group = self.groups.get(chat)
            if group and user in group.profiles:
                self.journal.membership(chat, user, active_member(member), event["date"], update["update_id"])
        elif message := update.get("message"):
            self.message(message)

    def message(self, message):
        chat = message.get("chat", {}).get("id")
        user = message.get("from", {}).get("id")
        if message.get("chat", {}).get("type") != "supergroup":
            return
        group = self.groups.get(chat)
        if not group or user not in group.profiles or self.journal.joined(chat, user) is None:
            return
        text = message.get("text", "")
        if text.casefold() in {"/report", f"/report@{self.username}"}:
            self.telegram.api("sendMessage", {
                "chat_id": chat, "text": "Выбери дату или отправь исправление к уже опубликованному отчёту.",
                "reply_markup": {"inline_keyboard": [
                    [{"text": "Поделиться за сегодня", "switch_inline_query_current_chat": f"отчёт {chat} сегодня"},
                     {"text": "За вчера", "switch_inline_query_current_chat": f"отчёт {chat} вчера"}],
                    [{"text": "Исправить сегодня",
                      "switch_inline_query_current_chat": f"отчёт {chat} сегодня исправление"},
                     {"text": "Исправить вчера",
                      "switch_inline_query_current_chat": f"отчёт {chat} вчера исправление"}],
                ]},
            })
            return
        snapshot, separator, token = text.rpartition(REPORT_CODE)
        via = message.get("via_bot", {}).get("id")
        if (via != self.bot_id or message.get("forward_origin") or message.get("is_automatic_forward")
                or not separator or re.fullmatch(r"[a-f0-9]{32}", token) is None):
            return
        result = self.journal.confirm(token, Publication(chat, user, message["message_id"], message["date"],
                                                         via, snapshot), self.bot_id)
        if result == "accepted":
            issued_date = re.search(r"Отчёт за (\d{2}\.\d{2}\.\d{4})", snapshot).group(1)
            posted = dt.datetime.fromtimestamp(message["date"], MOSCOW).strftime("%d.%m.%Y")
            self.telegram.api("sendMessage", {
                "chat_id": chat, "text": "Отчёт принят." if posted == issued_date else "Отчёт принят с опозданием.",
                "reply_parameters": {"message_id": message["message_id"], "allow_sending_without_reply": True},
            })

    def query(self, query):
        results = []
        match = QUERY.fullmatch(query.get("query", ""))
        if match and query.get("chat_type") == "supergroup":
            chat = int(match[1])
            user = query["from"]["id"]
            group = self.groups.get(chat)
            if group and user in group.profiles and self.journal.joined(chat, user) is not None:
                results = self.preview(chat, user, group, match[2] == "вчера", bool(match[3]))
        self.telegram.api("answerInlineQuery", {
            "inline_query_id": query["id"], "results": results, "cache_time": 0, "is_personal": True,
        })

    def preview(self, chat, user, group, yesterday, correction):
        issued = int(time.time())
        day = dt.datetime.fromtimestamp(issued, MOSCOW).date() - dt.timedelta(days=yesterday)
        if self.journal.has_report(chat, user, day) != correction:
            return []
        profile = group.profiles[user]
        try:
            if not active_member(self.telegram.api("getChatMember", {"chat_id": chat, "user_id": user})):
                return []
            sheet = self.ledger(user)
            if sheet is None:
                return []
            base_report = build_report(day, sheet.read("'События'!A2:O"), profile.practices,
                                       finance=profile.finance, finance_minimum=profile.finance_minimum)
        except ProviderError as error:
            logging.warning("Отчёт RX: %s", error)
            return []
        prefix = ("Исправление. " if correction else "") + ("Опоздание. " if yesterday else "")
        report = Report(prefix + base_report.text, base_report.meets_minimum)
        token = self.journal.prepare(chat, user, day, issued, report, correction=correction)
        if token is None:
            return []
        return [{
            "type": "article", "id": token, "title": f"Отчёт за {day:%d.%m}",
            "description": f"{group.name} · {'Исправление' if correction else 'Проверь перед отправкой'}",
            "input_message_content": {"message_text": report.text + REPORT_CODE + token},
        }]
