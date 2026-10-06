"""Native Streamlit inbox browser; persistent selection is not widget state."""

import math
import pandas as pd
import streamlit as st
import mail_service as service
from all_messages import browse, deletion_preview


def reset_confirmation():
    st.session_state.pop("inbox_confirmation", None)
    st.session_state.pop("inbox_allow_white", None)


def reconcile(store, scan):
    ss = st.session_state
    epoch = (store.account, scan["validity"] if scan else None)
    if ss.get("inbox_epoch") != epoch:
        ss.inbox_epoch = epoch
        ss.inbox_selected = []
        ss.inbox_page = 0
        ss.pop("inbox_open_uid", None)
        reset_confirmation()
    live = {m["uid"] for m in scan["messages"]} if scan else set()
    ss.inbox_selected = [u for u in ss.get("inbox_selected", []) if u in live]
    if ss.get("inbox_open_uid") not in live:
        ss.pop("inbox_open_uid", None)


def change_selection(uid, key):
    ss = st.session_state
    chosen = set(ss.get("inbox_selected", []))
    if ss[key]:
        chosen.add(uid)
    else:
        chosen.discard(uid)
    ss.inbox_selected = sorted(chosen, key=int)
    reset_confirmation()


def select_page(uids):
    ss = st.session_state
    ss.inbox_selected = sorted(set(ss.get("inbox_selected", [])) | set(uids), key=int)
    reset_confirmation()


def clear_selection():
    st.session_state.inbox_selected = []
    reset_confirmation()


def reset_page():
    st.session_state.inbox_page = 0


def turn_page(offset):
    st.session_state.inbox_page += offset


def open_message(uid):
    ss = st.session_state
    ss.inbox_open_uid = uid
    # Content is scoped to the currently opened message and mailbox epoch.
    ss.pop("message_content", None)


def close_reader():
    st.session_state.pop("inbox_open_uid", None)
    st.session_state.pop("message_content", None)


def confirm_deletion():
    ss = st.session_state
    preview = ss.pop("inbox_confirmation")
    ss.execute_request = {
        "preview": preview,
        "selected_uids": [m["uid"] for m in preview["targets"]],
    }
    ss.pop("inbox_allow_white", None)


