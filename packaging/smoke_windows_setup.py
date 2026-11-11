"""Install, launch, update and remove LinkRadar in an isolated Windows CI profile."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def run(executable, *args, cwd, env, timeout=120):
    subprocess.run([str(executable), *map(str, args)], cwd=cwd, env=env, timeout=timeout, check=True)


def smoke(setup, work):
    app = work / "LinkRadar тест"
    report = work / "smoke result.json"
    env = os.environ.copy()
    launch_env = env | {
        "TEMP": str(work),
        "TMP": str(work),
        "TMPDIR": str(work),
        "QT_QPA_PLATFORM": "windows",
        "NETWORK_WATCH_PREVIEW": str(work / "window.png"),
    }
    options = ("/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
    install_options = (*options, "/TASKS=", f"/DIR={app}")
    launcher = app / "LinkRadar.exe"
    uninstall = app / "unins000.exe"

    try:
        run(setup, *install_options, cwd=work, env=env)
        run(launcher, "--smoke-test", "--no-discovery", "--smoke-report", report, cwd=work, env=launch_env, timeout=35)
        result = json.loads(report.read_text(encoding="utf-8"))
        manifest = json.loads((app / "BUILD-MANIFEST.json").read_text(encoding="utf-8"))
        successful_probes = sum(target["window_stats"]["ok"] for target in result["targets"])
        if result["version"] != manifest["version"] or not successful_probes or not (work / "window.png").is_file():
            raise RuntimeError("Installed launcher did not produce a native LinkRadar measurement")
        print(f"Native LinkRadar {result['version']}: {successful_probes} successful loopback probes")
        if Path(result["profile"]).parent != work:
            raise RuntimeError("Smoke profile was created outside the temporary workspace")
        run(setup, *install_options, cwd=work, env=env)
        if not launcher.is_file() or not uninstall.is_file():
            raise RuntimeError("Reinstallation removed the launcher or the uninstaller")
    finally:
        if uninstall.is_file():
            run(uninstall, *options, cwd=work, env=env)
    if launcher.exists():
        raise RuntimeError("Uninstall left LinkRadar.exe in place")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("installer", type=Path)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.name != "nt":
        parser.error("Run this install and uninstall test on a disposable Windows GitHub Actions runner")
    with tempfile.TemporaryDirectory(prefix="linkradar-smoke-") as folder:
        smoke(args.installer.resolve(), Path(folder))
    print("Setup, GUI and uninstall verified")


if __name__ == "__main__":
    main()
