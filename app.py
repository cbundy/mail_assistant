
import streamlit as st
import imaplib
import email
from email.header import decode_header
from email.utils import parseaddr
from collections import defaultdict, Counter
from urllib.parse import urlparse
from datetime import datetime
from pathlib import Path
import pandas as pd
import requests
import re
import socket
import ipaddress
import json

st.set_page_config(page_title="iCloud Mail Assistant", page_icon="📬", layout="wide")

CACHE_FILE = Path(__file__).with_name("mail_cache.json")
LOG_FILE = Path(__file__).with_name("unsubscribe_log.json")

# ---------- UI ----------
LANG = st.sidebar.selectbox("Language / Язык", ["Русский", "English"], index=0)
RU = LANG == "Русский"

def T(ru, en):
    return ru if RU else en

st.markdown("""
<style>
html, body, .stApp, p, span, label, div, input, textarea, button,
div[data-testid="stMarkdownContainer"],
div[data-testid="stMetricValue"],
div[data-testid="stMetricLabel"],
div[data-testid="stSelectbox"],
div[data-testid="stRadio"],
div[data-testid="stCheckbox"] {
    font-size: 15px !important;
}
h1, h2, h3, h4 {
    font-size: 15px !important;
    font-weight: 700 !important;
    line-height: 1.35 !important;
}
div.stButton > button {
    min-height: 3.2rem;
    font-size: 15px !important;
    font-weight: 650 !important;
    border-radius: 12px;
}
div[data-testid="stRadio"] label,
div[data-testid="stCheckbox"] label,
div[data-testid="stSelectbox"] label {
    font-size: 15px !important;
}
</style>
""", unsafe_allow_html=True)

st.title("📬 iCloud Mail Assistant")
st.caption(T(
    "Строгая группировка • 3 режима • выбор конкретных писем",
    "Strict grouping • 3 action modes • per-message selection"
))

# ---------- Parsing helpers ----------

def decode_header_text(value):
    if not value:
        return ""
    out = []
    for part, enc in decode_header(value):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "utf-8", errors="replace"))
            except (LookupError, UnicodeDecodeError):
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)

def parse_unsubscribe(value):
    if not value:
        return []
    return re.findall(r"<([^>]+)>", value)

def is_safe_public_http_url(url):
    try:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        if p.hostname.lower() == "localhost":
            return False
        infos = socket.getaddrinfo(
            p.hostname,
            p.port or (443 if p.scheme == "https" else 80)
        )
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False

def connect(addr, password, readonly=True):
    m = imaplib.IMAP4_SSL("imap.mail.me.com", 993)
    m.login(addr.strip(), password.strip())
    m.select("INBOX", readonly=readonly)
    return m

def classify_subject(subject):
    s = (subject or "").lower()
    important = [
        "order", "zamów", "zamow", "delivery", "delivered", "shipment",
        "wysył", "wysyl", "dostaw", "tracking", "parcel", "package",
        "confirmation", "potwierdzenie", "payment", "płatność", "platnosc",
        "invoice", "faktura", "receipt", "refund", "zwrot", "return",
        "booking", "reservation", "rezerw", "ticket", "bilet", "security",
        "password", "login", "account", "verification", "verify", "kod", "code"
    ]
    promo = [
        "newsletter", "sale", "promo", "promotion", "zniż", "zniz", "discount",
        "offer", "oferta", "rabatt", "deal", "wyprzedaż", "wyprzedaz",
        "black friday", "cyber monday", "last chance", "only today",
        "tylko dziś", "tylko dzis", "special offer", "% off", "save "
    ]
    if any(x in s for x in important):
        return "important"
    if any(x in s for x in promo) or re.search(r"\b\d{1,2}%\b", s):
        return "promo"
    return "other"

# ---------- Company grouping ----------

GENERIC_TOKENS = {
    "newsletter", "news", "noreply", "reply", "email", "mail", "mailer",
    "shop", "store", "sklep", "market", "marketing", "promo", "promocje",
    "offers", "offer", "service", "support", "customer", "customers",
    "account", "team", "club", "app", "info", "kontakt", "contact",
    "notifications", "notification", "message", "messages", "official",
    "polska", "poland", "online", "centrum", "center", "reklama",
    "promotions", "orders", "order", "delivery", "transactional", "system",
    "id", "konto", "konto", "subskrypcja", "subskrypcje", "campaign",
    "campaigns", "mailing", "mailings"
}

