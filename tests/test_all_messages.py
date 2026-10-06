import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
import storage
import mail_service as svc
from all_messages import deletion_preview
from test_mail import msg, FakeIMAP


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = patch.object(storage, "DB_PATH", Path(self.tmp.name) / "inbox.db")
        self.patch.start()
        self.store = storage.Store("test@icloud.com")
        self.messages = [
            msg(str(u), received=u, unread=u % 2 == 0) for u in range(10, 40)
        ]
        for item in self.messages:
            item["subject"] = "Letter " + item["uid"]
        self.store.save_scan("123", len(self.messages), self.messages)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def app(self):
        at = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10
        )
        at.session_state["account"] = self.store.account
        at.session_state["password"] = "test-only"
        at.session_state["page"] = "mail"
        at.run()
        self.assertFalse(at.exception)
        return at

    def finish(self, at):
        job = at.session_state.get("job")
        if job:
            job.thread.join(5)
        at.run()
        self.assertFalse(at.exception)

    def connection(self, fake):
        @contextlib.contextmanager
        def conn(*args):
            yield fake

        return patch.object(svc, "connection", conn)

    def test_selection_survives_pagination_filters_navigation_and_reading(self):
        at = self.app()
        at.checkbox(key="inbox_uid_39").check().run()
        at.button(key="inbox_next").click().run()
        self.assertNotIn("inbox_uid_39", [c.key for c in at.checkbox])
        at.checkbox(key="inbox_uid_10").check().run()
        at.button(key="inbox_previous").click().run()
        self.assertTrue(at.checkbox(key="inbox_uid_39").value)
        at.text_input(key="inbox_search").input("Letter 20").run()
        self.assertEqual(at.session_state["inbox_selected"], ["10", "39"])
        at.radio(key="inbox_filter").set_value("unread").run()
        at.button(key="nav_white").click().run()
        at.button(key="nav_mail").click().run()
        self.assertEqual(at.session_state["inbox_selected"], ["10", "39"])
        at.text_input(key="inbox_search").input("").run()
        at.radio(key="inbox_filter").set_value("all").run()
        at.button(key="inbox_open_39").click().run()
        with patch.object(
            svc,
            "read_message",
            return_value={
                "uid": "39",
                "text": "Body <script>text only</script>",
                "truncated": False,
            },
        ):
            at.button(key="inbox_read").click().run()
            self.finish(at)
        self.assertEqual(at.session_state["inbox_selected"], ["10", "39"])
        self.assertTrue(at.checkbox(key="inbox_uid_39").value)
        self.assertTrue(any("text only" in t.value for t in at.text))
        at.button(key="inbox_close").click().run()
        at.checkbox(key="inbox_uid_39").uncheck().run()
        self.assertEqual(at.session_state["inbox_selected"], ["10"])

    def test_confirm_deletes_only_exact_selected_uids_once_with_history_and_undo(self):
        fake = FakeIMAP(self.messages)
        with self.connection(fake):
            at = self.app()
            at.checkbox(key="inbox_uid_39").check().run()
            at.button(key="inbox_next").click().run()
            at.checkbox(key="inbox_uid_10").check().run()
            at.button(key="inbox_review").click().run()
            self.assertFalse(any(c == "MOVE" for c, a in fake.calls))
            # Search/sort/page changes do not widen the frozen confirmation.
            at.text_input(key="inbox_search").input("Letter 20").run()
            self.assertEqual(
                {m["uid"] for m in at.session_state["inbox_confirmation"]["targets"]},
                {"10", "39"},
            )
            at.button(key="inbox_confirm").click().run()
            self.assertFalse(at.exception)
            self.assertEqual({a[0] for c, a in fake.calls if c == "MOVE"}, {"10", "39"})
            at.run()
            self.assertEqual(len([c for c, a in fake.calls if c == "MOVE"]), 2)
            self.assertEqual(at.session_state["inbox_selected"], [])
            self.assertEqual(len(self.store.scan()["messages"]), 28)
            self.assertEqual(self.store.history()[0]["status"], "done")
            self.assertEqual(len(self.store.last_moves()), 2)
            self.assertFalse(at.button(key="inbox_undo").disabled)
            # Existing Undo uses recorded Trash UIDs, then invalidates the scan.
            fake.messages = [
                dict(m, uid=str(int(m["uid"]) + 100))
                for m in self.messages
                if m["uid"] in {"10", "39"}
            ]
            fake.validity = "456"
            at.button(key="inbox_undo").click().run()
            self.finish(at)
            self.assertIsNone(self.store.scan())
            self.assertEqual(self.store.history()[0]["kind"], "undo")

    def test_whitelist_bulk_skip_manual_permission_and_execution_recheck(self):
        self.store.policy(["news@auchan.pl"], "white")
        fake = FakeIMAP(self.messages)
        with self.connection(fake):
            at = self.app()
            at.button(key="inbox_select_page").click().run()
            self.assertEqual(at.session_state["inbox_selected"], [])
            at.checkbox(key="inbox_uid_39").check().run()
            at.button(key="inbox_review").click().run()
            self.assertTrue(at.button(key="inbox_confirm").disabled)
            at.checkbox(key="inbox_allow_white").check().run()
            self.assertFalse(at.button(key="inbox_confirm").disabled)
            at.button(key="inbox_confirm").click().run()
            self.assertEqual(len([c for c, a in fake.calls if c == "MOVE"]), 1)
        # Permission is rechecked even if the policy changed after preview.
        self.store.policy(["news@auchan.pl"], "")
        preview = deletion_preview(self.store, "123", ["10"])
        self.store.policy(["news@auchan.pl"], "white")
        with self.connection(fake), self.assertRaisesRegex(svc.MailError, "protected"):
            svc.execute(self.store, "test", preview, ["10"], lambda *a: None)

    def test_refresh_loads_full_inbox_and_empty_states_work(self):
        self.store.save_scan("123", 30, self.messages[-2:])
        at = self.app()
        self.assertTrue(any("выборка" in i.value for i in at.info))
        with self.connection(FakeIMAP(self.messages)):
            at.button(key="inbox_refresh").click().run()
            self.finish(at)
        self.assertEqual(len(self.store.scan()["messages"]), 30)
        self.assertFalse(any("выборка" in i.value for i in at.info))
        at.text_input(key="inbox_search").input("no such subject").run()
        self.assertTrue(at.button(key="inbox_select_page").disabled)
        self.assertTrue(at.button(key="inbox_next").disabled)
        self.store.save_scan("123", 0, [])
        at.run()
        self.assertFalse(at.exception)
        self.assertTrue(at.button(key="inbox_review").disabled)
        self.store.invalidate_scan()
        at.run()
        self.assertFalse(at.exception)
        self.assertIsNotNone(at.button(key="inbox_refresh"))

    def test_uidvalidity_change_clears_selection_and_stops_frozen_delete(self):
        at = self.app()
        at.checkbox(key="inbox_uid_39").check().run()
        at.button(key="inbox_review").click().run()
        frozen = at.session_state["inbox_confirmation"]
        # Same UID in a new mailbox epoch must never inherit selection.
        self.store.save_scan("999", 30, self.messages)
        at.run()
        self.assertEqual(at.session_state["inbox_selected"], [])
        self.assertIsNone(at.session_state.get("inbox_confirmation"))
        fake = FakeIMAP(self.messages, validity="999")
        with self.connection(fake):
            result = svc.execute(self.store, "test", frozen, ["39"], lambda *a: None)
        self.assertEqual(result["error"], "stale")
        self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_cancelling_and_deselecting_invalidate_confirmation(self):
        at = self.app()
        at.checkbox(key="inbox_uid_39").check().run()
        at.button(key="inbox_review").click().run()
        at.button(key="inbox_cancel").click().run()
        self.assertIsNone(at.session_state.get("inbox_confirmation"))
        self.assertEqual(at.session_state["inbox_selected"], ["39"])
        at.button(key="inbox_review").click().run()
        at.checkbox(key="inbox_uid_39").uncheck().run()
        self.assertIsNone(at.session_state.get("inbox_confirmation"))
        self.assertTrue(at.button(key="inbox_review").disabled)

    def test_reader_company_transition_restores_search_page_and_selection(self):
        at = self.app()
        at.button(key="inbox_next").click().run()
        at.checkbox(key="inbox_uid_10").check().run()
        at.button(key="inbox_open_10").click().run()
        at.button(key="reader_company").click().run()
        self.assertEqual(at.session_state["inbox_company"], "auchan")
        at.button(key="company_back").click().run()
        self.assertEqual(at.session_state["inbox_page"], 1)
        self.assertTrue(at.checkbox(key="inbox_uid_10").value)
        at.text_input(key="inbox_search").input("Letter 10").run()
        at.button(key="inbox_open_10").click().run()
        at.button(key="reader_company").click().run()
        self.assertEqual(at.text_input(key="inbox_search").value, "")
        at.button(key="company_back").click().run()
        self.assertEqual(at.text_input(key="inbox_search").value, "Letter 10")

    def test_company_bulk_scope_respects_filter_and_explicit_all_override(self):
        at = self.app()
        at.radio(key="inbox_filter").set_value("unread").run()
        at.selectbox(key="mail_display").set_value("company_count").run()
        at.button(key="company_open_auchan").click().run()
        at.button(key="inbox_review").click().run()
        self.assertEqual(len(at.session_state["inbox_confirmation"]["targets"]), 15)
        at.checkbox(key="company_all").check().run()
        self.assertIsNone(at.session_state.get("inbox_confirmation"))
        at.button(key="inbox_review").click().run()
        self.assertEqual(len(at.session_state["inbox_confirmation"]["targets"]), 30)

    def test_unsubscribe_only_and_combined_actions_use_frozen_subscription_targets(self):
        for mode in ["unsubscribe_only", "unsubscribe_delete"]:
            with self.subTest(mode=mode):
                self.store.save_scan("123", 30, self.messages)
                fake = FakeIMAP(self.messages)
                with self.connection(fake), patch.object(svc, "one_click", return_value=("requested", "HTTP 200")) as unsub:
                    at = self.app()
                    at.checkbox(key="inbox_uid_39").check().run()
                    at.button(key="inbox_" + mode).click().run()
                    preview = at.session_state["inbox_confirmation"]
                    self.assertEqual(preview["mode"], mode)
                    self.assertEqual(len(preview["unsubs"]), 1)
                    at.button(key="inbox_confirm").click().run()
                    self.assertFalse(at.exception)
                    at.run()
                    self.assertEqual(unsub.call_count, 1)
                    self.assertEqual(len([c for c, a in fake.calls if c == "MOVE"]), 0 if mode == "unsubscribe_only" else 1)

    def test_view_settings_survive_menu_navigation(self):
        at = self.app()
        at.text_input(key="inbox_search").input("Letter").run()
        at.radio(key="inbox_filter").set_value("unread").run()
        at.selectbox(key="mail_display").set_value("company_name").run()
        at.button(key="nav_settings").click().run()
        at.button(key="nav_mail").click().run()
        self.assertEqual(at.text_input(key="inbox_search").value, "Letter")
        self.assertEqual(at.radio(key="inbox_filter").value, "unread")
        self.assertEqual(at.selectbox(key="mail_display").value, "company_name")

    def test_select_blacklist_respects_search_and_whitelist(self):
        extra = msg("40", "offers@bolt.eu", "Bolt", received=40)
        self.store.save_scan("123", 31, self.messages + [extra])
        self.store.policy(["news@auchan.pl", "offers@bolt.eu"], "black")
        self.store.policy(["news@auchan.pl"], "white")
        at = self.app()
        at.button(key="inbox_black").click().run()
        self.assertEqual(at.session_state["inbox_selected"], ["40"])
        at.button(key="inbox_clear").click().run()
        at.text_input(key="inbox_search").input("Letter 10").run()
        self.assertTrue(at.button(key="inbox_black").disabled)


if __name__ == "__main__":
    unittest.main()
