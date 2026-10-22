"""Update metadata validation and non-blocking UI on fixed local fixtures."""
import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import update_checker as updates
from qt_support import close_window
from PySide6.QtWidgets import QApplication


def feed(version='2.10.7',url=None):
    return json.dumps({'version':version,'download_url':url or 'https://github.com/mshnyrev13/vcs-network-watch/releases/tag/v'+version.lstrip('v')}).encode()


class MetadataTests(unittest.TestCase):
    def test_numeric_version_order(self):
        for remote,local,new in [('2.10.10','2.10.9',True),('2.10.6','2.9.9',True),('v2.10.6','2.10.6',False),('2.10.5','2.10.6',False)]:
            self.assertEqual(updates.parse_feed(feed(remote),local) is not None,new)
    def test_reject_prerelease_and_corrupt_versions(self):
        for version in ['2.10','2.10.7-beta','2.10.7+build','latest','2.10.7\n','2.10.9999999',None,123,True]:
            with self.subTest(version=version),self.assertRaises(ValueError):updates.parse_feed(feed(version,'https://disk.yandex.ru/d/example'),'2.10.6')
    def test_reject_unsafe_or_unrelated_download_links(self):
        for url in ['http://github.com/mshnyrev13/vcs-network-watch/releases/latest','file:///tmp/app','javascript:alert(1)',
                    'https://evil.test/app','https://github.com.evil.test/mshnyrev13/vcs-network-watch/releases/latest',
                    'https://user:pass@github.com/mshnyrev13/vcs-network-watch/releases/latest',
                    'https://github.com/other/repo/releases/latest','https://disk.yandex.ru/d/a?token=secret',
                    'https://github.com/mshnyrev13/vcs-network-watch/releases/latest\n','https://disk.yandex.ru:444/d/a']:
            with self.subTest(url=url),self.assertRaises(ValueError):updates.parse_feed(feed(url=url),'2.10.6')
    def test_disk_and_public_release_downloads_are_allowed(self):
        self.assertEqual(updates.parse_feed(feed(url='https://disk.yandex.ru/d/r26-99USQGWGvA'),'2.10.6').version,'2.10.7')
        self.assertEqual(updates.parse_feed(feed(url='https://github.com/mshnyrev13/linkradar-updates/releases/tag/v2.10.7'),'2.10.6').version,'2.10.7')
    def test_bounded_and_strict_json(self):
        for payload in [b'x'*(updates.MAX_BYTES+1),b'null',b'[]',b'{}',b'\xff',b'{"version":"2.10.7","version":"2.10.8"}',b'['*1500]:
            with self.subTest(size=len(payload)),self.assertRaises(ValueError):updates.parse_feed(payload,'2.10.6')
    def test_redirects_are_not_followed(self):
        with self.assertRaises(ValueError):updates.NoRedirect().redirect_request(None,None,None,None,None,None)
    def test_fetch_size_and_cancel(self):
        response=MagicMock();response.status=200;response.read1.side_effect=[b'x'*4096]*5
        response.__enter__.return_value=response
        opener=MagicMock();opener.open.return_value=response
        with patch.object(updates,'build_opener',return_value=opener):
            with self.assertRaises(ValueError):updates.fetch_feed(threading.Event())
            request=opener.open.call_args.args[0]
            self.assertEqual(request.full_url,updates.FEED_URL)
            self.assertNotIn('Authorization',dict(request.header_items()))
            stop=threading.Event();stop.set();self.assertEqual(updates.fetch_feed(stop),b'')


class UpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def drain(self,checker):
        deadline=time.monotonic()+3
        while checker.worker is not None and time.monotonic()<deadline:
            self.app.processEvents();time.sleep(.002)
        self.assertIsNone(checker.worker)
    def test_dedupe_and_explicit_recheck(self):
        checker=updates.UpdateChecker('2.10.6',fetcher=lambda stop:feed());available=[];checker.available.connect(available.append)
        for manual in [False,False,True]:checker.check(manual);self.drain(checker)
        self.assertEqual(len(available),2);checker.shutdown()
    def test_failure_is_silent_automatically_and_reported_manually(self):
        def bad(stop):raise RuntimeError('https://proxy/?token=secret')
        checker=updates.UpdateChecker('2.10.6',fetcher=bad);errors=[];checker.failed.connect(lambda message,manual:errors.append((message,manual)))
        for manual in [False,True]:checker.check(manual);self.drain(checker)
        self.assertEqual([flag for _,flag in errors],[False,True]);self.assertNotIn('secret',str(errors));checker.shutdown()
    def test_shutdown_cancels_and_suppresses_late_notification(self):
        entered=threading.Event()
        def wait(stop):entered.set();stop.wait(2);return feed()
        checker=updates.UpdateChecker('2.10.6',fetcher=wait);received=[];checker.available.connect(received.append)
        checker.check();self.assertTrue(entered.wait(1));checker.check(True);checker.shutdown();self.drain(checker)
        self.assertEqual(received,[]);checker.check(True);self.assertIsNone(checker.worker)
    def test_no_update_has_separate_manual_result(self):
        checker=updates.UpdateChecker('2.10.6',fetcher=lambda stop:feed('2.10.6'));done=[];checker.checked.connect(done.append)
        checker.check(True);self.drain(checker);self.assertEqual(done,[True]);checker.shutdown()
    def test_window_offer_and_browser_open_do_not_replace_app(self):
        import desktop
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.Discovery,'start'):
            window=desktop.Window(Path(folder));window.timer.stop();window.show();self.app.processEvents()
            self.assertFalse(window.update_banner.isVisible());self.assertFalse(window.updater.initial.isActive())
            release=updates.Release('2.10.7','https://github.com/mshnyrev13/vcs-network-watch/releases/tag/v2.10.7')
            window.offer_update(release);self.assertTrue(window.update_banner.isVisible());self.assertIn('2.10.7',window.update_label.text())
            with patch.object(desktop.QDesktopServices,'openUrl',return_value=True) as open_url:
                window.update_download.click();self.assertEqual(open_url.call_args.args[0].toString(),release.url)
            window.show_about()
            with patch.object(window.updater,'check') as check:
                window.about_dialog.update_button.click();check.assert_called_once_with(True)
            close_window(window);self.assertTrue(window.updater.closed);window.store.db.close();window.deleteLater();self.app.processEvents()

if __name__=='__main__':unittest.main()
