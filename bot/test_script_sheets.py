# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import unittest
from unittest.mock import Mock, patch

from .providers import ProviderError, ScriptSheets


class ScriptSheetsTest(unittest.TestCase):
    def setUp(self):
        self.url = "https://script.google.com/macros/s/test-deployment/exec"
        self.token = "a" * 64
        self.sheets = ScriptSheets(Mock(), self.url, self.token, "test-sheet")

    @patch("bot.providers.request")
    def test_reads_from_the_supported_google_redirect_without_resending_secret(self, request):
        redirect = "https://script.googleusercontent.com/macros/echo?user_content_key=test"
        request.side_effect = [
            Mock(status_code=302, headers={"Location": redirect}),
            Mock(status_code=200, json=Mock(return_value={"values": [["test"]]})),
        ]
        self.assertEqual(self.sheets.read("'Входящие'!A1:H1"), [["test"]])
        first, second = request.call_args_list
        self.assertEqual(first.args[2:], ("POST", self.url))
        self.assertEqual(first.kwargs["json"]["token"], self.token)
        self.assertEqual(first.kwargs["json"]["spreadsheet_id"], "test-sheet")
        self.assertEqual(second.args[2:], ("GET", redirect))
        self.assertEqual(second.kwargs, {"timeout": 30, "allow_redirects": False})

    @patch("bot.providers.request")
    def test_rejects_an_unrelated_redirect(self, request):
        request.return_value = Mock(status_code=302, headers={"Location": "https://other.example/macros/echo"})
        with self.assertRaisesRegex(ProviderError, "unexpected redirect"):
            self.sheets.read("'Входящие'!A1:H1")
        request.assert_called_once()

    @patch("bot.providers.request")
    def test_does_not_treat_a_failed_write_as_success(self, request):
        request.return_value = Mock(status_code=200, json=Mock(return_value={"error": "denied"}))
        with self.assertRaisesRegex(ProviderError, "operation rejected"):
            self.sheets.append("'События'!A:O", [[]])
