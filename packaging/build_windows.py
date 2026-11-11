"""Build the LinkRadar Windows installer in an x64 MSVC developer shell."""

import argparse
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import build_manifest

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.13.17"
PYTHON_SHA256 = "9e35319bffedab8b36ad44d8f377cb4f1733bcec257a0b6d04d94f01486ddb38"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
SERVICE_FILES = (
    "Install background.cmd",
    "Remove background.cmd",
    "install-background-windows.ps1",
    "remove-background-windows.ps1",
    "windows-service-safety.ps1",
)


def extract_runtime(archive, runtime):
    if build_manifest.digest(archive) != PYTHON_SHA256:
        raise ValueError("Embedded Python SHA-256 does not match the pinned python.org release")
    with zipfile.ZipFile(archive) as source:
        source.extractall(runtime)
    (runtime / "python313._pth").write_text(
        "python313.zip\n.\n../app\n../packages\n", encoding="ascii"
    )


def compile_launcher(stage, work):
    resources = work / "LinkRadar.rc"
    icon = (ROOT / "branding/NetworkWatch.ico").as_posix()
    resources.write_text(f'1 ICON "{icon}"\n', encoding="utf-16")
    subprocess.run(["rc", "/nologo", "/fo", "LinkRadar.res", resources.name], cwd=work, check=True)
    subprocess.run(
        [
            "cl", "/nologo", "/W4", "/WX", "/O2", "/MT",
            str(ROOT / "packaging/windows_launcher.c"), "LinkRadar.res",
            f"/Fe:{stage / 'LinkRadar.exe'}", "kernel32.lib", "user32.lib",
            "/link", "/MACHINE:X64", "/SUBSYSTEM:WINDOWS",
        ],
        cwd=work,
        check=True,
    )


def build(out, iscc, redist, version):
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="linkradar-build-") as folder:
        work = Path(folder)
        stage = work / "LinkRadar"
        app = stage / "app"
        runtime = stage / "runtime"
        app.mkdir(parents=True)
        archive = work / "python.zip"
        with urllib.request.urlopen(PYTHON_URL, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        extract_runtime(archive, runtime)
        for dll in sorted(redist.glob("*.dll")):
            if not (runtime / dll.name).exists():
                shutil.copy2(dll, runtime)
        subprocess.run(
            [
                sys.executable, "-m", "pip", "install", "--only-binary=:all:", "--no-deps",
                "--no-compile", "--target", str(stage / "packages"),
                "-r", str(ROOT / "requirements-desktop.txt"),
            ],
            check=True,
        )
        # pip-generated console shims are unused and can reference the build host.
        scripts = stage / "packages/bin"
        if scripts.is_dir():
            shutil.rmtree(scripts)
        for module in sorted(ROOT.glob("*.py")):
            shutil.copy2(module, app / module.name)
        branding = app / "branding"
        branding.mkdir()
        for name in ("NetworkWatch.ico", "network-watch-logo.png"):
            shutil.copy2(ROOT / "branding" / name, branding / name)
        for name in SERVICE_FILES:
            shutil.copy2(ROOT / "packaging" / name, stage / name)
        shutil.copy2(ROOT / "docs/windows.md", stage / "README.md")
        build_manifest.save(
            ROOT, stage / "BUILD-MANIFEST.json", version,
            platform="Windows-x64", python_version=PYTHON_VERSION, python_sha256=PYTHON_SHA256,
        )
        compile_launcher(stage, work)
        subprocess.run(
            [str(runtime / "python.exe"), "-B", "-c",
             "import desktop, certifi, requests, truststore; print(desktop.server.VERSION)"],
            cwd=work,
            check=True,
        )
        subprocess.run(
            [
                str(iscc), f"/DAppVersion={version}", f"/DPayloadDir={stage}",
                f"/DOutputFolder={out}", str(ROOT / "packaging/windows_setup.iss"),
            ],
            check=True,
        )
    installer = out / f"LinkRadar-{version}-Windows-x64-Setup.exe"
    print(f"{build_manifest.digest(installer)}  {installer.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "dist/windows")
    parser.add_argument("--iscc", type=Path, required=True, help="Path to Inno Setup 6 ISCC.exe")
    args = parser.parse_args()
    if sys.platform != "win32" or platform.machine().upper() != "AMD64" or sys.version_info[:2] != (3, 13):
        parser.error("Use Windows x64 and Python 3.13 to build the installer")
    if not all(shutil.which(tool) for tool in ("cl", "rc")):
        parser.error("Run from the x64 Native Tools shell for Visual Studio")
    redist_root = Path(os.environ.get("VCToolsRedistDir", "")) / "x64"
    redist = sorted(path.parent for path in redist_root.glob("Microsoft.VC*.CRT/msvcp140.dll"))
    if len(redist) != 1:
        parser.error("The x64 Visual C++ redistributable directory was not found")
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    version = re.search(r'^VERSION = "(\d+\.\d+\.\d+)"$', source, re.MULTILINE)
    if not version:
        parser.error("Expected VERSION = \"X.Y.Z\" in server.py")
    build(args.out.resolve(), args.iscc.resolve(), redist[0], version[1])


if __name__ == "__main__":
    main()
