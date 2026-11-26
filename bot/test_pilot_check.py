# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import unittest
from unittest.mock import Mock, patch

from .onboarding import DRIVE_ABOUT_URL, DRIVE_URL, REGISTRY_HEADERS, SHEET_MIME, PilotProvisioner
from .pilot_check import TEST_USERS, check, configured_test_participants, verify_telegram
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

    def test_pilot_allowlist_rejects_invalid_ids(self):
        self.assertEqual(configured_test_participants({TEST_USERS: "10, 20"}), {10, 20})
        for invalid in ("", "0", "-10", "10,", "everyone"):
            with self.subTest(value=invalid), self.assertRaisesRegex(ProviderError, TEST_USERS):
                configured_test_participants({TEST_USERS: invalid})


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
