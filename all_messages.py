"""Inbox browsing and exact-message deletion, independent of the UI."""

from copy import deepcopy
import mail_service as service


def browse(messages, query="", read_filter="all", sort="newest"):
    query = query.strip().casefold()
    matches = [
        m
        for m in messages
        if service.matches_read_filter(m, read_filter)
        and (
            not query
            or query
            in " ".join(
                str(m.get(key, "")) for key in ("subject", "sender", "name")
            ).casefold()
        )
    ]
    if sort in ("sender", "subject"):
        return sorted(
            matches,
            key=lambda m: (
                str(m.get(sort, "")).casefold(),
                -m["received"],
                -int(m["uid"]),
            ),
        )
    return sorted(
        matches, key=lambda m: (m["received"], int(m["uid"])), reverse=sort != "oldest"
    )


def deletion_preview(store, validity, selected_uids, allow_white=False):
    """Freeze only selected messages; never widen selection to a sender/group."""
    scan = store.scan()
    if not scan or scan["validity"] != validity:
        raise service.MailError("stale")
    selected = set(selected_uids)
    targets = [m for m in scan["messages"] if m["uid"] in selected]
    if not targets:
        raise service.MailError("no_messages")
    if {m["uid"] for m in targets} != selected:
        raise service.MailError("stale")
    senders = sorted({m["sender"] for m in targets})
    white = {s for s, r in store.rules().items() if r["policy"] == "white"}
    if set(senders) & white and not allow_white:
        raise service.MailError("protected")
    return dict(
        keys=[],
        mode="delete_only",
        scope="all",
        read_filter="all",
        allow_white=allow_white,
        validity=validity,
        targets=deepcopy(targets),
        unsubs=[],
        senders=senders,
        companies=senders,
        found=len(targets),
        excluded=0,
    )
