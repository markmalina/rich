# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

from dataclasses import dataclass
import hashlib
from pathlib import Path
import secrets
import time

from .providers import ProviderError


SAMPLE = Path(__file__).with_name("voice-sample.ogg")
MARKER = "eRex · проверка голоса "
INSTRUCTIONS = ("В течение 10 минут перешли это голосовое с подписью обратно в этот чат. "
                "Для проверки файла без пересылки ответь на это голосовое командой /voice_verify. "
                "Используется учебная запись. Расшифровка и запись в дневник выключены.")
INVALID = "Для этой проверки нужен свой свежий образец из /voice_check с его подписью."


@dataclass
class Challenge:
    expires: float
    token: str
    message_id: int
    file_key: str
    result: str = ""


class VoiceCheck:
    """The single Telegram polling worker owns short-lived challenges in memory."""

    def __init__(self, telegram, *, clock=time.monotonic):
        self.telegram = telegram
        self.clock = clock
        self.challenges = {}
        self.file_key = None

    def issue(self, user):
        now = self.clock()
        self.challenges = {key: value for key, value in self.challenges.items() if value.expires > now}
        if user in self.challenges and not self.challenges[user].result:
            self.telegram.message(user, "Образец уже отправлен. " + INSTRUCTIONS)
            return
        if len(self.challenges) >= 256 and user not in self.challenges:
            self.telegram.message(user, "Все проверки заняты. Повтори /voice_check через 10 минут.")
            return
        token = secrets.token_hex(6)
        voice = self.telegram.api("sendVoice", {"chat_id": user, "caption": f"{MARKER}{token}\n{INSTRUCTIONS}"},
                                  files={"voice": ("sample.ogg", SAMPLE.read_bytes(), "audio/ogg")})
        file_key = voice["voice"]["file_unique_id"]
        self.file_key = file_key
        self.challenges[user] = Challenge(now + 600, token, voice["message_id"], file_key)

    def handle(self, user, message):
        text = message.get("text", "").strip()
        if text == "/voice_check":
            self.issue(user)
            return True
        reply = text == "/voice_verify"
        sample_message = message.get("reply_to_message", {}) if reply else message
        voice = sample_message.get("voice", {})
        caption = sample_message.get("caption", "")
        key = voice.get("file_unique_id")
        if not reply and not (voice and (caption.startswith(MARKER) or (key and key == self.file_key))):
            return False
        challenge = self.challenges.get(user)
        if not challenge or challenge.expires <= self.clock():
            self.challenges.pop(user, None)
            self.telegram.message(user, INVALID)
            return True
        if (not key or key != challenge.file_key or not voice.get("file_id")
                or caption.split("\n", 1)[0] != MARKER + challenge.token
                or (reply and sample_message.get("message_id") != challenge.message_id)):
            self.telegram.message(user, INVALID)
            return True
        if challenge.result:
            self.telegram.message(user, challenge.result)
            return True

        try:
            data = self.telegram.media(voice["file_id"], voice.get("file_size"))
        except ProviderError:
            self.telegram.message(user, "Telegram пока не вернул файл. Повтори проверку этого же образца.")
            return True
        expected = SAMPLE.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != hashlib.sha256(expected).hexdigest():
            self.telegram.message(user, "Файл Telegram отличается от учебного оригинала. Проверка не пройдена.")
            return True
        route = "файл голосового из ответа" if reply else "входящее голосовое Telegram"
        challenge.result = (f"Проверено: {route}. Получено {len(data)} байт, SHA-256 {digest}. "
                            "Байты совпали с учебным оригиналом. Расшифровка и дневник не использовались.")
        self.telegram.message(user, challenge.result)
        return True
