import logging
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import error_log
import server


class Scrub(unittest.TestCase):
    def test_urls_lose_credentials_paths_queries_and_keys(self):
        text = error_log.scrub('connect rtmp://user:pw@live.twitch.tv/app/live_123456789_AbCdEfGhIj '
                               'srt://95.165.158.161:10999?streamid=#!::r=live/x,m=publish&passphrase=secret123&latency=200 '
                               'https://graphene.example/q?token=abc')
        self.assertIn('rtmp://live.twitch.tv', text)
        self.assertNotIn('/app', text)
        self.assertIn('srt://95.165.158.161:10999', text)
        self.assertIn('https://graphene.example', text)
        for secret in ('user:pw', 'live_123456789', 'streamid', 'secret123', 'token=abc', 'latency=200'):
            self.assertNotIn(secret, text)

    def test_key_value_pairs_tokens_and_headers_are_masked(self):
        text = error_log.scrub("{'key': 'live_987654321_zzzzzzzz', 'password': \"p@ss w\"} passphrase=xyz "
                               "Authorization: Bearer abcdefghijklmnop api_key=12345678")
        for secret in ('live_987654321', 'p@ss w', 'xyz', 'abcdefghijklmnop', '12345678'):
            self.assertNotIn(secret, text)
        self.assertIn('<скрыто>', text)

    def test_hosts_and_addresses_stay_for_diagnostics(self):
        self.assertEqual(error_log.scrub('ping 10.0.0.1 and ya.ru failed'), 'ping 10.0.0.1 and ya.ru failed')


class Journal(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = error_log.setup(self.folder.name)

    def tearDown(self):
        error_log.shutdown()
        self.folder.cleanup()

    def text(self):
        for handler in error_log.logger.handlers:
            handler.flush()
        return self.path.read_text(encoding='utf-8')

    def test_setup_is_idempotent_and_writes_a_start_line(self):
        self.assertEqual(error_log.setup(self.folder.name), self.path)
        self.assertEqual(self.path.name, 'errors.log')
        self.assertIn('Журнал ошибок включён', self.text())

    def test_thread_exception_is_written_with_traceback_and_scrubbed(self):
        def boom():
            raise RuntimeError('failed rtmp://h/app/live_123456789_secretkey')
        thread = threading.Thread(target=boom, name='probe-thread')
        thread.start()
        thread.join()
        text = self.text()
        self.assertIn('Необработанное исключение в потоке probe-thread', text)
        self.assertIn('Traceback', text)
        self.assertIn('RuntimeError', text)
        self.assertNotIn('live_123456789', text)

    def test_sys_excepthook_logs_uncaught_exceptions(self):
        try:
            raise ValueError('top level')
        except ValueError:
            with patch.object(sys, 'stderr', None):
                sys.excepthook(*sys.exc_info())
        self.assertIn('CRITICAL', self.text())
        self.assertIn('ValueError: top level', self.text())

    def test_keyed_errors_are_logged_once_until_resolved(self):
        for _ in range(3):
            error_log.error('Не удалось прочитать сетевую конфигурацию', OSError('timeout'), key='network-config')
        error_log.resolved('network-config')
        error_log.error('Не удалось прочитать сетевую конфигурацию', OSError('timeout'), key='network-config')
        self.assertEqual(self.text().count('Не удалось прочитать сетевую конфигурацию'), 2)

    def test_measurement_worker_error_reaches_the_journal(self):
        store = server.Store(Path(self.folder.name) / 'test.sqlite3')
        monitor = server.Monitor(store)
        monitor.internal_error('sid', {'id': 'target1'}, sqlite3.OperationalError('disk I/O error'), False)
        monitor.internal_error('sid', {'id': 'target1'}, sqlite3.OperationalError('disk I/O error'), True)  # same streak
        store.db.close()
        self.assertEqual(self.text().count('Внутренняя ошибка измерения цели target1'), 1)
        self.assertIn('OperationalError: disk I/O error', self.text())

    def test_service_has_its_own_file(self):
        error_log.shutdown()
        self.path = error_log.setup(self.folder.name, 'service')
        self.assertEqual(self.path.name, 'errors-service.log')


class Retention(unittest.TestCase):
    def test_entries_older_than_12_hours_are_removed_with_their_tracebacks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'errors.log'
            now = time.time()
            old = time.strftime(error_log.STAMP, time.localtime(now - error_log.RETENTION_SECONDS - 60))
            new = time.strftime(error_log.STAMP, time.localtime(now - 60))
            path.write_text(f'{old} ERROR [t] vcs: old\nTraceback (most recent call last):\n  old frame\n'
                            f'{new} ERROR [t] vcs: new\nTraceback (most recent call last):\n  new frame\n', encoding='utf-8')
            error_log.prune_file(path, now)
            text = path.read_text(encoding='utf-8')
            self.assertNotIn('old', text)
            self.assertIn('new frame', text)
            path.write_text(f'{old} ERROR [t] vcs: only old\n', encoding='utf-8')
            error_log.prune_file(path, now)
            self.assertFalse(path.exists())

    def test_handler_prunes_while_running_and_rotates_by_size(self):
        with tempfile.TemporaryDirectory() as folder:
            clock = [time.time()]
            handler = error_log.Handler(Path(folder) / 'errors.log', clock=lambda: clock[0])
            handler.setFormatter(error_log.Formatter('%(asctime)s %(message)s', error_log.STAMP))
            log = logging.getLogger('vcs-test-retention')
            log.addHandler(handler)
            log.propagate = False
            try:
                log.error('first')
                handler.flush()
                # 13 hours later the first entry expires on the next write.
                clock[0] += 13 * 3600
                with patch('time.time', lambda: clock[0]):
                    log.error('second')
                handler.flush()
                text = (Path(folder) / 'errors.log').read_text(encoding='utf-8')
                self.assertNotIn('first', text)
                self.assertIn('second', text)
                self.assertEqual(handler.maxBytes, error_log.MAX_BYTES)
                self.assertEqual(handler.backupCount, 1)
            finally:
                log.removeHandler(handler)
                handler.close()


class StoreClearKeepsJournal(unittest.TestCase):
    def test_normal_close_clears_data_but_keeps_the_error_journal(self):
        with tempfile.TemporaryDirectory() as folder:
            store = server.Store(Path(folder) / 'network.sqlite3', retention_seconds=server.RETENTION_SECONDS)
            (Path(folder) / 'errors.log').write_text('kept', encoding='utf-8')
            (Path(folder) / 'service-error.log').write_text('cleared', encoding='utf-8')
            store.clear()
            store.db.close()
            self.assertEqual((Path(folder) / 'errors.log').read_text(encoding='utf-8'), 'kept')
            self.assertEqual((Path(folder) / 'service-error.log').read_text(encoding='utf-8'), '')


if __name__ == '__main__':
    unittest.main()
