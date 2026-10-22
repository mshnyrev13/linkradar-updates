"""BPF authorization and installer tests; no system permissions/captures are changed."""
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import mac_capture

ROOT=Path(__file__).resolve().parents[1]

class CaptureSetup(unittest.TestCase):
 def setUp(self):
  self.patches=[patch.object(sys,'platform','darwin'),patch.object(sys,'frozen',True,create=True),patch.object(sys,'argv',['Network Watch']),patch.object(sys,'executable',str(mac_capture.APP_BINARY))]
  for p in self.patches:p.start()
  self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
 def test_existing_access_never_asks_for_admin(self):
  with patch.object(mac_capture,'access_ready',return_value=True),patch.object(subprocess,'run') as run:
   self.assertEqual(mac_capture.prepare(),('ready',''));run.assert_not_called()
 def test_missing_access_is_installed_before_readiness_is_claimed(self):
  with patch.object(mac_capture,'access_ready',side_effect=[False,False,True]),patch.object(subprocess,'run',return_value=Mock(returncode=0)) as run,patch.object(mac_capture.time,'sleep'):
   self.assertEqual(mac_capture.prepare(),('ready',''))
   self.assertEqual(run.call_args.args[0][-1],'--gui');self.assertTrue(run.call_args.args[0][1].endswith('install-capture-access-mac.command'))
   self.assertNotIn('timeout',run.call_args.kwargs)
 def test_cancel_keeps_ping_available_without_claiming_capture_ready(self):
  with patch.object(mac_capture,'access_ready',return_value=False),patch.object(subprocess,'run',return_value=Mock(returncode=1)):
   status,message=mac_capture.prepare();self.assertEqual(status,'cancelled');self.assertIn('ping работает',message)
 def test_membership_needs_relogin_is_explicit(self):
  with patch.object(mac_capture,'access_ready',return_value=False),patch.object(subprocess,'run',return_value=Mock(returncode=0)),patch.object(mac_capture.time,'monotonic',side_effect=[0,4]):
   status,message=mac_capture.prepare();self.assertEqual(status,'restart');self.assertIn('войдите снова',message)
 def test_installer_failure_keeps_normal_app_usable(self):
  with patch.object(mac_capture,'access_ready',return_value=False),patch.object(subprocess,'run',side_effect=OSError()):
   status,message=mac_capture.prepare();self.assertEqual(status,'error');self.assertIn('ping работает',message)
 def test_dmg_does_not_install_system_job(self):
  with patch.object(mac_capture,'access_ready',return_value=False),patch.object(sys,'executable','/Volumes/Watch/App'),patch.object(subprocess,'run') as run:
   status,message=mac_capture.prepare();self.assertEqual(status,'skipped');self.assertIn('Applications',message);run.assert_not_called()
 def test_development_windows_smoke_and_service_never_request_auth(self):
  with patch.object(mac_capture,'access_ready') as access,patch.object(subprocess,'run') as run:
   for flag in ('--smoke-test','--data-dir','--engine-service'):
    with patch.object(sys,'argv',['app',flag]):self.assertEqual(mac_capture.prepare()[0],'skipped')
   with patch.object(sys,'platform','win32'):self.assertEqual(mac_capture.prepare()[0],'skipped')
   with patch.object(sys,'frozen',False):self.assertEqual(mac_capture.prepare()[0],'skipped')
   access.assert_not_called();run.assert_not_called()
 def test_readiness_checks_devices_without_opening_them(self):
  device=Mock();device.is_symlink.return_value=False;device.stat.return_value=Mock(st_mode=stat.S_IFCHR|0o660)
  with patch.object(Path,'glob',return_value=[device]),patch.object(os,'access',return_value=True) as access,patch('builtins.open',side_effect=AssertionError('No capture/device open')):
   self.assertTrue(mac_capture.access_ready());access.assert_called_once_with(device,os.R_OK|os.W_OK)
  device.is_symlink.return_value=True
  with patch.object(Path,'glob',return_value=[device]):self.assertFalse(mac_capture.access_ready())
 @unittest.skipUnless(sys.platform=='darwin','macOS application startup with POSIX lock')
 def test_running_background_engine_does_not_skip_capture_preflight(self):
  import desktop
  events=[]
  with tempfile.TemporaryDirectory() as folder,patch.object(sys,'argv',['app','--data-dir',folder]),patch.object(desktop,'QApplication') as app,patch.object(desktop,'QLabel'),patch.object(desktop.error_log,'setup'),patch.object(desktop,'qInstallMessageHandler'),patch.object(desktop,'Window') as window,patch.object(desktop.mac_capture,'prepare',side_effect=lambda:(events.append('capture') or ('ready',''))),patch.object(desktop.mac_background,'prepare',side_effect=lambda f:(events.append('engine') or ('ready',''))):
   desktop.main();self.assertEqual(events,['capture','engine']);window.assert_called_once_with(Path(folder),require_service=True,service_folder=Path(folder))


