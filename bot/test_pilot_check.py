# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import fcntl
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from .onboarding import DRIVE_ABOUT_URL, DRIVE_URL, REGISTRY_HEADERS, SHEET_MIME, PilotProvisioner
from .pilot_check import closed_config, check, local_database, main, verify_telegram
from .providers import ProviderError


class TelegramPreflightTest(unittest.TestCase):
    def test_refuses_personal_token_when_the_expected_test_bot_is_different(self):
        telegram = Mock()
        telegram.api.return_value = {"id": 1, "username": "PersonalExampleBot", "is_bot": True}
        with self.assertRaisesRegex(ProviderError, "unexpected Telegram bot"):
            verify_telegram(telegram, "@StagingExampleBot")
        telegram.api.assert_called_once_with("getMe", {})

    def test_checks_inline_and_webhook_without_removing_either(self):
        telegram = Mock()
        account = {"id": 2, "username": "StagingExampleBot", "is_bot": True, "supports_inline_queries": True}
        telegram.api.side_effect = [account, {"url": "https://example.invalid/private"}]
        with self.assertRaisesRegex(ProviderError, "webhook is active"):
            verify_telegram(telegram, "stagingexamplebot", inline=True)
        self.assertEqual([call.args[0] for call in telegram.api.call_args_list], ["getMe", "getWebhookInfo"])
        telegram.api.side_effect = [{**account, "supports_inline_queries": False}]
        with self.assertRaisesRegex(ProviderError, "inline mode is disabled"):
            verify_telegram(telegram, "stagingexamplebot", inline=True)

    def test_can_exclude_the_personal_username_before_any_api_call(self):
        telegram = Mock()
        with self.assertRaisesRegex(ProviderError, "reserved for the personal environment"):
            verify_telegram(telegram, "@PersonalExampleBot", forbid="personalexamplebot")
        telegram.api.assert_not_called()


class GooglePreflightTest(unittest.TestCase):
    def setUp(self):
        self.pilot = PilotProvisioner(Mock(), "private-registry")
        self.pilot.registry = Mock()
        self.account = {"user": {"emailAddress": "tester@example.invalid"}}
        self.owned = {"id": "private-registry", "ownedByMe": True, "mimeType": SHEET_MIME, "trashed": False}
        self.private = {"permissions": [{"type": "user", "role": "owner"}]}

    def test_verifies_only_reads_from_the_expected_account_and_private_registry(self):
        self.pilot.registry.read.return_value = [REGISTRY_HEADERS]
        with patch.object(self.pilot, "google", side_effect=[self.account, self.owned, self.private]) as api:
            self.pilot.verify_registry("TESTER@example.invalid")
        url = f"{DRIVE_URL}/private-registry"
        self.assertEqual([call.args for call in api.call_args_list],
                         [("GET", DRIVE_ABOUT_URL), ("GET", url), ("GET", url + "/permissions")])
        self.pilot.registry.read.assert_called_once_with("'Пары'!A1:C1")

    def test_wrong_google_identity_or_public_registry_stops_before_reading_rows(self):
        with patch.object(self.pilot, "google", return_value=self.account) as api:
            with self.assertRaisesRegex(ProviderError, "unexpected Google account"):
                self.pilot.verify_registry("another@example.invalid")
            api.assert_called_once()
        shared = {"permissions": [*self.private["permissions"], {"type": "anyone", "role": "reader"}]}
        with patch.object(self.pilot, "google", side_effect=[self.account, self.owned, shared]):
            with self.assertRaisesRegex(ProviderError, "registry must be private"):
                self.pilot.verify_registry("tester@example.invalid")
        self.pilot.registry.read.assert_not_called()

    def test_incomplete_configuration_reports_only_missing_keys(self):
        with self.assertRaisesRegex(ProviderError, "missing variables.*TRACKER_PILOT_BOT_USERNAME") as raised:
            check({"TELEGRAM_BOT_TOKEN": "never-print-this"})
        self.assertNotIn("never-print-this", str(raised.exception))


