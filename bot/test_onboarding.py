# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from copy import deepcopy
import datetime as dt
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from .ledger import MOSCOW
from .main import Bot
from .onboarding import DRIVE_URL, REGISTRY, REGISTRY_HEADERS, PilotProvisioner
from .providers import ProviderError


class ProvisionerTest(unittest.TestCase):
    def test_two_registrations_use_distinct_pairs_and_reopen_saved_base(self):
        pilot = PilotProvisioner(Mock(), "registry")
        pilot.registry = Mock()
        pilot.registry.read.return_value = [REGISTRY_HEADERS]
        stores = [{}, {}]

        def save_to(store):
            def save(state):
                store.update(deepcopy(state))
            return save

        with patch.object(pilot, "google", side_effect=[
            {}, {"id": "fa"}, {}, {"id": "ba"}, {}, {"id": "fb"}, {}, {"id": "bb"},
        ]) as google, patch.object(pilot, "setup", return_value=0), \
                patch.object(pilot, "register") as register, patch("bot.onboarding.Sheets") as sheets:
            sheets.return_value.read.side_effect = [
                [], [["Face · RX", "https://docs.google.com/spreadsheets/d/fa/edit#gid=0", *[True] * 6]],
                [], [["Face · RX", "https://docs.google.com/spreadsheets/d/fb/edit#gid=0", *[True] * 6]],
            ]
            for store in stores:
                pilot.provision(None, save_to(store))
            pilot.provision(stores[0], Mock())
            self.assertEqual(sheets.call_args.args, ("ba",))
        self.assertNotEqual(stores[0]["key"], stores[1]["key"])
        self.assertEqual([call.args[0] for call in register.call_args_list], [["ba", "fa", 0], ["bb", "fb", 0]])
        self.assertEqual(google.call_count, 8)

    def test_repairs_ambiguous_drive_create_without_a_second_create(self):
        pilot = PilotProvisioner(Mock(), "registry")
        state = {"key": "test"}
        save = Mock()
        with patch.object(pilot, "google", side_effect=[
            {}, ProviderError("Google pilot", "connection"), {}, {"files": [{"id": "same-face"}]},
        ]) as google:
            with self.assertRaisesRegex(ProviderError, "connection"):
                pilot.file(state, save, "face")
            self.assertTrue(state["face_creating"])
            with self.assertRaisesRegex(ProviderError, "previous file creation"):
                pilot.file(state, save, "face")
            self.assertEqual(pilot.file(state, save, "face"), "same-face")
            self.assertEqual(pilot.file(state, save, "face"), "same-face")
        methods = [call.args for call in google.call_args_list]
        self.assertEqual(methods.count(("POST", DRIVE_URL)), 1)
        self.assertNotIn("face_creating", state)

    def test_resumes_registered_pair_after_an_uncertain_append(self):
        pilot = PilotProvisioner(Mock(), "registry")
        pair = ["my-base", "my-face", 0]
        state, save = {}, Mock()
        pilot.registry = Mock()
        pilot.registry.read.side_effect = [[REGISTRY_HEADERS], [REGISTRY_HEADERS, pair]]
        pilot.registry.append.side_effect = ProviderError("Google pilot", "connection")
        with self.assertRaises(ProviderError):
            pilot.register(pair, state, save)
        self.assertTrue(state["registry_appending"])
        pilot.register(pair, state, save)
        pilot.registry.append.assert_called_once_with(REGISTRY, [pair])

    def test_rejects_registry_pair_that_shares_a_file_in_another_role(self):
        pilot = PilotProvisioner(Mock(), "registry")
        pilot.registry = Mock()
        pilot.registry.read.return_value = [REGISTRY_HEADERS, ["other-base", "my-base", 0]]
        with self.assertRaisesRegex(ProviderError, "registry pair conflict"):
            pilot.register(["my-base", "my-face", 0], {}, Mock())
        pilot.registry.append.assert_not_called()

    def test_definitively_rejected_registry_append_can_be_retried(self):
        pilot = PilotProvisioner(Mock(), "registry")
        pair = ["my-base", "my-face", 0]
        state, save = {}, Mock()
        pilot.registry = Mock()
        pilot.registry.read.side_effect = [[REGISTRY_HEADERS], [REGISTRY_HEADERS], [REGISTRY_HEADERS, pair]]
        pilot.registry.append.side_effect = [ProviderError("Google Sheets", 429), 1]
        with self.assertRaises(ProviderError):
            pilot.register(pair, state, save)
        self.assertNotIn("registry_appending", state)
        pilot.register(pair, state, save)
        self.assertEqual(pilot.registry.append.call_count, 2)


class ParticipantDispatchTest(unittest.TestCase):
    def test_first_messages_and_retries_stay_in_each_users_database(self):
        telegram = Mock()
        legacy_sheet = Mock()
        model = Mock()
        provisioner = Mock()
        sheets = {10: Mock(), 20: Mock()}
        provisioning_states = []
        pending = iter(sheets)
        actions = []
        now = dt.datetime(2026, 11, 25, 14, 0, tzinfo=MOSCOW)

        def handle(bot, update, message, received):
            actions.append((update, bot.sheets, bot.get("question"), received))
            bot.put("question", message["text"])

        def provision(state, save):
            provisioning_states.append(deepcopy(state))
            if state is None:
                state = {"base": next(pending)}
                save(state)
            return sheets[state["base"]]

        provisioner.provision.side_effect = provision
        with tempfile.TemporaryDirectory(dir=".") as directory:
            bot = Bot(telegram, legacy_sheet, model, Path(directory), 7, provisioner)
            try:
                with patch.object(Bot, "handle", autospec=True, side_effect=handle):
                    for update, user, text in [(1, 10, "first"), (2, 20, "second"),
                                               (3, 10, "next"), (4, 7, "legacy")]:
                        bot.dispatch(update, {"from": {"id": user}, "chat": {"id": user}, "text": text}, now)
                    bot.dispatch(5, {"from": {"id": 20}, "chat": {"id": 10}}, now)
            finally:
                bot.db.close()
        self.assertEqual(provisioning_states, [None, None, {"base": 10}])
        self.assertEqual(actions, [(1, sheets[10], None, now), (2, sheets[20], None, now),
                                   (3, sheets[10], "first", now), (4, legacy_sheet, None, now)])
