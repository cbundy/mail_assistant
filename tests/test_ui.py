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
            at.sidebar.selectbox[0].set_value(lang).run()
            for page in ["white", "black", "groups", "history", "settings", "mail"]:
                at.sidebar.radio[0].set_value(page).run()
                self.assertFalse(at.exception, f"{lang} {page}")

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
            self.button(at, "Подготовить предпросмотр").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertIsNotNone(at.session_state["preview"])
            self.button(at, "Снять все отметки писем").click().run()
            self.assertTrue(self.button(at, "Подтвердить: Только удалить").disabled)
            self.button(at, "Отметить все письма").click().run()
            self.button(at, "Подтвердить: Только удалить").click().run()
            job = at.session_state["job"]
            if job:
                job.thread.join(5)
            at.run()
            self.assertFalse(at.exception)
            self.assertEqual(len([x for x in fake.calls if x[0] == "MOVE"]), 2)
            at.run()
            self.assertEqual(len([x for x in fake.calls if x[0] == "MOVE"]), 2)
            self.assertIsNone(at.session_state["preview"])
            self.assertTrue(any("удалено 2" in x.value for x in at.info))

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
            self.button(at, "Подготовить предпросмотр").click().run()
            self.assertFalse(at.exception)
            self.assertFalse(at.button)
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
            self.assertTrue(any(b.label.startswith("Подтвердить:") for b in at.button))

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


if __name__ == "__main__":
    unittest.main()
