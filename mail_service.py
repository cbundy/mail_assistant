"""IMAP operations. Workers never call Streamlit; progress uses a callback."""

import email
import imaplib
import json
import re
import ssl
import threading
from collections import defaultdict
from contextlib import contextmanager
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import urlparse
import requests
from mail_helpers import (
    decode_header_text,
    parse_unsubscribe,
    classify_subject,
    strict_brand_key,
    pretty_brand_name,
    is_safe_public_http_url,
)


class MailError(Exception):
    pass


_LOCKS = defaultdict(threading.Lock)


@contextmanager
def account_lock(account):
    lock = _LOCKS[account.strip().lower()]
    if not lock.acquire(blocking=False):
        raise MailError("busy")
    try:
        yield
    finally:
        lock.release()


def quote(value):
    if any(c in str(value) for c in "\r\n\x00"):
        raise MailError("invalid_input")
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def checked(result):
    if result[0] != "OK":
        raise MailError("server_command")
    return result[1]


@contextmanager
def connection(addr, password):
    m = imaplib.IMAP4_SSL(
        "imap.mail.me.com", 993, ssl_context=ssl.create_default_context(), timeout=30
    )
    try:
        m.login(addr.strip(), password.strip())
        # Refresh capabilities after authentication (some servers advertise MOVE only then).
        caps = checked(m.capability())
        m.capabilities = b" ".join(caps).upper().split()
        yield m
    finally:
        try:
            m.logout()
        except Exception:
            pass


def select(m, folder="INBOX", readonly=True):
    checked(m.select(quote(folder), readonly=readonly))
    _, values = m.response("UIDVALIDITY")
    if not values or not values[0]:
        raise MailError("missing_validity")
    return values[0].decode() if isinstance(values[0], bytes) else str(values[0])


def search(m, *criteria):
    d = checked(m.uid("SEARCH", None, *criteria))
    return [u.decode() for u in (d[0] or b"").split()]


def fetch(m, uids, progress=lambda *a: None):
    messages = []
    for start in range(0, len(uids), 100):
        batch = uids[start : start + 100]
        rows = checked(
            m.uid(
                "FETCH",
                ",".join(batch),
                "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST)])",
            )
        )
        for row in rows:
            if not isinstance(row, tuple):
                continue
            meta, raw = row[:2]
            uid = re.search(rb"\bUID (\d+)", meta)
            if not uid:
                raise MailError("missing_uid")
            msg = email.message_from_bytes(raw)
            name, sender = parseaddr(msg.get("From", ""))
            subject = decode_header_text(msg.get("Subject", ""))
            dt = re.search(rb'INTERNALDATE "([^"]+)"', meta)
            try:
                received = (
                    parsedate_to_datetime(dt[1].decode()).timestamp() if dt else 0
                )
            except (ValueError, TypeError, OverflowError):
                received = 0
            urls = parse_unsubscribe(msg.get("List-Unsubscribe", ""))
            messages.append(
                dict(
                    uid=uid[1].decode(),
                    sender=sender.lower().strip(),
                    name=decode_header_text(name),
                    subject=subject,
                    date=decode_header_text(msg.get("Date", "")),
                    received=received,
                    kind=classify_subject(subject),
                    message_id=msg.get("Message-ID", ""),
                    list_id=msg.get("List-ID", ""),
                    urls=urls,
                    one_click="list-unsubscribe=one-click"
                    in msg.get("List-Unsubscribe-Post", "").lower(),
                )
            )
        progress("scan", min(start + len(batch), len(uids)), len(uids))
    return messages


def scan(store, password, limit, progress):
    with account_lock(store.account), connection(store.account, password) as m:
        progress("connect", 0, 0)
        validity = select(m)
        uids = search(m, "ALL")
        total = len(uids)
        selected = uids[-limit:] if limit else uids
        messages = fetch(m, selected, progress)
        progress("analyse", 0, 0)
        store.save_scan(validity, total, messages)
        return dict(scanned=len(messages), total=total)


