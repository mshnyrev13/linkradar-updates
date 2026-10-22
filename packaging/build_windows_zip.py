"""Build the Windows x64 portable ZIP from this checkout.

The embedded Python runtime, installed packages and the GUI launcher are taken
from a previous Windows ZIP (they change only with requirements-desktop.txt or
windows_launcher.c); application code, scripts and documentation come from the
checkout. Usage:

    python packaging/build_windows_zip.py --base VCS-Network-Watch-2.8.1-Windows-x64.zip --out dist
"""
import argparse
import base64
import csv
import hashlib
import shutil
import struct
import sys
import tempfile
import zipfile
import build_manifest
from pathlib import Path
from pathlib import PurePosixPath

ROOT=Path(__file__).resolve().parents[1]
FOLDER='LinkRadar'
REUSED=('runtime','packages','Network Watch.exe','LinkRadar.exe')
SCRIPTS=('Install background.cmd','Remove background.cmd','install-background-windows.ps1','remove-background-windows.ps1','windows-service-safety.ps1')
DOCS={'RELEASE-NOTES-2.10.10.md':'RELEASE-NOTES-2.10.10.md','README-Windows.md':'README.md','STANDALONE.md':'STANDALONE.md','STREAM.md':'STREAM.md','SECURITY-AUDIT.md':'SECURITY-AUDIT.md','SPEED-AUDIT.md':'SPEED-AUDIT.md','AUDIT-2.10.3.md':'AUDIT-2.10.3.md','AUDIT-2.10.4.md':'AUDIT-2.10.4.md','AUDIT-2.10.5.md':'AUDIT-2.10.5.md','AUDIT-2.10.7.md':'AUDIT-2.10.7.md','AUDIT-2.10.8.md':'AUDIT-2.10.8.md','AUDIT-2.10.9.md':'AUDIT-2.10.9.md','RELEASE-NOTES-2.10.9.md':'RELEASE-NOTES-2.10.9.md','RELEASE-NOTES-2.10.8.md':'RELEASE-NOTES-2.10.8.md','RELEASE-NOTES-2.10.7.md':'RELEASE-NOTES-2.10.7.md','UPDATES.md':'UPDATES.md','RELEASE-NOTES-2.10.6.md':'RELEASE-NOTES-2.10.6.md'}
LAUNCHER_BAT='@echo off\r\ncd /d "%~dp0"\r\nstart "" "LinkRadar.exe"\r\n'

def brand_launcher(data):
    """Replace only display strings in our unsigned legacy launcher, in place.

    The executable code, offsets, resources and byte length remain unchanged.
    A signed or unrecognised launcher must be rebuilt from windows_launcher.c.
    """
    old='VCS Network Watch'.encode('utf-16le')+b'\0\0'
    if old not in data:
        if 'LinkRadar'.encode('utf-16le')+b'\0\0' not in data:raise ValueError('Unrecognised launcher')
        return data
    if data[:2]!=b'MZ':raise ValueError('Not a Windows PE launcher')
    pe=struct.unpack_from('<I',data,0x3c)[0];optional=pe+24
    if data[pe:pe+4]!=b'PE\0\0' or struct.unpack_from('<H',data,optional)[0]!=0x20b:raise ValueError('Expected PE32+ launcher')
    if any(struct.unpack_from('<II',data,optional+112+4*8)):raise ValueError('Signed launcher must be rebuilt')
    for before,after in [('VCS Network Watch','LinkRadar'),('Unable to launch Network Watch.','Unable to launch LinkRadar.')]:
        original=before.encode('utf-16le')+b'\0\0';replacement=after.encode('utf-16le')+b'\0\0'
        if data.count(original)!=1:raise ValueError('Unexpected launcher strings')
        data=data.replace(original,replacement+b'\0'*(len(original)-len(replacement)))
    result=bytearray(data);struct.pack_into('<I',result,optional+64,0)
    return bytes(result)

def version():
    for line in (ROOT/'server.py').read_text(encoding='utf-8').splitlines():
        if line.startswith('VERSION ='):return line.split('"')[1]
    raise SystemExit('VERSION not found in server.py')

def sha256(path):
    digest=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1<<20),b''):digest.update(chunk)
    return digest.hexdigest()

