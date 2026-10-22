"""Audit item 3: Windows engine data outside the user's folder, link checks before SYSTEM writes.

No service is installed: paths, SDDL, link handling and ACLs are checked in temporary folders.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import background_engine
import server
import service_paths

SID = 'S-1-5-21-1111111111-2222222222-3333333333-1001'
WINDOWS = os.name == 'nt'


def junction(link, target):
    subprocess.run(['cmd', '/d', '/c', 'mklink', '/J', str(link), str(target)], check=True, capture_output=True)


class Paths(unittest.TestCase):
    def test_folder_is_per_user_in_program_data(self):
        folder = service_paths.service_folder(SID, base=r'C:\ProgramData')
        self.assertEqual(folder.parent, Path(r'C:\ProgramData') / 'VCS Network Watch')
        self.assertRegex(folder.name, r'^[0-9a-f]{16}$')
        self.assertEqual(folder, service_paths.service_folder(SID.lower().replace('s-', 'S-', 1), base=r'C:\ProgramData'))
        self.assertNotEqual(folder, service_paths.service_folder(SID[:-4] + '1002', base=r'C:\ProgramData'))

    def test_invalid_sid_is_refused_before_any_path(self):
        for sid in ('S-1-5-21)(A;;GA;;;WD', '..\\..\\Windows', '', None, 'S-1'):
            with self.assertRaises(ValueError):
                service_paths.service_folder(sid)

    def test_user_gets_no_write_access_in_sddl(self):
        root, folder, key = service_paths.root_sddl(), service_paths.folder_sddl(SID), service_paths.key_sddl(SID)
        for sddl in (root, folder, key):
            self.assertTrue(sddl.startswith('O:SYD:P('), sddl)  # owner SYSTEM, protected DACL
            self.assertIn('(A;', sddl)
            self.assertIn(';FA;;;SY)', sddl)
            self.assertIn(';FA;;;BA)', sddl)
        # Users only list the root and the user's folder (not inherited), and read the key.
        self.assertIn('(A;;0x1200a9;;;BU)', root)
        self.assertIn('(A;;0x1200a9;;;' + SID + ')', folder)
        self.assertIn('(A;;FR;;;' + SID + ')', key)
        for sddl in (root, folder, key):
            for ace in re.findall(r'\(A;[^)]*\)', sddl):
                if not ace.endswith(';SY)') and not ace.endswith(';BA)'):
                    self.assertNotRegex(ace, r';(FA|FW|GA|GW|OI|CI)', ace)


class Links(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nw-links-')
        self.root = Path(self.temp.name)
        self.folder = self.root / 'service'
        self.folder.mkdir()
        self.outside = self.root / 'outside'
        self.outside.mkdir()
        self.victim = self.outside / 'victim.txt'
        self.victim.write_text('do not touch', encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def test_hard_link_is_removed_and_target_kept(self):
        os.link(self.victim, self.folder / 'network.sqlite3')
        (self.folder / 'service.lock').write_bytes(b'0')
        self.assertFalse(service_paths.plain_file(self.folder / 'network.sqlite3'))
        self.assertTrue(service_paths.plain_file(self.folder / 'service.lock'))
        self.assertEqual(service_paths.remove_unsafe(self.folder), ['network.sqlite3'])
        self.assertFalse(os.path.lexists(self.folder / 'network.sqlite3'))
        self.assertEqual(self.victim.read_text(encoding='utf-8'), 'do not touch')
        self.assertTrue((self.folder / 'service.lock').exists())

    @unittest.skipUnless(WINDOWS, 'NTFS junction')
    def test_junction_under_engine_name_is_unlinked_not_followed(self):
        junction(self.folder / 'errors-service.log', self.outside)
        self.assertTrue(service_paths.is_link(self.folder / 'errors-service.log'))
        self.assertEqual(service_paths.remove_unsafe(self.folder), ['errors-service.log'])
        self.assertEqual(os.listdir(self.outside), ['victim.txt'])

    @unittest.skipIf(WINDOWS, 'symbolic links need a privilege on Windows')
    def test_symlink_is_unlinked_not_followed(self):
        os.symlink(self.victim, self.folder / 'service.key')
        self.assertEqual(service_paths.remove_unsafe(self.folder), ['service.key'])
        self.assertEqual(self.victim.read_text(encoding='utf-8'), 'do not touch')

    def test_real_directory_under_engine_name_is_refused(self):
        (self.folder / 'service.lock').mkdir()
        with self.assertRaises(RuntimeError):
            service_paths.remove_unsafe(self.folder)

    @unittest.skipUnless(WINDOWS, 'owner check through a Windows handle')
    def test_foreign_owner_makes_engine_file_unsafe(self):
        path = self.folder / 'network.sqlite3'
        path.write_bytes(b'planted')
        actual_owner = service_paths.owner(path)
        # An elevated creator may default to Administrators as owner. Exercise
        # rejection and acceptance against the actual file owner, not TokenUser.
        foreign = service_paths.SYSTEM if actual_owner != service_paths.SYSTEM else SID
        self.assertEqual(service_paths.remove_unsafe(self.folder, owners=(foreign,)), ['network.sqlite3'])
        path.write_bytes(b'own')
        actual_owner = service_paths.owner(path)
        self.assertEqual(service_paths.remove_unsafe(self.folder, owners=(actual_owner,)), [])
        self.assertEqual(path.read_bytes(), b'own')

    def test_clear_never_truncates_through_hard_link(self):
        os.link(self.victim, self.folder / 'service-out.log')
        (self.folder / 'service-error.log').write_text('stderr', encoding='utf-8')
        store = server.Store(self.folder / 'network.sqlite3', retention_seconds=server.RETENTION_SECONDS)
        try:
            store.clear()
        finally:
            store.db.close()
        self.assertEqual(self.victim.read_text(encoding='utf-8'), 'do not touch')
        self.assertEqual((self.folder / 'service-error.log').read_text(encoding='utf-8'), '')


@unittest.skipUnless(WINDOWS, 'Windows security API')
class WindowsSecurity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='nw-acl-')
        self.root = Path(self.temp.name)
        self.me = service_paths.current_sid()
        # A normal user cannot hand ownership to SYSTEM: the same protected shape with their SID.
        self.sddl = 'D:P(A;OICI;FA;;;' + self.me + ')'

    def tearDown(self):
        self.temp.cleanup()

    def test_current_sid_matches_whoami(self):
        output = subprocess.run(['whoami', '/user', '/fo', 'csv', '/nh'], capture_output=True, text=True, check=True).stdout
        self.assertIn('"' + self.me + '"', output)

    def test_program_data_is_the_system_folder(self):
        self.assertTrue(Path(service_paths.program_data()).is_dir())

    def test_directory_gets_protected_dacl_through_handle(self):
        folder = self.root / 'engine'
        folder.mkdir()
        previous = service_paths.owner(folder)
        self.assertEqual(service_paths.secure_directory(folder, self.sddl), previous)
        self.assertEqual(service_paths.owner(folder), previous)  # no O: in this SDDL
        description = service_paths.describe(folder)
        self.assertIn('D:P', description)
        self.assertNotIn('(A;OICIID;', description)  # nothing inherited from the temp folder
        self.assertTrue(folder.is_dir())

    def test_explicit_owner_and_protected_dacl_are_applied(self):
        folder = self.root / 'explicit-owner'
        folder.mkdir()
        previous = service_paths.owner(folder)
        self.assertEqual(service_paths.secure_directory(folder, 'O:' + self.me + self.sddl), previous)
        self.assertEqual(service_paths.owner(folder), self.me)
        self.assertIn('D:P', service_paths.describe(folder))

    def test_planted_junction_is_replaced_by_real_folder(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'victim.txt').write_text('keep', encoding='utf-8')
        before = service_paths.describe(outside)
        junction(self.root / 'engine', outside)
        service_paths.secure_directory(self.root / 'engine', self.sddl)
        self.assertFalse(service_paths.is_link(self.root / 'engine'))
        self.assertEqual(os.listdir(self.root / 'engine'), [])
        self.assertEqual(service_paths.describe(outside), before)
        self.assertEqual((outside / 'victim.txt').read_text(encoding='utf-8'), 'keep')

    def test_file_in_place_of_folder_is_refused(self):
        (self.root / 'engine').write_text('x', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            service_paths.secure_directory(self.root / 'engine', self.sddl)

    def test_key_is_new_on_every_start_and_never_written_through_link(self):
        folder = self.root / 'engine'
        folder.mkdir()
        victim = self.root / 'victim.bin'
        victim.write_bytes(b'victim')
        os.link(victim, folder / 'service.key')
        key_sddl = 'D:P(A;;FA;;;' + self.me + ')'
        first = service_paths.write_key(folder, SID, key_sddl).read_bytes()
        second = service_paths.write_key(folder, SID, key_sddl).read_bytes()
        self.assertEqual((len(first), len(second)), (32, 32))
        self.assertNotEqual(first, second)
        self.assertEqual(victim.read_bytes(), b'victim')
        self.assertTrue(service_paths.plain_file(folder / 'service.key'))
        self.assertIn('D:P', service_paths.describe(folder / 'service.key'))

    def test_installer_scripts_compute_the_same_folder_id(self):
        for name in ('install-background-windows.ps1', 'remove-background-windows.ps1'):
            text = (ROOT / 'packaging' / name).read_text(encoding='utf-8')
            function = re.search(r'function Get-ServiceId\(.*?\n\}', text, re.S).group(0)
            command = function + "\nGet-ServiceId '" + SID + "'"
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.stdout.strip(), service_paths.folder_id(SID), name + result.stderr)


class Prepare(unittest.TestCase):
    def test_untrusted_previous_owner_wipes_every_engine_name(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'VCS Network Watch' / 'id'
            folder.mkdir(parents=True)
            for name in ('network.sqlite3', 'service.key', 'errors-service.log'):
                (folder / name).write_bytes(b'planted')
            (folder / 'notes.txt').write_text('foreign', encoding='utf-8')
            with patch.object(service_paths, 'enable_privileges'), patch.object(service_paths, 'secure_directory', side_effect=[service_paths.SYSTEM, 'S-1-5-21-9-9-9-1001']) as secure:
                removed = service_paths.prepare(folder, SID)
            self.assertEqual(sorted(removed), ['errors-service.log', 'network.sqlite3', 'service.key'])
            self.assertEqual(os.listdir(folder), ['notes.txt'])
            self.assertEqual([call.args[0] for call in secure.call_args_list], [folder.parent, folder])
            self.assertEqual(secure.call_args_list[1].args[1], service_paths.folder_sddl(SID))

    def test_trusted_folder_keeps_plain_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp) / 'VCS Network Watch' / 'id'
            folder.mkdir(parents=True)
            (folder / 'network.sqlite3').write_bytes(b'data')
            with patch.object(service_paths, 'enable_privileges'), patch.object(service_paths, 'secure_directory', return_value=service_paths.SYSTEM), patch.object(service_paths, 'owner', return_value=service_paths.SYSTEM):
                self.assertEqual(service_paths.prepare(folder, SID), [])
            self.assertEqual((folder / 'network.sqlite3').read_bytes(), b'data')

    def test_invalid_sid_touches_nothing(self):
        with patch.object(service_paths, 'secure_directory') as secure, self.assertRaises(ValueError):
            service_paths.prepare(Path('x'), 'S-1-5-21)(A;;GA;;;WD)')
        secure.assert_not_called()


class Engine(unittest.TestCase):
    def start(self, folder, **kwargs):
        stop = threading.Event()
        worker = threading.Thread(target=background_engine.run, args=(folder, stop), kwargs=kwargs, daemon=True)
        worker.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            proxy = background_engine.connect(folder)
            if proxy:
                return stop, worker, proxy
            time.sleep(.02)
        stop.set()
        worker.join(5)
        self.fail('engine did not answer')

    def test_service_mode_locks_folder_writes_key_and_restricts_pipe(self):
        calls = []
        def prepare(folder, sid):
            calls.append('prepare')
            Path(folder).mkdir(parents=True, exist_ok=True)
            return ['service.lock']
        def write_key(folder, sid):
            calls.append('key')
            (Path(folder) / 'service.key').write_bytes(os.urandom(32))
            if os.name!='nt':(Path(folder) / 'service.key').chmod(0o600)
        with tempfile.TemporaryDirectory(prefix='nw-svc-') as temp, patch.object(server.Monitor, 'session_workers', return_value=[]), \
                patch('power_guard.PowerGuard.start'), patch('power_guard.PowerGuard.stop'), \
                patch.object(background_engine.service_paths, 'prepare', side_effect=prepare), \
                patch.object(background_engine.service_paths, 'write_key', side_effect=write_key), \
                patch('windows_pipe.configure', side_effect=lambda sid: calls.append('pipe')):
            folder = Path(temp) / 'engine'
            stop, worker, proxy = self.start(folder, user_sid=SID)
            try:
                self.assertIn('session_data', proxy.status['capabilities'])
            finally:
                stop.set()
                worker.join(8)
            self.assertFalse(worker.is_alive())
            self.assertEqual(calls[:2], ['prepare', 'key'])
            self.assertEqual('pipe' in calls, WINDOWS)
            self.assertIn('удалены небезопасные записи', (folder / 'errors-service.log').read_text(encoding='utf-8'))

    def test_gui_session_data_goes_through_validated_ipc(self):
        with tempfile.TemporaryDirectory(prefix='nw-ipc-') as folder, patch.object(server.Monitor, 'session_workers', return_value=[]), \
                patch('power_guard.PowerGuard.start'), patch('power_guard.PowerGuard.stop'):
            stop, worker, proxy = self.start(folder)
            try:
                sid = proxy.start(server.validate_config({'targets': [{'host': 'ya.ru'}]}))
                proxy.record([[sid, 'speed', 't', time.time(), {'download': 42}]] + [[sid, 'graphene', 'g/' + str(i), time.time(), {'value': i}] for i in range(2500)])
                proxy.event(sid, 'synthetic speed event')
                for rows in ([[sid, 'engine_state', 'x', time.time(), {}]], [['missing', 'speed', 'x', time.time(), {}]],
                             [[sid, 'speed', 'x', float('nan'), {}]], [[sid, 'speed', 'x']], 'rows'):
                    with self.assertRaises(RuntimeError):
                        background_engine.request(folder, 'record', rows=rows)
                with self.assertRaises(RuntimeError):
                    proxy.event('missing', 'x')
                text = proxy.export(sid, {'192.0.2.1': 'hop.example'})
                doc = json.loads(text[text.index('{'):])
                kinds = [row['kind'] for row in doc['session_data']]
                self.assertEqual(kinds.count('graphene'), 2500)
                self.assertIn({'download': 42}, [row['data'] for row in doc['session_data'] if row['kind'] == 'speed'])
                self.assertIn('synthetic speed event', [event['message'] for event in doc['events']])
                self.assertEqual(doc['resolved_names']['192.0.2.1'], 'hop.example')
                proxy.clear()
            finally:
                stop.set()
                worker.join(8)
            self.assertFalse(worker.is_alive())


class Launch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global desktop
        import desktop

    def test_windows_task_names_only_the_user(self):
        with patch.object(desktop.background_engine, 'run') as run:
            desktop.engine_service(['--engine-service', '--user-sid', SID])
        run.assert_called_once_with(service_paths.service_folder(SID), user_sid=SID)
        with patch.object(desktop.background_engine, 'run') as run:
            desktop.engine_service(['--engine-service', '--data-dir', 'data'])
        run.assert_called_once_with('data')
        for argv in (['--engine-service'], ['--engine-service', '--data-dir', 'd', '--user-sid', SID]):
            with patch.object(desktop.background_engine, 'run') as run, patch('sys.stderr'), self.assertRaises(SystemExit):
                desktop.engine_service(argv)
            run.assert_not_called()

    def test_gui_looks_for_windows_engine_in_program_data(self):
        folder = Path('user-data')
        self.assertEqual(desktop.service_location(folder, ['app'], 'darwin'), folder)
        for flag in ('--data-dir', '--smoke-test'):
            self.assertEqual(desktop.service_location(folder, ['app', flag], 'win32'), folder)
        with patch.object(desktop.service_paths, 'current_sid', return_value=SID), patch.object(desktop.service_paths, 'program_data', return_value=r'C:\ProgramData'):
            self.assertEqual(desktop.service_location(folder, ['app'], 'win32'), Path(r'C:\ProgramData') / 'VCS Network Watch' / service_paths.folder_id(SID))

    def test_engine_left_in_user_folder_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            other = folder / 'program-data'
            with patch.object(desktop.background_engine, 'connect', return_value=object()) as connect:
                self.assertFalse(desktop.legacy_service(folder, other))  # no key: nothing installed there
                (folder / 'service.key').write_bytes(b'k')
                self.assertTrue(desktop.legacy_service(folder, other))
                self.assertTrue(desktop.legacy_service(folder, False))
                self.assertFalse(desktop.legacy_service(folder, folder))  # macOS/development: same folder
                connect.assert_called_with(folder)
            with patch.object(desktop.background_engine, 'connect', return_value=None):
                self.assertFalse(desktop.legacy_service(folder, other))


if __name__ == '__main__':
    unittest.main()
