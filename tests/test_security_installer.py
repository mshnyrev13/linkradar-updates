"""Execute privileged Mac logic with fake commands and a temporary filesystem."""
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import shlex
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

@unittest.skipUnless(sys.platform=='darwin','macOS plutil fixture')
class MacInstaller(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='lr-sec-install-');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bin=self.root/'bin';self.bin.mkdir();self.daemons=self.root/'daemons';self.daemons.mkdir()
        self.path=self.daemons/'tech.vcs.network-watch.engine.501.plist'
        fake='''#!PYTHON
import json,os,pathlib,shutil,stat,sys
root=pathlib.Path(ROOT);name=pathlib.Path(sys.argv[0]).name;a=sys.argv[1:]
with (root/'calls').open('a') as f:f.write(json.dumps([name,a])+'\\n')
if name=='id':print('0' if a==['-u'] else 'tester')
elif name=='dscl':print('NFSHomeDirectory: /Users/tester')
elif name=='stat':
 p=pathlib.Path(a[-1]);fmt=a[1]
 print(('999' if (root/'foreign-owner').exists() else '0') if fmt=='%u' else str(p.stat().st_nlink) if fmt=='%l' else oct(stat.S_IMODE(p.stat().st_mode))[2:])
elif name=='install':shutil.copyfile(a[-2],a[-1]);os.chmod(a[-1],0o644)
elif name=='launchctl':
 loaded=root/'loaded'
 if a[0]=='print':sys.exit(0 if loaded.exists() else 1)
 if a[0]=='bootout':loaded.unlink(missing_ok=True)
 if a[0]=='bootstrap':
  if (root/'fail-once').exists():(root/'fail-once').unlink();sys.exit(1)
  loaded.touch()
'''.replace('PYTHON',sys.executable).replace('ROOT',repr(str(self.root)))
        text=(ROOT/'packaging/configure-background-mac.sh').read_text(encoding='utf-8')
        for command in ('/usr/bin/id','/usr/bin/dscl','/usr/bin/stat','/usr/bin/install','/bin/launchctl'):
            target=self.bin/Path(command).name;target.write_text(fake);target.chmod(0o755);text=text.replace(command,str(target))
        text=text.replace('/Library/LaunchDaemons',str(self.daemons)).replace('/private/tmp/linkradar-launchd.',str(self.root/'stage.'))
        self.script=self.root/'configure.sh';self.script.write_text(text)
    def run_script(self,*args):
        return subprocess.run(['/bin/sh',str(self.script),*args],capture_output=True,text=True,timeout=8)
    def test_fixed_user_and_program_no_root_open_in_user_profile(self):
        p=self.run_script('501','0');self.assertEqual(p.returncode,0,p.stderr)
        d=plistlib.loads(self.path.read_bytes());self.assertEqual(d['UserName'],'tester')
        self.assertEqual(d['ProgramArguments'],['/Applications/LinkRadar.app/Contents/MacOS/LinkRadar','--engine-service','--data-dir','/Users/tester/Library/Application Support/VCS Network Watch'])
        self.assertEqual(d['StandardOutPath'],'/dev/null');self.assertEqual(d['StandardErrorPath'],'/dev/null')
        self.assertFalse(list(self.root.glob('stage.*')))
    def test_caller_cannot_supply_a_root_plist(self):
        malicious=self.root/'user.plist';malicious.write_bytes(plistlib.dumps({'Label':'bad','ProgramArguments':['/does-not-exist']}))
        p=self.run_script(str(malicious),'501','1');self.assertNotEqual(p.returncode,0);self.assertFalse(self.path.exists())
    def test_invalid_uids_rejected(self):
        for uid in ('0','500','501;bad','-1'):
            self.assertNotEqual(self.run_script(uid,'1').returncode,0)
        self.assertFalse(self.path.exists())
    def test_link_and_hardlink_destination_never_written(self):
        victim=self.root/'victim';victim.write_text('canary')
        self.path.symlink_to(victim)
        self.assertNotEqual(self.run_script('501','1').returncode,0);self.assertEqual(victim.read_text(encoding='utf-8'),'canary')
        self.path.unlink();os.link(victim,self.path)
        self.assertNotEqual(self.run_script('501','1').returncode,0);self.assertEqual(victim.read_text(encoding='utf-8'),'canary')
    def test_foreign_owner_is_rejected(self):
        (self.root/'foreign-owner').touch();self.assertNotEqual(self.run_script('501','0').returncode,0)
        self.assertFalse(self.path.exists())

    def test_unexpected_extended_acl_destination_is_refused(self):
        subprocess.run(['/bin/chmod','+a','everyone allow read',str(self.daemons)],check=True)
        try:self.assertNotEqual(self.run_script('501','0').returncode,0)
        finally:subprocess.run(['/bin/chmod','-N',str(self.daemons)],check=True)
        self.assertFalse(self.path.exists())
    def test_failed_upgrade_rolls_back_with_safe_stdio(self):
        old={'Label':'tech.vcs.network-watch.engine.501','UserName':'tester','ProgramArguments':['/old/engine'],'StandardOutPath':'/Users/tester/unsafe.log','StandardErrorPath':'/Users/tester/unsafe-err.log'}
        self.path.write_bytes(plistlib.dumps(old));self.path.chmod(0o644)
        (self.root/'loaded').touch();(self.root/'fail-once').touch()
        p=self.run_script('501','1');self.assertNotEqual(p.returncode,0)
        restored=plistlib.loads(self.path.read_bytes());self.assertEqual(restored['ProgramArguments'],old['ProgramArguments'])
        self.assertEqual(restored['StandardOutPath'],'/dev/null');self.assertTrue((self.root/'loaded').exists())

    def test_wrapper_uses_snapshot_when_source_changes_at_authorization(self):
        wrapper=self.root/'install-background-mac.command'
        payload=self.root/'configure-background-mac.sh';marker=self.root/'marker'
        payload.write_text('printf SAFE > '+shlex.quote(str(marker)))
        app=self.root/'app';app.touch();app.chmod(0o755)
        stub=self.root/'authorize'
        stub.write_text('#!'+sys.executable+'\nimport pathlib,subprocess,sys\n'+
                        'pathlib.Path('+repr(str(payload))+').write_text("printf MUTATED > '+str(marker)+'")\n'+
                        'subprocess.run(["/bin/sh","-c",sys.argv[2],"sh",*sys.argv[3:]],check=True)\n')
        stub.chmod(0o755)
        source=(ROOT/'packaging/install-background-mac.command').read_text(encoding='utf-8')
        source=source.replace('/Applications/LinkRadar.app/Contents/MacOS/LinkRadar',str(app)).replace('/usr/bin/osascript',str(stub))
        source=source.replace('task_user=$(id -un)','task_user=tester').replace('task_uid=$(id -u)','task_uid=501')
        source=source.replace('test "$(id -u)" != 0','test 501 != 0')
        line=next(line for line in source.splitlines() if line.startswith('task_home='))
        source=source.replace(line,'task_home='+shlex.quote(str(self.root/'home')))
        wrapper.write_text(source)
        p=subprocess.run(['/bin/sh',str(wrapper),'--gui','--upgrade'],capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr);self.assertEqual(marker.read_text(encoding='utf-8'),'SAFE')

if __name__=='__main__':unittest.main()
