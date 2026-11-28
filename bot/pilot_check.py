# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Check RX pilot credentials and destinations without changing remote state."""

import argparse
from contextlib import ExitStack, closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .coaching import day_arg
from .group_face import GroupFace, HEADER, TITLE
from .group_telegram import configured_groups, verify_group_chats
from .ledger import Clarification, MOSCOW
from .onboarding import OAUTH_KEYS, PILOT_REQUIRED, PilotProvisioner, oauth_session
from .providers import ProviderError, Telegram
from .school import COHORT_ID, create_cohort

BOT_USERNAME = "TRACKER_PILOT_BOT_USERNAME"
GOOGLE_EMAIL = "TRACKER_PILOT_GOOGLE_EMAIL"
PILOT_CONFIG_REQUIRED = (*PILOT_REQUIRED, BOT_USERNAME, GOOGLE_EMAIL)
SCRIPT_SETTINGS = ("TRACKER_APPS_SCRIPT_URL", "TRACKER_APPS_SCRIPT_TOKEN")
CLOSED_FLAGS = ("TRACKER_PILOT", "TRACKER_DEMO_CHECKOUT", "TRACKER_SCHOOL", "TRACKER_COACHING",
                "TRACKER_FULFILLMENT", "TRACKER_OPERATIONS", "TRACKER_OPERATIONS_ALERTS")
ADMISSION_SETTING = "TRACKER_PRIVATE_ADMISSION_IDS"
TERMS_SETTING = "TRACKER_PRIVATE_TERMS"
COHORT_SETTING = "TRACKER_SCHOOL_COHORT"


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


def closed_config(env, *, forbid_bot=None, forbid_data=None):
    """Validate a closed demo worker without contacting Telegram, Google or OpenAI."""
    required = ("TELEGRAM_BOT_TOKEN", "TRACKER_OWNER_ID", "TRACKER_DATA_DIR", *PILOT_CONFIG_REQUIRED,
                ADMISSION_SETTING, TERMS_SETTING, COHORT_SETTING, "TRACKER_SPREADSHEET_ID",
                *legacy_google_settings(env))
    if env.get("TRACKER_AI_DISABLED") != "1":
        required += ("OPENAI_API_KEY", "TRACKER_AI_DAILY_CALLS")
    missing = [name for name in required if not env.get(name, "").strip()]
    if missing:
        raise ProviderError("RX pilot", "missing closed-pilot variables: " + ", ".join(missing))
    disabled = [name for name in CLOSED_FLAGS if env.get(name) != "1"]
    if disabled:
        raise ProviderError("RX pilot", "closed-pilot flags must be 1: " + ", ".join(disabled))
    if env.get("TRACKER_TELEGRAM_ONLY") == "1":
        raise ProviderError("RX pilot", "TRACKER_TELEGRAM_ONLY conflicts with the closed pilot")
    if env.get("TRACKER_COMMERCIAL_OPEN") == "1" or env.get("TRACKER_PILOT_SELLER", "").strip():
        raise ProviderError("RX pilot", "commercial checkout must be disabled for the closed pilot")
    try:
        owner = int(env["TRACKER_OWNER_ID"])
        admitted = json.loads(env[ADMISSION_SETTING])
    except (ValueError, TypeError):
        raise ProviderError("RX pilot", "invalid closed-pilot owner or admission configuration") from None
    if not 0 < owner < 2**63 or (not isinstance(admitted, list)
                      or any(type(user) is not int or not 0 < user < 2**63 for user in admitted)):
        raise ProviderError("RX pilot", "invalid closed-pilot owner or admission configuration")
    if len(env[TERMS_SETTING]) > 3000:
        raise ProviderError("RX pilot", "closed-pilot terms are too long")
    if COHORT_ID.fullmatch(env[COHORT_SETTING]) is None:
        raise ProviderError("RX pilot", "invalid closed-pilot cohort")
    if env.get("TRACKER_AI_DISABLED") != "1":
        limit = env["TRACKER_AI_DAILY_CALLS"]
        if re.fullmatch(r"[1-9][0-9]{0,6}", limit) is None:
            raise ProviderError("RX pilot", "closed pilot requires a positive daily AI limit")
    expected_bot = env[BOT_USERNAME].strip().removeprefix("@").casefold()
    if re.fullmatch(r"[a-z0-9_]{5,32}", expected_bot) is None:
        raise ProviderError("RX pilot", f"invalid {BOT_USERNAME}")
    if forbid_bot is not None and expected_bot == forbid_bot.strip().removeprefix("@").casefold():
        raise ProviderError("RX pilot", "this bot is reserved for the personal environment")
    data = Path(env["TRACKER_DATA_DIR"])
    if not data.is_absolute():
        raise ProviderError("RX pilot", "TRACKER_DATA_DIR must be an absolute isolated path")
    if forbid_data is not None:
        resolved, forbidden = data.resolve(), Path(forbid_data).resolve()
        if resolved == forbidden or resolved.is_relative_to(forbidden) or forbidden.is_relative_to(resolved):
            raise ProviderError("RX pilot", "this data directory overlaps the personal environment")
    report_config(env)
    return {"cohort": env[COHORT_SETTING], "admitted": len(set(admitted)),
            "ai": "disabled" if env.get("TRACKER_AI_DISABLED") == "1" else "limited"}