def clean_tokens(text):
    text = (text or "").lower()
    text = re.sub(r"[^0-9a-ząćęłńóśźżа-яёіїєґ]+", " ", text, flags=re.I)
    return [t for t in text.split() if len(t) >= 3 and t not in GENERIC_TOKENS]

def registered_domain_root(address):
    if "@" not in (address or ""):
        return ""
    domain = address.split("@", 1)[1].lower().strip(".")
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return ""
    # Good-enough public-suffix handling for common patterns such as co.uk/com.pl.
    country_slds = {
        ("co", "uk"), ("com", "pl"), ("com", "ua"), ("com", "au"),
        ("com", "de"), ("com", "fr"), ("com", "it"), ("com", "es"),
    }
    if len(parts) >= 3 and (parts[-2], parts[-1]) in country_slds:
        root = parts[-3]
    elif len(parts) >= 2:
        root = parts[-2]
    else:
        root = parts[0]
    return root if len(root) >= 3 and root not in GENERIC_TOKENS else ""

def strict_brand_key(display_name, email_addr):
    """
    Conservative grouping:
    1) If the registered-domain root appears in the sender display name,
       use that domain root as the company key.
    2) Otherwise use the first meaningful token of the display name.
    3) If there is no meaningful display token, fall back to the domain root.
    This intentionally prefers under-grouping to accidental mega-groups.
    """
    display_tokens = clean_tokens(display_name)
    root = registered_domain_root(email_addr)

    if root and root in display_tokens:
        return root

    if display_tokens:
        return display_tokens[0]

    if root:
        return root

    return (email_addr or display_name or "unknown").lower()

def pretty_brand_name(key, senders):
    # Prefer the exact token casing as shown in a sender display name.
    for s in senders:
        display = str(s.get("Отправитель", "") or "")
        for token in re.findall(r"[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźżА-Яа-яЁёІіЇїЄєҐґ]+", display):
            if token.lower() == key.lower():
                return token
    if key:
        return key[:1].upper() + key[1:]
    return "Без названия"

def build_company_groups(df):
    if df is None or df.empty:
        return pd.DataFrame()

    groups = defaultdict(list)

    # First pass: compute a strict key per sender.
    for _, row in df.iterrows():
        item = row.to_dict()
        key = strict_brand_key(
            str(item.get("Отправитель", "") or ""),
            str(item.get("Email", "") or "")
        )
        groups[key].append(item)

    rows = []
    for key, senders in groups.items():
        pretty = pretty_brand_name(key, senders)

        total = sum(int(s.get("Писем", 0) or 0) for s in senders)
        promo = sum(int(s.get("Реклама ≈", 0) or 0) for s in senders)
        important = sum(int(s.get("Важное ≈", 0) or 0) for s in senders)
        other = sum(int(s.get("Другое ≈", 0) or 0) for s in senders)

        unsub_types = [s.get("Отписка", "—") for s in senders]
        if any("One-click" in x for x in unsub_types):
            unsub = "✅ One-click"
        elif any("Ссылка" in x for x in unsub_types):
            unsub = "🔗 Ссылка"
        elif any("Email" in x for x in unsub_types):
            unsub = "✉️ Email"
        else:
            unsub = "—"

        sender_names = []
        for s in senders:
            label = f"{s.get('Отправитель')} <{s.get('Email')}>"
            if label not in sender_names:
                sender_names.append(label)

        rows.append({
            "Компания": pretty,
            "Писем": total,
            "Отправителей": len(senders),
            "Реклама ≈": promo,
            "Важное ≈": important,
            "Другое ≈": other,
            "Отписка": unsub,
            "Варианты отправителя": " · ".join(sender_names[:4]),
            "_senders": senders,
            "_key": key,
        })

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(["Писем", "Компания"], ascending=[False, True])
    return result

# ---------- Scan/cache ----------

