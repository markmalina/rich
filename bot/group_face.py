# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import argparse
import datetime as dt
import os
import re

from tracking.bootstrap import cell

from .ledger import MOSCOW
from .onboarding import DRIVE_URL, OAUTH_KEYS, SHEET_MIME, oauth_session
from .providers import ProviderError, SHEETS_URL, Sheets, request

TITLE = "Отчёты RX"
HEADER = ["Дата отчёта", "Участник", "Опубликован (МСК)", "Отчёт", "Сообщение"]
AREA = "'Отчёты RX'!A1:E"
GROUP_TAG = "rxGroupChat"


def group_face_rows(reports, names):
    return [
        [day, names.get(author) or f"Участник {author}", dt.datetime.fromtimestamp(sent, MOSCOW).isoformat(),
         text, str(message)]
        for author, day, text, sent, message in reports
    ]


class GroupFace:
    def __init__(self, session, chat, file_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise ProviderError("Group Face", "invalid file ID")
        self.session = session
        self.chat = chat
        self.file_id = file_id
        self.drive_url = f"{DRIVE_URL}/{file_id}"
        self.sheets_url = f"{SHEETS_URL}/{file_id}"
        self.sheet = Sheets(file_id, session=session)

    def api(self, method, url, **kwargs):
        return request(self.session, "Group Face", method, url, timeout=15, **kwargs).json()

    def verify_private(self):
        metadata = self.api("GET", self.drive_url, params={
            "fields": "id,mimeType,ownedByMe,trashed,appProperties",
        })
        if (metadata.get("id") != self.file_id or metadata.get("mimeType") != SHEET_MIME
                or metadata.get("ownedByMe") is not True or metadata.get("trashed") is not False
                or metadata.get("appProperties", {}).get(GROUP_TAG) != str(self.chat)):
            raise ProviderError("Group Face", "file is not owned and bound to this group")
        access = self.api("GET", self.drive_url + "/permissions", params={
            "fields": "nextPageToken,permissions(type,role)", "pageSize": 20,
        })
        if access.get("nextPageToken") or access.get("permissions") != [{"type": "user", "role": "owner"}]:
            raise ProviderError("Group Face", "tracker-only file required")

    def tab(self):
        resource = self.api("GET", self.sheets_url, params={
            "fields": "spreadsheetId,sheets(properties(sheetId,sheetType,title,gridProperties))",
        })
        tabs = resource.get("sheets", [])
        if resource.get("spreadsheetId") != self.file_id or len(tabs) != 1:
            raise ProviderError("Group Face", "one report worksheet required")
        tab = tabs[0]["properties"]
        if tab.get("sheetType") != "GRID" or tab["gridProperties"]["columnCount"] < len(HEADER):
            raise ProviderError("Group Face", "unsupported worksheet structure")
        return tab

    def sync(self, rows):
        self.verify_private()
        tab = self.tab()
        existing = self.sheet.read(AREA)
        if tab["title"] != TITLE or not existing or existing[0] != HEADER:
            raise ProviderError("Group Face", "unsupported report header")
        if existing[1:] == rows:
            return
        last = max(len(existing), len(rows) + 1)
        count = tab["gridProperties"]["rowCount"]
        gid = tab["sheetId"]
        actions = []
        if last > count:
            actions.append({"appendDimension": {"sheetId": gid, "dimension": "ROWS", "length": last - count}})
        actions.append({"updateCells": {
            "range": {"sheetId": gid, "startRowIndex": 1, "endRowIndex": last,
                      "startColumnIndex": 0, "endColumnIndex": len(HEADER)},
            "rows": [{"values": [cell(value) for value in row]} for row in rows],
            "fields": "userEnteredValue",
        }})
        self.api("POST", self.sheets_url + ":batchUpdate", json={"requests": actions})
        if self.sheet.read(AREA) != [HEADER, *rows]:
            raise ProviderError("Group Face", "report projection not confirmed")

    def initialize(self):
        self.verify_private()
        tab = self.tab()
        if tab["title"] != TITLE:
            previous_title = tab["title"].replace("'", "''")
            if self.sheet.read(f"'{previous_title}'!A:E"):
                raise ProviderError("Group Face", "initial worksheet is not empty")
            self.api("POST", self.sheets_url + ":batchUpdate", json={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": tab["sheetId"], "title": TITLE, "gridProperties": {"frozenRowCount": 1}},
                "fields": "title,gridProperties.frozenRowCount",
            }}]})
        existing = self.sheet.read(AREA)
        if not existing:
            self.sheet.update("'Отчёты RX'!A1:E1", [HEADER])
        elif existing[0] != HEADER:
            raise ProviderError("Group Face", "unsupported report header")
        if self.sheet.read("'Отчёты RX'!A1:E1") != [HEADER]:
            raise ProviderError("Group Face", "report header not confirmed")


def main():
    parser = argparse.ArgumentParser(description="Prepare the tracker's private report workbook.")
    parser.add_argument("--group", required=True, type=int)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--create", action="store_true")
    action.add_argument("--resume", metavar="FILE_ID")
    args = parser.parse_args()
    if args.group >= 0:
        parser.error("Expected a Telegram group ID")
    missing = [name for name in OAUTH_KEYS if not os.environ.get(name)]
    if missing:
        parser.error("Missing environment variables: " + ", ".join(missing))
    with oauth_session(*(os.environ[name] for name in OAUTH_KEYS)) as session:
        if args.create:
            tag = str(args.group)
            existing = request(session, "Group Face", "GET", DRIVE_URL, timeout=15, params={
                "q": f"trashed = false and appProperties has {{ key='{GROUP_TAG}' and value='{tag}' }}",
                "fields": "nextPageToken,files(id)", "pageSize": 2,
            }).json()
            if existing.get("nextPageToken") or len(existing.get("files", [])) > 1:
                raise ProviderError("Group Face", "more than one file is tagged for the group")
            if existing.get("files"):
                file_id = existing["files"][0]["id"]
            else:
                file_id = request(session, "Group Face", "POST", DRIVE_URL, timeout=15, params={"fields": "id"},
                                  json={"name": "Face группы · RX", "mimeType": SHEET_MIME,
                                        "appProperties": {GROUP_TAG: tag}}).json()["id"]
            print(file_id, flush=True)
        else:
            file_id = args.resume
        GroupFace(session, args.group, file_id).initialize()


if __name__ == "__main__":
    main()
