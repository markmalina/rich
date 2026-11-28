# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Offline T34 benchmark with mocked APIs and no actual polling waits."""

from collections import Counter
from contextlib import closing
import json
import logging
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import time
from unittest.mock import Mock, patch
from urllib.parse import unquote

from bot.main import Bot
from bot.onboarding import PilotProvisioner
from bot.providers import ProviderError
from bot.test_pilot_acceptance import PilotGoogle, PilotTelegram, message
from bot.test_tracker import event

USERS = range(100, 130)
UPDATES = [message(index + 100, USERS[index % len(USERS)], f"Факт {index}",
                   message_id=index // len(USERS) + 1) for index in range(100)]

class Traffic(PilotTelegram):
    def __init__(self):
        super().__init__()
        self.bursts = [UPDATES[start:start+10] for start in range(0, len(UPDATES), 10)]
        self.recovering = False
        self.submitted = {}
        self.acknowledged = {}
        self.waits = []
        self.accepts = []

    def api(self, method, body):
        if method == "getUpdates":
            if self.bursts:
                updates = self.bursts.pop(0)
                stamp = time.perf_counter()
                self.latest_burst = stamp
                self.submitted.update((item["update_id"] - 100, stamp) for item in updates)
                return updates
            self.waits.append(body["timeout"])
            if not self.recovering or body["timeout"] == 25:
                raise KeyboardInterrupt
            return []
        if method == "sendMessage" and (fact := re.search(r"Записал в «БАЗУ»\.\nФакт (\d+)", body["text"])):
            key = int(fact[1])
            self.acknowledged.setdefault(key, time.perf_counter() - self.submitted[key])
        return super().api(method, body)

def quantile(data, p):
    seq = sorted(data)
    return round(seq[math.ceil(len(seq)*p)-1] * 1000, 3)

def execute():
    traffic, google, model = Traffic(), PilotGoogle(), Mock()
    attempts = Counter()
    lost_books = set()
    sheets_timings = []
    ai_timings = []
    affected = [f"tg-{user}-1" for user in range(100, 106)]
    recover = False

    def request(session, provider, method, url, **kwargs):
        address = unquote(url)
        if address.endswith("/'События'!A:O:append"):
            key = kwargs["json"]["values"][0][8]
            attempts[key] += 1
            if key == "tg-106-1" and attempts[key] == 1:
                raise ProviderError("Google Sheets", 429)
            if key in affected and attempts[key] == 1:
                google.request(session, provider, method, url, **kwargs)
                lost_books.add(address.split("/values/", 1)[0])
                raise ProviderError("Google Sheets", "connection")
        if not recover and "/values/" in address and address.endswith("/'События'!A2:O"):
            if address.split("/values/", 1)[0] in lost_books:
                raise ProviderError("Google Sheets", "connection")
        return google.request(session, provider, method, url, **kwargs)

    def timed_sheets_request(*args, **kwargs):
        started = time.perf_counter()
        try:
            return request(*args, **kwargs)
        finally:
            sheets_timings.append(time.perf_counter() - started)

    def plan(instructions, info, schema, image):
        started = time.perf_counter()
        value = event("fact", action="Чтение книги", summary=info["text"], quantity=1, unit="мин")
        ai_timings.append(time.perf_counter() - started)
        return {"decision": "record", "continues": False, "message": "", "events": [value]}

    model.plan.side_effect = plan
    with (TemporaryDirectory() as root,
          patch("bot.onboarding.request", side_effect=google.request),
          patch("bot.providers.request", side_effect=timed_sheets_request),
          patch("bot.main.time.sleep", side_effect=AssertionError("unexpected poll loop sleep"))):
        directory = Path(root)
        provisioner = PilotProvisioner(google.session, "registry")

        def start():
            return Bot(traffic, None, model, directory, 7, provisioner)

        bot = start()
        with closing(bot.db):
            for user in USERS:
                item = message(user-100, user, "/status", message_id=900)
                bot.receive([item])
                bot.process_update(item)
            assert len(google.created) == 60
            real_receive = bot.receive
            def timed_receive(updates):
                real_receive(updates)
                if updates:
                    traffic.accepts.append(time.perf_counter() - traffic.latest_burst)
            with patch.object(bot, "receive", side_effect=timed_receive):
                try:
                    bot.run()
                except KeyboardInterrupt:
                    pass
            assert bot.get("offset") == 200
            pending = bot.db.execute("SELECT COUNT(*) FROM event_writes").fetchone()[0]
            assert pending > 6, (pending, len(traffic.acknowledged), len(attempts), list(lost_books), traffic.calls[-4:])
            ack_before = len(traffic.acknowledged)
            normal_delays = list(traffic.acknowledged.values())

        recover = traffic.recovering = True
        traffic.waits.clear()
        started = time.perf_counter()
        later = time.time() + 180
        restarted = start()
        with closing(restarted.db), patch("bot.main.time.time", return_value=later):
            try:
                restarted.run()
            except KeyboardInterrupt:
                pass
            recovery_ms = round((time.perf_counter() - started) * 1000, 3)
            assert not restarted.db.execute("SELECT 1 FROM event_writes").fetchone()
            assert not restarted.db.execute("SELECT 1 FROM deferred_inbox").fetchone()
            assert traffic.waits[-1] == 25 and all(w == 1 for w in traffic.waits[:-1])
            assert len(traffic.acknowledged) == 100, len(traffic.acknowledged)
            assert len(traffic.accepts) == 10
            for index, user in enumerate(USERS):
                own = restarted.registered_sheet(user)
                rows = own.read("'События'!A2:O")
                expected = [f"Факт {j}" for j in range(index, 100, 30)]
                assert Counter(row[2] for row in rows) == Counter(expected), user
                inbox = own.read("'Входящие'!A2:H")
                assert [row[0] for row in inbox] == [f"tg-{user}-{n+1}" for n in range(len(expected))]
                assert all(row[8].startswith(f"tg-{user}-") for row in rows)
                assert len({row[11] for row in rows}) == len(rows)
            assert all(attempts[key] == 1 for key in affected)
            assert attempts["tg-106-1"] == 2
            assert sum(attempts.values()) == 101 and model.plan.call_count == 100

        print(json.dumps({"profiles":30, "bursts":10, "events":100, "pending_before":pending,
                          "completed_before":ack_before, "receipt_commit_p95_ms":quantile(traffic.accepts, .95),
                          "ack_p95_ms":quantile(traffic.acknowledged.values(), .95),
                          "ack_before_restart_p95_ms":quantile(normal_delays, .95),
                          "simulated_sheets_call_p95_ms":quantile(sheets_timings, .95),
                          "simulated_ai_call_p95_ms":quantile(ai_timings, .95),
                          "recovery_local_ms":recovery_ms, "recovery_poll_timeout_seconds":traffic.waits,
                          "sheet_append_attempts":sum(attempts.values()), "model_calls":model.plan.call_count}))

if __name__ == "__main__":
    logging.disable(logging.WARNING)
    execute()
