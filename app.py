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
from session_store import COOKIE, SessionStore
import mail_service as service

st.set_page_config(
    page_title="iCloud Mail Assistant",
    page_icon="📬",
    layout="wide",
    initial_sidebar_state="expanded",
)
VERSION = "3.3.11-test"
ss = st.session_state
preferences = Store()
if "language" not in ss:
    ss.language = preferences.get("language", "Русский")

LANGUAGE = ss.language
if "font_size" not in ss:
    ss.font_size = int(preferences.get("font_size", 16) or 16)

# The browser picks the light or dark theme from .streamlit/config.toml: the
# user's choice in localStorage if there is one, otherwise the system setting.
# This key and value format match Settings -> Theme in the Streamlit 1.65
# frontend (utils.*.js: `stActiveTheme-${location.pathname}-v2`, JSON "Light",
# "Dark" or "System"), so the toggle and that menu share one setting.
THEME_SWITCH_JS = """<script>
localStorage.setItem(
  "stActiveTheme-" + window.location.pathname + "-v2", JSON.stringify(%s)
);
window.location.reload();
</script>"""


def request_theme_switch():
    ss.theme_switch = "Dark" if ss.dark_theme else "Light"


def save_font_size():
    preferences.set("font_size", int(ss.font_size))


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


# Colours come from the active Streamlit theme (.streamlit/config.toml). The
# few custom elements below derive theirs from currentColor (the theme's text
# colour), so they follow light/dark without knowing which one is active.
st.markdown(
    f"""<style>
:root {{
  --primary: #A57CD1;
  --primary-hover: #C7A7E6;
  --primary-text: #25172F;
  --line: color-mix(in srgb, currentColor 16%, transparent);
  --tint: color-mix(in srgb, currentColor 5%, transparent);
}}
html, body, input, textarea, button, label, p {{
  font-family: "Segoe UI", Inter, Arial, sans-serif;
  font-size: {ss.font_size}px !important;
}}
h1 {{
  font-size: clamp(34px, 3vw, 46px) !important;
  line-height: 1.02 !important;
  font-weight: 750 !important;
  letter-spacing: -0.035em;
  margin-bottom: .2rem !important;
}}
h2, h3, h4, [data-testid="stMetricValue"] {{
  font-weight: 650 !important;
}}
h2 {{ font-size: {ss.font_size + 5}px !important; }}
h3 {{ font-size: {ss.font_size + 3}px !important; }}

[data-testid="stButton"] button,
[data-testid="stLinkButton"] a {{
  min-height: 40px;
  border-radius: 12px !important;
  font-weight: 500 !important;
  box-shadow: none !important;
}}
/* White on the lilac primary is ~3:1; dark text keeps it readable. */
[data-testid="stButton"] button[kind="primary"] {{
  font-weight: 650 !important;
}}
[data-testid="stButton"] button[kind="primary"]:not(:disabled) {{
  color: var(--primary-text) !important;
}}
[data-testid="stButton"] button[kind="primary"]:not(:disabled):hover {{
  background: var(--primary-hover) !important;
  border-color: var(--primary-hover) !important;
}}
/* Secondary buttons sit on a faint tint of the text colour, like the rows. */
[data-testid="stButton"] button:not([kind="primary"]),
[data-testid="stLinkButton"] a {{
  background-image: linear-gradient(var(--tint), var(--tint)) !important;
}}
[data-testid="stRadio"] [role="radiogroup"] {{
  gap: .9rem;
}}
[data-testid="stRadio"] label,
[data-testid="stCheckbox"] label {{
  border-radius: 10px !important;
}}
[data-testid="stDialog"] > div {{
  border-radius: 14px !important;
}}
[data-testid="stDataFrame"] {{
  border-radius: 12px !important;
  overflow: hidden;
}}
[data-testid="stSidebar"] [data-testid="stButton"] button {{
  justify-content: flex-start;
}}
/* Keyed containers are the bordered element in current Streamlit. */
.st-key-company_table {{
  border: 1px solid var(--line) !important;
  border-radius: 12px !important;
  padding: .65rem !important;
  gap: .45rem !important;
}}
.st-key-company_list {{
  gap: .3rem !important;
  scrollbar-gutter: stable;
}}
.st-key-company_header,
[class*="st-key-company_row_"] {{
  box-sizing: border-box;
  padding: .2rem .5rem !important;
  border: 1px solid var(--line) !important;
  border-radius: 10px !important;
  gap: 0 !important;
}}
.st-key-company_header {{
  border-color: transparent !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}}
/* Streamlit's -1rem markdown margin offsets a trailing <p> margin; these cells are
   bare <div>s, so it collapsed the column header and the first row overlapped it. */
.st-key-company_list [data-testid="stMarkdownContainer"] {{
  margin-bottom: 0 !important;
}}
[class*="st-key-company_row_"] {{
  background: var(--tint) !important;
}}
.st-key-company_list [data-testid="stHorizontalBlock"] {{
  align-items: center !important;
}}
.st-key-company_list [data-testid="stMarkdownContainer"] p {{
  margin: 0 !important;
}}
.company-col-head {{
  opacity: .68;
  line-height: 1.2;
}}
.company-col-head.center {{ text-align: center; }}
.company-cell {{
  display: flex;
  align-items: center;
  min-height: 32px;
  line-height: 1.2;
}}
.company-cell.center {{ justify-content: center; }}
[class*="st-key-company_row_"] [data-testid="stCheckbox"],
[class*="st-key-company_row_"] [data-testid="stCheckbox"] label {{
  min-height: 32px !important;
  margin: 0 !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
}}
[class*="st-key-company_row_"] [data-testid="stButton"] button {{
  min-height: 32px !important;
  padding-top: .15rem !important;
  padding-bottom: .15rem !important;
}}
[data-testid="stMainBlockContainer"] {{
  padding-top: 1.45rem;
  padding-bottom: 2.2rem;
  max-width: 1180px;
}}
/* One native, accessible toggle: label and track share the same flex row.
   Streamlit renders st.toggle as stCheckbox. */
.st-key-theme_toggle_area {{
  padding-top: 2.35rem;
  padding-right: 0;
}}
.st-key-theme_toggle_area .st-key-dark_theme {{
  width: 100% !important;
}}
.st-key-theme_toggle_area [data-testid="stCheckbox"] {{
  display: flex !important;
  justify-content: flex-end !important;
  width: 100% !important;
  min-height: 48px;
  margin: 0 !important;
  padding: 0 !important;
}}
.st-key-theme_toggle_area [data-testid="stCheckbox"] label {{
  display: flex !important;
  flex-direction: row-reverse !important;
  align-items: center !important;
  gap: 14px !important;
  min-height: 48px !important;
  margin: 0 !important;
  padding: 0 !important;
  cursor: pointer;
}}
/* first-of-type skips the visually hidden input/span in both BaseWeb
   and React Aria implementations. Real dimensions reserve layout space. */
.st-key-theme_toggle_area [data-testid="stCheckbox"] label > div:first-of-type {{
  box-sizing: border-box !important;
  position: relative !important;
  flex: 0 0 36px !important;
  width: 36px !important;
  height: 20px !important;
  min-width: 36px !important;
  margin: 0 !important;
  padding: 2px !important;
  border: 0 !important;
  border-radius: 999px !important;
  display: flex !important;
  align-items: center !important;
  transform: none !important;
  background: color-mix(in srgb, currentColor 22%, transparent) !important;
}}
.st-key-theme_toggle_area [data-testid="stCheckbox"] label > div:first-of-type > div {{
  box-sizing: border-box !important;
  flex: 0 0 16px !important;
  width: 16px !important;
  height: 16px !important;
  margin: 0 !important;
  border-radius: 50% !important;
  background: #FFFFFF !important;
  transform: translateX(0) !important;
  transition: transform 150ms ease !important;
}}
.st-key-theme_toggle_area [data-testid="stCheckbox"] label:has(input:checked) > div:first-of-type {{
  background: var(--primary) !important;
}}
.st-key-theme_toggle_area [data-testid="stCheckbox"] label:has(input:checked) > div:first-of-type > div {{
  transform: translateX(16px) !important;
}}
.st-key-theme_toggle_area [data-testid="stWidgetLabel"] {{
  display: flex !important;
  align-items: center !important;
  margin: 0 !important;
  padding: 0 !important;
  line-height: 1.25 !important;
}}
.st-key-theme_toggle_area [data-testid="stWidgetLabel"] p {{
  font-size: {ss.font_size}px !important;
  font-weight: 550;
  white-space: nowrap;
  line-height: 1.25 !important;
  margin: 0 !important;
  padding: 0 !important;
}}
.st-key-theme_toggle_area label:has(input:focus-visible) > div:first-of-type {{
  outline: 3px solid var(--primary-hover);
  outline-offset: 4px;
}}
.st-key-login_screen {{
  padding-top: 1.2rem;
}}
.st-key-login_screen [data-testid="stMarkdownContainer"] p {{
  line-height: 1.55 !important;
}}
.st-key-login_screen [data-testid="stTextInput"] {{
  margin-bottom: .45rem;
}}
.st-key-login_settings {{
  margin-top: 1.8rem;
  padding-top: 1.15rem;
  border-top: 1px solid var(--line);
}}
.st-key-login_settings [data-testid="stSelectbox"],
.st-key-login_settings [data-testid="stSlider"] {{
  margin-bottom: .8rem;
}}
.build-label {{
  opacity: .68;
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .04em;
  text-transform: uppercase;
  margin-top: -.3rem;
}}
.section-label {{
  opacity: .68;
  font-size: 12px;
  font-weight: 650;
  letter-spacing: .035em;
  text-transform: uppercase;
  margin: .25rem 0 .35rem 0;
}}
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


header_left, header_right = st.columns([7.4, 2.6], vertical_alignment="top")
with header_left:
    st.title("ICLOUD MAIL ASSISTANT")
    st.markdown(
        f'<div class="build-label">TEST BUILD {VERSION} · fix/icloud-delete</div>',
        unsafe_allow_html=True,
    )
with header_right:
    with st.container(key="theme_toggle_area"):
        theme_switch = ss.pop("theme_switch", None)
        if theme_switch is None:
            # Reflect the theme the browser is actually showing.
            ss.dark_theme = st.context.theme.type == "dark"
        st.toggle(
            T("Тёмная тема", "Dark theme"),
            key="dark_theme",
            on_change=request_theme_switch,
        )
        if theme_switch:
            st.html(
                THEME_SWITCH_JS % json.dumps(theme_switch),
                unsafe_allow_javascript=True,
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
            sessions.revoke(ss.get("session_token"))
            lang = ss.language
            for key in list(ss):
                del ss[key]
            ss.language = lang
            ss.forget_cookie = True
            st.rerun()
        st.caption(f"v{VERSION}")


@st.cache_resource
def signed_in_sessions():
    # One store per server process; see session_store.py for the security model.
    return SessionStore()


def write_cookie(token):
    # Streamlit cannot set cookies from Python, so the page sets it. A session
    # cookie (no Expires/Max-Age); an empty token deletes it.
    value = f"{COOKIE}={token}; Path=/; SameSite=Strict" + ("" if token else "; Max-Age=0")
    st.html(
        f"<script>document.cookie = {json.dumps(value)}"
        " + (location.protocol === 'https:' ? '; Secure' : '');</script>",
        unsafe_allow_javascript=True,
    )


# Restore a sign-in after a browser refresh. st.context.cookies holds the
# cookies sent when this browser session connected.
sessions = signed_in_sessions()
cookie_token = st.context.cookies.get(COOKIE)
if ss.get("session_token"):
    sessions.lookup(ss.session_token)  # restart the idle timer
elif cookie_token and not ss.get("account") and not ss.get("job"):
    restored = sessions.lookup(cookie_token)
    if restored:
        ss.account, ss.password = restored
        ss.session_token = cookie_token
    else:
        ss.forget_cookie = True

if ss.get("account"):
    sidebar()
    if ss.get("session_token") and cookie_token != ss.session_token:
        with st.sidebar:  # keeps the empty script element out of the page layout
            write_cookie(ss.session_token)
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
        ss.session_token = sessions.create(ss.account, ss.password)
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
    with st.container(key="login_screen"):
        st.subheader(T("Наведи порядок в почте iCloud", "Clean up your iCloud inbox"))
        st.write(
            T(
                "Находи рассылки, выбирай несколько компаний, отписывайся и переноси ненужные письма в Корзину. Перед удалением ты выбираешь конкретные письма.",
                "Find mailings, select multiple companies, unsubscribe and move unwanted messages to Trash. Review individual messages before deleting.",
            )
        )

        st.markdown(
            T(
                "**1. Введи адрес почты iCloud.**",
                "**1. Enter your iCloud email address.**",
            )
        )
        account = st.text_input(
            T("Email iCloud", "iCloud email"),
            key="email_input",
            placeholder="name@icloud.com",
        )

        st.markdown(
            T(
                "**2. Создай пароль приложения.**",
                "**2. Create an app-specific password.**",
            )
        )
        st.write(
            T(
                "В аккаунте Apple: «Вход и безопасность» → «Пароли приложений» → создать пароль, например для Mail Assistant. Для этого нужна двухфакторная аутентификация.",
                "In your Apple Account: Sign-In and Security → App-Specific Passwords → generate a password, for example for Mail Assistant. Two-factor authentication is required.",
            )
        )
        st.link_button(
            T("Открыть аккаунт Apple", "Open Apple Account"),
            "https://account.apple.com/",
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
                "Пароль хранится только в памяти приложения до выхода или перезапуска и не записывается на диск. История и заголовки писем хранятся локально.",
                "Your password stays in app memory until you sign out or the app restarts, and is never written to disk. History and message headers are stored locally.",
            )
        )

        if st.button(
            T("Подключиться к iCloud", "Connect to iCloud"),
            type="primary",
            width="stretch",
            disabled=not (account.strip() and password.strip()),
        ):
            ss.pending_password = password
            start("login", authenticate, account.strip().lower(), password)

        with st.container(key="login_settings"):
            st.markdown(
                f'<div class="section-label">{T("Интерфейс", "Interface")}</div>',
                unsafe_allow_html=True,
            )
            st.selectbox(
                "Language / Язык",
                ["Русский", "English"],
                key="language",
                on_change=lambda: preferences.set("language", ss.language),
            )
            st.slider(
                T("Размер текста", "Text size"),
                min_value=14,
                max_value=20,
                step=1,
                key="font_size",
                on_change=save_font_size,
                help=T(
                    "Меняет размер текста во всём интерфейсе.",
                    "Changes text size throughout the interface.",
                ),
            )
    if ss.get("forget_cookie"):
        write_cookie("")
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
    if current:
        st.dataframe(pd.DataFrame({"Email": current}), hide_index=True, width="stretch")
    else:
        st.caption(T("Список пуст.", "The list is empty."))

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
    if rows:
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption(T("Действий пока не было.", "No actions yet."))
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
    scan = store.scan()
    read_filters = {
        "all": ("Все", "All"),
        "unread": ("Непрочитанные", "Unread"),
        "read": ("Прочитанные", "Read"),
    }
    read_state_available = bool(scan and scan["messages"]) and all(
        "unread" in m for m in scan["messages"]
    )
    stored_read_filter = store.get("read_filter", "all")
    if stored_read_filter not in read_filters or not read_state_available:
        stored_read_filter = "all"

    st.markdown(
        f'<div class="section-label">{T("Показывать письма", "Show messages")}</div>',
        unsafe_allow_html=True,
    )
    read_filter = st.radio(
        T("Показывать письма", "Show messages"),
        list(read_filters),
        index=list(read_filters).index(stored_read_filter),
        format_func=lambda k: T(*read_filters[k]),
        horizontal=True,
        key="read_filter_control",
        disabled=not read_state_available,
        on_change=reset_read_filter_selection,
        label_visibility="collapsed",
    )
    if not read_state_available:
        st.caption(
            T(
                "После первого нового сканирования здесь станет доступен фильтр «Все / Непрочитанные / Прочитанные».",
                "After one fresh scan, the All / Unread / Read filter will be available here.",
            )
        )
        read_filter = "all"
    store.set("read_filter", read_filter)

    st.markdown(
        f'<div class="section-label">{T("Сканирование", "Scan")}</div>',
        unsafe_allow_html=True,
    )
    limits = [500, 1000, 5000, 0]
    default = store.get("limit", 1000)
    scan_cols = st.columns([2.2, 1.4])
    limit = scan_cols[0].selectbox(
        T("Сколько последних писем сканировать", "How many recent emails to scan"),
        limits,
        index=limits.index(default) if default in limits else 1,
        format_func=lambda n: (
            T(f"Последние {n} писем", f"Latest {n} emails")
            if n
            else T("Все письма", "All emails")
        ),
        label_visibility="collapsed",
    )
    store.set("limit", limit)
    if scan_cols[1].button(
        T("Сканировать почту", "Scan inbox"),
        type="primary",
        width="stretch",
    ):
        start("scan", service.scan, store, ss.password, limit)

    batch = store.last_moves()
    eligible = [
        row
        for row in batch
        if row["state"] == "moved" and row["dest_uid"] and row["dest_validity"]
    ]

    if not scan:
        st.info(
            T(
                "Начни со сканирования. Письма при этом не изменяются.",
                "Start with a scan. This does not modify messages.",
            )
        )
    else:
        def view_messages(group):
            return [
                m
                for m in group["messages"]
                if service.matches_read_filter(m, read_filter)
            ]

        st.markdown(
            f'<div class="section-label">{T("Компании", "Companies")}</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True, key="company_table"):
            toolbar = st.columns([2.3, 1, 1, 1.5])
            sorts = {
                "count": ("Больше всего писем", "Most emails"),
                "date": ("Последнее письмо", "Latest email"),
                "name": ("Название", "Name"),
                "deleted": ("Удалено за 3 дня", "Deleted in the last 3 days"),
            }
            stored_sort = store.get("sort", "count")
            if stored_sort not in sorts:
                stored_sort = "count"
            sort = toolbar[0].selectbox(
                T("Сортировка", "Sort"),
                list(sorts),
                index=list(sorts).index(stored_sort),
                format_func=lambda k: T(*sorts[k]),
                label_visibility="collapsed",
                key="company_sort",
            )
            store.set("sort", sort)

            query = st.text_input(
                T("Поиск компании или email", "Search company or email"),
                placeholder=T("Поиск компании или email", "Search company or email"),
                label_visibility="collapsed",
                key="company_search",
            )

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

            version = ss.get("selection_version", 0)
            select_key = lambda g: f"company_{version}_{g['key']}"

            if toolbar[1].button(
                T("Выбрать всё", "Select all"),
                key="select_all_companies",
                width="stretch",
            ):
                for g in visible:
                    ss[select_key(g)] = not g["protected"]
            if toolbar[2].button(
                T("Снять всё", "Deselect all"),
                key="deselect_all_companies",
                width="stretch",
            ):
                for g in groups:
                    ss[select_key(g)] = False
                ss.chosen_companies = []
            if toolbar[3].button(
                T("Выбрать чёрный список", "Select blacklist"),
                key="select_blacklist_companies",
                width="stretch",
            ):
                for g in visible:
                    ss[select_key(g)] = g["black"] and not g["protected"]

            chosen = set(ss.get("chosen_companies", []))
            with st.container(height=430, key="company_list", border=False):
                with st.container(border=False, key="company_header"):
                    heads = st.columns([4, 1, 1, 2, 1.35], vertical_alignment="center")
                    for index, (col, label) in enumerate(
                        zip(
                            heads,
                            [
                                T("Компания", "Company"),
                                T("Писем", "Emails"),
                                T("Непроч.", "Unread"),
                                T("Последнее", "Latest"),
                                T("Просмотр", "Preview"),
                            ],
                        )
                    ):
                        align_class = "" if index == 0 else " center"
                        col.markdown(
                            f'<div class="company-col-head{align_class}">{label}</div>',
                            unsafe_allow_html=True,
                        )
                for row_index, g in enumerate(visible):
                    if select_key(g) not in ss:
                        ss[select_key(g)] = g["key"] in chosen
                    with st.container(
                        border=False,
                        key=f"company_row_{row_index}_{g['key']}",
                    ):
                        cols = st.columns(
                            [4, 1, 1, 2, 1.35],
                            vertical_alignment="center",
                        )
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
                        cols[1].markdown(
                            f'<div class="company-cell center"><strong>{len(shown_messages)}</strong></div>',
                            unsafe_allow_html=True,
                        )
                        cols[2].markdown(
                            f'<div class="company-cell center">{"<strong>" + str(unread_count) + "</strong>" if unread_count else "0"}</div>',
                            unsafe_allow_html=True,
                        )
                        cols[3].markdown(
                            f'<div class="company-cell center">{date(latest_shown).split(" ")[0]}</div>',
                            unsafe_allow_html=True,
                        )
                        if cols[4].button(
                            T("Письма", "Emails"),
                            key="view_" + g["key"],
                            width="stretch",
                        ):
                            ss.view_company = g["key"]
                            ss.view_read_filter = read_filter
                            ss.message_content = None
                if not visible:
                    st.caption(T("Ничего не найдено.", "No matches."))

        selected = [g for g in groups if g["key"] in chosen]
        ss.chosen_companies = sorted(g["key"] for g in selected)

        st.markdown(
            f'<div class="section-label">{T("Действие", "Action")}</div>',
            unsafe_allow_html=True,
        )
        with st.container(border=True, key="action_panel"):
            st.caption(
                T(
                    f"Выбрано компаний: {len(selected)}",
                    f"Selected companies: {len(selected)}",
                )
            )
            hidden_count = len(
                [g for g in selected if g["key"] not in {v["key"] for v in visible}]
            )
            if hidden_count:
                st.caption(
                    T(
                        f"Скрыто текущим фильтром: {hidden_count}.",
                        f"Hidden by the current filter: {hidden_count}.",
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
                width="stretch",
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
                st.divider()
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
                    def message_key(uid):
                        return f"{prefix}_uid_{uid}"

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
                        width="stretch",
                    ):
                        for m in targets:
                            ss[message_key(m["uid"])] = True
                        ss.pop(prefix + "_restore", None)
                        st.rerun()
                    if b.button(
                        T("Снять все отметки писем", "Deselect all messages"),
                        key=prefix + "_deselect_all",
                        width="stretch",
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

                def queue_execute():
                    ss.execute_request = {
                        "preview": preview,
                        "selected_uids": list(ss.get(prefix + "_selection", [])),
                    }

                st.button(
                    label,
                    key="execute_action",
                    type="primary",
                    width="stretch",
                    disabled=not selected_uids and not preview["unsubs"],
                    on_click=queue_execute,
                )

        st.caption(
            T(
                f"Сохранено {date(scan['stamp'])} · в выборке {len(scan['messages'])} из {scan['total']} писем.",
                f"Saved {date(scan['stamp'])} · sample: {len(scan['messages'])} of {scan['total']} emails.",
            )
        )

    if st.button(
        T("↩ Вернуть последнее удаление", "↩ Undo last deletion"),
        key="undo_last_delete",
        disabled=not eligible,
    ):
        start("undo", service.undo, store, ss.password)
    if batch and len(eligible) != len(batch):
        st.caption(
            T(
                "Возврат доступен только для подтверждённо перемещённых писем с сохранённым новым UID.",
                "Undo is available only for confirmed moves with a saved destination UID.",
            )
        )


def close_messages():
    ss.pop("view_company", None)
    ss.pop("message_content", None)


@st.dialog(
    T("Письма компании", "Company emails"), width="large", on_dismiss=close_messages
)
def show_company_messages(group):
    st.subheader(group["name"])
    read_filter = ss.get("view_read_filter", "all")
    messages = sorted(
        [
            m
            for m in group["messages"]
            if service.matches_read_filter(m, read_filter)
        ],
        key=lambda m: (m["received"], int(m["uid"])),
        reverse=True,
    )
    st.caption(
        T(
            "Письма из последнего сканирования; это не выбор на удаление.",
            "Messages from the last scan; this is not a deletion selection.",
        )
        + " · "
        + T(
            {"all": "все", "unread": "непрочитанные", "read": "прочитанные"}.get(read_filter, "все"),
            {"all": "all", "unread": "unread", "read": "read"}.get(read_filter, "all"),
        )
    )
    with st.expander(T("Адреса и статистика", "Addresses and statistics")):
        st.text(", ".join(group["senders"]))
        st.write(T(*STATUS[group["status"]]))
        st.caption(
            T(
                f"Удалено за 3 дня: {group['recent']} · новых после отписки: {group['after']}. Счётчик ограничен сканированиями.",
                f"Deleted in 3 days: {group['recent']} · new after unsubscribe: {group['after']}. Counts are limited to scans.",
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

