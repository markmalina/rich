# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import argparse
import os
import re
from uuid import uuid4

from tracking.bootstrap import setup_requests, workbook

from .ledger import INBOX_HEADERS
from .providers import ProviderError, SHEETS_URL, Sheets, request

DRIVE_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
OAUTH_SCOPE = "https://www.googleapis.com/auth/drive.file"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"
REGISTRY = "'Пары'!A:C"
REGISTRY_HEADERS = ["БАЗА", "Face", "Лист Face"]
OAUTH_KEYS = ("TRACKER_OAUTH_CLIENT_ID", "TRACKER_OAUTH_CLIENT_SECRET", "TRACKER_OAUTH_REFRESH_TOKEN")
PILOT_REQUIRED = (*OAUTH_KEYS, "TRACKER_PILOT_REGISTRY_ID")


def oauth_session(client_id, client_secret, refresh_token):
    from google.auth.exceptions import GoogleAuthError
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2.credentials import Credentials

    class PilotOAuthSession(AuthorizedSession):
        def request(self, *args, **kwargs):
            try:
                return super().request(*args, **kwargs)
            except GoogleAuthError:
                raise ProviderError("Google OAuth", "authorization unavailable") from None

    return PilotOAuthSession(Credentials(
        token=None, refresh_token=refresh_token, client_id=client_id, client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token", scopes=[OAUTH_SCOPE],
    ))