def companies(store):
    scan = store.scan()
    if not scan:
        return []
    rules = store.rules()
    subs = store.subscriptions()
    after = store.after_unsubscribe()
    deleted = store.recent_deleted()
    groups = {}
    for msg in store.directory():
        sender = msg["sender"]
        rule = rules.get(sender, {})
        key = rule.get("group_id") or strict_brand_key(msg["name"], sender)
        g = groups.setdefault(
            key,
            dict(
                key=key,
                name=rule.get("group_name") or "",
                messages=[],
                senders={},
                latest=0,
            ),
        )
        g["senders"][sender] = msg["name"]
        g["latest"] = max(g["latest"], msg["latest"])
    for msg in scan["messages"]:
        sender = msg["sender"]
        rule = rules.get(sender, {})
        key = rule.get("group_id") or strict_brand_key(msg["name"], sender)
        g = groups.setdefault(
            key,
            dict(
                key=key,
                name=rule.get("group_name") or "",
                messages=[],
                senders={},
                latest=0,
            ),
        )
        g["messages"].append(msg)
        g["senders"][sender] = msg["name"]
        g["latest"] = max(g["latest"], msg["received"])
    for g in groups.values():
        if not g["name"]:
            g["name"] = pretty_brand_name(
                g["key"], [{"Отправитель": n} for n in g["senders"].values()]
            )
        g["protected"] = any(
            rules.get(s, {}).get("policy") == "white" for s in g["senders"]
        )
        g["black"] = any(
            rules.get(s, {}).get("policy") == "black" for s in g["senders"]
        )
        g["after"] = sum(after.get(s, 0) for s in g["senders"])
        g["recent"] = sum(deleted.get(s, 0) for s in g["senders"])
        statuses = {subs.get(s, {}).get("status", "active") for s in g["senders"]}
        g["status"] = (
            "white"
            if g["protected"]
            else (
                "black"
                if g["black"]
                else next(iter(statuses)) if len(statuses) == 1 else "mixed"
            )
        )
    return list(groups.values())


def unsubscribe_targets(messages):
    """Keep separate List-IDs; use one-click URL and its flag from the same message."""
    targets = {}
    for msg in sorted(messages, key=lambda x: (x["received"], int(x["uid"]))):
        if msg["urls"]:
            key = (msg["sender"], msg["list_id"] or "")
            targets[key] = msg
    return list(targets.values())


def prepare(store, password, keys, mode, scope, allow_white, progress):
    chosen = [g for g in companies(store) if g["key"] in keys]
    if not chosen:
        raise MailError("empty")
    senders = {s for g in chosen for s in g["senders"]}
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    if senders & white and not allow_white:
        raise MailError("protected")
    with account_lock(store.account), connection(store.account, password) as m:
        progress("connect", 0, 0)
        validity = select(m)
        uids = set()
        for i, sender in enumerate(sorted(senders)):
            if sender:
                uids.update(search(m, "HEADER", "FROM", quote(sender)))
            progress("prepare", i + 1, len(senders))
        messages = [
            x
            for x in fetch(m, sorted(uids, key=int), progress)
            if x["sender"] in senders
        ]
    targets = [
        x
        for x in messages
        if mode != "unsubscribe_only" and (scope == "all" or x["kind"] == "promo")
    ]
    return dict(
        keys=sorted(keys),
        mode=mode,
        scope=scope,
        allow_white=allow_white,
        validity=validity,
        targets=targets,
        unsubs=unsubscribe_targets(messages) if mode != "delete_only" else [],
        senders=sorted(senders),
        companies=[g["name"] for g in chosen],
    )


