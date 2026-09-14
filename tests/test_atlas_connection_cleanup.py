"""Connections must close without waiting for Python's cyclic garbage collector."""
import http.client
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from atlas_console import ConsoleServer
from atlas_journal import Journal
from atlas_native_meta import NativeMetadata


class ConnectionCleanup(unittest.TestCase):
    def test_native_metadata_closes_reads_including_sql_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'native.sqlite'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE threads (id TEXT, title TEXT)')
            db.execute("INSERT INTO threads VALUES ('one', 'Title')")
            db.commit(); db.close()
            meta = NativeMetadata(path, ttl=0)
            opened = []
            connect = meta._connect
            def track():
                conn = connect(); opened.append(conn); return conn
            meta._connect = track
            for _ in range(20):
                self.assertIn('one', meta.lookup(['one']))
            db = sqlite3.connect(path)
            db.execute('DROP TABLE threads'); db.commit(); db.close()
            self.assertEqual(meta.lookup(['one']), {})
            for conn in opened:
                with self.assertRaises(sqlite3.ProgrammingError): conn.execute('SELECT 1')

    def test_http_threads_close_their_journal_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / 'journal.sqlite')
            opened = []
            closed = []
            close = journal.close
            def track_close():
                conn = getattr(journal._local, 'conn', None)
                close()
                if conn is not None:
                    try: conn.execute('SELECT 1')
                    except sqlite3.ProgrammingError: closed.append(conn)
            journal.close = track_close
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    opened.append(self.service.journal._connect())
                    self.service.journal.draft('test/room')
                    self.send_response(200); self.end_headers()
                def log_message(self, *args): pass
            server = ConsoleServer(('127.0.0.1', 0), Handler, SimpleNamespace(journal=journal))
            # Join handlers on close so assertions don't race final cleanup.
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever); thread.start()
            try:
                for _ in range(20):
                    c=http.client.HTTPConnection(*server.server_address)
                    c.request('GET', '/'); r=c.getresponse(); self.assertEqual(r.status,200); r.read(); c.close()
            finally:
                server.shutdown(); server.server_close(); thread.join(); journal.close()
            self.assertEqual(len(opened),20)
            self.assertTrue(all(conn in closed for conn in opened))

if __name__ == '__main__': unittest.main()
