# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
from decimal import Decimal
import math
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SHEETS_EPOCH = dt.date(1899, 12, 30)
INBOX_HEADERS = [
    "ID входящего", "Получено (МСК)", "Источник", "Исходный текст", "Оригинал",
    "Расшифровка / распознавание", "ID событий со временем сообщения", "Исходное основание времени",
]
QUERY_FIELDS = {"from_date": "string", "to_date": "string", "category": "string", "action": "string", "search": "string"}
FINANCE_ACTIONS = {
    "income": "Доход",
    "expense": "Расход",
    "internal_transfer": "Перевод между своими счетами",
    "transfer": "Перевод денег",
}
PURCHASE_ACTION = "Приобретение"
PAYMENT_ACTION = "Оплата"
FINANCIAL_LABELS = {"Финансы", PURCHASE_ACTION, PAYMENT_ACTION, *FINANCE_ACTIONS.values()}
OUTGOING_ACTIONS = {PAYMENT_ACTION, FINANCE_ACTIONS["expense"]}
MONEY_ACTIONS = {PAYMENT_ACTION, *FINANCE_ACTIONS.values()}
EVENT_FIELDS = {
    "date": "string", "time": "string", "action": "string", "summary": "string",
    "quantity": "number", "unit": "string", "duration": "number", "resource": "string",
    "account": "string", "payment_account": "string", "destination_account": "string",
    "party": "string", "amount": "number", "currency": "string",
    "face_text": "string", "met": "number", "body_weight_kg": "number", "gratitude": "boolean",
}


def strict_object(properties):
    return {
        "type": "object", "properties": properties, "required": list(properties),
        "additionalProperties": False,
    }


def optional_fields(fields):
    return {key: {"type": [kind, "null"]} for key, kind in fields.items()}


PLAN_SCHEMA = strict_object({
    "decision": {"type": "string", "enum": ["record", "query", "clarify", "ignore"]},
    "continues": {"type": "boolean"},
    "message": {"type": "string"},
    "extracted": {"type": ["string", "null"]},
    "events": {"type": "array", "items": strict_object({
        "kind": {"type": "string", "enum": ["activity", "purchase", "fact", *FINANCE_ACTIONS]},
        **optional_fields(EVENT_FIELDS),
    })},
    "query": strict_object(optional_fields(QUERY_FIELDS)),
})


class Clarification(ValueError):
    pass


def sheets_timestamp(value):
    elapsed = value.astimezone(MOSCOW).replace(tzinfo=None) - dt.datetime.combine(SHEETS_EPOCH, dt.time())
    return elapsed.total_seconds() / 86400


def as_date(value):
    if isinstance(value, (int, float)):
        return SHEETS_EPOCH + dt.timedelta(days=int(value))
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        try:
            return dt.datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            return None


def event_clock(event, received):
    if event["date"]:
        day = as_date(event["date"])
        if day is None:
            raise Clarification("Уточни дату события.")
    else:
        day = received.date()
    if event["time"]:
        try:
            clock = dt.time.fromisoformat(event["time"])
        except ValueError:
            raise Clarification("Уточни время события.") from None
        if clock.tzinfo:
            raise Clarification("Уточни время события по Москве.")
        return (day - SHEETS_EPOCH).days, (clock.hour * 3600 + clock.minute * 60 + clock.second) / 86400, False
    value = sheets_timestamp(received)
    return (day - SHEETS_EPOCH).days, value % 1, True


def value_at(row, index, default=""):
    return row[index] if len(row) > index else default


def action_catalog(rows):
    actions = {row[0]: row for row in rows if row and row[0]}
    for action in FINANCE_ACTIONS.values():
        actions.setdefault(action, [action, "Финансы", "", "", "", "", "Финансы"])
    return actions


def met_number(value):
    try:
        result = float(str(value).replace(",", "."))
    except ValueError:
        return None
    return result if math.isfinite(result) and result > 0 else None