def local_database(path, *, now=None, max_queue_age=900, expect_restored=False, expected_cohort=None):
    """Read an isolated SQLite volume and fail closed on live data or stale work."""
    path = Path(path)
    if max_queue_age <= 0:
        raise ProviderError("RX pilot", "max queue age must be positive")
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    except (OSError, sqlite3.Error):
        raise ProviderError("RX pilot", "isolated database is unavailable") from None
    try:
        if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ProviderError("RX pilot", "isolated database failed integrity check")
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"state", "deferred_inbox", "event_writes", "pilot_orders", "coach_links"}
        if not required <= tables:
            raise ProviderError("RX pilot", "isolated database has an incomplete pilot schema")
        live = db.execute("SELECT COUNT(*) FROM pilot_orders WHERE demo=0").fetchone()[0]
        live += db.execute("SELECT COUNT(*) FROM coach_links WHERE demo=0").fetchone()[0]
        if "school_cohorts" in tables:
            live += db.execute("SELECT COUNT(*) FROM school_cohorts WHERE demo=0").fetchone()[0]
        if live:
            raise ProviderError("RX pilot", "isolated database contains live commercial records")
        if expected_cohort is not None and ("school_cohorts" not in tables or not db.execute(
            "SELECT 1 FROM school_cohorts WHERE id=? AND demo=1", (expected_cohort,)
        ).fetchone()):
            raise ProviderError("RX pilot", "configured training cohort is missing in the isolated database")
        queued = []
        pending = 0
        for table in ("deferred_inbox", "event_writes"):
            stamp = db.execute(f"SELECT MIN(queued_at) FROM {table}").fetchone()[0]
            pending += db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if stamp is not None:
                queued.append(stamp)
        current = now or dt.datetime.now(dt.timezone.utc)
        age = max(0, int(current.timestamp()) - min(queued)) if queued else None
        if age is not None and age >= max_queue_age:
            raise ProviderError("RX pilot", "isolated database has a stale queue")
        poll = "poll_waiting"
        if "operational_poll" in tables:
            row = db.execute("SELECT last_success FROM operational_poll WHERE id=1").fetchone()
            if row is not None:
                poll_age = int(current.timestamp()) - row[0] if type(row[0]) is int else max_queue_age + 1
                poll = "poll_ok" if 0 <= poll_age <= max_queue_age else "poll_stale"
        if poll == "poll_stale":
            raise ProviderError("RX pilot", "isolated database has a stale Telegram heartbeat")
        if expect_restored:
            if poll != "poll_waiting":
                raise ProviderError("RX pilot", "restored database already contains a polling heartbeat")
            if "private_consents" not in tables:
                raise ProviderError("RX pilot", "restored database has no private consent ledger")
            active = db.execute("SELECT COUNT(*) FROM private_consents WHERE version IS NOT NULL").fetchone()[0]
            if active:
                raise ProviderError("RX pilot", "restored database revived private consent")
        return {"integrity": "ok", "queue": pending, "oldest_seconds": age, "poll": poll,
                "mode": "demo-only"}
    except sqlite3.Error:
        raise ProviderError("RX pilot", "isolated database cannot be verified") from None
    finally:
        db.close()


def bootstrap_volume(directory, env, cohort, starts):
    """Build a complete demo-only schema off to the side, without network calls."""
    from .coaching import Coaching
    from .demo_checkout import DemoCheckout
    from .main import Bot
    from .operations import Operations
    from .sales import Sales

    class OfflineTelegram:
        def api(self, *args, **kwargs):
            raise ProviderError("RX pilot", "offline bootstrap attempted Telegram")

        def message(self, *args, **kwargs):
            raise ProviderError("RX pilot", "offline bootstrap attempted Telegram")

    owner = int(env["TRACKER_OWNER_ID"])
    admitted = frozenset(json.loads(env[ADMISSION_SETTING]))
    telegram = OfflineTelegram()
    bot = Bot(telegram, None, None, directory, owner, admitted=admitted, terms=env[TERMS_SETTING])
    try:
        bot.coaching = Coaching(bot.db, telegram, owner, demo_only=True, admitted=bot.has_private_access)
        bot.pilot_orders = Sales(bot.db, telegram, owner, access_scope=bot.school_access_scope)
        bot.demo_checkout = DemoCheckout(bot.db, telegram, bot.pilot_orders, bot.coaching,
                                         school_cohort=cohort, consent_version=bot.terms_version)
        bot.operations = Operations(bot.db, telegram, owner, directory,
                                    ai_disabled=env.get("TRACKER_AI_DISABLED") == "1", alerts=True)
        with bot.db:
            create_cohort(bot.db, cohort, starts, True, dt.datetime.now(MOSCOW))
        if bot.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ProviderError("RX pilot", "new isolated database failed integrity check")
    finally:
        if bot.operations is not None:
            bot.operations.ledger.close()
        bot.db.close()
    database = directory / "bot.sqlite"
    os.chmod(database, 0o600)
    with database.open("rb") as stream:
        os.fsync(stream.fileno())
    return database, directory / "operations"


