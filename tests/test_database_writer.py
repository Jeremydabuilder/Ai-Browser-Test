"""Unit tests for the background writer and database robustness."""

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.storage import Database, HistoryStore  # noqa: E402


class WriterTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.sqlite3")
        self.db = Database(self.path)
        self.history = HistoryStore(self.db)

    def tearDown(self):
        self.db.close()
        self._dir.cleanup()

    def test_writes_do_not_run_on_the_calling_thread(self):
        """add_visit must hand off, not execute inline."""
        caller = threading.current_thread().ident
        seen: list[int] = []
        self.db.submit_task(lambda conn: seen.append(threading.current_thread().ident))
        self.db.flush()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], caller)

    def test_queued_writes_are_visible_to_the_next_read(self):
        for i in range(50):
            self.history.add_visit(f"https://e{i}.example/", f"T{i}")
        # No explicit flush: reads flush for us.
        self.assertEqual(self.history.count(), 50)

    def test_collapse_is_atomic_under_the_writer(self):
        for _ in range(20):
            self.history.add_visit("https://same.example/", "T")
        self.assertEqual(self.history.count(), 1)

    def test_oversized_url_is_rejected(self):
        self.history.add_visit("https://e.example/" + "x" * 5000, "T")
        self.assertEqual(self.history.count(), 0)

    def test_long_title_is_truncated_not_rejected(self):
        self.history.add_visit("https://e.example/", "T" * 5000)
        entry = self.history.recent()[0]
        self.assertEqual(len(entry.title), 512)

    def test_close_drains_pending_writes(self):
        for i in range(100):
            self.history.add_visit(f"https://f{i}.example/", "T")
        self.db.close()
        reopened = Database(self.path)
        self.assertEqual(HistoryStore(reopened).count(), 100)
        reopened.close()

    def test_corrupt_database_file_is_replaced_not_fatal(self):
        corrupt_dir = tempfile.TemporaryDirectory()
        path = os.path.join(corrupt_dir.name, "bad.sqlite3")
        with open(path, "wb") as fh:
            fh.write(b"this is definitely not a sqlite database" * 40)
        db = Database(path)          # must not raise
        store = HistoryStore(db)
        store.add_visit("https://e.example/", "T")
        self.assertEqual(store.count(), 1)
        self.assertTrue(os.path.exists(path + ".corrupt"))
        db.close()
        corrupt_dir.cleanup()

    def test_the_failed_connection_is_closed_before_the_file_is_moved(self):
        """The reason this test exists is Windows.

        A file with an open handle cannot be renamed or deleted on Windows, so
        leaving the failed connection open made recovery raise PermissionError
        (WinError 32) - and a corrupt database then stopped the browser
        starting, which is precisely what the recovery is for. POSIX allows
        renaming an open file, so the bug was invisible here.

        Rather than pretend to be Windows, this asserts the invariant that
        makes it safe everywhere: at the moment the corrupt file is moved, no
        connection to it is still open. A closed sqlite3 connection raises
        ProgrammingError on use, which is how that is checked.
        """
        import sqlite3

        corrupt_dir = tempfile.TemporaryDirectory()
        path = os.path.join(corrupt_dir.name, "bad.sqlite3")
        with open(path, "wb") as fh:
            fh.write(b"still not a sqlite database" * 60)

        opened = []
        real_connect = sqlite3.connect

        def tracking_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            opened.append(conn)
            return conn

        still_open = []
        real_replace = os.replace

        def watching_replace(src, dst):
            for conn in opened:
                try:
                    conn.execute("SELECT 1")
                    still_open.append(conn)
                except sqlite3.ProgrammingError:
                    pass          # closed, which is what we want
                except sqlite3.DatabaseError:
                    pass          # open but unreadable - it is the corrupt one
            return real_replace(src, dst)

        sqlite3.connect = tracking_connect
        os.replace = watching_replace
        try:
            db = Database(path)
        finally:
            sqlite3.connect = real_connect
            os.replace = real_replace

        self.assertTrue(opened, "no connection was ever attempted")
        self.assertEqual(
            still_open, [],
            "the corrupt file was moved while a connection to it was open - "
            "this raises PermissionError (WinError 32) on Windows")
        db.close()
        corrupt_dir.cleanup()

    def test_writes_after_close_are_ignored(self):
        self.db.close()
        self.history.add_visit("https://e.example/", "T")  # must not raise


if __name__ == "__main__":
    unittest.main()