def render(store, start, T, date, show_error):
    ss = st.session_state
    st.subheader(T("Все письма", "All emails"))
    st.caption(
        T(
            "Входящие iCloud · просмотр и удаление отдельных писем",
            "iCloud Inbox · read and delete individual emails",
        )
    )
    scan = store.scan()
    reconcile(store, scan)
    refresh, undo = st.columns([3, 2])
    if refresh.button(
        T("Загрузить / обновить все письма", "Load / refresh all emails"),
        key="inbox_refresh",
        width="stretch",
    ):
        close_reader()
        reset_confirmation()
        start("scan", service.scan, store, ss.password, 0)
    if undo.button(
        T("Вернуть последнее удаление", "Undo last deletion"),
        key="inbox_undo",
        width="stretch",
        disabled=not store.last_moves(),
    ):
        close_reader()
        reset_confirmation()
        start("undo", service.undo, store, ss.password)
    if not scan:
        st.info(
            T(
                "Загрузи письма, чтобы открыть список Входящих.",
                "Load emails to open the inbox list.",
            )
        )
        return
    messages = scan["messages"]
    st.caption(
        T(
            f"Загружено: {len(messages)} из {scan['total']} · обновлено: {date(scan['stamp'])}",
            f"Loaded: {len(messages)} of {scan['total']} · updated: {date(scan['stamp'])}",
        )
    )
    if len(messages) < scan["total"]:
        st.info(
            T(
                "Сейчас показана сохранённая выборка. Нажми «Загрузить / обновить все письма», чтобы получить все Входящие без ограничения.",
                "This is a saved sample. Click Load / refresh all emails to load the entire Inbox without a limit.",
            )
        )
    st.text_input(
        T(
            "Поиск по теме, имени или email отправителя",
            "Search subject, name or sender email",
        ),
        key="inbox_search",
        on_change=reset_page,
    )
    filters, order, size = st.columns([3, 2, 1])
    labels = {
        "all": ("Все", "All"),
        "unread": ("Непрочитанные", "Unread"),
        "read": ("Прочитанные", "Read"),
    }
    read_filter = filters.radio(
        T("Показывать", "Show"),
        list(labels),
        horizontal=True,
        format_func=lambda v: T(*labels[v]),
        key="inbox_filter",
        on_change=reset_page,
        disabled=not messages or any("unread" not in m for m in messages),
    )
    sorts = {
        "newest": ("Сначала новые", "Newest first"),
        "oldest": ("Сначала старые", "Oldest first"),
        "sender": ("По отправителю", "By sender"),
        "subject": ("По теме", "By subject"),
    }
    sort = order.selectbox(
        T("Сортировка", "Sort"),
        list(sorts),
        format_func=lambda v: T(*sorts[v]),
        key="inbox_sort",
        on_change=reset_page,
    )
    per_page = size.selectbox(
        T("На странице", "Per page"),
        [25, 50, 100],
        key="inbox_size",
        on_change=reset_page,
    )
    filtered = browse(messages, ss.get("inbox_search", ""), read_filter, sort)
    page_count = max(1, math.ceil(len(filtered) / per_page))
    ss.inbox_page = min(max(0, ss.get("inbox_page", 0)), page_count - 1)
    visible = filtered[ss.inbox_page * per_page : (ss.inbox_page + 1) * per_page]
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    selected = set(ss.inbox_selected)
    visible_uids = {m["uid"] for m in visible}
    st.caption(
        T(
            f"Найдено: {len(filtered)} · выбрано: {len(selected)} · вне страницы: {len(selected - visible_uids)}",
            f"Found: {len(filtered)} · selected: {len(selected)} · outside this page: {len(selected - visible_uids)}",
        )
    )
    select, clear, review = st.columns([2, 1.5, 2.5])
    select.button(
        T("Выбрать страницу", "Select page"),
        key="inbox_select_page",
        on_click=select_page,
        args=([m["uid"] for m in visible if m["sender"] not in white],),
        disabled=not visible,
        width="stretch",
        help=T(
            "Пропускает отправителей из белого списка.", "Skips whitelisted senders."
        ),
    )
    clear.button(
        T("Снять все отметки", "Clear selection"),
        key="inbox_clear",
        on_click=clear_selection,
        disabled=not selected,
        width="stretch",
    )
    if review.button(
        T("Просмотреть и подтвердить", "Review and confirm"),
        key="inbox_review",
        disabled=not selected,
        width="stretch",
    ):
        reset_confirmation()
        # Preview can include protected messages, but permission must be given
        # on this frozen confirmation before it can be queued for execution.
        try:
            ss.inbox_confirmation = deletion_preview(
                store, scan["validity"], selected, True
            )
            ss.inbox_confirmation["allow_white"] = False
        except service.MailError as exc:
            show_error(str(exc))
    confirmation = ss.get("inbox_confirmation")
    if confirmation:
        with st.container(border=True):
            st.subheader(T("Подтверждение удаления", "Confirm deletion"))
            st.write(
                T(
                    f"В Корзину будут перемещены только эти письма: {len(confirmation['targets'])}.",
                    f"Only these emails will be moved to Trash: {len(confirmation['targets'])}.",
                )
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            T("Тема", "Subject"): m["subject"],
                            T("Отправитель", "Sender"): m["sender"],
                            T("Дата", "Date"): date(m["received"]),
                        }
                        for m in confirmation["targets"]
                    ]
                ),
                hide_index=True,
                width="stretch",
                height=min(300, 40 + 35 * len(confirmation["targets"])),
            )
            protected = bool(set(confirmation["senders"]) & white)
            if protected:
                st.warning(
                    T(
                        "В выборе есть письма отправителей из белого списка.",
                        "The selection includes emails from whitelisted senders.",
                    )
                )
                confirmation["allow_white"] = st.checkbox(
                    T(
                        "Я разрешаю удалить выбранные письма из белого списка",
                        "Allow deletion of selected whitelisted emails",
                    ),
                    key="inbox_allow_white",
                )
            yes, no = st.columns(2)
            yes.button(
                T(
                    f"Переместить в Корзину: {len(confirmation['targets'])} писем",
                    f"Move to Trash: {len(confirmation['targets'])} emails",
                ),
                key="inbox_confirm",
                type="primary",
                width="stretch",
                disabled=protected and not confirmation["allow_white"],
                on_click=confirm_deletion,
            )
            no.button(
                T("Отмена", "Cancel"),
                key="inbox_cancel",
                width="stretch",
                on_click=reset_confirmation,
            )

    item = next((m for m in messages if m["uid"] == ss.get("inbox_open_uid")), None)
    if item:
        with st.container(border=True):
            st.subheader(T("Просмотр письма", "Message reader"))
            st.text(item["subject"] or T("Без темы", "No subject"))
            st.text(
                f"{item['name']} <{item['sender']}> · {item['date'] or date(item['received'])}"
            )
            st.caption(
                T(
                    "Письмо не помечается прочитанным. Картинки и трекеры не загружаются.",
                    "Reading does not mark it as read. Images and trackers are not loaded.",
                )
            )
            content = ss.get("message_content")
            if content and content["uid"] == item["uid"]:
                if content["truncated"]:
                    st.caption(
                        T(
                            "Показано начало большого письма.",
                            "Showing the beginning of a large message.",
                        )
                    )
                with st.container(height=320, border=False):
                    st.text(
                        content["text"]
                        or T(
                            "В письме нет доступного текста.",
                            "No readable text in this email.",
                        )
                    )
            elif st.button(
                T("Загрузить текст письма", "Load message text"), key="inbox_read"
            ):
                start(
                    "read",
                    service.read_message,
                    store,
                    ss.password,
                    scan["validity"],
                    item,
                )
            st.button(
                T("Закрыть письмо", "Close message"),
                key="inbox_close",
                on_click=close_reader,
            )

    widths = [0.5, 4, 2.7, 1.7]
    with st.container(border=True, height=620, key="inbox_list"):
        header = st.columns(widths, vertical_alignment="center")
        for column, label in zip(
            header[1:],
            [
                T("Тема / открыть", "Subject / open"),
                T("Отправитель", "Sender"),
                T("Получено", "Received"),
            ],
        ):
            column.caption(label)
        if not visible:
            st.info(
                T(
                    (
                        "Писем нет."
                        if not messages
                        else "По этим условиям писем не найдено."
                    ),
                    "No emails." if not messages else "No emails match these filters.",
                )
            )
        for m in visible:
            cols = st.columns(widths, vertical_alignment="center")
            key = "inbox_uid_" + m["uid"]
            ss[key] = m["uid"] in selected
            cols[0].checkbox(
                T("Выбрать письмо", "Select email"),
                key=key,
                label_visibility="collapsed",
                on_change=change_selection,
                args=(m["uid"], key),
            )
            subject = m["subject"] or T("Без темы", "No subject")
            prefix = ("● " if m.get("unread") else "") + (
                "🔒 " if m["sender"] in white else ""
            )
            cols[1].button(
                prefix + subject,
                key="inbox_open_" + m["uid"],
                width="stretch",
                on_click=open_message,
                args=(m["uid"],),
                help=subject,
            )
            cols[2].text(m["name"] or m["sender"])
            if m["name"]:
                cols[2].caption(m["sender"])
            cols[3].text(date(m["received"]))
    prev, position, nxt = st.columns([1, 3, 1], vertical_alignment="center")
    prev.button(
        T("Назад", "Previous"),
        key="inbox_previous",
        on_click=turn_page,
        args=(-1,),
        disabled=ss.inbox_page == 0,
        width="stretch",
    )
    position.caption(
        T(
            f"Страница {ss.inbox_page + 1} из {page_count} · ● непрочитанное · 🔒 белый список",
            f"Page {ss.inbox_page + 1} of {page_count} · ● unread · 🔒 whitelist",
        )
    )
    nxt.button(
        T("Далее", "Next"),
        key="inbox_next",
        on_click=turn_page,
        args=(1,),
        disabled=ss.inbox_page + 1 >= page_count,
        width="stretch",
    )