def replace_certifi(packages,wheel):
    """Replace only the universal certifi distribution; verify wheel RECORD first."""
    with zipfile.ZipFile(wheel) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or len(names)>64:raise ValueError('Unexpected certifi wheel entries')
        metadata=[n for n in names if n.startswith('certifi-') and n.endswith('.dist-info/METADATA')]
        if len(metadata)!=1:raise ValueError('Expected a certifi wheel')
        info=metadata[0].split('/')[0]
        for name in names:
            path=PurePosixPath(name)
            if ('\\' in name or ':' in name or path.is_absolute() or '..' in path.parts
                    or not path.parts or path.parts[0] not in ('certifi',info)):
                raise ValueError('Unsafe certifi wheel entry')
            entry=archive.getinfo(name)
            if (entry.external_attr>>16)&0o170000==0o120000 or entry.file_size>4*1024*1024:
                raise ValueError('Unsafe certifi wheel member')
        wheel_metadata=archive.read(info+'/WHEEL').decode()
        if 'Root-Is-Purelib: true' not in wheel_metadata or 'Tag: py3-none-any' not in wheel_metadata:
            raise ValueError('Certifi wheel must be universal pure Python')
        rows=list(csv.reader(archive.read(info+'/RECORD').decode().splitlines()))
        recorded={row[0] for row in rows}
        if recorded!={n for n in names if not n.endswith('/')}:
            raise ValueError('Incomplete wheel RECORD')
        for name,digest,size in rows:
            if name==info+'/RECORD':continue
            payload=archive.read(name)
            expected='sha256='+base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip('=')
            if digest!=expected or size!=str(len(payload)):raise ValueError('Wheel RECORD mismatch')
        for path in [packages/'certifi',*packages.glob('certifi-*.dist-info')]:
            if path.exists():shutil.rmtree(path)
        archive.extractall(packages,names)


def build(base,out,certifi_wheel=None):
    out.mkdir(parents=True,exist_ok=True)
    target=out/f'LinkRadar-{version()}-Windows-x64.zip'
    with tempfile.TemporaryDirectory(prefix='nw-build-') as temp:
        with zipfile.ZipFile(base) as archive:
            if len(archive.infolist())>20000 or sum(i.file_size for i in archive.infolist())>2*1024**3:
                raise ValueError('Base ZIP exceeds build limits')
            names=[n for n in archive.namelist() if n.split('/',2)[1:2] and n.split('/',2)[1] in REUSED]
            if not names:raise SystemExit('Base ZIP has no runtime/packages/launcher')
            roots={name.split('/')[0] for name in names}
            if len(roots)!=1:raise SystemExit('Base ZIP must have one application folder')
            for name in names:
                if ('\\' in name or ':' in name or '..' in PurePosixPath(name).parts
                        or PurePosixPath(name).is_absolute()):raise SystemExit('Unsafe path in base ZIP: '+name)
                if (archive.getinfo(name).external_attr>>16)&0o170000==0o120000:
                    raise ValueError('Base ZIP contains a symlink')
            archive.extractall(temp,names)
        original=Path(temp)/roots.pop()
        package=Path(temp)/FOLDER
        if original!=package:original.rename(package)
        if certifi_wheel:replace_certifi(package/'packages',certifi_wheel)
        launcher=package/'LinkRadar.exe'
        if not launcher.exists():(package/'Network Watch.exe').rename(launcher)
        launcher.write_bytes(brand_launcher(launcher.read_bytes()))
        app=package/'app';app.mkdir()
        for module in sorted(ROOT.glob('*.py')):shutil.copy2(module,app/module.name)
        shutil.copytree(ROOT/'branding',app/'branding')
        for name in SCRIPTS:shutil.copy2(ROOT/'packaging'/name,package/name)
        for source,name in DOCS.items():shutil.copy2(ROOT/source,package/name)
        shutil.copy2(ROOT/'branding/NetworkWatch.ico',package/'LinkRadar.ico')
        (package/'Start LinkRadar.bat').write_bytes(LAUNCHER_BAT.encode())
        # Reusing an embedded interpreter is explicit and traceable. Dependencies
        # must be present at their pinned versions, not merely exist in a ZIP.
        for requirement in (ROOT/'requirements-desktop.txt').read_text().splitlines():
            if '==' not in requirement:continue
            name,pinned=requirement.strip().split('==')
            normalized=name.lower().replace('-','_')
            if not any(p.name.lower().replace('-','_')==normalized+'_'+pinned+'.dist_info' for p in (package/'packages').glob('*.dist-info')):
                raise ValueError('Base runtime does not match pinned dependency: '+requirement)
        if not (package/'runtime/pythonw.exe').is_file() or not any((package/'packages').rglob('qwindows.dll')):
            raise ValueError('Base runtime lacks Python GUI or Qt Windows plugin')
        build_manifest.save(ROOT,package/'BUILD-MANIFEST.json',version(),platform='Windows-x64',runtime_base_sha256=sha256(base),certifi_wheel_sha256=sha256(certifi_wheel) if certifi_wheel else None,executed_on_windows=False)
        with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
            for path in sorted(package.rglob('*')):
                archive.write(path,path.relative_to(temp).as_posix())
    return target

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--base',required=True,type=Path);parser.add_argument('--out',type=Path,default=ROOT.parent/'dist')
    parser.add_argument('--certifi-wheel',type=Path)
    args=parser.parse_args();target=build(args.base,args.out,args.certifi_wheel)
    print(target);print(sha256(target),target.name)

if __name__=='__main__':main()
