import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server


def ok(*args, **kwargs):
    return {'status': 'ok', 'latency': 1.0, 'detail': '', 'ip': '127.0.0.1', 'ts': time.time()}


def wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(.05)
    return False


class WorkerResilienceTests(unittest.TestCase):
    """Unexpected errors must not end a measurement silently (audit 16.10.2026, item 1)."""
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = server.Store(Path(self.folder.name) / 'test.sqlite3', retention_seconds=server.RETENTION_SECONDS)
        self.monitor = server.Monitor(self.store)
        patcher = patch('power_guard.PowerGuard.start')
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.monitor.stop()
        for worker in list(self.monitor.workers):
            worker.join(3)
        if self.monitor.timer_thread:
            self.monitor.timer_thread.join(3)
        self.store.db.close()
        self.folder.cleanup()

    def start(self):
        return self.monitor.start({'interval': .5, 'timeout': .5, 'duration_minutes': 1,
                                   'targets': [{'host': '127.0.0.1', 'mode': 'tcp', 'port': 1}]})

    def messages(self):
        return [e['message'] for e in self.store.rows('SELECT message FROM events ORDER BY ts')]

    def samples(self):
        return self.store.rows('SELECT COUNT(*) AS n FROM samples')[0]['n']

    def test_unexpected_probe_error_is_reported_and_measurement_continues(self):
        calls = {'n': 0}
        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] in (2, 3):
                raise KeyError('broken')
            return ok()
        with patch.object(server, 'probe', flaky):
            self.start()
            self.assertTrue(wait_for(lambda: calls['n'] >= 6))
        self.assertTrue(all(worker.is_alive() for worker in self.monitor.workers))
        messages = self.messages()
        # One event per failure streak, then a recovery event.
        self.assertEqual(sum('Внутренняя ошибка измерения (KeyError' in m for m in messages), 1)
        self.assertIn('Измерение восстановилось после внутренней ошибки', messages)
        self.assertGreaterEqual(self.samples(), 3)

    def test_database_write_error_does_not_kill_the_worker(self):
        real = self.store.execute
        failures = {'n': 0}
        def execute(sql, args=()):
            if sql.startswith('INSERT INTO samples') and failures['n'] < 2:
                failures['n'] += 1
                raise sqlite3.OperationalError('disk I/O error')
            return real(sql, args)
        with patch.object(server, 'probe', ok), patch.object(self.store, 'execute', execute):
            self.start()
            self.assertTrue(wait_for(lambda: self.samples() >= 2))
        self.assertEqual(failures['n'], 2)
        self.assertTrue(any('OperationalError: disk I/O error' in m for m in self.messages()))

    def test_dead_worker_is_restarted_by_the_session_timer(self):
        calls = {'n': 0}
        def worker(sid, target, config, started, stop):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('escaped')  # a bug outside the loop's own handler
            while not stop.wait(.05):
                pass
        with patch.object(server, 'WATCHDOG_SECONDS', .1), patch.object(server.Monitor, 'worker', lambda self, *a: worker(*a)):
            self.start()
            self.assertTrue(wait_for(lambda: calls['n'] == 2))
        self.assertTrue(wait_for(lambda: all(w.is_alive() for w in self.monitor.workers)))
        messages = self.messages()
        self.assertIn('Измерение остановлено внутренней ошибкой (RuntimeError: escaped)', messages)
        self.assertIn('Поток измерения перезапущен (1 из %d)' % server.WORKER_RESTARTS, messages)

    def test_restarts_are_limited(self):
        calls = {'n': 0}
        def broken(sid, target, config, started, stop):
            calls['n'] += 1
            raise RuntimeError('always')
        with patch.object(server, 'WATCHDOG_SECONDS', .05), patch.object(server.Monitor, 'worker', lambda self, *a: broken(*a)):
            self.start()
            self.assertTrue(wait_for(lambda: calls['n'] == 1 + server.WORKER_RESTARTS))
            time.sleep(.4)
        self.assertEqual(calls['n'], 1 + server.WORKER_RESTARTS)

    def test_route_worker_survives_an_unexpected_parser_error(self):
        calls = {'n': 0}
        def trace(ip, timeout, max_hops, stop=None, on_hop=None, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise IndexError('bad tracert line')
            hop = {'hop': 1, 'ip': ip, 'latency': 1.0, 'status': 'ok', 'probe_count': 1, 'detail': '', 'ts': time.time()}
            on_hop(hop)
            return {'hops': [hop], 'destination': ip, 'message': '', 'state': 'complete'}
        with patch.object(server, 'trace_cycle', trace):
            self.monitor.start({'interval': .5, 'timeout': .5, 'route_interval': .5, 'duration_minutes': 1,
                                'targets': [{'host': '127.0.0.1', 'mode': 'route'}]})
            self.assertTrue(wait_for(lambda: calls['n'] >= 3))
        messages = self.messages()
        self.assertTrue(any('Внутренняя ошибка измерения (IndexError' in m for m in messages))
        self.assertIn('Измерение восстановилось после внутренней ошибки', messages)
        self.assertTrue(all(worker.is_alive() for worker in self.monitor.workers))

    def test_stop_and_deadline_still_end_the_session(self):
        with patch.object(server, 'probe', ok):
            sid = self.start()
            self.assertTrue(wait_for(lambda: self.samples() >= 1))
            self.monitor.stop()
            self.monitor.timer_thread.join(5)
        self.assertFalse(self.monitor.timer_thread.is_alive())
        self.assertEqual(self.store.rows('SELECT status FROM sessions WHERE id=?', (sid,))[0]['status'], 'stopped')
        self.assertIsNone(self.monitor.sid)


if __name__ == '__main__':
    unittest.main()
