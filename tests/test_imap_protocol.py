"""Exercise Python's real IMAP parser against a local, strict scripted server."""

import contextlib
import imaplib
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mail_service as svc
from storage import Store
from test_mail import msg


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        def send(data):
            self.wfile.write(data + b"\r\n")
            self.wfile.flush()

        send(b"* OK test mailbox ready")
        while line := self.rfile.readline():
            tag, command = line.rstrip(b"\r\n").split(b" ", 1)
            self.server.commands.append(command)
            status = b"OK completed"
            if command == b"CAPABILITY":
                send(
                    b"* CAPABILITY IMAP4rev1 UIDPLUS"
                    + (b" MOVE" if self.server.move else b"")
                )
            elif command.startswith(b"LOGIN "):
                pass
            elif command in (b'EXAMINE "INBOX"', b'SELECT "INBOX"'):
                send(b"* 1 EXISTS")
                send(b"* OK [UIDVALIDITY 123] epoch")
            elif command.startswith(b"UID SEARCH"):
                send(b"* SEARCH" + (b" 10" if self.server.present else b""))
            elif command.startswith(b"UID FETCH 10 "):
                raw = b"From: Auchan <news@auchan.pl>\r\nSubject: Sale\r\nMessage-ID: <10@test>\r\n\r\n"
                if self.server.present:
                    send(
                        b'* 1 FETCH (UID 10 INTERNALDATE "05-Oct-2026 10:20:30 +0000" BODY[HEADER.FIELDS (FROM SUBJECT MESSAGE-ID)] {'
                        + str(len(raw)).encode()
                        + b"}"
                    )
                    self.wfile.write(raw + b")\r\n")
            elif command == b'LIST "" *':
                send(b'* LIST (\\HasNoChildren \\Trash) "/" "Deleted Messages"')
            elif command in (
                b'UID MOVE 10 "Deleted Messages"',
                b'UID COPY 10 "Deleted Messages"',
            ):
                status = b"OK [COPYUID 456 10 110] completed"
                if command.startswith(b"UID MOVE"):
                    self.server.present = False
                    send(b"* 1 EXPUNGE")
            elif command == b"UID STORE 10 +FLAGS.SILENT (\\Deleted)":
                pass
            elif command == b"UID EXPUNGE 10":
                self.server.present = False
                send(b"* 1 EXPUNGE")
            elif command == b"LOGOUT":
                send(b"* BYE closing")
                send(tag + b" OK logout")
                break
            else:
                status = b"BAD unexpected command"
                self.server.unexpected.append(command)
            send(tag + b" " + status)


class ProtocolTests(unittest.TestCase):
    def test_real_imap_parser_move_and_uidplus(self):
        for move in (True, False):
            with self.subTest(move=move), tempfile.TemporaryDirectory() as tmp:
                with socketserver.TCPServer(("127.0.0.1", 0), Handler) as server:
                    server.move, server.present = move, True
                    server.commands, server.unexpected = [], []
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()

                    @contextlib.contextmanager
                    def connect(*args):
                        client = imaplib.IMAP4(*server.server_address, timeout=3)
                        client.login("test", "test")
                        try:
                            yield client
                        finally:
                            client.logout()

                    try:
                        store = Store("test@icloud.com", Path(tmp) / "state.db")
                        store.save_scan("123", 1, [msg()])
                        with patch.object(svc, "connection", connect):
                            preview = svc.prepare(
                                store,
                                "pass",
                                ["auchan"],
                                "delete_only",
                                "all",
                                False,
                                lambda *a: None,
                            )
                            self.assertEqual(len(preview["targets"]), 1)
                            result = svc.execute(
                                store, "pass", preview, ["10"], lambda *a: None
                            )
                        self.assertEqual(result["moved"], 1, result)
                        self.assertNotIn("error", result)
                        self.assertFalse(server.present)
                        self.assertFalse(server.unexpected)
                        self.assertNotIn(b"EXPUNGE", server.commands)
                        self.assertIn(b"UID SEARCH UID 10", server.commands)
                        self.assertEqual(store.last_moves()[0]["dest_uid"], "110")
                    finally:
                        server.shutdown()
                        thread.join(3)


if __name__ == "__main__":
    unittest.main()