def batch_uid_fetch(mail, uids, header_fields, batch_size=120, progress_label=None):
    msgs = []
    total = len(uids)
    if total == 0:
        return msgs
    prog = st.progress(0, text=progress_label) if progress_label else None
    for start in range(0, total, batch_size):
        batch = uids[start:start + batch_size]
        uid_set = ",".join(batch)
        status, data = mail.uid(
            "fetch", uid_set,
            f"(BODY.PEEK[HEADER.FIELDS ({header_fields})])"
        )
        if status != "OK":
            continue
        for item in data:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                try:
                    msgs.append(email.message_from_bytes(item[1]))
                except Exception:
                    pass
        if prog:
            prog.progress(min((start + len(batch)) / total, 1.0), text=progress_label)
    return msgs

def scan_inbox(addr, password, limit):
    mail = connect(addr, password, readonly=True)
    status, data = mail.uid("search", None, "ALL")
    if status != "OK":
        mail.logout()
        raise RuntimeError("Не удалось получить список писем.")
    all_uids = [x.decode() for x in data[0].split()]
    total = len(all_uids)
    uids = all_uids if limit in ("Все", "All") else all_uids[-int(limit):]

    msgs = batch_uid_fetch(
        mail, uids,
        "FROM SUBJECT DATE LIST-UNSUBSCRIBE LIST-UNSUBSCRIBE-POST",
        progress_label="Сканирую заголовки писем…"
    )
    mail.logout()

    grouped = defaultdict(lambda: {
        "count": 0, "name": "", "subjects": [],
        "urls": [], "mailtos": [], "one_click": False,
        "promo": 0, "important": 0, "other": 0
    })

    for msg in msgs:
        name, addr2 = parseaddr(msg.get("From", ""))
        addr2 = (addr2 or "(без адреса)").lower().strip()
        name = decode_header_text(name)
        subj = decode_header_text(msg.get("Subject", ""))

        info = grouped[addr2]
        info["count"] += 1
        if name:
            info["name"] = name
        if subj:
            info["subjects"].append(subj)
            info["subjects"] = info["subjects"][-5:]

        kind = classify_subject(subj)
        info[kind] += 1

        lus = parse_unsubscribe(msg.get("List-Unsubscribe", ""))
        lup = (msg.get("List-Unsubscribe-Post", "") or "").lower()
        if "list-unsubscribe=one-click" in lup:
            info["one_click"] = True

        for item in lus:
            low = item.lower()
            if low.startswith("mailto:"):
                if item in info["mailtos"]:
                    info["mailtos"].remove(item)
                info["mailtos"].insert(0, item)
                info["mailtos"] = info["mailtos"][:5]
            elif low.startswith(("http://", "https://")):
                if item in info["urls"]:
                    info["urls"].remove(item)
                info["urls"].insert(0, item)
                info["urls"] = info["urls"][:5]

    rows = []
    for addr2, info in grouped.items():
        if info["one_click"] and info["urls"]:
            unsub = "✅ One-click"
        elif info["urls"]:
            unsub = "🔗 Ссылка"
        elif info["mailtos"]:
            unsub = "✉️ Email"
        else:
            unsub = "—"
        rows.append({
            "Писем": info["count"],
            "Отправитель": info["name"] or addr2,
            "Email": addr2,
            "Отписка": unsub,
            "Реклама ≈": info["promo"],
            "Важное ≈": info["important"],
            "Другое ≈": info["other"],
            "Примеры тем": " · ".join(info["subjects"][-3:]),
            "_urls": info["urls"],
            "_mailtos": info["mailtos"],
            "_one_click": info["one_click"],
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["Писем", "Отправитель"], ascending=[False, True])
    return total, len(uids), df

def save_cache(df, total, scanned):
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "scanned": scanned,
        "rows": df.to_dict(orient="records"),
    }
    CACHE_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

def load_cache():
    if not CACHE_FILE.exists():
        return None
    try:
        payload = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        return payload.get("saved_at", ""), payload["total"], payload["scanned"], pd.DataFrame(payload["rows"])
    except Exception:
        return None

# ---------- Unsubscribe ----------

def read_unsub_log():
    if not LOG_FILE.exists():
        return {}
    try:
        return json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def log_unsubscribe(sender, method, result):
    log = read_unsub_log()
    log[sender] = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "method": method,
        "result": result,
    }
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