def ledger_rows(events, origin, received, actions, default_weight):
    if not events or len(events) > 20:
        raise Clarification("Перечисли до 20 событий в одном сообщении.")
    result, by_message_time = [], []
    for index, event in enumerate(events, start=1):
        event_id = f"{origin}-e{index}"
        day, clock, used_receipt_time = event_clock(event, received)
        if used_receipt_time:
            by_message_time.append(event_id)
        units = event["unit"] or value_at(actions.get(event["action"], []), 2)
        duration = met_number(event["duration"])
        summary = event["summary"] or event["action"] or "Событие"

        def row(action, quantity, unit, resource, account="", party="", minutes=None, intensity=None,
                body_weight=None, face="", description=summary):
            if action not in actions and action not in FINANCE_ACTIONS.values():
                raise Clarification(f"Какое действие из справочника использовать для записи «{description}»?")
            return [
                (SHEETS_EPOCH + dt.timedelta(days=day)).strftime("%d.%m.%Y"),
                str(dt.timedelta(seconds=min(86399, int(round(clock * 86400))))).zfill(8),
                description, action, quantity if quantity is not None else "", unit or "",
                minutes or "", intensity or "", origin, body_weight or "", face, event_id,
                resource or "", account or "", party or "",
            ]

        kind = event["kind"]
        if kind == "purchase" or kind in FINANCE_ACTIONS:
            amount = event["amount"]
            currency = event["currency"] or ""
            if type(amount) not in (int, float) or met_number(amount) is None:
                raise Clarification("Какая была итоговая сумма?")
            if len(currency) != 3 or not currency.isascii() or not currency.isalpha() or currency != currency.upper():
                raise Clarification("В какой валюте была сумма?")
            if kind == "purchase":
                resource = event["resource"] or "Покупка"
                total = format(Decimal(str(amount)), "f")
                purchase_label = f"{summary} · итог {total} {currency}"
                result.append(row(PURCHASE_ACTION, event["quantity"] or 1, event["unit"] or "шт.",
                                  resource, event["account"] or "", event["party"], duration or 1,
                                  description=purchase_label))
                result.append(row(PAYMENT_ACTION, -amount, currency, "Деньги", event["payment_account"],
                                  event["party"], duration or 1, description="Оплата покупки"))
            elif kind == "internal_transfer":
                sender = (event["account"] or "").strip()
                recipient = (event.get("destination_account") or "").strip()
                if not sender or not recipient or sender.casefold() == recipient.casefold():
                    raise Clarification("С какого своего счёта и на какой свой счёт переведены деньги?")
                action = FINANCE_ACTIONS[kind]
                result.extend([
                    row(action, -amount, currency, "Деньги", sender, minutes=duration,
                        description=f"{action} · списание с {sender}"),
                    row(action, amount, currency, "Деньги", recipient,
                        description=f"{action} · поступление на {recipient}"),
                ])
            else:
                action = FINANCE_ACTIONS[kind]
                description = summary if summary != "Событие" else action
                signed = amount if kind == "income" else -amount
                result.append(row(action, signed, currency, "Деньги", event["account"], event["party"], duration,
                                  description=description))
                if event["gratitude"] and kind in {"expense", "transfer"}:
                    result.append(row("Благодарность", 1, "ед.", "Благодарность", party=event["party"],
                                      description="Получена благодарность"))
            continue

        action = event["action"]
        if not action:
            raise Clarification("Какое действие записать?")
        if action in FINANCIAL_LABELS or action == "Расход калорий":
            raise Clarification("Уточни, что произошло и какую сумму или действие нужно учесть.")
        exercise = value_at(actions.get(action, []), 6) == "Физические упражнения"
        met = met_number(event["met"]) if exercise else None
        weight = met_number(event["body_weight_kg"]) if exercise else None
        if exercise and duration is None:
            duration = 1
        result.append(row(action, event["quantity"], units, event["resource"], event["account"],
                          event["party"], duration, met, weight, event["face_text"] or ""))
        if exercise:
            intensity = met or met_number(value_at(actions[action], 3))
            estimate_weight = weight or met_number(default_weight)
            calories = None
            if intensity and estimate_weight:
                calories = -round(max(0, intensity - 1) * 3.5 * estimate_weight / 200 * duration, 1)
            result.append(row("Расход калорий", calories, "ккал", "Калории", "Тело",
                              description="Оценка активного расхода энергии"))
    return result, "|" + "|".join(by_message_time) + "|" if by_message_time else ""


def summarize_query(rows, actions, query, today):
    since = as_date(query["from_date"]) or today
    until = as_date(query["to_date"]) or since
    if since > until or (until - since).days > 366:
        raise Clarification("Уточни период до одного года.")
    matches = []
    for row in rows:
        date = as_date(value_at(row, 0))
        action = value_at(row, 3)
        if not date or not since <= date <= until or not action:
            continue
        if query["action"] and action != query["action"]:
            continue
        if query["category"] and query["category"] not in actions.get(action, [])[1:2] + actions.get(action, [])[6:7]:
            continue
        description = value_at(row, 2) or action
        part = " ".join(str(x) for x in [description, value_at(row, 12), value_at(row, 14)])
        if query["search"] and query["search"].casefold() not in part.casefold():
            continue
        time = value_at(row, 1)
        if isinstance(time, (int, float)):
            minute = round(time * 1440) % 1440
            time = f"{minute // 60:02d}:{minute % 60:02d}"
        date_label = date.strftime("%d.%m")
        quantity = value_at(row, 4)
        unit = value_at(row, 5)
        amount = f" · {quantity} {unit}".rstrip() if quantity != "" else ""
        matches.append((date, str(time), f"{date_label} {time} · {description}{amount}"))
    if not matches:
        return "За этот период подходящих записей в «БАЗЕ» нет."
    matches.sort()
    lines = [item[2] for item in matches[-15:]]
    return "\n".join([f"В «БАЗЕ» найдено строк: {len(matches)}.", *lines])[:3900]
