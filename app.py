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

st.set_page_config(
    page_title="iCloud Mail Assistant",
    page_icon="📬",
    layout="wide",
    initial_sidebar_state="expanded",
)
VERSION = "3.1.0"
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
        "iCloud отклонил почтовую команду. Открой технические подробности. Если вход уже выполнен, это не обязательно ошибка пароля.",
        "iCloud rejected a mail command. Open technical details. If you are signed in, this is not necessarily a password problem.",
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
    "no_messages": (
        "Нет выбранных писем для удаления. Открой предпросмотр и отметь письма.",
        "No messages selected for deletion. Open the preview and select messages.",
    ),
    "message_missing": (
        "Письмо уже отсутствует во Входящих. Обнови сканирование.",
        "This message is no longer in the inbox. Refresh the scan.",
    ),
    "move_unconfirmed": (
        "Сервер ответил на перемещение, но письмо осталось во Входящих. Операция остановлена: проверь Корзину и историю.",
        "The server replied to the move, but the message is still in the inbox. Stopped: check Trash and history.",
    ),
    "unexpected": (
        "Не удалось завершить действие. Проверь историю; уже выполненные шаги сохранены.",
        "Could not complete the action. Review history; completed steps were saved.",
    ),
    "server_command": (
        "iCloud не выполнил команду. Проверь историю перед повтором.",
        "iCloud did not complete the command. Review history before retrying.",
    ),
}


def show_error(code, detail=None):
    st.error(T(*ERRORS.get(code, ERRORS["unexpected"])))
    with st.expander(T("Технические подробности", "Technical details")):
        st.code(detail or code)