def try_one_click(urls):
    errors = []
    for url in urls[:3]:
        if not is_safe_public_http_url(url):
            errors.append("небезопасная ссылка")
            continue
        try:
            r = requests.post(
                url,
                data="List-Unsubscribe=One-Click",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "iCloud-Mail-Assistant/2.0",
                },
                timeout=20,
                allow_redirects=True,
            )
            if 200 <= r.status_code < 400:
                return True, f"HTTP {r.status_code}", url
            errors.append(f"HTTP {r.status_code}")
        except Exception as e:
            errors.append(type(e).__name__)
    return False, ", ".join(errors) if errors else "не удалось", None

# ---------- Live deletion ----------

def fetch_sender_candidates(addr, password, sender):
    mail = connect(addr, password, readonly=True)
    status, data = mail.uid("search", None, "HEADER", "FROM", f'"{sender}"')
    if status != "OK":
        mail.logout()
        raise RuntimeError(f"Не удалось найти письма {sender}.")
    uids = [x.decode() for x in data[0].split()]

    pairs = []
    for uid in uids:
        status, d = mail.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if status != "OK":
            continue
        found = None
        for item in d:
            if isinstance(item, tuple) and len(item) >= 2:
                found = email.message_from_bytes(item[1])
                break
        if found:
            pairs.append((uid, found))
    mail.logout()

    out = []
    for uid, msg in pairs:
        _, parsed = parseaddr(msg.get("From", ""))
        parsed = (parsed or "").lower().strip()
        if parsed != sender.lower().strip():
            continue
        subj = decode_header_text(msg.get("Subject", ""))
        out.append({
            "uid": uid,
            "sender": sender,
            "subject": subj,
            "date": msg.get("Date", ""),
            "kind": classify_subject(subj)
        })
    return out

def find_trash_folder(mail):
    status, rows = mail.list()
    if status != "OK":
        return None
    decoded = [r.decode(errors="replace") for r in rows if isinstance(r, (bytes, bytearray))]
    for line in decoded:
        if "\\Trash" in line:
            m = re.search(r' "([^"]+)"$', line)
            if m:
                return m.group(1)
            return line.rsplit(" ", 1)[-1].strip('"')
    for fallback in ["Deleted Messages", "Trash", "Bin", "Kosz", "Deleted"]:
        for line in decoded:
            if fallback.lower() in line.lower():
                m = re.search(r' "([^"]+)"$', line)
                if m:
                    return m.group(1)
                return fallback
    return None

def quote_mailbox(name):
    safe = str(name).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{safe}"'

def move_uids_to_trash(addr, password, uids):
    if not uids:
        return 0, None
    mail = connect(addr, password, readonly=False)
    trash = find_trash_folder(mail)
    if not trash:
        mail.logout()
        raise RuntimeError("Не удалось определить папку Корзины iCloud.")
    trash_arg = quote_mailbox(trash)

    moved = 0
    supports_move = any(
        (cap.decode() if isinstance(cap, bytes) else str(cap)).upper() == "MOVE"
        for cap in getattr(mail, "capabilities", ())
    )

    for uid in uids:
        if supports_move:
            status, _ = mail.uid("MOVE", uid, trash_arg)
            if status == "OK":
                moved += 1
                continue

        status, _ = mail.uid("COPY", uid, trash_arg)
        if status == "OK":
            mail.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
            moved += 1

    if moved and not supports_move:
        mail.expunge()
    mail.logout()
    return moved, trash

# ---------- State/sidebar ----------

for k, default in [
    ("df", None), ("total", None), ("scanned", None),
    ("cache_time", None), ("company_preview", None)
]:
    if k not in st.session_state:
        st.session_state[k] = default

