# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Check RX pilot credentials and destinations without changing remote state."""

import argparse
from contextlib import ExitStack
import json
import os
import re

from .group_face import GroupFace, HEADER, TITLE
from .group_telegram import configured_groups, verify_group_chats
from .onboarding import OAUTH_KEYS, PILOT_REQUIRED, PilotProvisioner, oauth_session
from .providers import ProviderError, Telegram

BOT_USERNAME = "TRACKER_PILOT_BOT_USERNAME"
GOOGLE_EMAIL = "TRACKER_PILOT_GOOGLE_EMAIL"
TEST_USERS = "TRACKER_PILOT_TEST_USERS"
PILOT_CONFIG_REQUIRED = (*PILOT_REQUIRED, BOT_USERNAME, GOOGLE_EMAIL, TEST_USERS)
SCRIPT_SETTINGS = ("TRACKER_APPS_SCRIPT_URL", "TRACKER_APPS_SCRIPT_TOKEN")


def configured_test_participants(env):
    values = [value.strip() for value in env[TEST_USERS].split(",")]
    if any(re.fullmatch(r"[1-9][0-9]{0,15}", value) is None for value in values):
        raise ProviderError("RX pilot", f"invalid {TEST_USERS}")
    return frozenset(map(int, values))


def legacy_google_settings(env):
    return SCRIPT_SETTINGS if any(env.get(name) for name in SCRIPT_SETTINGS) else ("GOOGLE_SERVICE_ACCOUNT_JSON",)


def report_config(env):
    groups_json = env.get("TRACKER_REPORT_GROUPS")
    tokens_json = env.get("TRACKER_REPORT_FACE_TOKENS")
    try:
        groups = configured_groups(json.loads(groups_json)) if groups_json else {}
        tokens = json.loads(tokens_json) if tokens_json else {}
        face_chats = {str(chat) for chat, group in groups.items() if group.face}
        if (not isinstance(tokens, dict) or set(tokens) != face_chats
                or any(not isinstance(value, str) or not value.strip() for value in tokens.values())):
            raise ValueError("missing or invalid group Face credentials")
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ProviderError("RX pilot", "invalid group report configuration") from None
    return groups, tokens


def verify_telegram(telegram, expected, *, inline=False, forbid=None):
    username = expected.strip().removeprefix("@").casefold()
    if re.fullmatch(r"[a-z0-9_]{5,32}", username) is None:
        raise ProviderError("RX pilot", f"invalid {BOT_USERNAME}")
    if forbid is not None and username == forbid.strip().removeprefix("@").casefold():
        raise ProviderError("RX pilot", "this bot is reserved for the personal environment")
    result = telegram.api("getMe", {})
    if result.get("is_bot") is not True or result.get("username", "").casefold() != username:
        raise ProviderError("RX pilot", "unexpected Telegram bot")
    if inline and result.get("supports_inline_queries") is not True:
        raise ProviderError("RX pilot", "Telegram inline mode is disabled")
    if telegram.api("getWebhookInfo", {}).get("url"):
        raise ProviderError("RX pilot", "Telegram webhook is active, polling is unavailable")
    return result


def check(env, forbid=None):
    if env.get("TRACKER_TELEGRAM_ONLY") == "1":
        raise ProviderError("RX pilot", "TRACKER_TELEGRAM_ONLY conflicts with the pilot")
    required = ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "TRACKER_DATA_DIR", *PILOT_CONFIG_REQUIRED)
    if env.get("TRACKER_OWNER_ID"):
        required += ("TRACKER_SPREADSHEET_ID", *legacy_google_settings(env))
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise ProviderError("RX pilot", "missing variables: " + ", ".join(missing))
    configured_test_participants(env)
    groups, face_tokens = report_config(env)

    import requests

    with ExitStack() as stack:
        telegram = Telegram(stack.enter_context(requests.Session()), env["TELEGRAM_BOT_TOKEN"])
        account = verify_telegram(telegram, env[BOT_USERNAME], inline=bool(groups), forbid=forbid)
        google = stack.enter_context(oauth_session(*(env[name] for name in OAUTH_KEYS)))
        PilotProvisioner(google, env["TRACKER_PILOT_REGISTRY_ID"]).verify_registry(env[GOOGLE_EMAIL])
        verify_group_chats(telegram, account["id"], groups)
        for chat, group in groups.items():
            if group.face:
                group_google = stack.enter_context(oauth_session(env[OAUTH_KEYS[0]], env[OAUTH_KEYS[1]],
                                                                 face_tokens[str(chat)]))
                face = GroupFace(group_google, chat, group.face)
                face.verify_private()
                if face.tab()["title"] != TITLE or face.sheet.read("'Отчёты RX'!A1:E1") != [HEADER]:
                    raise ProviderError("Group Face", "unsupported report header")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbid-bot", metavar="USERNAME", help="Reject the personal bot during a test setup")
    args = parser.parse_args()
    try:
        check(os.environ, args.forbid_bot)
    except ProviderError as error:
        parser.exit(1, f"{error}\n")
    except (ValueError, KeyError, TypeError, AttributeError, ImportError) as error:
        parser.exit(1, f"RX pilot: {type(error).__name__}\n")
    print("Проверены Telegram, Google OAuth, права на реестр и настроенные группы с Face.")
    print("Запись двух тестовых пользователей и общий таймер Apps Script проверяются отдельно.")


if __name__ == "__main__":
    main()
