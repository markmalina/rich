# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Deterministic input for the existing Rich event schema."""

import re

from .ledger import EVENT_FIELDS, FINANCE_ACTIONS, FINANCIAL_LABELS, PURCHASE_ACTION, Clarification

RECORD_COMMAND = re.compile(r"^/record(?:\s|$)", re.IGNORECASE)
QUANTITY = re.compile(r"([0-9]{1,12}(?:[.,][0-9]{1,6})?)(?:\s+([^|\n]{1,16}))?")
MONEY = re.compile(r"([0-9]{1,12}(?:[.,][0-9]{1,2})?)\s+([A-Za-z]{3})")
FINANCIAL_KINDS = {label: kind for kind, label in FINANCE_ACTIONS.items()} | {PURCHASE_ACTION: "purchase"}
HELP = ("Ручная запись. Укажи действие из своей «БАЗЫ». Например:\n"
        "/record Завтрак | Гранола | 80 г\n"
        "/record Приобретение | Покупка | 360 RUB | Карта\n"
        "Для записи по исходнику ответь на своё сообщение, фото или голосовое. "
        "Последним полем можно указать дату и время МСК: 2026-11-27 09:30.")


def decimal_text(text):
    return float(text.replace(",", ".")) if "," in text or "." in text else int(text)


def parse_manual(text, actions):
    arguments = RECORD_COMMAND.sub("", text, count=1).strip()
    fields = [part.strip() for part in arguments.split("|")]
    if len(arguments) > 1200 or not 2 <= len(fields) <= 5 or not fields[0] or not 1 <= len(fields[1]) <= 300:
        raise Clarification(HELP)
    label, summary, *args = fields
    action = next((key for key in actions if key.casefold() == label.casefold()), None)
    if action is None:
        raise Clarification("Укажи точное название действия из листа «Действия» своей «БАЗЫ».")
    event = {**dict.fromkeys(EVENT_FIELDS), "kind": "fact", "action": action, "summary": summary}
    if kind := FINANCIAL_KINDS.get(action):
        apply_money(event, kind, args)
        clock = args[2] if len(args) == 3 else ""
    else:
        if action in FINANCIAL_LABELS or len(args) > 2:
            raise Clarification(HELP)
        if args and args[0] not in {"", "?"}:
            number = QUANTITY.fullmatch(args[0])
            if not number:
                raise Clarification("Количество укажи числом с единицей, например 80 г. Для неизвестного введи ?.")
            event.update(quantity=decimal_text(number[1]), unit=number[2])
        clock = args[1] if len(args) == 2 else ""
    if clock:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2}(?::\d{2})?)?", clock):
            raise Clarification("Укажи дату и время МСК, например 2026-11-27 09:30.")
        event["date"], _, event["time"] = clock.partition(" ")
    return event


def apply_money(event, kind, args):
    amount = MONEY.fullmatch(args[0]) if 2 <= len(args) <= 3 else None
    if not amount or not args[1] or len(args[1]) > 120:
        raise Clarification("Укажи сумму с валютой и счёт через вертикальную черту, например 360 RUB | Карта.")
    event.update(kind=kind, amount=decimal_text(amount[1]), currency=amount[2].upper(), account=args[1])
    if kind == "purchase":
        event.update(resource=event["summary"], account=None, payment_account=args[1])
    elif kind == "internal_transfer":
        sender, _, recipient = args[1].partition("→")
        event.update(account=sender.strip(), destination_account=recipient.strip())