with st.sidebar:
    st.header(T("Подключение", "Connection"))
    icloud_email = st.text_input("iCloud email", placeholder="name@icloud.com")
    app_password = st.text_input(T("Пароль приложения", "App-specific password"), type="password")
    limit = st.selectbox(T("Сколько писем сканировать", "How many emails to scan"), [100, 250, 500, T("Все", "All")], index=3)

    if st.button(T("🔎 Просканировать", "🔎 Scan"), use_container_width=True):
        if not icloud_email or not app_password:
            st.error(T("Введи email и пароль приложения.", "Enter your email and app-specific password."))
        else:
            try:
                with st.spinner(T("Сканирую…", "Scanning…")):
                    total, scanned, df = scan_inbox(icloud_email, app_password, limit)
                st.session_state.df = df
                st.session_state.total = total
                st.session_state.scanned = scanned
                st.session_state.cache_time = datetime.now().isoformat(timespec="seconds")
                st.session_state.company_preview = None
                save_cache(df, total, scanned)
                st.success(T("Готово.", "Done."))
            except Exception as e:
                st.error(str(e))

    if st.button(T("📦 Загрузить прошлое сканирование", "📦 Load previous scan"), use_container_width=True):
        cached = load_cache()
        if cached:
            saved_at, total, scanned, df = cached
            st.session_state.df = df
            st.session_state.total = total
            st.session_state.scanned = scanned
            st.session_state.cache_time = saved_at
            st.session_state.company_preview = None
            st.success(T("Загружено без нового сканирования.", "Loaded without rescanning."))
        else:
            st.info(T("Сохранённого результата пока нет.", "No saved scan yet."))

st.info(T("🔒 Пароль не сохраняется. Группировка компаний строится локально.", "🔒 Your password is not saved. Company grouping is built locally."))

df = st.session_state.df
if df is None or df.empty:
    st.markdown(T(
        """
**Что умеет эта версия**

- строгая группировка компаний;
- три режима: удалить / отписаться / сделать оба действия;
- все письма автоматически отмечены для удаления, но любую галочку можно снять;
- единый размер интерфейса 15 px;
- переключатель Русский / English;
- прошлое сканирование можно загрузить без повторного ожидания.
""",
        """
**What this version can do**

- strict company grouping;
- three modes: delete / unsubscribe / do both;
- all emails are selected for deletion by default, but any checkbox can be unticked;
- consistent 15 px interface font;
- Russian / English language switch;
- previous scan can be loaded without waiting again.
"""
    ))
    st.stop()

companies = build_company_groups(df)

c1, c2, c3 = st.columns(3)
c1.metric(T("Писем", "Emails"), st.session_state.total)
c2.metric(T("Отправителей", "Senders"), len(df))
c3.metric(T("Компаний", "Companies"), len(companies))

st.subheader(T("Компании", "Companies"))
st.caption(T(
    "Строгая группировка: лучше недообъединить, чем смешать разные компании.",
    "Strict grouping: it is safer to under-group than to mix unrelated companies."
))

company_table = companies[[
    "Компания", "Писем", "Отправителей", "Отписка",
    "Реклама ≈", "Важное ≈", "Другое ≈", "Варианты отправителя"
]].copy()

if not RU:
    company_table = company_table.rename(columns={
        "Компания": "Company",
        "Писем": "Emails",
        "Отправителей": "Senders",
        "Отписка": "Unsubscribe",
        "Реклама ≈": "Ads ≈",
        "Важное ≈": "Important ≈",
        "Другое ≈": "Other ≈",
        "Варианты отправителя": "Sender variants",
    })

st.dataframe(company_table, use_container_width=True, hide_index=True)

st.divider()
st.subheader(T("🧹 Массовые действия", "🧹 Bulk actions"))

st.caption(T(
    "Отметь галочками одну или несколько компаний. Потом выбери, что сделать со всеми выбранными.",
    "Select one or more companies with checkboxes, then choose what to do with all selected companies."
))

select_table = companies[[
    "Компания", "Писем", "Отправителей", "Отписка",
    "Реклама ≈", "Важное ≈", "Другое ≈"
]].copy()

select_col = T("Выбрать", "Select")
select_table.insert(0, select_col, False)

if not RU:
    select_table = select_table.rename(columns={
        "Компания": "Company",
        "Писем": "Emails",
        "Отправителей": "Senders",
        "Отписка": "Unsubscribe",
        "Реклама ≈": "Ads ≈",
        "Важное ≈": "Important ≈",
        "Другое ≈": "Other ≈",
    })

company_col = T("Компания", "Company")
emails_col = T("Писем", "Emails")
senders_col = T("Отправителей", "Senders")
unsub_col = T("Отписка", "Unsubscribe")
ads_col = T("Реклама ≈", "Ads ≈")
important_col = T("Важное ≈", "Important ≈")
other_col = T("Другое ≈", "Other ≈")

