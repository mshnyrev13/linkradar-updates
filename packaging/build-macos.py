"""Build a standalone arm64 app on macOS; never install system components."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import ssl
from payload import check_clean_payload, remove_documentation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--dmg', action='store_true', help='Create a DMG with only the app and Applications link')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Build this package on macOS.')
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from linkradar import VERSION
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Some CPython distributions use an on-disk CA deny list in ssl.py.
    # Preserve that policy in a frozen app rather than dropping it or leaving
    # SSLContext dependent on a file outside the standalone bundle.
    ca_policy = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
    runtime_data = ['--add-data', f'{ca_policy}:.'] if ca_policy.is_file() else []
    macos = root / 'packaging/macos'
    branding = root / 'linkradar/branding'
    runtime_data += ['--add-data', f'{branding / "network-watch-logo.png"}:linkradar/branding']
    for name in (
        'install-background-mac.command',
        'remove-background-mac.command',
        'configure-background-mac.sh',
        'install-capture-access-mac.command',
        'remove-capture-access-mac.command',
        'bpf-access-root.sh',
    ):
        runtime_data += ['--add-data', f'{macos / name}:packaging']
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '--windowed', '--onedir', '--name', 'LinkRadar',
        '--distpath', str(output / 'dist'), '--workpath', str(output / 'work'),
        '--specpath', str(output), '--target-architecture', 'arm64',
        '--osx-bundle-identifier', 'tech.vcs.network-watch',
        '--icon', str(branding / 'NetworkWatch.icns'),
        '--paths', str(root),
        '--collect-data', 'certifi', '--copy-metadata', 'truststore',
        '--copy-metadata', 'PySide6-Essentials', '--copy-metadata', 'shiboken6',
    ] + runtime_data + [str(root / 'linkradar/__main__.py')], cwd=root, check=True)
    app = output / 'dist/LinkRadar.app'
    # UDP readiness through Network.framework triggers/waits for the ordinary
    # Local Network decision before native ICMP starts. No root or extra grants.
    subprocess.run(['/usr/bin/xcrun', 'swiftc', '-O', '-target', 'arm64-apple-macos15.0',
                    str(macos / 'mac-network-access.swift'),
                    '-o', str(app / 'Contents/MacOS/LinkRadarNetworkAccess')], check=True)
    plist = app / 'Contents/Info.plist'
    info = plistlib.loads(plist.read_bytes())
    info.update(CFBundleDisplayName='LinkRadar', CFBundleShortVersionString=VERSION, CFBundleVersion=VERSION,
                NSAppSleepDisabled=True, LSMinimumSystemVersion='15.0',
                NSLocalNetworkUsageDescription='Диагностика сети, LLDP и ручной захват пакетов выбранного адаптера.')
    plist.write_bytes(plistlib.dumps(info))
    remove_documentation(app)
    check_clean_payload(app)
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
