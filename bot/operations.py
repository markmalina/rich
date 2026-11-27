# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Local recovery and privacy operations for the pilot, separate from Google journals."""

import argparse
import datetime as dt
import io
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .coaching import day_arg, valid_id
from .ledger import Clarification, MOSCOW
from .providers import ProviderError

TABLES = {"pilot_orders", "school_enrolments", "pilot_kits", "pilot_stock", "pilot_refunds",
          "coach_links", "coach_payments", "deferred_inbox", "event_writes", "privacy_requests"}
ALERT_INTERVAL = dt.timedelta(minutes=1)
WARNING_QUEUE_SECONDS = 15 * 60


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def copy_database(source, destination, *, overwrite=False, deletion_ledger=None):
    destination=Path(destination)
    if not overwrite and destination.exists():
        raise FileExistsError("destination already exists")
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd, tmp=tempfile.mkstemp(dir=destination.parent,prefix=".snapshot-")
    os.close(fd)
    try:
        target=sqlite3.connect(tmp)
        try:
            source.backup(target)
            if deletion_ledger is not None:
                with target:
                    apply_privacy(target,deletion_ledger)
            if target.execute("PRAGMA quick_check").fetchone()!=("ok",):
                raise RuntimeError("SQLite verification failed")
        finally:
            target.close()
        with open(tmp,"rb") as stream:
            os.fsync(stream.fileno())
        if overwrite:
            os.replace(tmp,destination)
        else:
            os.link(tmp,destination)
        directory=os.open(destination.parent,os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(tmp).unlink(missing_ok=True)


def apply_privacy(db, ledger):
    if not table_exists(db,"school_work"):
        return
    for user, day in ledger.execute("SELECT user,recorded FROM erasures"):
        orders="SELECT id FROM pilot_orders WHERE buyer=? AND created<=?"
        db.execute(f"UPDATE school_enrolments SET suspended=1 WHERE order_id IN ({orders})",(user,day))
        db.execute(f"DELETE FROM school_work WHERE order_id IN ({orders})",(user,day))
        db.execute(f"DELETE FROM school_comments WHERE order_id IN ({orders})",(user,day))
        if table_exists(db,"school_activity"):
            db.execute(f"DELETE FROM school_activity WHERE order_id IN ({orders})",(user,day))
        if table_exists(db,"privacy_requests"):
            db.execute("UPDATE privacy_requests SET resolved=? WHERE user=? AND kind='school' "
                       "AND resolved IS NULL AND requested<=?", (day,user,day))


class Operations:
    def __init__(self,db,telegram,owner,directory,*,ai_quota=None,ai_disabled=False,alerts=False):
        self.db=db
        self.telegram=telegram
        self.owner=owner
        self.ai_quota=ai_quota
        self.ai_disabled=ai_disabled
        self.alerts=alerts
        self.last_poll=None
        self._next_alert_check=None
        self.directory=Path(directory)
        self.storage=self.directory/"operations"
        self.storage.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.ledger_path=self.storage/"erasures.sqlite"
        self._last_day=None
        self._next_backup_attempt=None
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS privacy_requests(
                    user INTEGER NOT NULL, kind TEXT NOT NULL, requested TEXT NOT NULL,
                    resolved TEXT, PRIMARY KEY(user,kind)
                )
            """)
            self.db.execute("CREATE TABLE IF NOT EXISTS operational_incidents(code TEXT PRIMARY KEY)")
        fd=os.open(self.ledger_path,os.O_WRONLY|os.O_CREAT,0o600)
        os.close(fd)
        self.ledger=sqlite3.connect(self.ledger_path)
        with self.ledger:
            self.ledger.execute("CREATE TABLE IF NOT EXISTS erasures(user INTEGER PRIMARY KEY, recorded TEXT NOT NULL)")
        with self.db:
            apply_privacy(self.db,self.ledger)

    def snapshot(self, today, *, replace=False):
        filename=self.storage/f"rx-{today}.sqlite"
        if not filename.exists() or replace:
            copy_database(self.db,filename,overwrite=replace)
        return filename

    def run_daily(self,now):
        day=now.astimezone(MOSCOW).date()
        if day==self._last_day or (self._next_backup_attempt and now<self._next_backup_attempt):
            return
        try:
            self.snapshot(day)
        except (OSError, sqlite3.Error, RuntimeError):
            self._next_backup_attempt=now+dt.timedelta(minutes=5)
            raise
        self._last_day=day
        self._next_backup_attempt=None

    def handle(self,update_id,user,message,now):
        cmd,_,arg=message.get("text","").strip().partition(" ")
        valid={"/pilot_health","/pilot_backup","/pilot_network","/pilot_export","/pilot_privacy",
               "/pilot_purge_school","/pilot_privacy_pending"}
        if cmd not in valid:
            return False
        try:
            if cmd=="/pilot_export":
                record=self.export(user)
                data=io.BytesIO(json.dumps(record,ensure_ascii=False,indent=2).encode())
                self.telegram.api("sendDocument",{"chat_id":user},files={"document":("rx-pilot-data.json",data)})
                return True
            if cmd=="/pilot_privacy":
                if arg not in {"school","all"}:
                    raise Clarification("Укажи /pilot_privacy school для учебных работ или /pilot_privacy all для обращения о всех данных.")
                with self.db:
                    self.db.execute("""INSERT INTO privacy_requests VALUES(?,?,?,NULL)
                        ON CONFLICT(user,kind) DO UPDATE SET requested=excluded.requested,resolved=NULL
                        WHERE privacy_requests.resolved IS NOT NULL""",(user,arg,now.isoformat()))
                result=("Запрос получен. Учётные документы сохраняются по отдельной процедуре. "
                        "Оператор определит срок и допустимый объём обработки.")
            else:
                if user!=self.owner:
                    raise Clarification("Нужны права оператора.")
                result=self.admin(cmd,arg,now)
        except Clarification as exc:
            result=str(exc)
        except (OSError,sqlite3.Error,RuntimeError) as exc:
            # Telegram I/O failures should still be retried by the worker.
            if cmd=="/pilot_export":
                raise
            result=f"Операция не подтверждена. Тип ошибки: {type(exc).__name__}."
        self.telegram.message(user,result)
        return True

    def admin(self,cmd,arg,now):
        if cmd=="/pilot_health":
            return self.health()
        if cmd=="/pilot_backup":
            self.snapshot(now.astimezone(MOSCOW).date(),replace=True)
            return "Локальная копия SQLite создана и прошла проверку целостности. Рабочий том и Google проверяются отдельно."
        if cmd=="/pilot_network":
            return self.network(day_arg(arg))
        if cmd=="/pilot_privacy_pending":
            pending=self.db.execute("SELECT COUNT(*) FROM privacy_requests WHERE resolved IS NULL").fetchone()[0]
            return f"Открытых запросов приватности: {pending}. Полные запросы и их сроки разбирает оператор."
        if not valid_id(arg):
            raise Clarification("Укажи ID ученика из его запроса.")
        target=int(arg)
        consent=self.db.execute("SELECT 1 FROM privacy_requests WHERE user=? AND kind='school' AND resolved IS NULL",
                                (target,)).fetchone()
        if not consent:
            raise Clarification("Подтверждённого запроса на удаление учебных работ нет.")
        with self.ledger:
            self.ledger.execute("INSERT OR REPLACE INTO erasures VALUES(?,?)",(target,now.isoformat()))
        with self.db:
            apply_privacy(self.db,self.ledger)
        return "Учебные работы обработаны. В журнале удаления сохранено основание для восстановления; заказы учтены отдельно."

    def network(self,day):
        if not table_exists(self.db,"coach_payments"):
            raise Clarification("Сопровождение пока недоступно.")
        day=str(day)
        people, links, owner = self.db.execute("""
            SELECT COUNT(DISTINCT l.client), COUNT(*),
                   COUNT(DISTINCT CASE WHEN l.tracker=? THEN l.client END)
            FROM coach_payments p JOIN coach_links l ON l.id=p.link
            WHERE p.day=? AND l.demo=0
        """,(self.owner,day)).fetchone()
        return f"{day} МСК. Уникальных платящих {people}. Оплаченных назначений {links}. У владельца {owner}."

    def oldest_queue(self, now):
        stamps = [self.db.execute(f"SELECT MIN(queued_at) FROM {table}").fetchone()[0]
                  for table in ("deferred_inbox", "event_writes") if table_exists(self.db, table)]
        oldest = min((stamp for stamp in stamps if stamp is not None), default=None)
        return max(0,int(now.timestamp())-oldest) if oldest is not None else None

    def check_alerts(self, now):
        if not self.alerts or not self.owner or (self._next_alert_check and now < self._next_alert_check):
            return
        self._next_alert_check = now + ALERT_INTERVAL
        disk = shutil.disk_usage(self.directory)
        age = self.oldest_queue(now)
        flags = {"disk_low": disk.free < max(32*1024*1024, disk.total//20),
                 "queue_stale": age is not None and age >= WARNING_QUEUE_SECONDS}
        previous = {code for (code,) in self.db.execute("SELECT code FROM operational_incidents")}
        active = {code for code, relevant in flags.items() if relevant}
        with self.db:
            self.db.executemany("DELETE FROM operational_incidents WHERE code=?", [(code,) for code in previous-active])
        if new := active-previous:
            try:
                self.telegram.message(self.owner, "RX · эксплуатация. " + ", ".join(sorted(new)) + ". /pilot_health")
            except ProviderError as error:
                logging.warning("Уведомление RX: %s", error)
                return
            with self.db:
                self.db.executemany("INSERT OR IGNORE INTO operational_incidents VALUES (?)", [(code,) for code in new])

    def health(self, now=None):
        now = now or dt.datetime.now(MOSCOW)
        counts={t:self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in sorted(TABLES) if table_exists(self.db,t)}
        disk=shutil.disk_usage(self.directory)
        info=f"Свободно на томе {disk.free//(1024*1024)} МиБ."
        if disk.free<max(32*1024*1024,disk.total//20):
            info+=" Мало места."
        backups=list(self.storage.glob("rx-*.sqlite"))
        latest=max((p.stem.removeprefix("rx-") for p in backups),default="ещё нет")
        age=self.oldest_queue(now)
        oldest=f" Самая старая: {age//60} мин." if age is not None else ""
        last_poll = self.last_poll.astimezone(MOSCOW).strftime("%d.%m %H:%M") if self.last_poll else "ожидается"
        ai = "выключен" if self.ai_disabled else "лимит обращений не задан"
        if self.ai_quota is not None and not self.ai_disabled:
            used,limit=self.ai_quota.status(now)
            ai=f"обращения {used} из {limit} за день МСК"
        return (f"Копия SQLite: {latest}. {info}\n"
                f"Опрос Telegram: {last_poll}. ИИ: {ai}.\n"
                f"Очередь личных записей: {counts.get('event_writes',0)}. "
                f"Ожидают во «Входящих»: {counts.get('deferred_inbox',0)}.{oldest} "
                f"Заказы: {counts.get('pilot_orders',0)}. Зачисления: {counts.get('school_enrolments',0)}. "
                f"Движения выдачи: {counts.get('pilot_stock',0)}. Возвраты: {counts.get('pilot_refunds',0)}.")

    def export(self,user):
        record={}
        if table_exists(self.db,"school_work"):
            record["school_work"]=[dict(order_id=i,day=d,text=t,shared=bool(s),submitted=stamp)
                                   for i,d,t,s,stamp in self.db.execute("""
                SELECT order_id,day,text,shared,submitted FROM school_work WHERE learner=? ORDER BY submitted
            """,(user,))]
        if table_exists(self.db,"school_activity"):
            record["school_activity"]=[dict(order_id=i,active_on=stamp) for i,stamp in self.db.execute("""
                SELECT a.order_id,a.active_on FROM school_activity a
                JOIN school_enrolments e ON e.order_id=a.order_id WHERE e.learner=? ORDER BY a.active_on
            """,(user,))]
        if table_exists(self.db,"pilot_orders"):
            record["orders"]=[dict(order_id=i,kind=k,amount_minor=a,currency="RUB",demo=bool(d),state=s)
                              for i,k,a,d,s in self.db.execute("""
                SELECT id,kind,total,demo,state FROM pilot_orders WHERE buyer=? ORDER BY id
            """,(user,))]
        if table_exists(self.db,"pilot_tracking_days"):
            record["tracking_days"]=[dict(order_id=i,day=d,practice=p,recorded=stamp)
                                     for i,d,p,stamp in self.db.execute("""
                SELECT t.order_id,t.day,t.practice,t.recorded FROM pilot_tracking_days t
                JOIN pilot_orders o ON o.id=t.order_id WHERE o.buyer=? ORDER BY t.day,t.practice
            """,(user,))]
        if table_exists(self.db,"coach_marks"):
            record["own_marks"]=[dict(day=d,value=v,recorded=stamp) for d,v,stamp in self.db.execute("""
                SELECT day,value,published FROM coach_marks WHERE author=? ORDER BY published
            """,(user,))]
        return record


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source",type=Path)
    parser.add_argument("destination",type=Path)
    parser.add_argument("--deletion-ledger",type=Path,required=True)
    args=parser.parse_args()
    if not args.source.is_file() or not args.deletion_ledger.is_file():
        parser.error("source and deletion ledger must exist")
    source=sqlite3.connect(args.source.resolve().as_uri()+"?mode=ro",uri=True)
    ledger=sqlite3.connect(args.deletion_ledger.resolve().as_uri()+"?mode=ro",uri=True)
    try:
        copy_database(source,args.destination,deletion_ledger=ledger)
    finally:
        source.close()
        ledger.close()
    print("SQLite: копия восстановлена, журнал удалений применён.")


if __name__=="__main__":
    main()
