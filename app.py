"""iCloud Mail Assistant — local desktop UI, Russian / English."""

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import pandas as pd
import streamlit as st
from storage import Store
import mail_service as service

st.set_page_config(page_title="iCloud Mail Assistant", page_icon="📬", layout="wide")
VERSION = "3.0.0"
ss = st.session_state
preferences = Store()
if "language" not in ss:
    ss.language = preferences.get("language", "Русский")

LANGUAGE = ss.language


def T(ru, en):
    return ru if LANGUAGE == "Русский" else en


def date(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


STATUS = {
    "active": ("Активная рассылка / отправитель", "Active mailing / sender"),
    "requested": ("Запрос на отписку принят", "Unsubscribe request accepted"),
    "manual": ("Ручная отписка", "Manual unsubscribe"),
    "failed": ("Ошибка отписки", "Unsubscribe failed"),
    "partial": ("Часть рассылок требует действия", "Some mailings need action"),
    "mixed": ("Разные статусы отправителей", "Mixed sender statuses"),
    "white": ("Белый список", "Whitelist"),
    "black": ("Чёрный список", "Blacklist"),
}
ERRORS = {
    "busy": (
        "Для этого аккаунта уже выполняется действие. Дождись завершения.",
        "An action is already running for this account. Please wait.",
    ),
    "protected": (
        "Выбраны защищённые отправители. Разреши действие явно или убери их из выбора.",
        "Protected senders are selected. Explicitly allow the action or deselect them.",
    ),
    "stale": (
        "Почтовая папка изменилась. Подготовь новый предпросмотр.",
        "The mailbox changed. Prepare a fresh preview.",
    ),
    "network": (
        "Не удалось связаться с iCloud. Проверь интернет. Если действие уже началось, проверь историю перед повтором.",
        "Could not reach iCloud. Check your connection and review history before retrying an action.",
    ),
    "imap": (
        "iCloud отклонил запрос. Проверь адрес и пароль приложения; обычный пароль Apple не подходит.",
        "iCloud rejected the request. Check your email and app-specific password; your regular Apple password will not work.",
    ),
    "no_trash": (
        "Не удалось определить Корзину iCloud.",
        "Could not identify the iCloud Trash mailbox.",
    ),
    "unsafe_move": (
        "Сервер не поддерживает безопасное перемещение выбранных писем. Удаление остановлено.",
        "The server does not support safe targeted moves. Deletion was stopped.",
    ),
    "missing_mapping": (
        "Письмо скопировано, но сервер не вернул его новый идентификатор. Удаление остановлено; проверь Корзину.",
        "The message was copied but its new UID was not returned. Deletion stopped; check Trash.",
    ),
    "undo_unavailable": (
        "Нет писем с надёжно сохранёнными идентификаторами для возврата. Проверь Корзину вручную.",
        "No messages have reliable saved identifiers for Undo. Check Trash manually.",
    ),
    "empty": ("Сначала выбери компании.", "Select companies first."),
    "unexpected": (
        "Не удалось завершить действие. Проверь историю; уже выполненные шаги сохранены.",
        "Could not complete the action. Review history; completed steps were saved.",
    ),
    "server_command": (
        "iCloud не выполнил команду. Проверь историю перед повтором.",
        "iCloud did not complete the command. Review history before retrying.",
    ),
}


def show_error(code):
    st.error(T(*ERRORS.get(code, ERRORS["unexpected"])))
    with st.expander(T("Технические подробности", "Technical details")):
        st.code(code)


st.markdown(
    """<style>
html,body,[data-testid="stApp"],input,textarea,button,label,p {font-family:Arial,sans-serif;font-size:15px!important;}
h1,h2,h3,h4,[data-testid="stMetricValue"] {font-family:Arial,sans-serif;font-size:15px!important;font-weight:700;}
[data-testid="stButton"] button,[data-testid="stLinkButton"] a {min-height:48px;border-radius:10px;font-size:15px!important;}
</style>""",
    unsafe_allow_html=True,
)


class Job:
    def __init__(self, kind, fn, args):
        self.kind = kind
        self.stage = "connect"
        self.current = 0
        self.total = 0
        self.result = None
        self.error = None
        self.done = False
        self.lock = threading.Lock()

        def work():
            try:
                self.result = fn(*args, self.progress)
            except Exception as exc:
                self.error = service.error_code(exc)
            finally:
                self.done = True

        self.thread = threading.Thread(target=work, daemon=True)

    def progress(self, stage, current, total):
        with self.lock:
            self.stage, self.current, self.total = stage, current, total


def start(kind, fn, *args):
    if ss.get("job") and not ss.job.done:
        return
    ss.preview = None
    ss.result = None
    ss.job = Job(kind, fn, args)
    ss.job.thread.start()
    st.rerun()


def authenticate(account, password, progress):
    with service.connection(account, password) as m:
        service.select(m)
    return account


st.title("📬 iCloud Mail Assistant")
if ss.get("job"):
    job = ss.job
    if not job.done:
        st.info(
            T(
                "Выполняю действие. Кнопки будут доступны после завершения.",
                "Working. Controls will be available when the action finishes.",
            )
        )

        @st.fragment(run_every=0.5)
        def progress_view():
            stages = {
                "connect": ("Подключение к iCloud", "Connecting to iCloud"),
                "scan": ("Сканирование заголовков", "Scanning headers"),
                "analyse": ("Анализ отправителей", "Analysing senders"),
                "prepare": (
                    "Поиск писем для предпросмотра",
                    "Finding preview messages",
                ),
                "unsubscribe": (
                    "Отправка запросов на отписку",
                    "Sending unsubscribe requests",
                ),
                "delete": ("Перемещение в Корзину", "Moving to Trash"),
                "undo": ("Возврат писем", "Restoring messages"),
            }
            with job.lock:
                stage, current, total = job.stage, job.current, job.total
            label = T(*stages.get(stage, stages["connect"]))
            st.status(label, state="running", expanded=False)
            if total:
                st.progress(
                    min(current / total, 1.0),
                    text=f"{label} · {current}/{total} · {min(100,round(current/total*100))}%",
                )
            else:
                st.caption(T("Ожидаю ответа сервера…", "Waiting for the server…"))
            if job.done:
                st.rerun(scope="app")

        progress_view()
        st.stop()
    ss.job = None
    if job.error:
        ss.last_error = job.error
    elif job.kind == "login":
        ss.account = job.result
        ss.password = ss.pop("pending_password", "")
        ss.pop("password_input", None)
    elif job.kind == "prepare":
        ss.preview = job.result
        ss.preview_id = uuid.uuid4().hex
    else:
        ss.result = job.result
        ss.result_kind = job.kind
        if job.kind in ("execute", "undo", "scan"):
            ss.selection_version = ss.get("selection_version", 0) + 1
            ss.chosen_companies = []
    ss.pop("pending_password", None)

if ss.get("last_error"):
    show_error(ss.pop("last_error"))

if not ss.get("account"):
    st.selectbox(
        "Language / Язык",
        ["Русский", "English"],
        key="language",
        on_change=lambda: preferences.set("language", ss.language),
    )
    st.subheader(T("Наведи порядок в почте iCloud", "Clean up your iCloud inbox"))
    st.write(
        T(
            "Находи рассылки, выбирай несколько компаний, отписывайся и переноси ненужные письма в Корзину. Перед удалением ты выбираешь конкретные письма.",
            "Find mailings, select multiple companies, unsubscribe and move unwanted messages to Trash. Review individual messages before deleting.",
        )
    )
    st.markdown(
        T("**1. Введи адрес почты iCloud.**", "**1. Enter your iCloud email address.**")
    )
    account = st.text_input(
        T("Email iCloud", "iCloud email"),
        key="email_input",
        placeholder="name@icloud.com",
    )
    st.markdown(
        T("**2. Создай пароль приложения.**", "**2. Create an app-specific password.**")
    )
    st.write(
        T(
            "В аккаунте Apple: «Вход и безопасность» → «Пароли приложений» → создать пароль, например для Mail Assistant. Для этого нужна двухфакторная аутентификация.",
            "In your Apple Account: Sign-In and Security → App-Specific Passwords → generate a password, for example for Mail Assistant. Two-factor authentication is required.",
        )
    )
    st.link_button(
        T("Открыть аккаунт Apple", "Open Apple Account"), "https://account.apple.com/"
    )
    st.markdown(
        T(
            "**3. Вставь пароль приложения и подключись.**",
            "**3. Paste the app-specific password and connect.**",
        )
    )
    password = st.text_input(
        T(
            "Пароль приложения (не обычный пароль Apple)",
            "App-specific password (not your regular Apple password)",
        ),
        type="password",
        key="password_input",
    )
    st.caption(
        T(
            "Пароль используется только в памяти текущей сессии и не записывается на диск. История и заголовки писем хранятся локально.",
            "Your password stays in session memory and is never written to disk. History and message headers are stored locally.",
        )
    )
    if st.button(
        T("Подключиться к iCloud", "Connect to iCloud"),
        type="primary",
        disabled=not (account.strip() and password.strip()),
    ):
        ss.pending_password = password
        start("login", authenticate, account.strip().lower(), password)
    st.stop()

store = Store(ss.account)
with st.sidebar:
    st.caption(ss.account)
    st.selectbox(
        "Language / Язык",
        ["Русский", "English"],
        key="language",
        on_change=lambda: preferences.set("language", ss.language),
    )
    pages = {
        "mail": ("Почта", "Mail"),
        "white": ("Белый список", "Whitelist"),
        "black": ("Чёрный список", "Blacklist"),
        "groups": ("Объединение компаний", "Company groups"),
        "history": ("История", "History"),
        "settings": ("Настройки", "Settings"),
    }
    page = st.radio(T("Меню", "Menu"), list(pages), format_func=lambda x: T(*pages[x]))
    st.caption(f"v{VERSION}")
    if st.button(T("Выйти", "Sign out")):
        lang = ss.language
        for key in list(ss):
            del ss[key]
        ss.language = lang
        st.rerun()

history = store.history()
if history:
    latest = history[0]
    d = json.loads(latest["detail"])
    st.caption(
        T("Последнее действие", "Last action")
        + f": {date(latest['stamp'])} · "
        + T("удалено", "deleted")
        + f" {d.get('moved',0)} · "
        + T("запросов на отписку принято", "unsubscribe requests accepted")
        + f" {d.get('requested',0)} · "
        + T("возвращено", "restored")
        + f" {d.get('restored',0)}"
    )

groups = service.companies(store)
all_senders = sorted({s for g in groups for s in g["senders"]} | set(store.rules()))

if page in ("white", "black"):
    st.subheader(T(*pages[page]))
    st.write(
        T(
            "Белый список защищает от массовых действий; включить такие компании можно только с отдельным разрешением.",
            "Whitelist entries are excluded from bulk selection and require explicit permission.",
        )
        if page == "white"
        else T(
            "Чёрный список помогает быстро выбрать нежелательных отправителей. Ничего не удаляется автоматически.",
            "Blacklist entries help select unwanted senders quickly. Nothing is deleted automatically.",
        )
    )
    chosen = st.multiselect(T("Добавить отправителей", "Add senders"), all_senders)
    company_keys = st.multiselect(
        T("Или целые компании", "Or entire companies"),
        [g["key"] for g in groups],
        format_func=lambda k: next(g["name"] for g in groups if g["key"] == k),
    )
    extra = st.text_input(T("Или введи точный email", "Or enter an exact email"))
    if st.button(T("Добавить", "Add")):
        targets = set(chosen) | {
            s for g in groups if g["key"] in company_keys for s in g["senders"]
        }
        if extra.strip():
            if "@" in extra and not any(c in extra for c in "\r\n ,"):
                targets.add(extra.strip().lower())
            else:
                st.error(
                    T("Введи один полный email.", "Enter one complete email address.")
                )
        if targets:
            store.policy(targets, page)
            ss.preview = None
            st.rerun()
    current = [s for s, r in store.rules().items() if r["policy"] == page]
    remove = st.multiselect(T("Удалить из списка", "Remove from list"), current)
    if st.button(T("Убрать выбранных", "Remove selected"), disabled=not remove):
        store.policy(remove, "")
        ss.preview = None
        st.rerun()
    st.dataframe(pd.DataFrame({"Email": current}), hide_index=True, width="stretch")

elif page == "groups":
    st.subheader(T("Ручная группировка", "Manual grouping"))
    st.caption(
        T(
            "Правила запоминаются для точных адресов отправителей.",
            "Rules are remembered for exact sender addresses.",
        )
    )
    selected = st.multiselect(
        T("Отправители для объединения", "Senders to merge"), all_senders
    )
    name = st.text_input(T("Название компании", "Company name"))
    if st.button(
        T("Объединить выбранных", "Merge selected"),
        disabled=len(selected) < 2 or not name.strip(),
    ):
        store.group(selected, name.strip())
        ss.preview = None
        st.rerun()
    if groups:
        key = st.selectbox(
            T("Компания для разделения", "Company to split"),
            [g["key"] for g in groups],
            format_func=lambda k: next(g["name"] for g in groups if g["key"] == k),
        )
        senders = list(next(g for g in groups if g["key"] == key)["senders"])
        st.write(", ".join(senders))
        split = st.multiselect(
            T("Отделить выбранных отправителей", "Split selected senders"), senders
        )
        if st.button(
            T("Отделить в самостоятельные группы", "Split into individual groups"),
            disabled=not split,
        ):
            store.group(split, split=True)
            ss.preview = None
            st.rerun()
    manual = [s for s, r in store.rules().items() if r["group_id"]]
    reset = st.multiselect(
        T("Вернуть автоматическую группировку", "Restore automatic grouping"), manual
    )
    if st.button(
        T("Сбросить выбранные правила", "Reset selected rules"), disabled=not reset
    ):
        store.group(reset, reset=True)
        ss.preview = None
        st.rerun()

elif page == "history":
    st.subheader(T("История действий", "Action history"))
    rows = []
    for h in history:
        d = json.loads(h["detail"])
        rows.append(
            {
                T("Дата", "Date"): date(h["stamp"]),
                T("Действие", "Action"): h["kind"],
                T("Статус", "Status"): h["status"],
                T("Компании", "Companies"): ", ".join(d.get("companies", [])),
                T("Удалено", "Deleted"): d.get("moved", 0),
                T("Запросов принято", "Requests accepted"): d.get("requested", 0),
                T("Возвращено", "Restored"): d.get("restored", 0),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(
        T(
            "running после перезапуска означает прерванную операцию: проверь почту перед повтором. partial означает частичное выполнение.",
            "A running entry after restart means an interrupted operation: check your mail before retrying. partial means partial completion.",
        )
    )
    with st.expander(T("Подробности последнего действия", "Last action details")):
        if history:
            st.json(json.loads(history[0]["detail"]))

elif page == "settings":
    st.subheader(T("Настройки", "Settings"))
    st.write(
        T(
            "Язык и сортировка сохраняются автоматически. Пароль не сохраняется.",
            "Language and sorting are saved automatically. Passwords are never saved.",
        )
    )
    st.caption(
        T(
            "Локальная база содержит адреса, темы, ссылки отписки и историю. Не отправляй её друзьям и не загружай в GitHub.",
            "The local database contains addresses, subjects, unsubscribe links and history. Do not share it with friends or upload it to GitHub.",
        )
    )
    legacy = Path(__file__).with_name("mail_cache.json")
    if legacy.exists():
        st.info(
            T(
                "Найден кэш старой версии без привязки к аккаунту и UIDVALIDITY. Для новой версии один раз просканируй почту: это нужно для достоверной истории и отмены удаления. Старый файл остаётся на месте.",
                "An old cache without account identity and UIDVALIDITY was found. Scan once for reliable history and Undo. The old file is preserved.",
            )
        )

else:
    scan = store.scan()
    limits = [500, 1000, 5000, 0]
    default = store.get("limit", 1000)
    limit = st.selectbox(
        T("Сколько последних писем сканировать", "How many recent emails to scan"),
        limits,
        index=limits.index(default) if default in limits else 1,
        format_func=lambda n: str(n) if n else T("Все", "All"),
    )
    store.set("limit", limit)
    cols = st.columns(2)
    if cols[0].button(
        T("Сканировать почту", "Scan inbox"), type="primary", width="stretch"
    ):
        start("scan", service.scan, store, ss.password, limit)
    batch = store.last_moves()
    eligible = [
        r
        for r in batch
        if r["state"] == "moved" and r["dest_uid"] and r["dest_validity"]
    ]
    if cols[1].button(
        T("↩ Вернуть последнее удаление", "↩ Undo last deletion"),
        disabled=not eligible,
        width="stretch",
    ):
        start("undo", service.undo, store, ss.password)
    if batch and len(eligible) != len(batch):
        st.caption(
            T(
                "Возврат доступен только для писем с подтверждённым перемещением и сохранённым новым UID. Подробности — в истории.",
                "Undo is available only for confirmed moves with saved destination UIDs. See history for details.",
            )
        )
    if not scan:
        st.info(
            T(
                "Начни со сканирования. Письма при этом не изменяются.",
                "Start with a scan. This does not modify messages.",
            )
        )
    else:
        st.caption(
            T(
                f"Сохранено {date(scan['stamp'])} · в выборке {len(scan['messages'])} из {scan['total']} писем. Данные загружены из локальной базы.",
                f"Saved {date(scan['stamp'])} · sample: {len(scan['messages'])} of {scan['total']} messages. Loaded from local database.",
            )
        )
        query = st.text_input(T("Поиск компании или email", "Search company or email"))
        sorts = {
            "count": ("Больше всего писем", "Most emails"),
            "date": ("Последнее письмо", "Latest email"),
            "name": ("Название", "Name"),
            "deleted": ("Удалено за 30 дней", "Deleted in the last 30 days"),
        }
        sort = st.selectbox(
            T("Сортировка", "Sort"),
            list(sorts),
            index=list(sorts).index(store.get("sort", "count")),
            format_func=lambda k: T(*sorts[k]),
        )
        store.set("sort", sort)
        visible = [
            g
            for g in groups
            if query.casefold() in (g["name"] + " " + " ".join(g["senders"])).casefold()
        ]
        visible.sort(
            key=lambda g: (
                len(g["messages"])
                if sort == "count"
                else (
                    g["latest"]
                    if sort == "date"
                    else g["recent"] if sort == "deleted" else g["name"].casefold()
                )
            ),
            reverse=sort != "name",
        )
        version = ss.get("selection_version", 0)
        select_key = lambda g: f"company_{version}_{g['key']}"
        buttons = st.columns(3)
        if buttons[0].button(T("Выбрать всё", "Select all")):
            for g in visible:
                ss[select_key(g)] = not g["protected"]
            ss.preview = None
        if buttons[1].button(T("Снять всё", "Deselect all")):
            for g in visible:
                ss[select_key(g)] = False
            ss.preview = None
        if buttons[2].button(T("Выбрать чёрный список", "Select blacklist")):
            for g in visible:
                ss[select_key(g)] = g["black"] and not g["protected"]
            ss.preview = None
        selected = []
        for g in visible:
            if select_key(g) not in ss:
                ss[select_key(g)] = g["key"] in ss.get("chosen_companies", [])
            cols = st.columns([3, 1, 2, 2])
            if cols[0].checkbox(
                f"{g['name']}" + (" 🔒" if g["protected"] else ""), key=select_key(g)
            ):
                selected.append(g)
            cols[1].write(
                T(f"{len(g['messages'])} писем", f"{len(g['messages'])} emails")
            )
            cols[2].write(date(g["latest"]))
            cols[3].write(T(*STATUS[g["status"]]))
            with st.expander(
                T("Адреса и статистика: ", "Addresses and statistics: ") + g["name"]
            ):
                st.write(", ".join(g["senders"]))
                st.write(
                    T(
                        f"Удалено за 30 дней: {g['recent']} · новых после запроса отписки: {g['after']}",
                        f"Deleted in 30 days: {g['recent']} · new after unsubscribe request: {g['after']}",
                    )
                )
        st.caption(
            T(
                "Счётчик новых писем учитывает только письма, обнаруженные при сканированиях, по времени получения сервером. При ограниченной выборке он может быть неполным.",
                "New-message counts include only messages discovered by scans, using server receipt times. Limited scans may give incomplete counts.",
            )
        )
        modes = {
            "delete_only": ("Только удалить", "Delete only"),
            "unsubscribe_only": ("Только отписаться", "Unsubscribe only"),
            "both": ("Отписаться и удалить", "Unsubscribe and delete"),
        }
        mode = st.radio(
            T("Действие", "Action"),
            list(modes),
            index=list(modes).index(store.get("mode", "delete_only")),
            format_func=lambda k: T(*modes[k]),
        )
        store.set("mode", mode)
        scope = "promo"
        if mode != "unsubscribe_only":
            scope = st.radio(
                T("Какие письма удалить", "Which emails to delete"),
                ["promo", "all"],
                index=["promo", "all"].index(store.get("scope", "promo")),
                format_func=lambda k: (
                    T("Только вероятная реклама", "Likely advertising only")
                    if k == "promo"
                    else T(
                        "Все письма выбранных компаний",
                        "All emails from selected companies",
                    )
                ),
            )
        store.set("scope", scope)
        allow_white = False
        if any(g["protected"] for g in selected):
            consent_key = "consent_" + ",".join(sorted(g["key"] for g in selected))
            if consent_key not in ss:
                ss[consent_key] = bool(
                    ss.get("preview")
                    and ss.preview["allow_white"]
                    and ss.preview["keys"] == sorted(g["key"] for g in selected)
                )
            allow_white = st.checkbox(
                T(
                    "Я разрешаю это действие для выбранных компаний из белого списка",
                    "I explicitly allow this action for selected whitelisted companies",
                ),
                key=consent_key,
            )
        keys = sorted(g["key"] for g in selected)
        ss.chosen_companies = keys
        if st.button(
            T("Подготовить предпросмотр", "Prepare preview"),
            disabled=not keys
            or (any(g["protected"] for g in selected) and not allow_white),
        ):
            start(
                "prepare",
                service.prepare,
                store,
                ss.password,
                keys,
                mode,
                scope,
                allow_white,
            )
        preview = ss.get("preview")
        if preview and (
            preview["keys"],
            preview["mode"],
            preview["scope"],
            preview["allow_white"],
        ) == (keys, mode, scope, allow_white):
            st.subheader(T("Предпросмотр и подтверждение", "Preview and confirmation"))
            st.write(", ".join(preview["companies"]))
            targets = preview["targets"]
            prefix = "message_" + ss.preview_id
            selected_uids = []
            if targets:
                a, b = st.columns(2)
                if a.button(T("Отметить все письма", "Select all messages")):
                    ss[prefix + "_default"] = True
                    ss[prefix + "_revision"] = ss.get(prefix + "_revision", 0) + 1
                if b.button(T("Снять все отметки писем", "Deselect all messages")):
                    ss[prefix + "_default"] = False
                    ss[prefix + "_revision"] = ss.get(prefix + "_revision", 0) + 1
                table = pd.DataFrame(
                    [
                        {
                            "selected": ss.get(prefix + "_default", True),
                            "sender": m["sender"],
                            "date": m["date"],
                            "subject": m["subject"],
                            "kind": (
                                T("реклама", "advertising")
                                if m["kind"] == "promo"
                                else (
                                    T("важное", "important")
                                    if m["kind"] == "important"
                                    else T("другое", "other")
                                )
                            ),
                            "uid": m["uid"],
                        }
                        for m in targets
                    ]
                )
                edited = st.data_editor(
                    table,
                    hide_index=True,
                    width="stretch",
                    disabled=["sender", "date", "subject", "kind", "uid"],
                    column_config={
                        "selected": st.column_config.CheckboxColumn(
                            T("Удалить", "Delete")
                        ),
                        "sender": T("Отправитель", "Sender"),
                        "date": T("Дата", "Date"),
                        "subject": T("Тема", "Subject"),
                        "kind": T("Тип", "Type"),
                        "uid": None,
                    },
                    key=prefix + str(ss.get(prefix + "_revision", 0)),
                )
                selected_uids = edited.loc[edited.selected, "uid"].tolist()
            auto = sum(
                bool(m["one_click"])
                and any(urlparse(u).scheme == "https" for u in m["urls"])
                for m in preview["unsubs"]
            )
            manual = len(preview["unsubs"]) - auto
            no_method = (
                len(set(preview["senders"]) - {m["sender"] for m in preview["unsubs"]})
                if mode != "delete_only"
                else 0
            )
            st.write(
                T(
                    f"Компаний: {len(keys)} · в Корзину: {len(selected_uids)} · автоотписок: до {auto} · ручных: {manual} · без способа отписки: {no_method}",
                    f"Companies: {len(keys)} · to Trash: {len(selected_uids)} · automatic requests: up to {auto} · manual: {manual} · no unsubscribe method: {no_method}",
                )
            )
            st.caption(
                T(
                    "Реклама определяется приблизительно по теме. Проверь выбранные письма. Успешный запрос отписки не гарантирует её немедленное выполнение.",
                    "Advertising is estimated from subjects. Check selected messages. An accepted unsubscribe request does not guarantee immediate removal.",
                )
            )
            if st.button(
                T("Подтвердить: ", "Confirm: ") + T(*modes[mode]),
                type="primary",
                disabled=not selected_uids and not preview["unsubs"],
            ):
                start(
                    "execute",
                    service.execute,
                    store,
                    ss.password,
                    preview,
                    selected_uids,
                )

if ss.get("result") is not None:
    result = ss.result
    if result.get("error"):
        show_error(result["error"])
    if ss.get("result_kind") == "scan":
        st.success(
            T(
                f"Готово · просканировано {result['scanned']} писем",
                f"Done · scanned {result['scanned']} messages",
            )
        )
    elif ss.get("result_kind") == "undo":
        st.success(
            T(
                f"Возвращено: {result.get('restored',0)} · отсутствуют: {result.get('skipped',0)}. Обнови сканирование.",
                f"Restored: {result.get('restored',0)} · missing: {result.get('skipped',0)}. Refresh the scan.",
            )
        )
    else:
        label = (
            T("Выполнено частично", "Partially completed")
            if result.get("error")
            else T("Готово", "Done")
        )
        st.info(
            label
            + T(
                f" · удалено {result.get('moved',0)} · запросов отписки принято {result.get('requested',0)} · ручных {result.get('manual',0)} · ошибок отписки {result.get('failed',0)} · уже отсутствуют {result.get('skipped',0)}",
                f" · deleted {result.get('moved',0)} · unsubscribe requests accepted {result.get('requested',0)} · manual {result.get('manual',0)} · unsubscribe failures {result.get('failed',0)} · already missing {result.get('skipped',0)}",
            )
        )
        for sender, url in sorted(set(tuple(x) for x in result.get("links", []))):
            if urlparse(url).scheme in ("http", "https", "mailto"):
                st.link_button(
                    T("Ручная отписка: ", "Manual unsubscribe: ") + sender, url
                )
