import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mac_background, background_engine, server


class FirstLaunch(unittest.TestCase):
    def setUp(self):
        self.folder=Path('/private/tmp/nw-first-launch-test')
        self.patches=[patch.object(sys,'platform','darwin'),patch.object(sys,'frozen',True,create=True),
                      patch.object(sys,'argv',['VCS Network Watch']),
                      patch.object(sys,'executable',str(mac_background.APP_BINARY)),
                      # Simulated macOS paths also run on Windows, where os.getuid is absent.
                      patch.object(mac_background.os,'getuid',return_value=501,create=True),
                      patch.object(Path,'exists',return_value=False),
                      patch.object(mac_background,'pending_update',return_value=None),
                      patch.object(mac_background,'remember_update',side_effect=lambda folder,sid,paused:dict(sid=sid,paused=bool(paused)))]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])

    def service(self, version=None, sid=None, paused=False, executable=None):
        return Mock(sid=sid,status={'version':version or server.VERSION,'active_id':sid,
            'paused':paused,'executable':executable or str(mac_background.APP_BINARY),
            'capabilities':list(background_engine.GUI_CAPABILITIES)})

    def test_running_service_never_invokes_installer(self):
        with patch.object(mac_background.background_engine,'connect',return_value=self.service()),patch.object(mac_background.subprocess,'run') as run:
            self.assertEqual(mac_background.prepare(self.folder),('ready',''));run.assert_not_called()

    def test_stale_engine_is_upgraded_and_active_session_resumed_with_pause(self):
        old=self.service('2.8.1','existing-session',True,executable='/Applications/Old.app/engine')
        new=self.service()
        with patch.object(Path,'exists',return_value=True),patch.object(mac_background.background_engine,'connect',side_effect=[old,old,new]),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)) as run,patch.object(mac_background.time,'sleep'):
            self.assertEqual(mac_background.prepare(self.folder),('ready',''))
        self.assertEqual(run.call_args.args[0][-2:],['--gui','--upgrade'])
        new.resume.assert_called_once_with('existing-session',paused=True)

    def test_same_version_from_old_app_path_still_requires_rebinding(self):
        old=self.service(executable='/Applications/Old.app/engine');new=self.service()
        with patch.object(Path,'exists',return_value=True),patch.object(mac_background.background_engine,'connect',side_effect=[old,new]),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)) as run:
            self.assertEqual(mac_background.prepare(self.folder),('ready',''))
        self.assertIn('--upgrade',run.call_args.args[0]);new.resume.assert_not_called()

    def test_cancelled_upgrade_cannot_connect_gui_to_old_engine_or_start_another(self):
        with patch.object(Path,'exists',return_value=True),patch.object(mac_background.background_engine,'connect',return_value=self.service('2.8.1','session')),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=1)):
            status,message=mac_background.prepare(self.folder)
        self.assertEqual(status,'unavailable');self.assertIn('Данные сохранены',message)

    def test_recovery_never_overwrites_another_active_session(self):
        new=self.service(sid='different-session')
        with patch.object(Path,'exists',return_value=True),patch.object(mac_background.background_engine,'connect',side_effect=[self.service('2.8.1','original-session'),new]),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)):
            status,message=mac_background.prepare(self.folder)
        self.assertEqual(status,'unavailable');new.resume.assert_not_called()

    def test_old_unregistered_process_is_not_killed(self):
        with patch.object(mac_background.background_engine,'connect',return_value=self.service('2.8.1')),patch.object(mac_background.subprocess,'run') as run:
            self.assertEqual(mac_background.prepare(self.folder)[0],'unavailable');run.assert_not_called()

    def test_older_gui_cannot_downgrade_a_newer_engine(self):
        with patch.object(mac_background.background_engine,'connect',return_value=self.service('99.0.0')),patch.object(mac_background.subprocess,'run') as run:
            status,message=mac_background.prepare(self.folder)
        self.assertEqual(status,'unavailable');self.assertIn('понижение',message);run.assert_not_called()

    def test_unreachable_registered_job_is_reconfigured(self):
        with patch.object(Path,'exists',return_value=True),patch.object(mac_background.background_engine,'connect',side_effect=[None,self.service()]),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)) as run:
            self.assertEqual(mac_background.prepare(self.folder),('ready',''))
        self.assertIn('--upgrade',run.call_args.args[0])

    def test_gui_rejects_old_engine_even_with_compatible_protocol_before_db_open(self):
        import desktop
        app=desktop.QApplication.instance() or desktop.QApplication([])
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.background_engine,'connect',return_value=self.service('2.8.1')),patch.object(desktop.server,'Store') as store:
            with self.assertRaisesRegex(RuntimeError,'2.8.1.*'+server.VERSION):desktop.Window(Path(folder),require_service=True)
            store.assert_not_called()

    def test_next_launch_recovers_interrupted_upgrade_without_second_installer(self):
        new=self.service()
        with patch.object(mac_background,'pending_update',return_value={'sid':'original','paused':True}),patch.object(mac_background.background_engine,'connect',return_value=new),patch.object(mac_background.subprocess,'run') as run,patch.object(Path,'unlink'):
            self.assertEqual(mac_background.prepare(self.folder),('ready',''))
        new.resume.assert_called_once_with('original',paused=True);run.assert_not_called()

    def test_failed_upgrade_keeps_pending_state_for_next_launch(self):
        with patch.object(Path,'exists',return_value=True),patch.object(Path,'unlink') as unlink,patch.object(mac_background.background_engine,'connect',return_value=self.service('2.8.1','original')),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=1)),patch.object(mac_background,'remember_update',return_value={'sid':'original','paused':False}) as remember:
            self.assertEqual(mac_background.prepare(self.folder)[0],'unavailable')
        remember.assert_called_once_with(self.folder,'original',False);unlink.assert_not_called()

    @unittest.skipIf(sys.platform=='win32','POSIX paths and imports under simulated darwin')
    def test_first_launch_installs_then_waits_for_ipc(self):
        with patch.object(mac_background.background_engine,'connect',side_effect=[None,None,self.service()]),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)) as run,patch.object(mac_background.time,'sleep'):
            self.assertEqual(mac_background.prepare(self.folder),('ready',''))
            args=run.call_args.args[0];self.assertEqual(args[0],'/bin/sh');self.assertEqual(args[-1],'--gui')
            self.assertTrue(args[1].endswith('packaging/install-background-mac.command'))

    def test_cancel_authentication_keeps_foreground_available(self):
        with patch.object(mac_background.background_engine,'connect',return_value=None),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=1)),patch.object(Path,'exists',return_value=False):
            self.assertEqual(mac_background.prepare(self.folder)[0],'foreground')

    def test_existing_unreachable_job_prevents_second_engine(self):
        with patch.object(mac_background.background_engine,'connect',return_value=None),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=1)),patch.object(Path,'exists',return_value=True):
            self.assertEqual(mac_background.prepare(self.folder)[0],'unavailable')

    def test_success_without_ipc_is_not_claimed_ready(self):
        with patch.object(mac_background.background_engine,'connect',return_value=None),patch.object(mac_background.subprocess,'run',return_value=Mock(returncode=0)),patch.object(mac_background.time,'monotonic',side_effect=[0,21]):
            self.assertEqual(mac_background.prepare(self.folder)[0],'unavailable')

    def test_dmg_launch_does_not_install_job_pointing_to_removable_app(self):
        with patch.object(sys,'executable','/Volumes/Network Watch/VCS Network Watch.app/Contents/MacOS/VCS Network Watch'),patch.object(mac_background.background_engine,'connect',return_value=None),patch.object(mac_background.subprocess,'run') as run:
            status,message=mac_background.prepare(self.folder);self.assertEqual(status,'skipped');self.assertIn('Applications',message);run.assert_not_called()

    def test_windows_development_and_smoke_launches_never_install(self):
        with patch.object(mac_background.subprocess,'run') as run:
            with patch.object(sys,'platform','win32'):self.assertEqual(mac_background.prepare(self.folder)[0],'skipped')
            with patch.object(sys,'frozen',False):self.assertEqual(mac_background.prepare(self.folder)[0],'skipped')
            for flag in ('--smoke-test','--data-dir'):
                with patch.object(sys,'argv',['app',flag]):self.assertEqual(mac_background.prepare(self.folder)[0],'skipped')
            run.assert_not_called()

    @unittest.skipIf(sys.platform=='win32','POSIX paths and imports under simulated darwin')
    def test_gui_cannot_recover_database_if_ready_service_disappears(self):
        import desktop
        app=desktop.QApplication.instance() or desktop.QApplication([])
        with tempfile.TemporaryDirectory() as folder,patch.object(desktop.background_engine,'connect',return_value=None),patch.object(desktop.server,'Store') as store:
            with self.assertRaises(RuntimeError):desktop.Window(Path(folder),require_service=True)
            store.assert_not_called()

