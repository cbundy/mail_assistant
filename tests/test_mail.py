import contextlib
import json
import imaplib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
import mail_service as svc
from storage import Store


def msg(uid="10", sender="news@auchan.pl", name="Auchan", kind="promo", received=100):
    return dict(
        uid=uid,
        sender=sender,
        name=name,
        kind=kind,
        received=received,
        subject="Sale",
        date="",
        message_id="<" + uid + "@test>",
        list_id="",
        urls=["https://example.com/unsub/" + uid],
        one_click=True,
    )


class FakeIMAP:
    def __init__(self, messages=None, caps=(b"MOVE", b"UIDPLUS"), validity="123"):
        self.messages = messages if messages is not None else [msg()]
        self.capabilities = caps
        self.validity = validity
        self.mapping = None
        self.calls = []
        self.folder = "INBOX"
        self.fail_at = None
        self.removed = set()

    def select(self, folder, readonly=True):
        self.folder = folder.strip('"')
        return "OK", [b"1"]

    def response(self, key):
        if key == "UIDVALIDITY":
            return key, [self.validity.encode()]
        if key == "COPYUID":
            v = self.mapping
            self.mapping = None
            return key, [v]
        return key, [None]

    def list(self):
        return "OK", [b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"']

    def uid(self, command, *args):
        command = command.upper()
        self.calls.append((command, args))
        if self.fail_at == command:
            raise TimeoutError("simulated disconnect")
        if command == "SEARCH":
            if len(args) >= 3 and args[1] == "UID":
                return "OK", [
                    b" ".join(
                        m["uid"].encode()
                        for m in self.messages
                        if m["uid"] == args[2] and m["uid"] not in self.removed
                    )
                ]
            return "OK", [b" ".join(m["uid"].encode() for m in self.messages)]
        if command == "FETCH":
            chosen = [m for m in self.messages if m["uid"] in args[0].split(",")]
            return "OK", [
                (
                    f'1 (UID {m["uid"]} INTERNALDATE "05-Oct-2026 10:20:30 +0000"'.encode(),
                    f'From: {m["name"]} <{m["sender"]}>\r\nSubject: {m["subject"]}\r\nMessage-ID: {m["message_id"]}\r\nList-Unsubscribe: <https://example.com/unsub>\r\nList-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n\r\n'.encode(),
                )
                for m in chosen
            ]
        if command in ("MOVE", "COPY"):
            self.mapping = f"456 {args[0]} {int(args[0])+100}".encode()
        if command in ("MOVE", "EXPUNGE"):
            self.removed.add(args[0])
        return "OK", [b"done"]


class MailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.store = Store("one@icloud.com", self.path)
        self.store.save_scan("123", 1, [msg()])

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, fake):
        @contextlib.contextmanager
        def conn(*args):
            yield fake

        return patch.object(svc, "connection", conn)

    def preview(self, mode="delete_only", targets=None):
        return dict(
            mode=mode,
            senders=["news@auchan.pl"],
            allow_white=False,
            validity="123",
            companies=["Auchan"],
            targets=targets if targets is not None else [msg()],
            unsubs=[msg()] if mode != "delete_only" else [],
        )

    def run_action(self, fake, preview=None, uids=None):
        with self.ctx(fake):
            return svc.execute(
                self.store,
                "not-persisted",
                preview or self.preview(),
                uids if uids is not None else ["10"],
                lambda *a: None,
            )

    def test_account_isolation(self):
        other = Store("two@icloud.com", self.path)
        self.store.policy(["news@auchan.pl"], "white")
        self.store.set("sort", "name")
        self.assertIsNone(other.scan())
        self.assertEqual(other.rules(), {})
        self.assertIsNone(other.get("sort"))
        self.assertEqual(other.history(), [])


    def test_operation_trace_is_persisted_and_running_can_be_recovered(self):
        op = self.store.operation("delete_only", {"companies": ["Auchan"]})
        self.store.trace(op, "connect_start")
        row = self.store.history()[0]
        detail = json.loads(row["detail"])
        self.assertEqual(detail["last_step"], "connect_start")
        self.assertEqual(detail["trace"][-1]["step"], "connect_start")
        self.assertEqual(self.store.recover_running(), 1)
        row = self.store.history()[0]
        detail = json.loads(row["detail"])
        self.assertEqual(row["status"], "interrupted")
        self.assertEqual(detail["last_step"], "interrupted_on_restart")

    def test_empty_delete_is_not_success(self):
        with self.assertRaisesRegex(svc.MailError, "no_messages"):
            self.run_action(FakeIMAP(), uids=[])
        self.assertFalse(self.store.history())

    def test_move_ok_but_source_remains_is_not_success(self):
        fake = FakeIMAP()
        original = fake.uid

        def uid(command, *args):
            result = original(command, *args)
            if command == "MOVE":
                fake.removed.clear()
            return result

        fake.uid = uid
        result = self.run_action(fake)
        self.assertEqual(result["error"], "move_unconfirmed")
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.store.last_moves()[0]["state"], "uncertain")
        self.assertEqual(len(self.store.scan()["messages"]), 1)

    def test_error_diagnostics_do_not_include_private_reply(self):
        detail = svc.error_details(
            imaplib.IMAP4.error(
                "UID command error: BAD private-password mail@example.com"
            )
        )
        self.assertIn("command=UID", detail)
        self.assertIn("response=BAD", detail)
        self.assertNotIn("private-password", detail)
        self.assertNotIn("mail@example.com", detail)

    def test_email_html_is_only_text(self):
        raw = b'Content-Type: text/html; charset=utf-8\r\n\r\n<head>hidden</head><p>Hello &amp; welcome</p><script>evil()</script><img src="https://tracker.test/x"><p>World</p>'
        text = svc.message_text(raw)
        self.assertIn("Hello & welcome", text)
        self.assertIn("World", text)
        self.assertNotIn("evil", text)
        self.assertNotIn("tracker", text)
        self.assertNotIn("hidden", text)

    def test_read_uses_peek_and_readonly(self):
        fake = FakeIMAP()
        with self.ctx(fake):
            value = svc.read_message(self.store, "pass", "123", msg(), lambda *a: None)
        self.assertEqual(value["uid"], "10")
        self.assertTrue(
            any("BODY.PEEK[]<0." in a[1] for c, a in fake.calls if c == "FETCH")
        )
        self.assertFalse(
            any(c in ("STORE", "MOVE", "COPY", "EXPUNGE") for c, a in fake.calls)
        )

    def test_white_black_mutually_exclusive(self):
        self.store.policy(["a@b.c"], "white")
        self.store.policy(["a@b.c"], "black")
        self.assertEqual(self.store.rules()["a@b.c"]["policy"], "black")

    def test_manual_groups_persist(self):
        self.store.save_scan(
            "123",
            3,
            [
                msg(),
                msg("11", "b@other.com", "Other"),
                msg("12", "c@auchan.pl", "Auchan"),
            ],
        )
        self.store.group(["news@auchan.pl", "b@other.com"], "Merged")
        groups = svc.companies(Store(self.store.account, self.path))
        self.assertEqual(len(groups), 2)
        self.assertTrue(
            any(g["name"] == "Merged" and len(g["senders"]) == 2 for g in groups)
        )
        self.store.group(["news@auchan.pl"], split=True)
        self.assertEqual(len(svc.companies(self.store)), 3)

    def test_brands_do_not_mix(self):
        self.store.save_scan(
            "123",
            3,
            [
                msg(),
                msg("11", "n@myheritage.com", "MyHeritage"),
                msg("12", "a@apple.com", "Apple ID"),
            ],
        )
        self.assertEqual(
            {g["name"] for g in svc.companies(self.store)},
            {"Auchan", "MyHeritage", "Apple"},
        )

    def test_seen_counter_deduplicates_and_survives_deletion(self):
        with patch("storage.time.time", return_value=50):
            self.store.unsubscribe("news@auchan.pl", "requested")
        self.store.save_scan("123", 1, [msg()])
        self.store.save_scan("123", 1, [msg()])
        self.assertEqual(self.store.after_unsubscribe()["news@auchan.pl"], 1)
        self.store.remove_cached("123", "10")
        self.assertEqual(self.store.after_unsubscribe()["news@auchan.pl"], 1)

    def test_preview_matches_exact_address(self):
        fake = FakeIMAP(
            [msg(), msg("11", "other@auchan.pl", "Auchan"), msg("12", "bad@x.com")]
        )
        with self.ctx(fake):
            p = svc.prepare(
                self.store,
                "pass",
                ["auchan"],
                "delete_only",
                "all",
                False,
                lambda *a: None,
            )
        self.assertEqual([m["uid"] for m in p["targets"]], ["10"])

    def test_unchecked_message_never_moved(self):
        fake = FakeIMAP([msg(), msg("11")])
        result = self.run_action(fake, self.preview(targets=[msg(), msg("11")]), ["11"])
        self.assertEqual(result["moved"], 1)
        self.assertEqual([a[0] for c, a in fake.calls if c == "MOVE"], ["11"])

    def test_delete_only_never_unsubscribes(self):
        with patch.object(svc, "one_click") as one:
            self.run_action(FakeIMAP())
            one.assert_not_called()

    def test_unsubscribe_only_never_moves(self):
        fake = FakeIMAP()
        with patch.object(svc, "one_click", return_value=("requested", "OK")):
            result = self.run_action(fake, self.preview("unsubscribe_only", []), [])
        self.assertEqual(result["requested"], 1)
        self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_whitelist_rechecked_at_execution(self):
        self.store.policy(["news@auchan.pl"], "white")
        with self.assertRaisesRegex(svc.MailError, "protected"):
            self.run_action(FakeIMAP())

    def test_uidvalidity_change_prevents_all_effects(self):
        fake = FakeIMAP(validity="999")
        with patch.object(svc, "one_click") as one:
            result = self.run_action(fake, self.preview("both"))
            one.assert_not_called()
        self.assertEqual(result["error"], "stale")
        self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_move_records_new_uid_and_quotes_trash(self):
        fake = FakeIMAP()
        result = self.run_action(fake)
        row = self.store.last_moves()[0]
        self.assertEqual(
            (row["dest_uid"], row["dest_validity"], row["state"]),
            ("110", "456", "moved"),
        )
        self.assertIn(("MOVE", ("10", '"Deleted Messages"')), fake.calls)
        self.assertFalse(any(command == "COPY" for command, _ in fake.calls))
        self.assertEqual(result["moved"], 1)
        self.assertEqual(len(self.store.scan()["messages"]), 0)
        self.assertEqual(svc.companies(self.store)[0]["recent"], 1)
        self.assertNotIn(
            "not-persisted", self.path.read_bytes().decode(errors="ignore")
        )

    def test_uidplus_fallback_when_move_is_explicitly_rejected(self):
        fake = FakeIMAP()
        original = fake.uid

        def uid(command, *args):
            if command.upper() == "MOVE":
                fake.calls.append(("MOVE", args))
                return "NO", [b"move rejected"]
            return original(command, *args)

        fake.uid = uid
        result = self.run_action(fake)
        self.assertEqual(result["moved"], 1)
        self.assertIn(("COPY", ("10", '"Deleted Messages"')), fake.calls)
        self.assertIn(("EXPUNGE", ("10",)), fake.calls)

    def test_uidplus_only_uses_targeted_expunge(self):
        fake = FakeIMAP(caps=(b"UIDPLUS",))
        self.run_action(fake)
        self.assertIn(("EXPUNGE", ("10",)), fake.calls)

    def test_no_capabilities_stops_without_copy(self):
        fake = FakeIMAP(caps=())
        result = self.run_action(fake)
        self.assertEqual(result["error"], "unsafe_move")
        self.assertFalse(any(c == "COPY" for c, a in fake.calls))

    def test_disconnect_preserves_uncertain_not_success(self):
        fake = FakeIMAP()
        fake.fail_at = "MOVE"
        result = self.run_action(fake)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(result["error"], "network")
        self.assertEqual(self.store.last_moves()[0]["state"], "uncertain")
        self.assertEqual(self.store.history()[0]["status"], "partial")

    def test_partial_copy_not_counted_as_delete(self):
        fake = FakeIMAP(caps=(b"UIDPLUS",))
        fake.fail_at = "STORE"
        result = self.run_action(fake)
        self.assertEqual(result["moved"], 0)
        self.assertEqual(self.store.last_moves()[0]["state"], "copied")
        self.assertEqual(self.store.last_moves()[0]["dest_uid"], "110")

    def test_undo_uses_destination_uid(self):
        self.run_action(FakeIMAP())
        m = msg("110")
        m["message_id"] = "<10@test>"
        fake = FakeIMAP([m], validity="456")
        with self.ctx(fake):
            result = svc.undo(self.store, "pass", lambda *a: None)
        self.assertEqual(result["restored"], 1)
        self.assertIn(("MOVE", ("110", '"INBOX"')), fake.calls)
        self.assertIsNone(self.store.scan())
        self.assertEqual(self.store.last_moves(), [])

    def test_undo_refuses_changed_trash(self):
        self.run_action(FakeIMAP())
        fake = FakeIMAP(validity="789")
        with self.ctx(fake):
            result = svc.undo(self.store, "pass", lambda *a: None)
        self.assertEqual(result["error"], "stale")
        self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_one_click_flag_bound_to_message(self):
        older = msg()
        older["one_click"] = True
        newer = msg("11", received=200)
        newer["one_click"] = False
        self.assertFalse(svc.unsubscribe_targets([older, newer])[0]["one_click"])

    def test_distinct_mailing_lists_kept(self):
        a = msg()
        b = msg("11")
        a["list_id"] = "offers"
        b["list_id"] = "loyalty"
        self.assertEqual(len(svc.unsubscribe_targets([a, b])), 2)

    def test_redirect_is_not_success_or_followed(self):
        session = Mock()
        session.__enter__ = Mock(return_value=session)
        session.__exit__ = Mock(return_value=False)
        session.post.return_value.status_code = 302
        with patch.object(
            svc, "is_safe_public_http_url", return_value=True
        ), patch.object(svc.requests, "Session", return_value=session):
            status, _ = svc.one_click(msg())
        self.assertEqual(status, "failed")
        self.assertFalse(session.post.call_args.kwargs["allow_redirects"])

    def test_account_lock_blocks_parallel_actions(self):
        with svc.account_lock(self.store.account):
            with self.assertRaisesRegex(svc.MailError, "busy"):
                with svc.account_lock(self.store.account):
                    pass

    def test_quoted_input_rejects_command_injection(self):
        with self.assertRaises(svc.MailError):
            svc.quote("a\r\nDELETE INBOX")


if __name__ == "__main__":
    unittest.main()