class ClosedPilotPreflightTest(unittest.TestCase):
    def setUp(self):
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.data = Path(self.root.name) / "closed"
        self.env = {
            "TELEGRAM_BOT_TOKEN": "synthetic-token", "TRACKER_OWNER_ID": "7",
            "TRACKER_DATA_DIR": str(self.data), "TRACKER_OAUTH_CLIENT_ID": "client",
            "TRACKER_OAUTH_CLIENT_SECRET": "secret", "TRACKER_OAUTH_REFRESH_TOKEN": "refresh",
            "TRACKER_PILOT_REGISTRY_ID": "registry", "TRACKER_PILOT_BOT_USERNAME": "ClosedPilotBot",
            "TRACKER_PILOT_GOOGLE_EMAIL": "pilot@example.invalid", "TRACKER_PRIVATE_ADMISSION_IDS": "[]",
            "TRACKER_PRIVATE_TERMS": "Учебные условия v1", "TRACKER_SCHOOL_COHORT": "first-2026",
            "TRACKER_AI_DISABLED": "1", "TRACKER_SPREADSHEET_ID": "isolated-sheet",
            "TRACKER_APPS_SCRIPT_URL": "https://example.invalid/script", "TRACKER_APPS_SCRIPT_TOKEN": "fixture",
            **{name: "1" for name in ("TRACKER_PILOT", "TRACKER_DEMO_CHECKOUT", "TRACKER_SCHOOL",
                                       "TRACKER_COACHING", "TRACKER_FULFILLMENT", "TRACKER_OPERATIONS",
                                       "TRACKER_OPERATIONS_ALERTS")},
        }

    def database(self):
        path = self.data / "bot.sqlite"
        path.parent.mkdir()
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE deferred_inbox(update_id INTEGER,user INTEGER,received TEXT,payload TEXT,queued_at INTEGER);
            CREATE TABLE event_writes(user INTEGER,origin TEXT,queued_at INTEGER);
            CREATE TABLE pilot_orders(id INTEGER PRIMARY KEY,demo INTEGER);
            CREATE TABLE coach_links(id INTEGER PRIMARY KEY,demo INTEGER);
            CREATE TABLE school_cohorts(
                id TEXT PRIMARY KEY, starts TEXT, version TEXT, demo INTEGER, created TEXT
            );
            INSERT INTO school_cohorts VALUES ('first-2026','2026-11-28','fixture',1,'fixture');
            CREATE TABLE private_consents(user INTEGER PRIMARY KEY,version TEXT);
            CREATE TABLE operational_poll(id INTEGER PRIMARY KEY,last_success INTEGER);
        """)
        db.commit()
        db.close()
        return path

    def test_safe_demo_config_and_restored_database_are_ready_without_remote_calls(self):
        config = closed_config(self.env, forbid_bot="PersonalBot", forbid_data=Path(self.root.name) / "personal")
        self.assertEqual(config, {"cohort": "first-2026", "admitted": 0, "ai": "disabled"})
        state = local_database(self.database(), now=dt.datetime(2026, 11, 28, tzinfo=dt.timezone.utc),
                               expect_restored=True)
        self.assertEqual(state["integrity"], "ok")
        self.assertEqual(state["mode"], "demo-only")
        self.assertEqual(state["poll"], "poll_waiting")

    def test_local_cli_reports_only_aggregate_readiness(self):
        path = self.database()
        output = StringIO()
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(path), "--expect-restored",
                "--forbid-bot", "PersonalBot", "--forbid-data-dir", str(Path(self.root.name) / "personal")]
        with patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", argv), redirect_stdout(output):
            main()
        self.assertEqual(output.getvalue(),
                         "Закрытый учебный контур: настройки безопасны, SQLite ok, "
                         "режим demo-only, очередь 0, опрос poll_waiting.\n")

    def assert_cli_rejects(self, env, database, error):
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(database)]
        stderr = StringIO()
        with (patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv),
              patch("bot.pilot_check.check") as remote, patch("sys.stderr", stderr),
              self.assertRaises(SystemExit) as failed):
            main()
        self.assertEqual(failed.exception.code, 1)
        self.assertIn(error, stderr.getvalue())
        remote.assert_not_called()

    def test_cli_checks_owner_sheet_settings_and_named_cohort(self):
        path = self.database()
        for key in ("TRACKER_SPREADSHEET_ID", "TRACKER_APPS_SCRIPT_TOKEN"):
            with self.subTest(missing=key):
                env = {name: value for name, value in self.env.items() if name != key}
                self.assert_cli_rejects(env, path, key)
        self.assert_cli_rejects({**self.env, "TRACKER_REPORT_GROUPS": "{invalid"}, path,
                                "invalid group report configuration")
        self.assert_cli_rejects({**self.env, "TRACKER_SCHOOL_COHORT": " first-2026 "}, path,
                                "invalid closed-pilot cohort")
        self.assert_cli_rejects({**self.env, "TRACKER_SCHOOL_COHORT": "missing"}, path,
                                "configured training cohort is missing")

    def test_cli_requires_database_to_live_in_the_selected_volume(self):
        path = self.database()
        elsewhere = self.data.parent / "other.sqlite"
        path.rename(elsewhere)
        path.symlink_to(elsewhere)
        self.assert_cli_rejects(self.env, path, "--database must be bot.sqlite in TRACKER_DATA_DIR")

    def test_prepared_volume_can_initialize_exactly_one_demo_cohort_without_remote_calls(self):
        path = self.database()
        personal = Path(self.root.name) / "personal"
        personal.mkdir()
        reserved = personal / "bot.sqlite"
        reserved.write_bytes(b"fixture personal file")
        env = {**self.env, "TRACKER_SCHOOL_COHORT": "offline-rehearsal"}
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(path),
                "--forbid-bot", "PersonalBot", "--forbid-data-dir", str(personal), "--init-cohort"]

        def initialize(day):
            output = StringIO()
            with (patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", [*argv, day]),
                  patch("bot.pilot_check.check") as remote, redirect_stdout(output)):
                main()
            remote.assert_not_called()
            return output.getvalue()

        self.assertIn("Учебный поток создан", initialize("2026-12-01"))
        before = path.read_bytes()
        self.assertIn("Учебный поток уже подготовлен", initialize("2026-12-01"))
        with patch("sys.stderr", StringIO()) as errors, self.assertRaises(SystemExit) as conflict:
            initialize("2026-12-02")
        self.assertEqual(conflict.exception.code, 1)
        self.assertIn("другими параметрами", errors.getvalue())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(reserved.read_bytes(), b"fixture personal file")
        self.assertEqual(local_database(path, expected_cohort="offline-rehearsal",
                                       expect_restored=True)["poll"], "poll_waiting")

    def test_clean_volume_bootstraps_complete_demo_schema_before_first_poll(self):
        personal = Path(self.root.name) / "personal"
        personal.mkdir()
        env = {**self.env, "TRACKER_SCHOOL_COHORT": "clean-rehearsal"}
        path = self.data / "bot.sqlite"
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(path),
                "--forbid-bot", "PersonalBot", "--forbid-data-dir", str(personal),
                "--init-cohort", "2026-12-01", "--expect-restored"]
        output = StringIO()
        with (patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv),
              patch("bot.pilot_check.check") as remote, redirect_stdout(output)):
            main()
        remote.assert_not_called()
        self.assertIn("Учебный поток создан", output.getvalue())
        self.assertIn("режим demo-only, очередь 0, опрос poll_waiting", output.getvalue())
        self.assertTrue(path.is_file())
        self.assertTrue((self.data / "operations" / "erasures.sqlite").is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT id,starts,demo FROM school_cohorts").fetchall(),
                         [("clean-rehearsal", "2026-12-01", 1)])
        self.assertEqual(db.execute("SELECT COUNT(*) FROM private_consents").fetchone(), (0,))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM operational_poll").fetchone(), (0,))
        required = {"pilot_orders", "pilot_goods", "school_enrolments", "coach_links",
                    "pilot_tracking_days", "privacy_requests", "deferred_inbox", "event_writes"}
        tables = {name for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue(required <= tables)

    def test_clean_volume_refuses_unknown_files_and_held_worker_lock(self):
        self.data.mkdir()
        sentinel = self.data / "unknown.data"
        sentinel.write_text("keep", encoding="utf-8")
        personal = Path(self.root.name) / "personal"
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(self.data / "bot.sqlite"),
                "--forbid-bot", "PersonalBot", "--forbid-data-dir", str(personal),
                "--init-cohort", "2026-12-01"]
        with (patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", argv),
              patch("bot.pilot_check.check") as remote, patch("sys.stderr", StringIO()) as errors,
              self.assertRaises(SystemExit) as unsafe):
            main()
        self.assertEqual(unsafe.exception.code, 1)
        self.assertIn("data directory is not empty", errors.getvalue())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse((self.data / "bot.sqlite").exists())
        remote.assert_not_called()

        sentinel.unlink()
        with (self.data / "bot.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", argv),
                  patch("sys.stderr", StringIO()) as errors, self.assertRaises(SystemExit) as busy):
                main()
            self.assertEqual(busy.exception.code, 1)
            self.assertIn("worker lock is already held", errors.getvalue())
        self.assertFalse((self.data / "bot.sqlite").exists())

    def test_clean_volume_rolls_back_local_ledger_when_database_publish_fails(self):
        personal = Path(self.root.name) / "personal"
        env = {**self.env, "TRACKER_SCHOOL_COHORT": "rollback-rehearsal"}
        path = self.data / "bot.sqlite"
        argv = ["pilot_check", "--closed", "--local-only", "--database", str(path),
                "--forbid-bot", "PersonalBot", "--forbid-data-dir", str(personal),
                "--init-cohort", "2026-12-01"]
        replace = os.replace

        def fail_database(source, destination):
            if Path(destination) == path:
                raise OSError("synthetic publish failure")
            return replace(source, destination)

        with (patch.dict(os.environ, env, clear=True), patch.object(sys, "argv", argv),
              patch("bot.pilot_check.os.replace", side_effect=fail_database),
              patch("sys.stderr", StringIO()) as errors, self.assertRaises(SystemExit) as failed):
            main()
        self.assertEqual(failed.exception.code, 1)
        self.assertIn("could not be initialized", errors.getvalue())
        self.assertFalse(path.exists())
        self.assertFalse((self.data / "operations").exists())

    def test_offline_initialization_respects_the_worker_lock(self):
        path = self.database()
        before = path.read_bytes()
        argv = ["pilot_check", "--closed", "--local-only", "--init-cohort", "2026-12-01",
                "--database", str(path), "--forbid-bot", "PersonalBot",
                "--forbid-data-dir", str(Path(self.root.name) / "personal")]
        with (self.data / "bot.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with (patch.dict(os.environ, self.env, clear=True), patch.object(sys, "argv", argv),
                  patch("bot.pilot_check.check") as remote, patch("sys.stderr", StringIO()) as errors,
                  self.assertRaises(SystemExit) as busy):
                main()
            self.assertEqual(busy.exception.code, 1)
            self.assertIn("worker lock is already held", errors.getvalue())
            remote.assert_not_called()
        self.assertEqual(path.read_bytes(), before)

    def test_rejects_personal_resources_commercial_mode_and_unlimited_ai(self):
        with self.assertRaisesRegex(ProviderError, "reserved for the personal environment"):
            closed_config(self.env, forbid_bot="closedpilotbot")
        with self.assertRaisesRegex(ProviderError, "overlaps the personal environment"):
            closed_config(self.env, forbid_data=self.data)
        with self.assertRaisesRegex(ProviderError, "overlaps the personal environment"):
            closed_config(self.env, forbid_data=Path(self.root.name))
        unsafe = {**self.env, "TRACKER_COMMERCIAL_OPEN": "1"}
        with self.assertRaisesRegex(ProviderError, "commercial checkout"):
            closed_config(unsafe)
        unlimited = {**self.env, "TRACKER_AI_DISABLED": "0", "OPENAI_API_KEY": "synthetic"}
        with self.assertRaisesRegex(ProviderError, "TRACKER_AI_DAILY_CALLS"):
            closed_config(unlimited)

    def test_rejects_live_records_stale_queue_and_revived_consent(self):
        path = self.database()
        now = dt.datetime(2026, 11, 28, 12, tzinfo=dt.timezone.utc)
        db = sqlite3.connect(path)
        with db:
            db.execute("INSERT INTO pilot_orders VALUES (1,0)")
        with self.assertRaisesRegex(ProviderError, "live commercial records"):
            local_database(path, now=now, expect_restored=True)
        with db:
            db.execute("DELETE FROM pilot_orders")
            db.execute("INSERT INTO deferred_inbox VALUES (1,8,'x','{}',?)", (int(now.timestamp()) - 901,))
        with self.assertRaisesRegex(ProviderError, "stale queue"):
            local_database(path, now=now, expect_restored=True)
        with db:
            db.execute("DELETE FROM deferred_inbox")
            db.execute("INSERT INTO private_consents VALUES (8,'v1')")
        db.close()
        with self.assertRaisesRegex(ProviderError, "revived private consent"):
            local_database(path, now=now, expect_restored=True)