def trash_folder(m):
    rows = checked(m.list())
    fallback = None
    for row in rows:
        if not isinstance(row, bytes):
            continue
        # Parse flags, delimiter, and the full mailbox name (including spaces).
        match = re.match(rb'\(([^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (.+)$', row)
        if not match:
            continue
        name = match[2].decode("ascii")
        if name.startswith('"') and name.endswith('"'):
            name = re.sub(r"\\(.)", r"\1", name[1:-1])
        if b"\\trash" in match[1].lower():
            return name
        if name.lower() in {"deleted messages", "trash", "bin", "kosz", "deleted"}:
            fallback = name
    if fallback:
        return fallback
    raise MailError("no_trash")


def copy_mapping(m, uid):
    _, values = m.response("COPYUID")
    for value in values or []:
        if not value:
            continue
        parts = value.decode().split()
        if len(parts) == 3 and parts[1] == str(uid) and parts[2].isdigit():
            return parts[0], parts[2]
    return None, None


def move_one(m, uid, destination, record=lambda *a: None):
    """Never issue broad EXPUNGE. Persist COPYUID before removing the source."""
    caps = {
        c.decode().upper() if isinstance(c, bytes) else c.upper()
        for c in m.capabilities
    }
    m.response("COPYUID")  # discard stale mapping
    if "MOVE" in caps:
        checked(m.uid("MOVE", uid, quote(destination)))
        validity, dest_uid = copy_mapping(m, uid)
        record("moved", validity, dest_uid)
        return validity, dest_uid
    if "UIDPLUS" not in caps:
        raise MailError("unsafe_move")
    checked(m.uid("COPY", uid, quote(destination)))
    validity, dest_uid = copy_mapping(m, uid)
    record("copied", validity, dest_uid)
    if not dest_uid:
        raise MailError("missing_mapping")
    checked(m.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)"))
    checked(m.uid("EXPUNGE", uid))
    record("moved", validity, dest_uid)
    return validity, dest_uid


def one_click(msg):
    if not msg["one_click"]:
        return "manual", ""
    candidates = [u for u in msg["urls"] if urlparse(u).scheme == "https"]
    for url in candidates:
        if not is_safe_public_http_url(url):
            continue
        try:
            # Never automatically follow redirects to unchecked hosts.
            with requests.Session() as session:
                session.trust_env = False
                r = session.post(
                    url,
                    data={"List-Unsubscribe": "One-Click"},
                    timeout=(10, 20),
                    allow_redirects=False,
                )
            if 200 <= r.status_code < 300:
                return "requested", f"HTTP {r.status_code}"
        except requests.RequestException:
            pass
    return "failed", "Automatic request was not accepted; use the manual link."


