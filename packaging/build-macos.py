"""Build a standalone arm64 app on macOS; never install system components."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import build_manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--dmg', action='store_true', help='Create a DMG with only the app and Applications link')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build this package on macOS.')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from server import VERSION
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--windowed', '--onedir', '--name', 'LinkRadar',
        '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
        '--specpath', str(output), '--target-architecture', 'arm64',
        '--osx-bundle-identifier', 'tech.vcs.network-watch',
        '--icon', str(root / 'branding/NetworkWatch.icns'),
        '--add-data', f'{root / "branding"}:branding',
        '--add-data', f'{root / "packaging"}:packaging',
        '--collect-data', 'certifi', '--copy-metadata', 'truststore',
        '--copy-metadata', 'PySide6-Essentials', '--copy-metadata', 'shiboken6',
        str(root / 'desktop.py'),
    ], cwd=root, check=True)
    app = output / 'dist/LinkRadar.app'
    # UDP readiness through Network.framework triggers/waits for the ordinary
    # Local Network decision before native ICMP starts. No root or extra grants.
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', '-target', 'arm64-apple-macos15.0',
                    str(root / 'packaging/mac-network-access.swift'),
                    '-o', str(app / 'Contents/MacOS/LinkRadarNetworkAccess')], check=True)
    plist = app / 'Contents/Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info.update(CFBundleDisplayName='LinkRadar', CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION,
                NSAppSleepDisabled=True, LSMinimumSystemVersion='15.0',
                NSLocalNetworkUsageDescription='Диагностика сети, LLDP и ручной захват пакетов выбранного адаптера.')
    plist.write_bytes(plistlib.dumps(info))
    documentation = app / 'Contents/Resources/Documentation'
    documentation.mkdir(parents=True, exist_ok=True)
    build_manifest.save(root,documentation/'BUILD-MANIFEST.json',VERSION,platform='macOS-arm64',minimum_macos='15.0',signing='ad-hoc')
    for source, name in [('RELEASE-NOTES-2.10.10.md', 'RELEASE-NOTES-2.10.10.md'), ('README-macOS.md', 'README.md'),
                         ('STANDALONE.md', 'STANDALONE.md'), ('STREAM.md', 'STREAM.md'),
                         ('SECURITY-AUDIT.md','SECURITY-AUDIT.md'),
                         ('SPEED-AUDIT.md','SPEED-AUDIT.md'),
                         ('AUDIT-2.10.3.md','AUDIT-2.10.3.md'),
                         ('AUDIT-2.10.4.md','AUDIT-2.10.4.md'),
                         ('AUDIT-2.10.5.md','AUDIT-2.10.5.md'),
                         ('AUDIT-2.10.7.md','AUDIT-2.10.7.md'),
                         ('AUDIT-2.10.8.md','AUDIT-2.10.8.md'),
                         ('AUDIT-2.10.9.md','AUDIT-2.10.9.md'),
                         ('RELEASE-NOTES-2.10.9.md','RELEASE-NOTES-2.10.9.md'),
                         ('RELEASE-NOTES-2.10.8.md','RELEASE-NOTES-2.10.8.md'),
                         ('RELEASE-NOTES-2.10.7.md','RELEASE-NOTES-2.10.7.md'),
                         ('UPDATES.md','UPDATES.md'), ('RELEASE-NOTES-2.10.6.md','RELEASE-NOTES-2.10.6.md')]:
        shutil.copy2(root / source, documentation / name)
    subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(app)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    print(app)
    if args.dmg:
        target = output / f'LinkRadar-{VERSION}-macOS-arm64.dmg'
        with tempfile.TemporaryDirectory(prefix='dmg-stage-', dir=output) as folder:
            stage = Path(folder)
            shutil.copytree(app, stage / app.name, symlinks=True)
            (stage / 'Applications').symlink_to('/Applications', target_is_directory=True)
            subprocess.run(['/usr/bin/hdiutil', 'create', '-volname', f'LinkRadar {VERSION}',
                            '-srcfolder', str(stage), '-format', 'UDZO', '-ov', str(target)], check=True)
        print(target)


if __name__ == '__main__':
    main()