class ServiceRecovery(unittest.TestCase):
    def test_authenticated_resume_keeps_history_adapter_and_pause(self):
        import json
        import threading
        import time
        with tempfile.TemporaryDirectory(prefix='lr-upgrade-test-') as folder,patch.object(server.Monitor,'session_workers',return_value=[]),patch('power_guard.PowerGuard.start'),patch('power_guard.PowerGuard.stop'):
            store=server.Store(Path(folder)/'network.sqlite3',recover=False)
            config=server.validate_config({'continuous':True,'interface':'en0','targets':[{'host':'192.0.2.1'}]})
            started=time.time();sid='same-session';target=config['targets'][0]['id']
            store.execute('INSERT INTO sessions VALUES (?,?,?,?,?)',(sid,started,started+1,'stopped',json.dumps(config)))
            store.execute('INSERT INTO samples VALUES (?,?,?,?,?,?,?)',(sid,target,started,'ok',12,'192.0.2.1','previous result'))
            store.db.close()
            stop=threading.Event();worker=threading.Thread(target=background_engine.run,args=(folder,stop));worker.start()
            try:
                deadline=time.monotonic()+3;proxy=None
                while time.monotonic()<deadline and not proxy:
                    proxy=background_engine.connect(folder);time.sleep(.01)
                self.assertIsNotNone(proxy);self.assertTrue(background_engine.current(proxy.status))
                self.assertIsNone(proxy.sid)
                self.assertEqual(proxy.resume(sid,paused=True),sid)
                new=background_engine.connect(folder)
                self.assertEqual(new.status['config']['interface'],'en0');self.assertTrue(new.status['paused'])
                snapshot=new.snapshot(sid,300)
                self.assertEqual(snapshot['session']['started'],started)
                self.assertEqual(snapshot['targets'][0]['last']['detail'],'previous result')
                self.assertEqual(snapshot['targets'][0]['window_stats']['count'],1)
                with self.assertRaises(RuntimeError):new.resume(sid)
                new.clear()
            finally:
                stop.set();worker.join(10);self.assertFalse(worker.is_alive())


class UpgradeJournal(unittest.TestCase):
    def test_state_survives_new_process_and_clears_only_after_resume(self):
        import stat
        with tempfile.TemporaryDirectory() as folder:
            value=mac_background.remember_update(folder,'original',True)
            self.assertEqual(mac_background.pending_update(folder),value)
            # Windows st_mode does not represent DACLs. This is the macOS upgrade
            # journal; keep its POSIX privacy assertion on the platforms that have it.
            if os.name!='nt':
                self.assertEqual(stat.S_IMODE((Path(folder)/'engine-update.json').stat().st_mode),0o600)
            service=Mock(sid=None)
            service.resume.side_effect=RuntimeError('Temporarily unavailable')
            with self.assertRaises(RuntimeError):mac_background.restore_update(folder,service,value)
            self.assertIsNotNone(mac_background.pending_update(folder))
            service.resume.side_effect=None
            mac_background.restore_update(folder,service,value)
            self.assertIsNone(mac_background.pending_update(folder))

    def test_expired_update_is_not_resumed_after_retention(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(mac_background.time,'time',return_value=1):mac_background.remember_update(folder,'old',False)
            self.assertIsNone(mac_background.pending_update(folder))


if __name__=='__main__':unittest.main()
