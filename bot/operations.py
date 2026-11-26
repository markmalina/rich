# SPDX-FileCopyrightText: 2026 System Rich contributors
# SPDX-License-Identifier: AGPL-3.0-only

"""Local recovery and privacy operations for the pilot, separate from Google journals."""

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .coaching import day_arg, valid_id
from .ledger import Clarification, MOSCOW

TABLES = {"pilot_orders", "school_enrolments", "pilot_kits", "pilot_stock", "pilot_refunds",
          "coach_links", "coach_payments", "event_writes", "privacy_requests"}


def table_exists(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def copy_database(source, destination, *, overwrite=False):
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
        if table_exists(db,"privacy_requests"):
            db.execute("UPDATE privacy_requests SET resolved=? WHERE user=? AND kind='school' "
                       "AND resolved IS NULL AND requested<=?", (day,user,day))


class Operations:
    def __init__(self,db,telegram,owner,directory):
        self.db=db
        self.telegram=telegram
        self.owner=owner
        self.directory=Path(directory)
        self.storage=self.directory/"operations"
        self.storage.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.ledger_path=self.storage/"erasures.sqlite"
        self._last_day=None
        with self.db:
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS privacy_requests(
                    user INTEGER NOT NULL, kind TEXT NOT NULL, requested TEXT NOT NULL,
                    resolved TEXT, PRIMARY KEY(user,kind)
                )
            """)
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
        if day!=self._last_day:
            self._last_day=day
            self.snapshot(day)

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

    def health(self):
        counts={t:self.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in sorted(TABLES) if table_exists(self.db,t)}
        disk=shutil.disk_usage(self.directory)
        info=f"Свободно на томе {disk.free//(1024*1024)} МиБ."
        if disk.free<max(32*1024*1024,disk.total//20):
            info+=" Мало места."
        backups=list(self.storage.glob("rx-*.sqlite"))
        latest=max((p.stem.removeprefix("rx-") for p in backups),default="ещё нет")
        return (f"Копия SQLite: {latest}. {info}\n"
                f"Очередь личных записей: {counts.get('event_writes',0)}. "
                f"Заказы: {counts.get('pilot_orders',0)}. Зачисления: {counts.get('school_enrolments',0)}. "
                f"Движения выдачи: {counts.get('pilot_stock',0)}. Возвраты: {counts.get('pilot_refunds',0)}.")

    def export(self,user):
        record={}
        if table_exists(self.db,"school_work"):
            record["school_work"]=[dict(order_id=i,day=d,text=t,shared=bool(s),submitted=stamp)
                                   for i,d,t,s,stamp in self.db.execute("""
                SELECT order_id,day,text,shared,submitted FROM school_work WHERE learner=? ORDER BY submitted
            """,(user,))]
        if table_exists(self.db,"pilot_orders"):
            record["orders"]=[dict(order_id=i,kind=k,amount_minor=a,currency="RUB",demo=bool(d),state=s)
                              for i,k,a,d,s in self.db.execute("""
                SELECT id,kind,total,demo,state FROM pilot_orders WHERE buyer=? ORDER BY id
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
        copy_database(source,args.destination)
        with sqlite3.connect(args.destination) as restored:
            apply_privacy(restored,ledger)
            if restored.execute("PRAGMA quick_check").fetchone()!=("ok",):
                raise RuntimeError("restore verification failed")
    finally:
        source.close()
        ledger.close()
    print("SQLite: копия восстановлена, журнал удалений применён.")


if __name__=="__main__":
    main()