class PilotProvisioner:
    def __init__(self, session, registry_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", registry_id):
            raise ProviderError("RX pilot", "invalid registry ID")
        self.session = session
        self.registry_id = registry_id
        self.registry = Sheets(registry_id, session=session)

    def google(self, method, url, **kwargs):
        return request(self.session, "Google pilot", method, url, timeout=30, **kwargs).json()

    def verify_registry(self, expected_email):
        account = self.google("GET", DRIVE_ABOUT_URL, params={"fields": "user(emailAddress)"})
        email = account.get("user", {}).get("emailAddress", "")
        if not email or email.casefold() != expected_email.strip().casefold():
            raise ProviderError("RX pilot", "unexpected Google account")
        url = f"{DRIVE_URL}/{self.registry_id}"
        metadata = self.google("GET", url, params={"fields": "id,mimeType,ownedByMe,trashed"})
        if (metadata.get("id") != self.registry_id or metadata.get("mimeType") != SHEET_MIME
                or metadata.get("ownedByMe") is not True or metadata.get("trashed") is not False):
            raise ProviderError("RX pilot", "registry must be owned by the pilot Google account")
        access = self.google("GET", url + "/permissions", params={
            "fields": "nextPageToken,permissions(type,role)", "pageSize": 20,
        })
        if access.get("nextPageToken") or access.get("permissions") != [{"type": "user", "role": "owner"}]:
            raise ProviderError("RX pilot", "registry must be private")
        if self.registry.read("'Пары'!A1:C1") != [REGISTRY_HEADERS]:
            raise ProviderError("RX pilot", "unsupported registry schema")

    def provision(self, state, save):
        if state is None:
            state = {"key": uuid4().hex}
            save(state)
        if not state.get("ready"):
            registry = self.registry.read(REGISTRY)
            if not registry or registry[0] != REGISTRY_HEADERS:
                raise ProviderError("RX pilot", "unsupported registry schema")
            face_id = self.file(state, save, "face")
            face_gid = self.setup(face_id, "Face · RX", face=True)
            base_id = self.file(state, save, "base")
            self.setup(base_id, "БАЗА · RX")
            base = Sheets(base_id, session=self.session)
            route = ["Face · RX", f"https://docs.google.com/spreadsheets/d/{face_id}/edit#gid={face_gid}",
                     True, True, True, True, True, True]
            if base.read("'Faces'!A2:H2") != [route]:
                base.update("'Faces'!A2:H2", [route])
                if base.read("'Faces'!A2:H2") != [route]:
                    raise ProviderError("RX pilot", "Face route not confirmed")
            self.register([base_id, face_id, face_gid])
            state["ready"] = True
            save(state)
        return Sheets(state["base"], session=self.session)

    def file(self, state, save, role):
        if state.get(role):
            return state[role]
        tag = state["key"] + "-" + role
        found = self.google("GET", DRIVE_URL, params={
            "q": f"trashed = false and mimeType = '{SHEET_MIME}' and "
                 f"appProperties has {{ key='rxPilot' and value='{tag}' }}",
            "fields": "nextPageToken,files(id)", "pageSize": 2,
        })
        files = found.get("files", [])
        if len(files) > 1 or found.get("nextPageToken"):
            raise ProviderError("RX pilot", "duplicate tagged file")
        if files:
            file_id = files[0]["id"]
        else:
            intent = role + "_creating"
            if state.get(intent):
                raise ProviderError("RX pilot", "previous file creation not confirmed")
            state[intent] = True
            save(state)
            try:
                result = self.google("POST", DRIVE_URL, params={"fields": "id"}, json={
                    "name": "Face · RX" if role == "face" else "БАЗА · RX",
                    "mimeType": SHEET_MIME, "appProperties": {"rxPilot": tag},
                })
            except ProviderError as error:
                if error.status in {400, 401, 403, 429}:
                    state.pop(intent)
                    save(state)
                raise
            file_id = result.get("id")
        if not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
            raise ProviderError("RX pilot", "unconfirmed file ID")
        state[role] = file_id
        state.pop(role + "_creating", None)
        save(state)
        return file_id

    def setup(self, file_id, title, face=False):
        url = f"{SHEETS_URL}/{file_id}"
        resource = workbook(title, face=face)
        expected = [tab["properties"]["title"] for tab in resource["sheets"]]
        actual = self.google("GET", url, params={"fields": "sheets(properties(sheetId,title))"})
        tabs = [sheet["properties"] for sheet in actual["sheets"]]
        default = tabs[0]["sheetId"]
        if [tab["title"] for tab in tabs] != expected:
            if len(tabs) != 1 or tabs[0]["title"] in expected:
                raise ProviderError("RX pilot", "unexpected workbook structure")
            tab_name = tabs[0]["title"].replace("'", "''")
            if Sheets(file_id, session=self.session).read(f"'{tab_name}'!A:Z"):
                raise ProviderError("RX pilot", "initial worksheet is not empty")
            batch = setup_requests(workbook(title, face=face, default_sheet_id=default),
                                   existing_default=True, face=face)
            self.google("POST", url + ":batchUpdate", json=batch)
        sheets = Sheets(file_id, session=self.session)
        header = [cell["userEnteredValue"]["stringValue"]
                  for cell in resource["sheets"][0]["data"][0]["rowData"][0]["values"]]
        first = "'События'!A1:J1" if face else "'Входящие'!A1:H1"
        if sheets.read(first) != [header] or (not face and
                (header != INBOX_HEADERS or sheets.read("'События'!I1") != [["ID входящего"]])):
            raise ProviderError("RX pilot", "workbook headers not confirmed")
        return default

    def register(self, pair):
        rows = self.registry.read(REGISTRY)
        if not rows or rows[0] != REGISTRY_HEADERS:
            raise ProviderError("RX pilot", "unsupported registry schema")
        matches = [row for row in rows[1:] if any(cell in pair[:2] for cell in row[:2])]
        if matches and matches != [pair]:
            raise ProviderError("RX pilot", "registry pair conflict")
        if not matches:
            self.registry.append(REGISTRY, [pair])
            if self.registry.read(REGISTRY).count(pair) != 1:
                raise ProviderError("RX pilot", "registry write not confirmed")


def init_registry(session, registry_id):
    if registry_id is None:
        response = request(session, "Google Drive", "POST", DRIVE_URL, timeout=30, params={"fields": "id"},
                           json={"name": "Реестр Face · RX", "mimeType": SHEET_MIME})
        registry_id = response.json()["id"]
        print(registry_id, flush=True)
    pilot = PilotProvisioner(session, registry_id)
    url = f"{SHEETS_URL}/{registry_id}"
    resource = pilot.google("GET", url, params={"fields": "sheets(properties(sheetId,title))"})
    tabs = [item["properties"] for item in resource["sheets"]]
    if len(tabs) != 1:
        raise ProviderError("RX pilot", "registry must have one worksheet")
    tab = tabs[0]
    if tab["title"] != "Пары":
        name = tab["title"].replace("'", "''")
        if pilot.registry.read(f"'{name}'!A:Z"):
            raise ProviderError("RX pilot", "initial registry worksheet is not empty")
        pilot.google("POST", url + ":batchUpdate", json={"requests": [{"updateSheetProperties": {
            "properties": {"sheetId": tab["sheetId"], "title": "Пары"}, "fields": "title",
        }}]})
    rows = pilot.registry.read(REGISTRY)
    if not rows:
        pilot.registry.update("'Пары'!A1:C1", [REGISTRY_HEADERS])
    elif rows[0] != REGISTRY_HEADERS:
        raise ProviderError("RX pilot", "unsupported registry schema")
    if pilot.registry.read("'Пары'!A1:C1") != [REGISTRY_HEADERS]:
        raise ProviderError("RX pilot", "registry headers not confirmed")


def main():
    parser = argparse.ArgumentParser(description="Set up the private Face registry with RX OAuth.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--create-registry", action="store_true")
    action.add_argument("--resume-registry", metavar="FILE_ID")
    args = parser.parse_args()
    missing = [name for name in OAUTH_KEYS if not os.environ.get(name)]
    if missing:
        parser.error("Missing environment variables: " + ", ".join(missing))
    init_registry(oauth_session(*(os.environ[name] for name in OAUTH_KEYS)), args.resume_registry)


if __name__ == "__main__":
    main()
