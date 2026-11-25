# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import datetime as dt
import sqlite3
import unittest
from unittest.mock import Mock, patch

from .group_face import HEADER, GROUP_TAG, GroupFace, group_face_rows
from .group_reports import Publication, Report
from .group_telegram import Group, GroupTelegram, Profile
from .onboarding import SHEET_MIME
from .providers import ProviderError
from .test_group_reports import stamp


DAY = dt.date(2026, 11, 25)


class GroupFaceTest(unittest.TestCase):
    def setUp(self):
        self.face = GroupFace(Mock(), -100, "dedicated-face")
        self.face.sheet = Mock()
        self.good = {"id": "dedicated-face", "mimeType": SHEET_MIME, "ownedByMe": True,
                     "trashed": False, "appProperties": {GROUP_TAG: "-100"}}
        self.permission = {"permissions": [{"type": "user", "role": "owner"}]}

    def test_shared_sheet_or_another_groups_sheet_is_never_written(self):
        public = {"permissions": [*self.permission["permissions"], {"type": "anyone", "role": "reader"}]}
        other_group = {**self.good, "appProperties": {GROUP_TAG: "-200"}}
        for responses in ([self.good, public], [other_group]):
            with patch.object(self.face, "api", side_effect=responses) as api:
                with self.assertRaises(ProviderError):
                    self.face.sync([])
                self.assertTrue(all(call.args[0] == "GET" for call in api.call_args_list))
        self.face.sheet.assert_not_called()
        self.face.sheet.read.assert_not_called()

    def test_latest_rows_replace_stale_data_using_literal_values(self):
        start = stamp(DAY)
        rows = group_face_rows([(1, DAY.isoformat(), "Исправление", start, 90)], {1: "=Формула"})
        tab = {"spreadsheetId": self.face.file_id, "sheets": [{"properties": {
            "title": "Отчёты RX", "sheetId": 1, "sheetType": "GRID",
            "gridProperties": {"rowCount": 10, "columnCount": 5},
        }}]}
        self.face.sheet.read.side_effect = [[HEADER, ["старый"], ["устаревший"]], [HEADER, *rows]]
        with patch.object(self.face, "api", side_effect=[self.good, self.permission, tab, {}]) as api:
            self.face.sync(rows)
        update = api.call_args.kwargs["json"]["requests"][0]["updateCells"]
        self.assertEqual(update["range"]["endRowIndex"], 3)
        self.assertEqual(update["rows"][0]["values"][1], {"userEnteredValue": {"stringValue": "=Формула"}})
        self.face.sheet.read.assert_called()


class ReportHistoryTest(unittest.TestCase):
    def setUp(self):
        self.telegram = Mock()
        self.telegram.api.return_value = {"status": "member"}
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.face = Mock()
        profiles = {1: Profile((), True), 2: Profile((), True)}
        groups = {
            -100: Group("Своя группа", profiles, {1: "Трекер"}, "face-a"),
            -200: Group("Чужая группа", {1: profiles[1]}, {1: "Трекер"}, None),
        }
        self.rx = GroupTelegram(self.telegram, {"id": 9, "username": "TestRX"}, self.db,
                               groups, Mock(), {-100: self.face})
        self.journal = self.rx.journal
        self.start = stamp(DAY, 15)
        self.journal.membership(-100, 1, True, self.start - 100, 1)
        self.journal.membership(-200, 1, True, self.start - 100, 2)
        self.journal.membership(-100, 2, True, self.start + 10, 3)
        self.publish(-100, 1, "Старая запись", 20, self.start)
        self.publish(-200, 1, "Чужая запись", 21, self.start)
        self.publish(-100, 1, "Исправление", 22, self.start + 11, correction=True)

    def publish(self, chat, author, text, message, sent, correction=False):
        token = self.journal.prepare(chat, author, DAY, sent, Report(text, True), correction=correction)
        self.assertEqual(self.journal.confirm(token, Publication(chat, author, message, sent, 9, text), 9),
                         "accepted")

    def test_private_history_never_reads_before_join_or_after_leave(self):
        request = {"chat": {"type": "private", "id": 2}, "from": {"id": 2}, "text": "/reports"}
        self.assertTrue(self.rx.private_history(request))
        self.telegram.message.assert_called_once_with(2, "Своя группа · Трекер\nИсправление")
        self.telegram.message.reset_mock()
        self.telegram.api.return_value = {"status": "left"}
        self.assertTrue(self.rx.private_history(request))
        self.telegram.message.assert_called_once_with(2, "За эту страницу нет доступных групповых отчётов.")

    def test_projection_retries_after_failure_and_only_delivers_latest_version(self):
        self.face.sync.side_effect = [ProviderError("Group Face", 503), None]
        with patch("bot.group_telegram.time.monotonic", side_effect=[100, 101, 165, 240]):
            for _ in range(4):
                self.rx.sync_group_faces()
        self.assertEqual(self.face.sync.call_count, 2)
        self.assertEqual(self.face.sync.call_args_list[0], self.face.sync.call_args_list[1])
        rows = self.face.sync.call_args.args[0]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3:], ["Исправление", "22"])