edited_companies = st.data_editor(
    select_table,
    use_container_width=True,
    hide_index=True,
    height=360,
    disabled=[
        company_col, emails_col, senders_col, unsub_col,
        ads_col, important_col, other_col,
    ],
    column_config={
        select_col: st.column_config.CheckboxColumn(
            select_col,
            help=T(
                "Отметь компании, с которыми хочешь сделать действие.",
                "Select the companies you want to act on."
            ),
            default=False,
        )
    },
    key="company_bulk_selector",
)

selected_company_names = edited_companies.loc[
    edited_companies[select_col] == True, company_col
].astype(str).tolist()

selected_company_rows = companies[
    companies["Компания"].astype(str).isin(selected_company_names)
].copy()

st.caption(T(
    f"Выбрано компаний: {len(selected_company_rows)}",
    f"Companies selected: {len(selected_company_rows)}"
))

if not selected_company_rows.empty:
    with st.expander(T("Показать выбранные компании", "Show selected companies")):
        for _, crow in selected_company_rows.iterrows():
            st.write(f"• **{crow['Компания']}** — {crow['Писем']} {T('писем', 'emails')}")

action_options = {
    T("🗑️ Только удалить старые письма — НЕ отписываться",
      "🗑️ Delete old emails only — DO NOT unsubscribe"): "delete_only",
    T("🚫 Только отписаться — ничего не удалять",
      "🚫 Unsubscribe only — do not delete"): "unsubscribe_only",
    T("🚫 + 🗑️ Отписаться и удалить старые письма",
      "🚫 + 🗑️ Unsubscribe and delete old emails"): "both",
}
action_label = st.radio(
    T("Что сделать с выбранными компаниями?", "What should be done with the selected companies?"),
    list(action_options.keys()),
    index=2
)
action_mode = action_options[action_label]

delete_scope = None
if action_mode != "unsubscribe_only":
    scope_options = {
        T("Только письма, похожие на рекламу", "Only emails that look like advertising"): "promo",
        T("Все письма выбранных компаний", "All emails from selected companies"): "all",
    }
    delete_scope_label = st.radio(
        T("Какие старые письма убрать?", "Which old emails should be removed?"),
        list(scope_options.keys()),
        index=0
    )
    delete_scope = scope_options[delete_scope_label]

if st.button(
    T("👀 Подготовить массовое действие", "👀 Prepare bulk action"),
    use_container_width=True,
    disabled=selected_company_rows.empty
):
    if not icloud_email or not app_password:
        st.error(T(
            "Для живого действия введи email и пароль приложения слева.",
            "For live actions, enter your email and app-specific password on the left."
        ))
    else:
        try:
            all_senders = []
            all_items = []

            for _, crow in selected_company_rows.iterrows():
                company_senders = crow["_senders"]
                all_senders.extend(company_senders)
                for s in company_senders:
                    all_items.extend(
                        fetch_sender_candidates(
                            icloud_email,
                            app_password,
                            s["Email"]
                        )
                    )

            unique_senders = {}
            for s in all_senders:
                unique_senders[s["Email"]] = s
            all_senders = list(unique_senders.values())

            unique_items = {}
            for item in all_items:
                unique_items[str(item["uid"])] = item
            all_items = list(unique_items.values())

            do_unsubscribe = action_mode != "delete_only"
            do_delete = action_mode != "unsubscribe_only"

            if do_delete and delete_scope == "promo":
                targets = [x for x in all_items if x["kind"] == "promo"]
            elif do_delete and delete_scope == "all":
                targets = all_items
            else:
                targets = []

            st.session_state.company_preview = {
                "company_keys": sorted(selected_company_rows["_key"].astype(str).tolist()),
                "company_names": selected_company_rows["Компания"].astype(str).tolist(),
                "action_mode": action_mode,
                "delete_scope": delete_scope,
                "targets": targets,
                "senders": all_senders,
                "do_unsubscribe": do_unsubscribe,
                "do_delete": do_delete,
            }
        except Exception as e:
            st.error(str(e))

preview = st.session_state.company_preview
current_company_keys = sorted(
    selected_company_rows["_key"].astype(str).tolist()
) if not selected_company_rows.empty else []

