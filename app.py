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
VERSION = "3.2.0-test"
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
:root {
  --bg: #2E2535;
  --surface: #3A2F43;
  --surface-2: #4A3A59;
  --lilac: #A57CD1;
  --lilac-soft: #C7A7E6;
  --text: #EBDCF8;
  --warm: #FFEFD6;
}
html,body,[data-testid="stApp"],[data-testid="stAppViewContainer"],input,textarea,button,label,p {
  font-family:Arial,sans-serif;
  font-size:15px!important;
}
[data-testid="stAppViewContainer"], .stApp {
  background:var(--bg)!important;
  color:var(--text)!important;
}
[data-testid="stHeader"] {
  background:rgba(46,37,53,.96)!important;
}
[data-testid="stSidebar"] {
  background:var(--surface-2)!important;
  border-right:1px solid rgba(199,167,230,.35);
}
[data-testid="stSidebar"] * { color:var(--text); }
h1,h2,h3,h4,[data-testid="stMetricValue"] {
  font-family:Arial,sans-serif;
  font-size:15px!important;
  font-weight:700;
  color:var(--warm)!important;
}
p,label,[data-testid="stCaptionContainer"] { color:var(--text)!important; }
a { color:var(--lilac-soft)!important; }
[data-testid="stButton"] button,[data-testid="stLinkButton"] a {
  min-height:46px;
  border-radius:12px!important;
  font-size:15px!important;
  border:1px solid rgba(199,167,230,.55)!important;
  background:var(--surface-2)!important;
  color:var(--text)!important;
  transition:all .15s ease;
}
[data-testid="stButton"] button:hover,[data-testid="stLinkButton"] a:hover {
  background:var(--lilac-soft)!important;
  color:var(--bg)!important;
  border-color:var(--text)!important;
}
[data-testid="stButton"] button[kind="primary"] {
  background:var(--lilac)!important;
  color:var(--bg)!important;
  border-color:var(--lilac-soft)!important;
  font-weight:700!important;
}
[data-testid="stButton"] button:disabled {
  background:#3B3143!important;
  color:#877992!important;
  border-color:#594A63!important;
  opacity:.72!important;
}
[data-testid="stSidebar"] [data-testid="stButton"] button {
  justify-content:flex-start;
}
[data-baseweb="input"] > div,
[data-baseweb="select"] > div,
[data-baseweb="textarea"] > div {
  background:var(--surface)!important;
  border-color:rgba(199,167,230,.4)!important;
  color:var(--text)!important;
}
[data-baseweb="input"] input,
[data-baseweb="textarea"] textarea {
  color:var(--text)!important;
}
[data-baseweb="popover"] > div,
[role="listbox"] {
  background:var(--surface-2)!important;
  color:var(--text)!important;
}
[data-testid="stVerticalBlockBorderWrapper"] > div {
  border-color:rgba(199,167,230,.28)!important;
}
[data-testid="stProgress"] > div > div {
  background:var(--lilac)!important;
}
.st-key-company_list [data-testid="stButton"] button { min-height:32px; }
.st-key-company_list [data-testid="stHorizontalBlock"] { align-items:center; }
.st-key-company_list p { margin-bottom:0; }
[data-testid="stMainBlockContainer"] { padding-top:1.5rem;padding-bottom:1.5rem; }
.build-badge {
  display:inline-block;
  margin:.15rem 0 .85rem 0;
  padding:.32rem .65rem;
  border:1px solid rgba(199,167,230,.4);
  border-radius:999px;
  background:rgba(165,124,209,.12);
  color:#C7A7E6;
  font-size:12px;
  letter-spacing:.02em;
}
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


def execute_pending_request(store):
    """Run a queued destructive action independently of the preview UI.

    Streamlit callbacks run before the next full app render.  The request must
    therefore be consumed near the top level, not inside the preview block that
    created the button.
    """
    if not ss.get("execute_request"):
        return

    request = ss.pop("execute_request")
    st.info(
        T(
            "Команда принята. Выполняю действие…",
            "Command received. Running action…",
        )
    )
    status = st.status(
        T("Подключение к iCloud…", "Connecting to iCloud…"),
        state="running",
        expanded=True,
    )
    bar = st.progress(0, text=T("Начинаю…", "Starting…"))

    def execute_progress(stage, current, total):
        labels = {
            "connect": T("Подключение к iCloud", "Connecting to iCloud"),
            "unsubscribe": T(
                "Отправка запросов на отписку",
                "Sending unsubscribe requests",
            ),
            "delete": T("Перемещение в Корзину", "Moving to Trash"),
        }
        label_text = labels.get(stage, labels["connect"])
        status.update(label=label_text, state="running")
        if total:
            bar.progress(
                min(current / total, 1.0),
                text=f"{label_text} · {current}/{total}",
            )
        else:
            bar.progress(0, text=label_text)

    try:
        result = service.execute(
            store,
            ss.password,
            request["preview"],
            request["selected_uids"],
            execute_progress,
        )
    except Exception as exc:
        result = {
            "error": service.error_code(exc),
            "error_detail": service.error_details(exc),
            "moved": 0,
            "requested": 0,
            "manual": 0,
            "failed": 0,
            "skipped": 0,
        }

    ss.result = result
    ss.result_kind = "execute"
    ss.preview = None
    ss.selection_version = ss.get("selection_version", 0) + 1
    ss.chosen_companies = []
    ss.pop("view_company", None)

    if result.get("error"):
        status.update(
            label=T(
                "Действие завершилось с ошибкой",
                "Action finished with an error",
            ),
            state="error",
            expanded=True,
        )
        show_error(result["error"], result.get("error_detail"))
    else:
        bar.progress(1.0, text=T("Готово", "Done"))
        status.update(
            label=T("Готово", "Done"),
            state="complete",
            expanded=False,
        )

    st.rerun()


