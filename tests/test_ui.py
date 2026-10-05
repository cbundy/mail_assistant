import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
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

    def test_all_pages_ru_en(self):
        at = self.app()
        for lang in ["Русский", "English"]:
            at.button(key="nav_settings").click().run()
            at.selectbox(key="language").set_value(lang).run()
            for page in ["white", "black", "groups", "history", "settings", "mail"]:
                at.button(key="nav_" + page).click().run()
                self.assertFalse(at.exception, f"{lang} {page}")
                self.assertEqual(len(at.sidebar.button), 7)
                self.assertFalse(at.sidebar.radio)
                self.assertFalse(at.sidebar.selectbox)

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
            self.assertEqual(len(at.sidebar.button), 7)
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