@unittest.skipUnless(sys.platform=='darwin','macOS shell filesystem fixture')
class InstallerFixture(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.root=Path(self.temp.name);self.log=self.root/'calls';self.bin=self.root/'bin';self.bin.mkdir()
  self.helper_dir=self.root/'PrivilegedHelperTools/tech.vcs.network-watch.capture';self.daemons=self.root/'LaunchDaemons';self.daemons.mkdir();(self.root/'PrivilegedHelperTools').mkdir()
  self.dev=self.root/'dev';self.dev.mkdir()
  for name in ('bpf0','bpf1'):(self.dev/name).touch()
  fake='''#!PYTHON
import os,sys,json,shutil,pathlib
name=pathlib.Path(sys.argv[0]).name;args=sys.argv[1:];root=pathlib.Path(ROOT)
with (root/'calls').open('a') as f:f.write(json.dumps([name,args])+'\\n')
if name=='id':print('0' if args==['-u'] else 'tester')
elif name=='sysctl':print('2')
elif name=='stat':print('501' if (root/'unsafe-owner').exists() else '0' if args[1]=='%u' else '755')
elif name=='dscl':sys.exit(0 if (root/'group').exists() else 1)
elif name=='dseditgroup':(root/'group').touch()
elif name=='chgrp':pass
elif name=='chmod':
 if args[0].isdigit():os.chmod(args[1],int(args[0],8))
elif name=='install':
 out=[];i=0;mode=0o755
 while i<len(args):
  if args[i] in ('-o','-g','-m'):
   if args[i]=='-m':mode=int(args[i+1],8)
   i+=2
  else:out.append(args[i]);i+=1
 if out[0]=='-d':path=pathlib.Path(out[1]);path.mkdir(parents=True,exist_ok=True)
 else:path=pathlib.Path(out[1]);shutil.copyfile(out[0],path)
 os.chmod(path,mode)
elif name=='launchctl':
 state=root/'loaded'
 if args[0]=='print':sys.exit(0 if state.exists() else 1)
 if args[0]=='bootstrap':state.touch()
 if args[0]=='bootout':state.unlink(missing_ok=True)
'''.replace('PYTHON',sys.executable).replace('ROOT',repr(str(self.root)))
  self.script=(ROOT/'packaging/bpf-access-root.sh').read_text(encoding='utf-8')
  commands=['/usr/bin/id','/usr/sbin/sysctl','/usr/bin/stat','/usr/bin/dscl','/usr/sbin/dseditgroup','/usr/bin/chgrp','/bin/chmod','/usr/bin/install','/bin/launchctl']
  for command in commands:
   target=self.bin/Path(command).name;target.write_text(fake);target.chmod(0o755);self.script=self.script.replace(command,str(target))
  self.script=self.script.replace('/Library/PrivilegedHelperTools',str(self.root/'PrivilegedHelperTools')).replace('/Library/LaunchDaemons',str(self.daemons)).replace('/dev/bpf',str(self.dev/'bpf')).replace('<string>/dev</string>',f'<string>{self.dev}</string>')
  # Only fixture files are regular; production requires actual character devices.
  self.script=self.script.replace('test -c "$device"','test -f "$device"')
  self.path=self.root/'root-fixture.sh';self.path.write_text(self.script)
 def run_script(self,action='install',uid='501',arguments=()):
  env=dict(os.environ,VCS_BPF_UID=uid,VCS_BPF_ACTION=action,VCS_BPF_SOURCE=self.script)
  return subprocess.run(['/bin/sh',str(self.path),*arguments],env=env,capture_output=True,text=True,timeout=10)
 def calls(self):return [json.loads(line) for line in self.log.read_text(encoding='utf-8').splitlines()]
 def test_install_grants_only_requesting_account_and_installs_fixed_root_job(self):
  result=self.run_script();self.assertEqual(result.returncode,0,result.stderr)
  additions=[a for n,a in self.calls() if n=='dseditgroup' and '-a' in a]
  self.assertEqual(additions,[['-q','-o','edit','-a','tester','-t','user','access_bpf']])
  helper=self.helper_dir/'bpf-access.sh';self.assertEqual(helper.read_text(encoding='utf-8'),self.script+'\n');self.assertEqual(stat.S_IMODE(helper.stat().st_mode),0o755)
  plist=plistlib.loads((self.daemons/'tech.vcs.network-watch.bpf.plist').read_bytes())
  self.assertEqual(plist['ProgramArguments'],[str(helper),'--apply']);self.assertTrue(plist['RunAtLoad']);self.assertEqual(plist['WatchPaths'],[str(self.dev)])
  self.assertNotIn('desktop.py',str(plist));self.assertNotIn('UserName',plist)
  self.assertEqual([a[0] for n,a in self.calls() if n=='chmod' and a[0].startswith('g')],['g+rw','g+rw'])
 def test_installed_apply_path_does_not_change_membership_or_execute_app(self):
  self.assertEqual(self.run_script().returncode,0);self.log.write_text('')
  result=subprocess.run(['/bin/sh',str(self.helper_dir/'bpf-access.sh'),'--apply'],capture_output=True,text=True,timeout=10)
  self.assertEqual(result.returncode,0,result.stderr);self.assertFalse(any(n=='dseditgroup' for n,a in self.calls()))
 def test_symlink_or_foreign_owned_destination_is_rejected_before_group_change(self):
  self.helper_dir.symlink_to(self.dev,target_is_directory=True)
  self.assertNotEqual(self.run_script().returncode,0);self.assertFalse(any(n=='dseditgroup' for n,a in self.calls()))
  self.helper_dir.unlink();self.helper_dir.mkdir();(self.root/'unsafe-owner').touch();self.log.write_text('')
  self.assertNotEqual(self.run_script().returncode,0);self.assertFalse(any(n=='dseditgroup' for n,a in self.calls()))
 def test_invalid_uid_fails_before_group_changes(self):
  for uid in ('0','123','501;touch injected'):
   result=self.run_script(uid=uid);self.assertNotEqual(result.returncode,0)
  self.assertFalse(any(n=='dseditgroup' for n,a in self.calls()))
 def test_remove_keeps_shared_group_and_wifi_tools_untouched(self):
  self.assertEqual(self.run_script().returncode,0);self.log.write_text('')
  result=self.run_script('remove');self.assertEqual(result.returncode,0,result.stderr)
  self.assertFalse(self.helper_dir.exists());self.assertFalse((self.daemons/'tech.vcs.network-watch.bpf.plist').exists())
  self.assertFalse(any(n=='dseditgroup' for n,a in self.calls()));self.assertTrue((self.root/'group').exists())

if __name__=='__main__':unittest.main()