if (
    preview
    and preview.get("company_keys") == current_company_keys
    and preview.get("action_mode") == action_mode
    and preview.get("delete_scope") == delete_scope
):
    targets = preview["targets"]
    senders = preview["senders"]
    selected_targets = targets

    one_click_count = 0
    manual_count = 0
    no_unsub_count = 0

    for s in senders:
        urls = s.get("_urls", []) if isinstance(s.get("_urls", []), list) else []
        mailtos = s.get("_mailtos", []) if isinstance(s.get("_mailtos", []), list) else []
        if bool(s.get("_one_click")) and urls:
            one_click_count += 1
        elif urls or mailtos:
            manual_count += 1
        else:
            no_unsub_count += 1

    st.markdown(T("**Что произойдёт**", "**What will happen**"))
    st.write(T(
        f"🏢 Выбрано компаний: {len(preview.get('company_names', []))}",
        f"🏢 Companies selected: {len(preview.get('company_names', []))}"
    ))

    if preview.get("do_unsubscribe"):
        st.write(T(
            f"🚫 Автоматическая one-click отписка: до {one_click_count} отправителей",
            f"🚫 Automatic one-click unsubscribe: up to {one_click_count} senders"
        ))
        if manual_count:
            st.write(T(
                f"🔗 Для {manual_count} отправителей может понадобиться ручная отписка.",
                f"🔗 {manual_count} senders may require manual unsubscribe."
            ))
        if no_unsub_count:
            st.write(T(
                f"ℹ️ У {no_unsub_count} отправителей стандартная отписка не найдена.",
                f"ℹ️ No standard unsubscribe method was found for {no_unsub_count} senders."
            ))
    else:
        st.write(T("🚫 Отписка: не выполняется", "🚫 Unsubscribe: will not run"))

    if preview.get("do_delete"):
        st.write(T(
            f"🗑️ Найдено кандидатов на удаление: {len(targets)}",
            f"🗑️ Deletion candidates found: {len(targets)}"
        ))
    else:
        st.write(T("🗑️ Удаление: не выполняется", "🗑️ Deletion: will not run"))

    if targets:
        delete_col = T("Удалить", "Delete")
        company_name_col = T("Компания", "Company")
        sender_col = T("Отправитель", "Sender")
        date_col = T("Дата", "Date")
        subject_col = T("Тема", "Subject")
        type_col = T("Тип", "Type")

        sender_to_company = {}
        for _, crow in selected_company_rows.iterrows():
            for s in crow["_senders"]:
                sender_to_company[str(s["Email"]).lower()] = str(crow["Компания"])

        preview_df = pd.DataFrame([
            {
                delete_col: True,
                company_name_col: sender_to_company.get(str(x["sender"]).lower(), ""),
                sender_col: x["sender"],
                date_col: x["date"],
                subject_col: x["subject"],
                type_col: (
                    T("реклама", "advertising") if x["kind"] == "promo"
                    else (T("важное", "important") if x["kind"] == "important" else T("другое", "other"))
                ),
                "_uid": str(x["uid"]),
            }
            for x in targets
        ])

        edited_df = st.data_editor(
            preview_df,
            use_container_width=True,
            hide_index=True,
            height=460,
            disabled=[
                company_name_col, sender_col, date_col,
                subject_col, type_col, "_uid",
            ],
            column_config={
                delete_col: st.column_config.CheckboxColumn(
                    delete_col,
                    help=T(
                        "Сними галочку, если это письмо нужно оставить.",
                        "Untick this box if you want to keep this email."
                    ),
                    default=True,
                ),
                "_uid": None,
            },
            key="bulk_message_selector",
        )

        selected_uids = set(
            edited_df.loc[
                edited_df[delete_col] == True,
                "_uid"
            ].astype(str).tolist()
        )

        selected_targets = [
            x for x in targets if str(x["uid"]) in selected_uids
        ]

        st.caption(T(
            f"Выбрано для удаления: {len(selected_targets)} из {len(targets)}. "
            "Все письма отмечены автоматически — сними галочки с тех, которые хочешь оставить.",
            f"Selected for deletion: {len(selected_targets)} of {len(targets)}. "
            "All emails are selected automatically — untick any messages you want to keep."
        ))

    st.warning(T(
        "Это финальное подтверждение. Действие применится ко всем выбранным компаниям.",
        "This is the final confirmation. The action will apply to all selected companies."
    ))

    action_button_label = {
        "delete_only": T(
            "🗑️ УДАЛИТЬ ВЫБРАННЫЕ ПИСЬМА",
            "🗑️ DELETE SELECTED EMAILS"
        ),
        "unsubscribe_only": T(
            "🚫 ОТПИСАТЬСЯ ОТ ВЫБРАННЫХ КОМПАНИЙ",
            "🚫 UNSUBSCRIBE FROM SELECTED COMPANIES"
        ),
        "both": T(
            "🚫 ОТПИСАТЬСЯ + 🗑️ УДАЛИТЬ ВЫБРАННЫЕ ПИСЬМА",
            "🚫 UNSUBSCRIBE + 🗑️ DELETE SELECTED EMAILS"
        ),
    }[action_mode]

    if st.button(
        action_button_label,
        type="primary",
        use_container_width=True
    ):
        if not icloud_email or not app_password:
            st.error(T(
                "Введи email и пароль приложения слева.",
                "Enter your email and app-specific password on the left."
            ))
        else:
            unsub_ok = 0
            unsub_failed = []
            manual_links = []
            moved = 0
            trash = None

            with st.spinner(T(
                "Выполняю массовое действие…",
                "Running bulk action…"
            )):
                if preview.get("do_unsubscribe"):
                    for s in senders:
                        sender_email = s["Email"]
                        urls = s.get("_urls", []) if isinstance(s.get("_urls", []), list) else []
                        mailtos = s.get("_mailtos", []) if isinstance(s.get("_mailtos", []), list) else []
                        one_click = bool(s.get("_one_click"))

                        if one_click and urls:
                            ok, result, used = try_one_click(urls)
                            log_unsubscribe(sender_email, "one-click", result)
                            if ok:
                                unsub_ok += 1
                            else:
                                unsub_failed.append((sender_email, result))
                                if urls and is_safe_public_http_url(urls[0]):
                                    manual_links.append((sender_email, urls[0]))
                        elif urls:
                            manual_links.append((sender_email, urls[0]))
                        elif mailtos:
                            manual_links.append((sender_email, mailtos[0]))

                if preview.get("do_delete") and selected_targets:
                    moved, trash = move_uids_to_trash(
                        icloud_email,
                        app_password,
                        [x["uid"] for x in selected_targets]
                    )

            result_parts = []

            if preview.get("do_unsubscribe"):
                result_parts.append(T(
                    f"автоматических отписок принято — {unsub_ok}",
                    f"automatic unsubscribe requests accepted — {unsub_ok}"
                ))

            if preview.get("do_delete"):
                result_parts.append(T(
                    f"писем перемещено в Корзину — {moved}",
                    f"emails moved to Trash — {moved}"
                ))

            st.success(T("Готово: ", "Done: ") + "; ".join(result_parts) + ".")

            if preview.get("do_unsubscribe") and unsub_failed:
                st.warning(T(
                    f"Не удалось автоматически отписаться от {len(unsub_failed)} отправителей.",
                    f"Automatic unsubscribe failed for {len(unsub_failed)} senders."
                ))

            if preview.get("do_unsubscribe") and manual_links:
                st.markdown(T(
                    "**Остались ручные отписки**",
                    "**Manual unsubscribes remaining**"
                ))

                seen = set()
                for sender_email, link in manual_links:
                    if (sender_email, link) in seen:
                        continue
                    seen.add((sender_email, link))

                    if (
                        link.lower().startswith(("http://", "https://"))
                        and is_safe_public_http_url(link)
                    ):
                        st.link_button(
                            T(
                                f"🔗 Отписаться вручную: {sender_email}",
                                f"🔗 Unsubscribe manually: {sender_email}"
                            ),
                            link,
                            use_container_width=True
                        )
                    elif link.lower().startswith("mailto:"):
                        st.markdown(T(
                            f"[✉️ Отписаться письмом: {sender_email}]({link})",
                            f"[✉️ Unsubscribe by email: {sender_email}]({link})"
                        ))

            st.caption(T(
                "Пересканировать почту сразу не нужно. Можно продолжать работать по сохранённому списку.",
                "You do not need to rescan immediately. You can keep working from the saved list."
            ))

st.divider()
st.caption(T(
    "Группировка консервативная: ключ строится из названия отправителя и домена. Перед действием всегда показываются конкретные адреса и письма.",
    "Grouping is conservative: the key is built from the sender name and domain. Specific addresses and emails are always shown before an action."
))