def execute(store, password, preview, selected_uids, progress):
    with account_lock(store.account):
        white = {s for s, r in store.rules().items() if r["policy"] == "white"}
        if set(preview["senders"]) & white and not preview["allow_white"]:
            raise MailError("protected")
        targets = [m for m in preview["targets"] if m["uid"] in selected_uids]
        op = store.operation(preview["mode"], {"companies": preview["companies"]})
        result = dict(
            moved=0,
            requested=0,
            manual=0,
            failed=0,
            skipped=0,
            links=[],
            companies=preview["companies"],
            outcomes=[],
        )
        try:
            # Validate mailbox epoch before any side effect, including unsubscribe.
            with connection(store.account, password) as m:
                progress("connect", 0, 0)
                if select(m, readonly=False) != preview["validity"]:
                    raise MailError("stale")
                if preview["mode"] != "delete_only":
                    per_sender = defaultdict(list)
                    for i, item in enumerate(preview["unsubs"]):
                        progress("unsubscribe", i, len(preview["unsubs"]))
                        status, detail = one_click(item)
                        per_sender[item["sender"]].append(status)
                        result["outcomes"].append(
                            {
                                "sender": item["sender"],
                                "action": "unsubscribe",
                                "status": status,
                            }
                        )
                        result[status if status in result else "failed"] += 1
                        if status != "requested":
                            result["links"].extend(
                                (item["sender"], url)
                                for url in item["urls"]
                                if urlparse(url).scheme in ("https", "http", "mailto")
                            )
                        # Persist every accepted request even if a later stream fails.
                        store.unsubscribe(item["sender"], status, detail)
                        progress("unsubscribe", i + 1, len(preview["unsubs"]))
                    for sender, statuses in per_sender.items():
                        if len(set(statuses)) > 1:
                            store.unsubscribe(sender, "partial")
                if targets:
                    destination = trash_folder(m)
                    caps = {
                        x.decode().upper() if isinstance(x, bytes) else x.upper()
                        for x in m.capabilities
                    }
                    if not {"MOVE", "UIDPLUS"} & caps:
                        raise MailError("unsafe_move")
                    for i, item in enumerate(targets):
                        progress("delete", i, len(targets))
                        live = fetch(m, [item["uid"]])
                        if not live:
                            result["skipped"] += 1
                            store.remove_cached(preview["validity"], item["uid"])
                            continue
                        if (
                            live[0]["sender"],
                            live[0]["message_id"],
                            live[0]["subject"],
                        ) != (item["sender"], item["message_id"], item["subject"]):
                            raise MailError("stale")
                        ident = store.prepare_move(
                            op, "INBOX", preview["validity"], item, destination
                        )
                        try:
                            move_one(
                                m,
                                item["uid"],
                                destination,
                                lambda state, v, u: store.move_state(
                                    ident, state, v, u
                                ),
                            )
                        except Exception:
                            # Do not retry an ambiguous network result automatically.
                            with store.connect() as c:
                                c.execute(
                                    "UPDATE moves SET state='uncertain' WHERE id=? AND state='pending'",
                                    (ident,),
                                )
                            raise
                        result["moved"] += 1
                        result["outcomes"].append(
                            {
                                "sender": item["sender"],
                                "action": "delete",
                                "uid": item["uid"],
                                "status": "moved",
                            }
                        )
                        store.remove_cached(preview["validity"], item["uid"])
                        progress("delete", i + 1, len(targets))
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = error_code(exc)
            store.finish(op, "partial", result)
        return result


def undo(store, password, progress):
    with account_lock(store.account):
        batch = store.last_moves()
        eligible = [
            r
            for r in batch
            if r["state"] == "moved" and r["dest_uid"] and r["dest_validity"]
        ]
        if not eligible:
            raise MailError("undo_unavailable")
        op = store.operation("undo", {"count": len(eligible)})
        result = dict(restored=0, failed=0, skipped=0)
        try:
            with connection(store.account, password) as m:
                for i, row in enumerate(eligible):
                    progress("undo", i, len(eligible))
                    if select(m, row["destination"], False) != row["dest_validity"]:
                        raise MailError("stale")
                    live = fetch(m, [row["dest_uid"]])
                    old = json.loads(row["message"])
                    if not live:
                        store.move_state(row["id"], "missing")
                        result["skipped"] += 1
                        continue
                    if (
                        live[0]["sender"],
                        live[0]["message_id"],
                        live[0]["subject"],
                    ) != (old["sender"], old["message_id"], old["subject"]):
                        raise MailError("stale")
                    store.move_state(row["id"], "restoring")
                    move_one(m, row["dest_uid"], row["source"])
                    store.move_state(row["id"], "restored")
                    result["restored"] += 1
                    store.invalidate_scan()
                    progress("undo", i + 1, len(eligible))
            store.finish(op, "done", result)
        except Exception as exc:
            result["error"] = error_code(exc)
            store.finish(op, "partial", result)
        return result


def error_code(exc):
    if isinstance(exc, MailError):
        return str(exc)
    if isinstance(exc, imaplib.IMAP4.error):
        return "imap"
    if isinstance(exc, (TimeoutError, OSError)):
        return "network"
    return "unexpected"