st.markdown(
    """<style>
html,body,[data-testid="stApp"],input,textarea,button,label,p {font-family:Arial,sans-serif;font-size:15px!important;}
h1,h2,h3,h4,[data-testid="stMetricValue"] {font-family:Arial,sans-serif;font-size:15px!important;font-weight:700;}
[data-testid="stButton"] button,[data-testid="stLinkButton"] a {min-height:48px;border-radius:10px;font-size:15px!important;}
[data-testid="stSidebar"] [data-testid="stButton"] button {justify-content:flex-start;}
.st-key-company_list [data-testid="stButton"] button {min-height:32px;}
.st-key-company_list [data-testid="stHorizontalBlock"] {align-items:center;}
.st-key-company_list p {margin-bottom:0;}
[data-testid="stMainBlockContainer"] {padding-top:1.5rem;padding-bottom:1.5rem;}
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
        self.detail = None
        self.done = False
        self.lock = threading.Lock()

        def work():
            try:
                self.result = fn(*args, self.progress)
            except Exception as exc:
                self.error = service.error_code(exc)
                self.detail = service.error_details(exc)
            finally:
                self.done = True

        self.thread = threading.Thread(target=work, daemon=True)

    def progress(self, stage, current, total):
        with self.lock:
            self.stage, self.current, self.total = stage, current, total


def start(kind, fn, *args):
    if ss.get("job") and not ss.job.done:
        return
    if kind != "read":
        ss.preview = None
    elif ss.get("preview"):
        prefix = "message_" + ss.preview_id
        ss[prefix + "_restore"] = ss.get(prefix + "_selection", [])
        ss[prefix + "_revision"] = ss.get(prefix + "_revision", 0) + 1
    ss.result = None
    ss.pop("last_error", None)
    ss.pop("last_error_detail", None)
    ss.job = Job(kind, fn, args)
    ss.job.thread.start()
    st.rerun()


def authenticate(account, password, progress):
    with service.connection(account, password) as m:
        service.select(m)
    return account


st.title("📬 iCloud Mail Assistant")
pages = {
    "mail": ("Почта", "Mail"),
    "white": ("Белый список", "Whitelist"),
    "black": ("Чёрный список", "Blacklist"),
    "groups": ("Объединение компаний", "Company groups"),
    "history": ("История", "History"),
    "settings": ("Настройки", "Settings"),
}


def navigate(page):
    ss.page = page
    ss.pop("view_company", None)
    ss.preview = None


def sidebar():
    # Always render the same sidebar, including while a worker is running.
    busy = bool(ss.get("job") and not ss.job.done)
    with st.sidebar:
        st.subheader(T("Меню", "Menu"))
        st.caption(ss.account)
        for key, labels in pages.items():
            st.button(
                T(*labels),
                key="nav_" + key,
                width="stretch",
                type="primary" if ss.get("page", "mail") == key else "secondary",
                disabled=busy,
                on_click=navigate,
                args=(key,),
            )
        if st.button(
            T("Выйти", "Sign out"), key="signout", width="stretch", disabled=busy
        ):
            lang = ss.language
            for key in list(ss):
                del ss[key]
            ss.language = lang
            st.rerun()
        st.caption(f"v{VERSION}")


if ss.get("account"):
    sidebar()
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
                "read": ("Загрузка текста письма", "Loading message text"),
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
        ss.last_error_detail = job.detail
    elif job.kind == "login":
        ss.account = job.result
        ss.password = ss.pop("pending_password", "")
        ss.pop("password_input", None)
        st.rerun()
    elif job.kind == "prepare":
        ss.preview = job.result
        ss.preview_id = uuid.uuid4().hex
    elif job.kind == "read":
        ss.message_content = job.result
    else:
        ss.result = job.result
        ss.result_kind = job.kind
        if job.kind in ("execute", "undo", "scan"):
            ss.selection_version = ss.get("selection_version", 0) + 1
            ss.chosen_companies = []
            ss.pop("view_company", None)
    ss.pop("pending_password", None)

if ss.get("last_error"):
    show_error(ss.last_error, ss.get("last_error_detail"))

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
page = ss.get("page", "mail")

history = store.history()
if history and page != "mail":
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
    st.selectbox(
        "Language / Язык",
        ["Русский", "English"],
        key="language",
        on_change=lambda: preferences.set("language", ss.language),
    )
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
    query = st.text_input(
        T("Поиск компании или email", "Search company or email"),
        placeholder=T("Поиск компании или email", "Search company or email"),
        label_visibility="collapsed",
        key="company_search",
    )
    scan = store.scan()
    limits = [500, 1000, 5000, 0]
    default = store.get("limit", 1000)
    cols = st.columns([2, 2, 2])
    limit = cols[0].selectbox(
        T("Сколько последних писем сканировать", "How many recent emails to scan"),
        limits,
        index=limits.index(default) if default in limits else 1,
        format_func=lambda n: (
            T(f"Сканировать: {n} писем", f"Scan: {n} emails")
            if n
            else T("Сканировать: все письма", "Scan: all emails")
        ),
        label_visibility="collapsed",
    )
    store.set("limit", limit)
    if cols[1].button(
        T("Сканировать почту", "Scan inbox"), type="primary", width="stretch"
    ):
        start("scan", service.scan, store, ss.password, limit)
    batch = store.last_moves()
    eligible = [
        r
        for r in batch
        if r["state"] == "moved" and r["dest_uid"] and r["dest_validity"]
    ]
    if cols[2].button(
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
        action_area = st.container()
        toolbar = st.columns([2, 1, 1, 1.5])
        sorts = {
            "count": ("Больше всего писем", "Most emails"),
            "date": ("Последнее письмо", "Latest email"),
            "name": ("Название", "Name"),
            "deleted": ("Удалено за 30 дней", "Deleted in the last 30 days"),
        }
        sort = toolbar[0].selectbox(
            T("Сортировка", "Sort"),
            list(sorts),
            index=list(sorts).index(store.get("sort", "count")),
            format_func=lambda k: T(*sorts[k]),
            label_visibility="collapsed",
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
        buttons = toolbar[1:]
        if buttons[0].button(T("Выбрать всё", "Select all")):
            for g in visible:
                ss[select_key(g)] = not g["protected"]
            ss.preview = None
        if buttons[1].button(T("Снять всё", "Deselect all")):
            for g in groups:
                ss[select_key(g)] = False
            ss.chosen_companies = []
            ss.preview = None
        if buttons[2].button(T("Выбрать чёрный список", "Select blacklist")):
            for g in visible:
                ss[select_key(g)] = g["black"] and not g["protected"]
            ss.preview = None
        # Widget-independent selection survives search, dialogs and worker reruns.
        chosen = set(ss.get("chosen_companies", []))
        with st.container(height=390, key="company_list", border=True):
            heads = st.columns([4, 1, 2, 1.5])
            for col, label in zip(
                heads,
                [
                    T("Компания", "Company"),
                    T("Писем", "Emails"),
                    T("Последнее", "Latest"),
                    T("Просмотр", "Preview"),
                ],
            ):
                col.caption(label)
            for g in visible:
                if select_key(g) not in ss:
                    ss[select_key(g)] = g["key"] in chosen
                cols = st.columns([4, 1, 2, 1.5])
                checked = cols[0].checkbox(
                    g["name"] + (" 🔒" if g["protected"] else ""),
                    key=select_key(g),
                    help=T(*STATUS[g["status"]]) + " · " + ", ".join(g["senders"]),
                )
                if checked:
                    chosen.add(g["key"])
                else:
                    chosen.discard(g["key"])
                cols[1].write(str(len(g["messages"])))
                cols[2].write(date(g["latest"]).split(" ")[0])
                if cols[3].button(
                    T("Письма", "Emails"), key="view_" + g["key"], width="stretch"
                ):
                    ss.view_company = g["key"]
                    ss.message_content = None
            if not visible:
                st.caption(T("Ничего не найдено.", "No matches."))
        selected = [g for g in groups if g["key"] in chosen]
        ss.chosen_companies = sorted(g["key"] for g in selected)
        st.caption(
            T(
                f"Компаний: {len(visible)} · выбрано: {len(selected)}",
                f"Companies: {len(visible)} · selected: {len(selected)}",
            )
        )
        with action_area:
            st.caption(
                T(
                    f"Выбрано компаний: {len(selected)}. Сначала просмотр, затем подтверждение.",
                    f"Selected companies: {len(selected)}. Review first, then confirm.",
                )
            )
            hidden_count = len(
                [g for g in selected if g["key"] not in {v["key"] for v in visible}]
            )
            if hidden_count:
                st.caption(
                    T(
                        f"Из них скрыто поиском: {hidden_count}. «Снять всё» очищает весь выбор.",
                        f"Hidden by search: {hidden_count}. ‘Deselect all’ clears the entire selection.",
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
                horizontal=True,
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
                    horizontal=True,
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
                T("Просмотреть и подтвердить →", "Review and confirm →"),
                key="prepare_action",
                type="primary",
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
            ) != (keys, mode, scope, allow_white):
                ss.preview = None
                preview = None
            if preview and (
                preview["keys"],
                preview["mode"],
                preview["scope"],
                preview["allow_white"],
            ) == (keys, mode, scope, allow_white):
                st.subheader(
                    T("Предпросмотр и подтверждение", "Preview and confirmation")
                )
                with st.expander(T("Выбранные компании", "Selected companies")):
                    st.write(", ".join(preview["companies"]))
                targets = preview["targets"]
                confirmation = st.container()
                if mode != "unsubscribe_only" and not targets:
                    st.warning(
                        T(
                            "Писем для удаления нет. Фильтр «Только вероятная реклама» мог исключить их — выбери «Все письма выбранных компаний» и снова нажми «Просмотреть и подтвердить». Если и там пусто, письма уже не во Входящих.",
                            "No deletion candidates. The advertising filter may have excluded them: choose ‘All emails from selected companies’ and review again. If still empty, the messages are no longer in the inbox.",
                        )
                    )
                elif mode != "unsubscribe_only" and preview.get("excluded"):
                    st.caption(
                        T(
                            f"Фильтр рекламы исключил писем: {preview['excluded']}.",
                            f"Advertising filter excluded {preview['excluded']} messages.",
                        )
                    )
                prefix = "message_" + ss.preview_id
                selected_uids = []
                if targets:
                    a, b = st.columns(2)
                    if a.button(T("Отметить все письма", "Select all messages")):
                        ss.pop(prefix + "_restore", None)
                        ss[prefix + "_default"] = True
                        ss[prefix + "_revision"] = ss.get(prefix + "_revision", 0) + 1
                    if b.button(T("Снять все отметки писем", "Deselect all messages")):
                        ss.pop(prefix + "_restore", None)
                        ss[prefix + "_default"] = False
                        ss[prefix + "_revision"] = ss.get(prefix + "_revision", 0) + 1
                    table = pd.DataFrame(
                        [
                            {
                                "selected": (
                                    m["uid"] in ss[prefix + "_restore"]
                                    if prefix + "_restore" in ss
                                    else ss.get(prefix + "_default", True)
                                ),
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
                        height=280,
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
                    ss[prefix + "_selection"] = selected_uids
                auto = sum(
                    bool(m["one_click"])
                    and any(urlparse(u).scheme == "https" for u in m["urls"])
                    for m in preview["unsubs"]
                )
                manual = len(preview["unsubs"]) - auto
                no_method = (
                    len(
                        set(preview["senders"])
                        - {m["sender"] for m in preview["unsubs"]}
                    )
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
                with confirmation:
                    label = (
                        T(
                            f"Переместить в Корзину: {len(selected_uids)} писем",
                            f"Move {len(selected_uids)} emails to Trash",
                        )
                        if mode == "delete_only"
                        else (
                            T("Подтвердить отписку", "Confirm unsubscribe")
                            if mode == "unsubscribe_only"
                            else T(
                                f"Отписаться и переместить в Корзину: {len(selected_uids)} писем",
                                f"Unsubscribe and move {len(selected_uids)} emails to Trash",
                            )
                        )
                    )
                    if st.button(
                        label,
                        key="execute_action",
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


def close_messages():
    ss.pop("view_company", None)
    ss.pop("message_content", None)


@st.dialog(
    T("Письма компании", "Company emails"), width="large", on_dismiss=close_messages
)
def show_company_messages(group):
    st.subheader(group["name"])
    messages = sorted(
        group["messages"], key=lambda m: (m["received"], int(m["uid"])), reverse=True
    )
    st.caption(
        T(
            "Письма из последнего сканирования; это не выбор на удаление.",
            "Messages from the last scan; this is not a deletion selection.",
        )
    )
    with st.expander(T("Адреса и статистика", "Addresses and statistics")):
        st.text(", ".join(group["senders"]))
        st.write(T(*STATUS[group["status"]]))
        st.caption(
            T(
                f"Удалено за 30 дней: {group['recent']} · новых после отписки: {group['after']}. Счётчик ограничен сканированиями.",
                f"Deleted in 30 days: {group['recent']} · new after unsubscribe: {group['after']}. Counts are limited to scans.",
            )
        )
    if messages:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        T("Тема", "Subject"): m["subject"],
                        T("Дата", "Date"): m["date"] or date(m["received"]),
                        T("Отправитель", "Sender"): m["sender"],
                    }
                    for m in messages
                ]
            ),
            hide_index=True,
            height=240,
            width="stretch",
        )
        uid = st.selectbox(
            T("Письмо для чтения", "Message to read"),
            [m["uid"] for m in messages],
            format_func=lambda u: next(
                m["subject"] or T("Без темы", "No subject")
                for m in messages
                if m["uid"] == u
            ),
            key="read_uid_" + group["key"],
        )
        item = next(m for m in messages if m["uid"] == uid)
        if st.button(
            T("Открыть текст письма", "Open message text"), key="read_message"
        ):
            ss.message_content = None
            start(
                "read",
                service.read_message,
                store,
                ss.password,
                store.scan()["validity"],
                item,
            )
        content = ss.get("message_content")
        if content and content["uid"] == uid:
            if content["truncated"]:
                st.caption(
                    T(
                        "Показано начало большого письма.",
                        "Showing the beginning of a large message.",
                    )
                )
            st.text(
                content["text"]
                or T(
                    "Текст недоступен; возможно, письмо содержит только изображения.",
                    "No text available; the message may contain only images.",
                )
            )
        st.caption(
            T(
                "Без загрузки картинок и трекеров. Письмо не помечается прочитанным.",
                "No images or trackers are loaded. The message is not marked as read.",
            )
        )
    else:
        st.info(
            T(
                "В сохранённой выборке писем нет. Просканируй почту заново.",
                "No messages in the saved sample. Scan the inbox again.",
            )
        )
    if st.button(T("Закрыть", "Close"), key="close_messages"):
        close_messages()
        st.rerun()


if ss.get("view_company") and page == "mail":
    group = next((g for g in groups if g["key"] == ss.view_company), None)
    if group:
        show_company_messages(group)

if ss.get("result") is not None:
    result = ss.result
    if result.get("error"):
        show_error(result["error"], result.get("error_detail"))
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

if history and page == "mail":
    latest = history[0]
    detail = json.loads(latest["detail"])
    st.caption(
        T("Последнее действие", "Last action")
        + f": {date(latest['stamp'])} · "
        + T("удалено", "deleted")
        + f" {detail.get('moved', 0)} · "
        + T("запросов отписки принято", "unsubscribe requests accepted")
        + f" {detail.get('requested', 0)}"
    )
