"""Parsing and conservative grouping retained from the working version."""

import re, socket, ipaddress
from urllib.parse import urlparse
from email.header import decode_header
from collections import defaultdict
import pandas as pd


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
            p.hostname, p.port or (443 if p.scheme == "https" else 80)
        )
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        return True
    except Exception:
        return False


def classify_subject(subject):
    s = (subject or "").lower()
    important = [
        "order",
        "zamów",
        "zamow",
        "delivery",
        "delivered",
        "shipment",
        "wysył",
        "wysyl",
        "dostaw",
        "tracking",
        "parcel",
        "package",
        "confirmation",
        "potwierdzenie",
        "payment",
        "płatność",
        "platnosc",
        "invoice",
        "faktura",
        "receipt",
        "refund",
        "zwrot",
        "return",
        "booking",
        "reservation",
        "rezerw",
        "ticket",
        "bilet",
        "security",
        "password",
        "login",
        "account",
        "verification",
        "verify",
        "kod",
        "code",
    ]
    promo = [
        "newsletter",
        "sale",
        "promo",
        "promotion",
        "zniż",
        "zniz",
        "discount",
        "offer",
        "oferta",
        "rabatt",
        "deal",
        "wyprzedaż",
        "wyprzedaz",
        "black friday",
        "cyber monday",
        "last chance",
        "only today",
        "tylko dziś",
        "tylko dzis",
        "special offer",
        "% off",
        "save ",
    ]
    if any(x in s for x in important):
        return "important"
    if any(x in s for x in promo) or re.search(r"\b\d{1,2}%\b", s):
        return "promo"
    return "other"


# ---------- Company grouping ----------

GENERIC_TOKENS = {
    "newsletter",
    "news",
    "noreply",
    "reply",
    "email",
    "mail",
    "mailer",
    "shop",
    "store",
    "sklep",
    "market",
    "marketing",
    "promo",
    "promocje",
    "offers",
    "offer",
    "service",
    "support",
    "customer",
    "customers",
    "account",
    "team",
    "club",
    "app",
    "info",
    "kontakt",
    "contact",
    "notifications",
    "notification",
    "message",
    "messages",
    "official",
    "polska",
    "poland",
    "online",
    "centrum",
    "center",
    "reklama",
    "promotions",
    "orders",
    "order",
    "delivery",
    "transactional",
    "system",
    "id",
    "konto",
    "konto",
    "subskrypcja",
    "subskrypcje",
    "campaign",
    "campaigns",
    "mailing",
    "mailings",
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
        ("co", "uk"),
        ("com", "pl"),
        ("com", "ua"),
        ("com", "au"),
        ("com", "de"),
        ("com", "fr"),
        ("com", "it"),
        ("com", "es"),
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
        for token in re.findall(
            r"[0-9A-Za-zĄĆĘŁŃÓŚŹŻąćęłńóśźżА-Яа-яЁёІіЇїЄєҐґ]+", display
        ):
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
            str(item.get("Отправитель", "") or ""), str(item.get("Email", "") or "")
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

        rows.append(
            {
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
            }
        )

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values(["Писем", "Компания"], ascending=[False, True])
    return result
