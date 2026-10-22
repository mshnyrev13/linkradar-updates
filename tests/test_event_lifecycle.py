import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server

class EventLifecycleTests(unittest.TestCase):
    def test_restart_clears_events_preserves_measurements(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.sqlite3'
            store=server.Store(path)
            store.event('old','','info','old event')
            store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',('old','target',1,'ok',12,'127.0.0.1',''))
            store.db.close()
            reopened=server.Store(path)
            self.assertEqual(reopened.rows('SELECT * FROM events'),[])
            self.assertEqual(len(reopened.rows('SELECT * FROM samples')),1)
            reopened.db.close()

    def test_new_session_clears_previous_events(self):
        with tempfile.TemporaryDirectory() as folder:
            store=server.Store(Path(folder)/'test.sqlite3')
            store.event('old','','info','old event')
            monitor=server.Monitor(store)
            config=server.validate_config({'duration_minutes':1,'targets':[{'host':'127.0.0.1'}]})
            with patch('threading.Thread.start'),patch('power_guard.PowerGuard.start'):
                sid=monitor.start(config)
            events=store.rows('SELECT * FROM events')
            self.assertTrue(events)
            self.assertTrue(all(e['session']==sid for e in events))
            store.db.close()
