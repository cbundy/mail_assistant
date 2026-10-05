"""Account-scoped SQLite state. Credentials are never persisted."""

import json
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).with_name("mail_assistant.sqlite3")


class Store:
    def __init__(self, account="", path=None):
        self.account = account.strip().lower()
        self.path = path or DB_PATH
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS settings(account TEXT, key TEXT, value TEXT, PRIMARY KEY(account,key));
            CREATE TABLE IF NOT EXISTS senders(account TEXT, sender TEXT, policy TEXT DEFAULT '', group_id TEXT, group_name TEXT, PRIMARY KEY(account,sender));
            CREATE TABLE IF NOT EXISTS directory(account TEXT, sender TEXT, name TEXT, latest REAL, PRIMARY KEY(account,sender));
            CREATE TABLE IF NOT EXISTS scans(account TEXT PRIMARY KEY, stamp REAL, validity TEXT, total INTEGER, messages TEXT);
            CREATE TABLE IF NOT EXISTS seen(account TEXT, validity TEXT, uid TEXT, sender TEXT, received REAL, PRIMARY KEY(account,validity,uid));
            CREATE TABLE IF NOT EXISTS unsub(account TEXT, sender TEXT, status TEXT, stamp REAL, accepted REAL, detail TEXT, PRIMARY KEY(account,sender));
            CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, account TEXT, kind TEXT, stamp REAL, status TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS moves(id INTEGER PRIMARY KEY, operation TEXT, account TEXT, source TEXT, validity TEXT, uid TEXT, destination TEXT, dest_validity TEXT, dest_uid TEXT, sender TEXT, message TEXT, state TEXT);
            CREATE INDEX IF NOT EXISTS moves_account ON moves(account,operation);
            CREATE INDEX IF NOT EXISTS seen_sender ON seen(account,sender,received);
            """)

    @contextmanager
    def connect(self):
        c = sqlite3.connect(self.path, timeout=20)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            with c:
                yield c
        finally:
            c.close()

    def get(self, key, default=None):
        with self.connect() as c:
            row = c.execute(
                "SELECT value FROM settings WHERE account=? AND key=?",
                (self.account, key),
            ).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO settings VALUES(?,?,?)",
                (self.account, key, json.dumps(value)),
            )

    def directory(self):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM directory WHERE account=?", (self.account,)
                )
            ]

    def rules(self):
        with self.connect() as c:
            return {
                r["sender"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM senders WHERE account=?", (self.account,)
                )
            }

    def policy(self, senders, policy):
        with self.connect() as c:
            for sender in senders:
                c.execute(
                    "INSERT INTO senders(account,sender,policy) VALUES(?,?,?) ON CONFLICT(account,sender) DO UPDATE SET policy=excluded.policy",
                    (self.account, sender, policy),
                )

    def group(self, senders, name=None, split=False, reset=False):
        group_id = "manual:" + uuid.uuid4().hex
        with self.connect() as c:
            for sender in senders:
                gid = None if reset else ("sender:" + sender if split else group_id)
                label = None if reset else (sender if split else name)
                c.execute(
                    "INSERT INTO senders(account,sender,group_id,group_name) VALUES(?,?,?,?) ON CONFLICT(account,sender) DO UPDATE SET group_id=excluded.group_id,group_name=excluded.group_name",
                    (self.account, sender, gid, label),
                )

    def save_scan(self, validity, total, messages):
        with self.connect() as c:
            c.execute(
                "INSERT OR REPLACE INTO scans VALUES(?,?,?,?,?)",
                (self.account, time.time(), validity, total, json.dumps(messages)),
            )
            c.executemany(
                "INSERT OR IGNORE INTO seen VALUES(?,?,?,?,?)",
                [
                    (self.account, validity, m["uid"], m["sender"], m["received"])
                    for m in messages
                ],
            )
            for m in sorted(messages, key=lambda x: x["received"]):
                c.execute(
                    "INSERT INTO directory VALUES(?,?,?,?) ON CONFLICT(account,sender) DO UPDATE SET name=CASE WHEN excluded.latest>=directory.latest THEN excluded.name ELSE directory.name END,latest=MAX(directory.latest,excluded.latest)",
                    (self.account, m["sender"], m["name"], m["received"]),
                )

    def scan(self):
        with self.connect() as c:
            r = c.execute(
                "SELECT * FROM scans WHERE account=?", (self.account,)
            ).fetchone()
        if not r:
            return None
        r = dict(r)
        r["messages"] = json.loads(r["messages"])
        return r

    def remove_cached(self, validity, uid):
        scan = self.scan()
        if scan and scan["validity"] == validity:
            msgs = [m for m in scan["messages"] if m["uid"] != uid]
            if len(msgs) != len(scan["messages"]):
                with self.connect() as c:
                    c.execute(
                        "UPDATE scans SET messages=?,total=MAX(0,total-1) WHERE account=?",
                        (json.dumps(msgs), self.account),
                    )

    def invalidate_scan(self):
        with self.connect() as c:
            c.execute("DELETE FROM scans WHERE account=?", (self.account,))

    def unsubscribe(self, sender, status, detail=""):
        now = time.time()
        with self.connect() as c:
            c.execute(
                "INSERT INTO unsub VALUES(?,?,?,?,?,?) ON CONFLICT(account,sender) DO UPDATE SET status=excluded.status,stamp=excluded.stamp,accepted=CASE WHEN excluded.accepted IS NOT NULL THEN excluded.accepted ELSE unsub.accepted END,detail=excluded.detail",
                (
                    self.account,
                    sender,
                    status,
                    now,
                    now if status == "requested" else None,
                    detail,
                ),
            )

    def subscriptions(self):
        with self.connect() as c:
            return {
                r["sender"]: dict(r)
                for r in c.execute(
                    "SELECT * FROM unsub WHERE account=?", (self.account,)
                )
            }

    def after_unsubscribe(self):
        with self.connect() as c:
            return dict(
                c.execute(
                    "SELECT s.sender,COUNT(*) FROM seen s JOIN unsub u ON s.account=u.account AND s.sender=u.sender WHERE s.account=? AND u.accepted IS NOT NULL AND s.received>u.accepted GROUP BY s.sender",
                    (self.account,),
                ).fetchall()
            )

    def operation(self, kind, detail):
        ident = uuid.uuid4().hex
        with self.connect() as c:
            c.execute(
                "INSERT INTO operations VALUES(?,?,?,?,?,?)",
                (ident, self.account, kind, time.time(), "running", json.dumps(detail)),
            )
        return ident

    def finish(self, ident, status, detail):
        with self.connect() as c:
            c.execute(
                "UPDATE operations SET status=?,detail=? WHERE id=? AND account=?",
                (status, json.dumps(detail), ident, self.account),
            )

    def history(self):
        with self.connect() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM operations WHERE account=? ORDER BY stamp DESC LIMIT 500",
                    (self.account,),
                )
            ]

    def prepare_move(self, op, source, validity, m, destination):
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO moves(operation,account,source,validity,uid,destination,sender,message,state) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    op,
                    self.account,
                    source,
                    validity,
                    m["uid"],
                    destination,
                    m["sender"],
                    json.dumps(m),
                    "pending",
                ),
            )
            return cur.lastrowid

    def move_state(self, ident, state, dest_validity=None, dest_uid=None):
        with self.connect() as c:
            c.execute(
                "UPDATE moves SET state=?,dest_validity=COALESCE(?,dest_validity),dest_uid=COALESCE(?,dest_uid) WHERE id=? AND account=?",
                (state, dest_validity, dest_uid, ident, self.account),
            )

    def last_moves(self):
        with self.connect() as c:
            op = c.execute(
                "SELECT operation FROM moves WHERE account=? AND state IN ('moved','restoring','uncertain','copied') ORDER BY id DESC LIMIT 1",
                (self.account,),
            ).fetchone()
            if not op:
                return []
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM moves WHERE account=? AND operation=?",
                    (self.account, op[0]),
                )
            ]

    def recent_deleted(self):
        with self.connect() as c:
            return dict(
                c.execute(
                    "SELECT m.sender,COUNT(*) FROM moves m JOIN operations o ON m.operation=o.id WHERE m.account=? AND m.state='moved' AND o.stamp>? GROUP BY m.sender",
                    (self.account, time.time() - 30 * 86400),
                ).fetchall()
            )
