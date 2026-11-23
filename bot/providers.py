# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

import base64
import json
from urllib.parse import quote

SHEETS_URL = "https://sheets.googleapis.com/v4/spreadsheets"
OPENAI_URL = "https://api.openai.com/v1"
MAX_MEDIA = 18 * 1024 * 1024


class ProviderError(RuntimeError):
    def __init__(self, provider, status):
        super().__init__(f"{provider}: {status}")
        self.provider = provider
        self.status = status


def request(session, provider, method, url, **kwargs):
    import requests

    try:
        response = session.request(method, url, **kwargs)
    except requests.RequestException:
        raise ProviderError(provider, "connection") from None
    if not response.ok:
        raise ProviderError(provider, response.status_code)
    return response


class Telegram:
    def __init__(self, session, token):
        self.session = session
        self.base = "https://api.telegram.org"
        self.token = token

    def api(self, method, body, **kwargs):
        options = {"data": body, **kwargs} if "files" in kwargs else {"json": body}
        response = request(self.session, "Telegram", "POST",
                           f"{self.base}/bot{self.token}/{method}", timeout=45, **options).json()
        if not response.get("ok"):
            raise ProviderError("Telegram", response.get("error_code", "request rejected"))
        return response["result"]

    def message(self, chat_id, text):
        return self.api("sendMessage", {"chat_id": chat_id, "text": text[:3900]})

    def media(self, file_id, known_size):
        if known_size and known_size > MAX_MEDIA:
            raise ProviderError("Telegram media", "size")
        path = self.api("getFile", {"file_id": file_id})["file_path"]
        with request(self.session, "Telegram media", "GET", f"{self.base}/file/bot{self.token}/{path}",
                     timeout=45, stream=True) as response:
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_MEDIA:
                    raise ProviderError("Telegram media", "size")
        return bytes(data)


class Sheets:
    def __init__(self, spreadsheet_id, credentials_info):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials

        credentials = Credentials.from_service_account_info(
            credentials_info, scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.session = AuthorizedSession(credentials)
        self.url = f"{SHEETS_URL}/{quote(spreadsheet_id, safe='')}/values"

    def read(self, area):
        url = f"{self.url}/{quote(area, safe='')}"
        return request(self.session, "Google Sheets", "GET", url,
                       params={"valueRenderOption": "UNFORMATTED_VALUE"}, timeout=30).json().get("values", [])

    def append(self, area, rows):
        url = f"{self.url}/{quote(area, safe='')}:append"
        return request(self.session, "Google Sheets", "POST", url,
                       params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                       json={"values": rows}, timeout=30).json()["updates"]["updatedRows"]

    def update(self, area, values):
        url = f"{self.url}/{quote(area, safe='')}"
        request(self.session, "Google Sheets", "PUT", url, params={"valueInputOption": "RAW"},
                json={"values": values}, timeout=30)


class Model:
    def __init__(self, session, key, model, transcribe_model):
        self.session = session
        self.headers = {"Authorization": f"Bearer {key}"}
        self.model = model
        self.transcribe_model = transcribe_model

    def transcribe(self, name, data, mime):
        response = request(
            self.session, "OpenAI transcription", "POST", f"{OPENAI_URL}/audio/transcriptions",
            headers=self.headers, data={"model": self.transcribe_model},
            files={"file": (name, data, mime)}, timeout=100,
        ).json()
        return response["text"]

    def plan(self, instructions, payload, schema, image=None):
        content = [{"type": "input_text", "text": json.dumps(payload, ensure_ascii=False)}]
        if image:
            data, mime = image
            url = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
            content.append({"type": "input_image", "image_url": url, "detail": "high"})
        response = request(
            self.session, "OpenAI", "POST", f"{OPENAI_URL}/responses", timeout=100,
            headers=self.headers, json={
                "model": self.model, "store": False, "instructions": instructions,
                "input": [{"role": "user", "content": content}],
                "text": {"format": {"type": "json_schema", "name": "tracker_plan", "schema": schema, "strict": True}},
            },
        ).json()
        if response.get("status") == "completed":
            for output in response.get("output", []):
                for part in output.get("content", []):
                    if part.get("type") == "output_text":
                        return json.loads(part["text"])
        raise ProviderError("OpenAI", "no plan")