st.title("📬 iCloud Mail Assistant")
st.markdown(
    '<div class="build-badge">TEST BUILD 3.2.0 · fix/icloud-delete</div>',
    unsafe_allow_html=True,
)
pages = {
    "mail": ("Почта", "Mail"),
    "white": ("Белый список", "Whitelist"),
    "black": ("Чёрный список", "Blacklist"),
    "groups": ("Объединение компаний", "Company groups"),
    "history": ("История", "History"),
    "settings": ("Настройки", "Settings"),
}


def reset_read_filter_selection():
    ss.chosen_companies = []
    ss.selection_version = ss.get("selection_version", 0) + 1
    ss.preview = None


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
recovery_key = "_recovered_operations_" + ss.account
if not ss.get(recovery_key):
    ss.recovered_operations = store.recover_running()
    ss[recovery_key] = True

# A queued confirmation must run before page-specific UI.  Otherwise navigating
# away or losing the preview can strand the request until some later rerun.
execute_pending_request(store)

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
    if ss.get("recovered_operations"):
        st.info(
            T(
                f"После перезапуска помечено прерванными операций: {ss.recovered_operations}.",
                f"Operations marked interrupted after restart: {ss.recovered_operations}.",
            )
        )
        ss.recovered_operations = 0

    rows = []
    for h in history:
        d = json.loads(h["detail"])
        rows.append(
            {
                T("Дата", "Date"): date(h["stamp"]),
                T("Действие", "Action"): h["kind"],
                T("Статус", "Status"): h["status"],
                T("Последний этап", "Last step"): d.get("last_step", "—"),
                T("Компании", "Companies"): ", ".join(d.get("companies", [])),
                T("Удалено", "Deleted"): d.get("moved", 0),
                T("Запросов принято", "Requests accepted"): d.get("requested", 0),
                T("Возвращено", "Restored"): d.get("restored", 0),
            }
        )
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    st.caption(
        T(
            "interrupted означает, что предыдущий процесс завершился до финального статуса. partial — действие дошло до ошибки, но часть шагов могла успеть выполниться.",
            "interrupted means the previous app process ended before a final status. partial means the action reached an error after some steps may have completed.",
        )
    )
    with st.expander(T("Диагностика последнего действия", "Last action diagnostics")):
        if history:
            detail = json.loads(history[0]["detail"])
            trace = detail.get("trace", [])
            if trace:
                trace_rows = []
                for event in trace:
                    row = {
                        T("Время", "Time"): date(event.get("stamp")),
                        T("Этап", "Step"): event.get("step", "—"),
                    }
                    for key in ("index", "total", "uid", "move", "uidplus", "mapped", "found", "status", "error", "detail"):
                        if key in event:
                            row[key] = event[key]
                    trace_rows.append(row)
                st.dataframe(
                    pd.DataFrame(trace_rows),
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.info(
                    T(
                        "Для этой старой операции подробная диагностика ещё не записывалась.",
                        "Detailed diagnostics were not recorded for this older operation.",
                    )
                )
            st.json({k: v for k, v in detail.items() if k != "trace"})

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

        read_state_available = bool(scan["messages"]) and all(
            "unread" in m for m in scan["messages"]
        )
        read_filters = {
            "all": ("Все", "All"),
            "unread": ("Непрочитанные", "Unread"),
            "read": ("Прочитанные", "Read"),
        }
        stored_read_filter = store.get("read_filter", "all")
        if stored_read_filter not in read_filters:
            stored_read_filter = "all"
        if not read_state_available:
            stored_read_filter = "all"

        filter_row = st.columns([2, 3])
        sorts = {
            "count": ("Больше всего писем", "Most emails"),
            "date": ("Последнее письмо", "Latest email"),
            "name": ("Название", "Name"),
            "deleted": ("Удалено за 30 дней", "Deleted in the last 30 days"),
        }
        sort = filter_row[0].selectbox(
            T("Сортировка", "Sort"),
            list(sorts),
            index=list(sorts).index(store.get("sort", "count")),
            format_func=lambda k: T(*sorts[k]),
            label_visibility="collapsed",
        )
        read_filter = filter_row[1].radio(
            T("Показывать письма", "Show messages"),
            list(read_filters),
            index=list(read_filters).index(stored_read_filter),
            format_func=lambda k: T(*read_filters[k]),
            horizontal=True,
            key="read_filter_control",
            disabled=not read_state_available,
            on_change=reset_read_filter_selection,
        )
        if not read_state_available:
            st.caption(
                T(
                    "Чтобы фильтровать прочитанные и непрочитанные письма, один раз пересканируй почту.",
                    "Rescan the inbox once to enable read/unread filtering.",
                )
            )
            read_filter = "all"
        store.set("sort", sort)
        store.set("read_filter", read_filter)

        def view_messages(group):
            return [
                m
                for m in group["messages"]
                if service.matches_read_filter(m, read_filter)
            ]

        visible = [
            g
            for g in groups
            if query.casefold()
            in (g["name"] + " " + " ".join(g["senders"])).casefold()
            and (read_filter == "all" or view_messages(g))
        ]
        visible.sort(
            key=lambda g: (
                len(view_messages(g))
                if sort == "count"
                else (
                    max(
                        (m["received"] for m in view_messages(g)),
                        default=g["latest"],
                    )
                    if sort == "date"
                    else g["recent"] if sort == "deleted" else g["name"].casefold()
                )
            ),
            reverse=sort != "name",
        )

        toolbar = st.columns([1, 1, 1.5])
        version = ss.get("selection_version", 0)
        select_key = lambda g: f"company_{version}_{g['key']}"
        buttons = toolbar
        if buttons[0].button(T("Выбрать всё", "Select all")):
            for g in visible:
                ss[select_key(g)] = not g["protected"]
        if buttons[1].button(T("Снять всё", "Deselect all")):
            for g in groups:
                ss[select_key(g)] = False
            ss.chosen_companies = []
        if buttons[2].button(T("Выбрать чёрный список", "Select blacklist")):
            for g in visible:
                ss[select_key(g)] = g["black"] and not g["protected"]
        # Widget-independent selection survives search, dialogs and worker reruns.
        chosen = set(ss.get("chosen_companies", []))
        with st.container(height=390, key="company_list", border=True):
            heads = st.columns([4, 1, 1, 2, 1.5])
            for col, label in zip(
                heads,
                [
                    T("Компания", "Company"),
                    T("Писем", "Emails"),
                    T("Непроч.", "Unread"),
                    T("Последнее", "Latest"),
                    T("Просмотр", "Preview"),
                ],
            ):
                col.caption(label)
            for g in visible:
                if select_key(g) not in ss:
                    ss[select_key(g)] = g["key"] in chosen
                cols = st.columns([4, 1, 1, 2, 1.5])
                checked = cols[0].checkbox(
                    g["name"] + (" 🔒" if g["protected"] else ""),
                    key=select_key(g),
                    help=T(*STATUS[g["status"]]) + " · " + ", ".join(g["senders"]),
                )
                if checked:
                    chosen.add(g["key"])
                else:
                    chosen.discard(g["key"])
                shown_messages = view_messages(g)
                unread_count = sum(
                    1 for m in g["messages"] if m.get("unread") is True
                )
                latest_shown = max(
                    (m["received"] for m in shown_messages),
                    default=g["latest"],
                )
                cols[1].write(str(len(shown_messages)))
                cols[2].write(str(unread_count))
                cols[3].write(date(latest_shown).split(" ")[0])
                if cols[4].button(
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
                key="action_mode",
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
                    key="delete_scope",
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
                    read_filter,
                )
            preview = ss.get("preview")
            if preview:
                preview_mode = preview["mode"]
                preview_read_filter = preview.get("read_filter", "all")
                st.subheader(
                    T("Предпросмотр и подтверждение", "Preview and confirmation")
                )
                with st.expander(T("Выбранные компании", "Selected companies")):
                    st.write(", ".join(preview["companies"]))
                st.caption(
                    T("Фильтр писем: ", "Message filter: ")
                    + T(*read_filters.get(preview_read_filter, read_filters["all"]))
                )
                targets = preview["targets"]
                if preview_mode != "unsubscribe_only" and not targets:
                    st.warning(
                        T(
                            "Писем для удаления нет. Фильтр «Только вероятная реклама» мог исключить их — выбери «Все письма выбранных компаний» и снова нажми «Просмотреть и подтвердить». Если и там пусто, письма уже не во Входящих.",
                            "No deletion candidates. The advertising filter may have excluded them: choose ‘All emails from selected companies’ and review again. If still empty, the messages are no longer in the inbox.",
                        )
                    )
                elif preview_mode != "unsubscribe_only" and preview.get("excluded"):
                    st.caption(
                        T(
                            f"Фильтр рекламы исключил писем: {preview['excluded']}.",
                            f"Advertising filter excluded {preview['excluded']} messages.",
                        )
                    )
                prefix = "message_" + ss.preview_id
                selected_uids = []
                if targets:
                    # Each message owns one stable checkbox state.  Do not use a
                    # data_editor here: rebuilding its DataFrame on every rerun can
                    # discard the editor delta and collapse the surrounding preview.
                    def message_key(uid):
                        return f"{prefix}_uid_{uid}"

                    # Initialise only once for this preview.  A restored selection
                    # (e.g. after opening a message reader) wins over the default.
                    restore = ss.get(prefix + "_restore")
                    default_selected = ss.get(prefix + "_default", True)
                    for m in targets:
                        key = message_key(m["uid"])
                        if key not in ss:
                            ss[key] = (
                                m["uid"] in restore
                                if restore is not None
                                else default_selected
                            )

                    a, b = st.columns(2)
                    if a.button(
                        T("Отметить все письма", "Select all messages"),
                        key=prefix + "_select_all",
                    ):
                        for m in targets:
                            ss[message_key(m["uid"])] = True
                        ss.pop(prefix + "_restore", None)
                        st.rerun()
                    if b.button(
                        T("Снять все отметки писем", "Deselect all messages"),
                        key=prefix + "_deselect_all",
                    ):
                        for m in targets:
                            ss[message_key(m["uid"])] = False
                        ss.pop(prefix + "_restore", None)
                        st.rerun()

                    with st.container(height=300, border=True):
                        heads = st.columns([0.8, 2.3, 2.2, 4.7])
                        heads[0].caption(T("Удалить", "Delete"))
                        heads[1].caption(T("Дата", "Date"))
                        heads[2].caption(T("Отправитель", "Sender"))
                        heads[3].caption(T("Тема", "Subject"))

                        for m in targets:
                            row = st.columns([0.8, 2.3, 2.2, 4.7])
                            checked = row[0].checkbox(
                                T("Удалить", "Delete"),
                                key=message_key(m["uid"]),
                                label_visibility="collapsed",
                            )
                            row[1].write(m["date"] or "—")
                            row[2].write(m["sender"])
                            subject = m["subject"] or T("Без темы", "No subject")
                            kind = (
                                T("реклама", "advertising")
                                if m["kind"] == "promo"
                                else (
                                    T("важное", "important")
                                    if m["kind"] == "important"
                                    else T("другое", "other")
                                )
                            )
                            row[3].write(f"{subject} · {kind}")
                            if checked:
                                selected_uids.append(m["uid"])

                    ss[prefix + "_selection"] = list(selected_uids)
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
                    if preview_mode != "delete_only"
                    else 0
                )
                st.write(
                    T(
                        f"Компаний: {len(preview['keys'])} · в Корзину: {len(selected_uids)} · автоотписок: до {auto} · ручных: {manual} · без способа отписки: {no_method}",
                        f"Companies: {len(preview['keys'])} · to Trash: {len(selected_uids)} · automatic requests: up to {auto} · manual: {manual} · no unsubscribe method: {no_method}",
                    )
                )
                st.caption(
                    T(
                        "Реклама определяется приблизительно по теме. Проверь выбранные письма. Успешный запрос отписки не гарантирует её немедленное выполнение.",
                        "Advertising is estimated from subjects. Check selected messages. An accepted unsubscribe request does not guarantee immediate removal.",
                    )
                )
                label = (
                    T(
                        f"Переместить в Корзину: {len(selected_uids)} писем",
                        f"Move {len(selected_uids)} emails to Trash",
                    )
                    if preview_mode == "delete_only"
                    else (
                        T("Подтвердить отписку", "Confirm unsubscribe")
                        if preview_mode == "unsubscribe_only"
                        else T(
                            f"Отписаться и переместить в Корзину: {len(selected_uids)} писем",
                            f"Unsubscribe and move {len(selected_uids)} emails to Trash",
                        )
                    )
                )

                # Do not execute the destructive action inside the same widget run.
                # Store an immutable request first; the next app rerun executes it.
                def queue_execute():
                    ss.execute_request = {
                        "preview": preview,
                        "selected_uids": list(ss.get(prefix + "_selection", [])),
                    }

                st.button(
                    label,
                    key="execute_action",
                    type="primary",
                    disabled=not selected_uids and not preview["unsubs"],
                    on_click=queue_execute,
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
