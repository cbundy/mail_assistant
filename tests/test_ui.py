import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch
from streamlit.runtime.context import ContextProxy, StreamlitTheme
from streamlit.testing.v1 import AppTest
import storage
import mail_service as svc
from test_mail import msg, FakeIMAP


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.db"
        self.patcher = patch.object(storage, "DB_PATH", self.path)
        self.patcher.start()
        self.store = storage.Store("test@icloud.com")
        self.store.save_scan("123", 2, [msg(), msg("11", "offers@bolt.eu", "Bolt")])

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def app(self, login=True):
        at = AppTest.from_file(
            str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=10
        )
        if login:
            at.session_state["account"] = "test@icloud.com"
            at.session_state["password"] = "test-only"
        at.run()
        self.assertFalse(at.exception)
        return at

    def button(self, at, label):
        return next(b for b in at.button if b.label == label)

    def test_login_screen_and_languages(self):
        at = self.app(False)
        self.assertEqual(len(at.text_input), 2)
        self.assertTrue(self.button(at, "Подключиться к iCloud").disabled)
        at.selectbox[0].set_value("English").run()
        self.assertFalse(at.exception)
        self.assertTrue(self.button(at, "Connect to iCloud").disabled)
        self.assertEqual(storage.Store().get("language"), "English")

    def theme_scripts(self, at):
        return [e.proto.body for e in at.get("html") if "stActiveTheme" in e.proto.body]

    def test_theme_toggle_persists(self):
        # Light (or unknown) active theme: toggle off; switching on stores the
        # browser's theme choice ("Dark") and reloads the page.
        at = self.app()
        self.assertFalse(at.toggle(key="dark_theme").value)
        self.assertEqual(self.theme_scripts(at), [])
        at.toggle(key="dark_theme").set_value(True).run()
        self.assertFalse(at.exception)
        [script] = self.theme_scripts(at)
        self.assertIn('"stActiveTheme-" + window.location.pathname + "-v2"', script)
        self.assertIn('JSON.stringify("Dark")', script)
        self.assertIn("window.location.reload()", script)
        self.assertIsNone(storage.Store().get("theme_mode"))

    def test_theme_toggle_follows_active_dark_theme(self):
        dark = PropertyMock(return_value=StreamlitTheme({"type": "dark"}))
        with patch.object(ContextProxy, "theme", new_callable=lambda: dark):
            at = self.app()
            self.assertTrue(at.toggle(key="dark_theme").value)
            at.toggle(key="dark_theme").set_value(False).run()
            self.assertFalse(at.exception)
            [script] = self.theme_scripts(at)
            self.assertIn('JSON.stringify("Light")', script)
            # Once the switch is sent, later reruns track the active theme again.
            at.run()
            self.assertTrue(at.toggle(key="dark_theme").value)
            self.assertEqual(self.theme_scripts(at), [])

    def test_font_size_setting_persists(self):
        at = self.app()
        at.button(key="nav_settings").click().run()
        at.slider(key="font_size").set_value(18).run()
        self.assertFalse(at.exception)
        self.assertEqual(storage.Store().get("font_size"), 18)
        self.assertEqual(at.slider(key="font_size").value, 18)

    def test_all_pages_ru_en(self):
        at = self.app()
        for lang in ["Русский", "English"]:
            at.button(key="nav_settings").click().run()
            at.selectbox(key="language").set_value(lang).run()
            for page in ["white", "black", "groups", "history", "settings", "mail", "inbox"]:
                at.button(key="nav_" + page).click().run()
                self.assertFalse(at.exception, f"{lang} {page}")
                self.assertEqual(len(at.sidebar.button), 8)
                self.assertFalse(at.sidebar.radio)
                self.assertFalse(at.sidebar.selectbox)

    def test_read_filter_switches_visible_companies(self):
        self.store.save_scan(
            "123",
            2,
            [
                msg("10", "news@auchan.pl", "Auchan", unread=True),
                msg("11", "offers@bolt.eu", "Bolt", unread=False),
            ],
        )
        at = self.app()

        at.radio(key="read_filter_control").set_value("unread").run()
        labels = [c.label for c in at.checkbox if c.key and c.key.startswith("company_")]
        self.assertIn("Auchan", labels)
        self.assertNotIn("Bolt", labels)

        at.radio(key="read_filter_control").set_value("read").run()
        labels = [c.label for c in at.checkbox if c.key and c.key.startswith("company_")]
        self.assertIn("Bolt", labels)
        self.assertNotIn("Auchan", labels)

    def test_select_all_excludes_whitelist(self):
        self.store.policy(["news@auchan.pl"], "white")
        at = self.app()
        self.button(at, "Выбрать всё").click().run()
        self.assertFalse(at.exception)
        selected = [c.label for c in at.checkbox if c.value]
        self.assertEqual(selected, ["Bolt"])
        self.button(at, "Снять всё").click().run()
        self.assertFalse(any(c.value for c in at.checkbox))

    def test_prepare_delete_and_no_replay(self):
        fake = FakeIMAP([msg(), msg("11", "offers@bolt.eu", "Bolt")])

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app()
            self.button(at, "Выбрать всё").click().run()
            # Choose all messages rather than the advertising heuristic for the test.
            next(r for r in at.radio if r.label == "Какие письма удалить").set_value(
                "all"
            ).run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertIsNotNone(at.session_state["preview"])
            self.button(at, "Снять все отметки писем").click().run()
            self.assertTrue(at.button(key="execute_action").disabled)
            self.button(at, "Отметить все письма").click().run()
            at.button(key="execute_action").click().run()
            self.assertFalse(at.exception)
            self.assertIsNone(at.session_state.get("job"))
            self.assertEqual(len([x for x in fake.calls if x[0] == "MOVE"]), 2)
            at.run()
            self.assertEqual(len([x for x in fake.calls if x[0] == "MOVE"]), 2)
            self.assertIsNone(at.session_state["preview"])
            self.assertTrue(any("удалено 2" in x.value for x in at.info))

    def test_unchecking_one_message_keeps_preview_and_other_selection(self):
        fake = FakeIMAP([msg(), msg("11", "news@auchan.pl", "Auchan")])

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app()
            next(c for c in at.checkbox if c.label == "Auchan").check().run()
            next(r for r in at.radio if r.label == "Какие письма удалить").set_value(
                "all"
            ).run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()

            preview_id = at.session_state["preview_id"]
            first_key = f"message_{preview_id}_uid_10"
            second_key = f"message_{preview_id}_uid_11"
            self.assertTrue(at.checkbox(key=first_key).value)
            self.assertTrue(at.checkbox(key=second_key).value)

            at.checkbox(key=first_key).uncheck().run()

            self.assertFalse(at.exception)
            self.assertIsNotNone(at.session_state["preview"])
            self.assertFalse(at.checkbox(key=first_key).value)
            self.assertTrue(at.checkbox(key=second_key).value)
            self.assertFalse(at.button(key="execute_action").disabled)
            self.assertIn("1 писем", at.button(key="execute_action").label)

    def test_prepared_preview_is_frozen_from_outer_selection(self):
        fake = FakeIMAP([msg(), msg("11", "offers@bolt.eu", "Bolt")])

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app()
            next(c for c in at.checkbox if c.label == "Auchan").check().run()
            next(r for r in at.radio if r.label == "Какие письма удалить").set_value(
                "all"
            ).run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()

            preview_id = at.session_state["preview_id"]
            preview = at.session_state["preview"]
            self.assertIsNotNone(preview)
            self.assertEqual(preview["mode"], "delete_only")

            # The prepared transaction remains valid and executable even if the
            # live setup controls above are changed afterwards.
            next(c for c in at.checkbox if c.label == "Auchan").uncheck().run()

            self.assertFalse(at.exception)
            self.assertIsNotNone(at.session_state["preview"])
            self.assertIsNotNone(
                at.checkbox(key=f"message_{preview_id}_uid_10")
            )
            self.assertFalse(at.button(key="execute_action").disabled)
            self.assertNotIn("preview_stale", at.session_state)

    def test_queued_execute_runs_even_after_preview_context_is_gone(self):
        fake = FakeIMAP()

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app()
            next(c for c in at.checkbox if c.label == "Auchan").check().run()
            next(r for r in at.radio if r.label == "Какие письма удалить").set_value(
                "all"
            ).run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            preview = at.session_state["preview"]
            self.assertIsNotNone(preview)

            # Reproduce the real-browser failure: the callback queued the action,
            # then the preview/page context disappeared before the next render.
            at.session_state["execute_request"] = {
                "preview": preview,
                "selected_uids": [m["uid"] for m in preview["targets"]],
            }
            at.session_state["preview"] = None
            at.session_state["page"] = "history"
            at.run()

            self.assertFalse(at.exception)
            self.assertEqual(len([x for x in fake.calls if x[0] == "MOVE"]), 1)
            self.assertNotIn("execute_request", at.session_state)
            self.assertEqual(at.session_state["result_kind"], "execute")

    def test_slow_preview_preserves_selection_and_consent(self):
        import threading

        release = threading.Event()
        fake = FakeIMAP()
        self.store.policy(["news@auchan.pl"], "white")
        original = svc.prepare

        def slow(*args):
            release.wait(5)
            return original(*args)

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn), patch.object(svc, "prepare", slow):
            at = self.app()
            next(c for c in at.checkbox if c.label.startswith("Auchan")).check().run()
            next(
                c for c in at.checkbox if c.label.startswith("Я разрешаю")
            ).check().run()
            at.button(key="prepare_action").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(len(at.sidebar.button), 8)
            self.assertTrue(all(b.disabled for b in at.sidebar.button))
            job = at.session_state["job"]
            release.set()
            job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertTrue(
                next(c for c in at.checkbox if c.label.startswith("Auchan")).value
            )
            self.assertTrue(
                next(c for c in at.checkbox if c.label.startswith("Я разрешаю")).value
            )
            self.assertIsNotNone(at.session_state["preview"])
            self.assertIsNotNone(at.button(key="execute_action"))

    def test_login_job_completes(self):
        fake = FakeIMAP()

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app(False)
            at.text_input[0].input("test@icloud.com")
            at.text_input[1].input("password-only-in-memory").run()
            self.button(at, "Подключиться к iCloud").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(at.session_state["account"], "test@icloud.com")
            self.assertEqual(at.session_state["password"], "password-only-in-memory")
            self.assertNotIn(
                "password-only-in-memory",
                self.path.read_bytes().decode(errors="ignore"),
            )

    def test_search_preserves_hidden_selection(self):
        at = self.app()
        next(c for c in at.checkbox if c.label == "Auchan").check().run()
        at.text_input(key="company_search").input("Bolt").run()
        self.assertEqual(at.session_state["chosen_companies"], ["auchan"])
        self.assertFalse(at.button(key="prepare_action").disabled)
        at.text_input(key="company_search").input("").run()
        self.assertTrue(next(c for c in at.checkbox if c.label == "Auchan").value)

    def test_company_message_dialog_has_subjects_and_reader(self):
        at = self.app()
        at.button(key="view_auchan").click().run()
        self.assertFalse(at.exception)
        self.assertTrue(any("Sale" in str(d.value) for d in at.dataframe))
        self.assertIsNotNone(at.button(key="read_message"))
        at.button(key="close_messages").click().run()
        self.assertFalse(at.exception)
        self.assertNotIn("view_company", at.session_state)

    def test_empty_ad_filter_explains_no_deletion(self):
        item = msg()
        item["subject"] = "Hello friend"
        fake = FakeIMAP([item])

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        with patch.object(svc, "connection", conn):
            at = self.app()
            next(c for c in at.checkbox if c.label == "Auchan").check().run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertTrue(
                any("Писем для удаления нет" in w.value for w in at.warning)
            )
            self.assertTrue(at.button(key="execute_action").disabled)
            self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_controls_precede_bounded_company_list(self):
        many = [msg(str(i), f"news@brand{i}.test", f"Brand{i}") for i in range(10, 110)]
        self.store.save_scan("123", 100, many)
        at = self.app()
        self.assertTrue(at.button(key="prepare_action").disabled)
        # Streamlit tree order is visual order, including out-of-order container writes.
        labels = [x.key for x in at.main.button]
        self.assertLess(
            labels.index("prepare_action"),
            next(i for i, k in enumerate(labels) if k and k.startswith("view_")),
        )
        self.assertEqual(
            len([c for c in at.checkbox if c.key.startswith("company_")]),
            len(svc.companies(self.store)),
        )

    def test_reader_does_not_restore_unchecked_messages(self):
        import threading

        release = threading.Event()
        fake = FakeIMAP()

        @contextlib.contextmanager
        def conn(*args):
            yield fake

        def read(*args):
            release.wait(5)
            return {"uid": "10", "text": "Test body", "truncated": False}

        with patch.object(svc, "connection", conn), patch.object(
            svc, "read_message", read
        ):
            at = self.app()
            next(c for c in at.checkbox if c.label == "Auchan").check().run()
            at.button(key="prepare_action").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.button(at, "Снять все отметки писем").click().run()
            self.assertTrue(at.button(key="execute_action").disabled)
            at.button(key="view_auchan").click().run()
            at.button(key="read_message").click().run()
            job = at.session_state["job"]
            release.set()
            job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertTrue(at.button(key="execute_action").disabled)
            self.assertFalse(any(c == "MOVE" for c, a in fake.calls))

    def test_deselect_all_clears_hidden_companies(self):
        at = self.app()
        self.button(at, "Выбрать всё").click().run()
        at.text_input(key="company_search").input("Bolt").run()
        self.button(at, "Снять всё").click().run()
        self.assertEqual(at.session_state["chosen_companies"], [])
        at.text_input(key="company_search").input("").run()
        self.assertFalse(any(c.value for c in at.checkbox))


if __name__ == "__main__":
    unittest.main()