def initialize_cohort(path, cohort, starts, *, max_queue_age, env):
    """Prepare or seed an isolated volume while its worker is stopped."""
    import fcntl

    path = path.resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.parent / "bot.lock"
    try:
        with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not path.exists():
                unexpected = [item.name for item in path.parent.iterdir() if item.name != "bot.lock"]
                if unexpected:
                    raise ProviderError("RX pilot", "new isolated data directory is not empty")
                with tempfile.TemporaryDirectory(dir=path.parent, prefix=".pilot-init-") as temporary:
                    staged = Path(temporary)
                    database, operations = bootstrap_volume(staged, env, cohort, starts)
                    local_database(database, max_queue_age=max_queue_age, expect_restored=True,
                                   expected_cohort=cohort)
                    installed_operations = path.parent / "operations"
                    os.replace(operations, installed_operations)
                    try:
                        os.replace(database, path)
                    except OSError:
                        os.replace(installed_operations, operations)
                        raise
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                return True
            local_database(path, max_queue_age=max_queue_age, expect_restored=True)
            with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)) as db, db:
                return create_cohort(db, cohort, starts, True, dt.datetime.now(MOSCOW))
    except BlockingIOError:
        raise ProviderError("RX pilot", "isolated worker lock is already held") from None
    except (OSError, sqlite3.Error):
        raise ProviderError("RX pilot", "isolated training cohort could not be initialized") from None


def check(env, forbid=None):
    if env.get("TRACKER_TELEGRAM_ONLY") == "1":
        raise ProviderError("RX pilot", "TRACKER_TELEGRAM_ONLY conflicts with the pilot")
    required = ("TELEGRAM_BOT_TOKEN", "TRACKER_DATA_DIR", *PILOT_CONFIG_REQUIRED)
    if env.get("TRACKER_AI_DISABLED") != "1":
        required += ("OPENAI_API_KEY",)
    if env.get("TRACKER_OWNER_ID"):
        required += ("TRACKER_SPREADSHEET_ID", *legacy_google_settings(env))
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise ProviderError("RX pilot", "missing variables: " + ", ".join(missing))
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
    parser.add_argument("--closed", action="store_true", help="Validate fail-closed settings for a training cohort")
    parser.add_argument("--local-only", action="store_true", help="Skip remote read-only checks during rehearsal")
    parser.add_argument("--database", type=Path, help="Read the isolated bot.sqlite before launch")
    parser.add_argument("--forbid-data-dir", type=Path, help="Reject the personal worker data directory")
    parser.add_argument("--expect-restored", action="store_true", help="Require a restored DB with cleared consent and poll")
    parser.add_argument("--max-queue-age", type=int, default=900)
    parser.add_argument("--init-cohort", metavar="YYYY-MM-DD",
                        help="Initialize the configured training cohort offline on a prepared volume")
    args = parser.parse_args()
    try:
        if args.init_cohort is not None and not (args.closed and args.local_only
                and args.forbid_bot and args.forbid_data_dir):
            raise ProviderError("RX pilot", "cohort initialization requires --closed, --local-only and both exclusions")
        if args.closed:
            config = closed_config(os.environ, forbid_bot=args.forbid_bot, forbid_data=args.forbid_data_dir)
            if args.database is None:
                raise ProviderError("RX pilot", "--database is required for the closed-pilot check")
            expected_database = Path(os.environ["TRACKER_DATA_DIR"]).resolve() / "bot.sqlite"
            if args.database.resolve() != expected_database:
                raise ProviderError("RX pilot", "--database must be bot.sqlite in TRACKER_DATA_DIR")
            if args.init_cohort is not None:
                created = initialize_cohort(expected_database, config["cohort"], day_arg(args.init_cohort),
                                            max_queue_age=args.max_queue_age, env=os.environ)
                print("Учебный поток создан." if created else "Учебный поток уже подготовлен.")
            state = local_database(args.database, max_queue_age=args.max_queue_age,
                                   expect_restored=args.expect_restored, expected_cohort=config["cohort"])
        elif args.local_only or args.database or args.forbid_data_dir or args.expect_restored:
            raise ProviderError("RX pilot", "local rehearsal options require --closed")
        if not args.local_only:
            check(os.environ, args.forbid_bot)
    except (Clarification, ProviderError) as error:
        parser.exit(1, f"{error}\n")
    except (ValueError, KeyError, TypeError, AttributeError, ImportError) as error:
        parser.exit(1, f"RX pilot: {type(error).__name__}\n")
    if args.closed:
        print(f"Закрытый учебный контур: настройки безопасны, SQLite {state['integrity']}, "
              f"режим {state['mode']}, очередь {state['queue']}, опрос {state['poll']}.")
    if not args.local_only:
        print("Проверены Telegram, Google OAuth, права на реестр и настроенные группы с Face.")
        print("Запись двух тестовых пользователей и общий таймер Apps Script проверяются отдельно.")


if __name__ == "__main__":
    main()
