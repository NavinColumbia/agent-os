#!/usr/bin/env python3
"""android_runner.py — Android build + device/emulator control via adb (ADR 0004).

Unlike iOS, Android tooling runs natively on Linux: this box builds APKs (gradle) and drives devices
via adb. The EMULATOR needs KVM (absent on WSL2), so it runs on the Mac (or a physical device);
`adb connect <host>:5555` attaches a remote emulator, then everything below works the same.
Screenshots flow into the object store + QA pipeline like web/iOS.

    android_runner.py check
    android_runner.py devices
    android_runner.py connect <host:port>      # attach a remote emulator (e.g. on the Mac)
    android_runner.py shot <out.png>           # screenshot the connected device
    android_runner.py build <gradle_project>   # ./gradlew assembleDebug
Run with the agent-os venv python.
"""
import os
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
import audit  # noqa: E402

SDK = Path.home() / "android-sdk"
ADB = str(SDK / "platform-tools" / "adb")


def _adb(*args, timeout=120):
    p = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
    audit.append(actor="android-runner", action="adb", resource=" ".join(args)[:120], decision="executed",
                 payload={"rc": p.returncode})
    return p


def check():
    ver = _adb("--version").stdout.splitlines()[0] if Path(ADB).exists() else ""
    bt = sorted((SDK / "build-tools").glob("*")) if (SDK / "build-tools").exists() else []
    plats = sorted((SDK / "platforms").glob("*")) if (SDK / "platforms").exists() else []
    return {"adb": ver, "build_tools": [p.name for p in bt], "platforms": [p.name for p in plats],
            "java": subprocess.run(["java", "-version"], capture_output=True, text=True).stderr.splitlines()[0] if subprocess.run(["bash", "-c", "command -v java"], capture_output=True).returncode == 0 else ""}


def devices():
    return _adb("devices").stdout.strip()


def connect(hostport):
    return _adb("connect", hostport).stdout.strip()


def screenshot(out):
    data = subprocess.run([ADB, "exec-out", "screencap", "-p"], capture_output=True, timeout=60).stdout
    Path(out).write_bytes(data)
    return len(data)


def build_apk(project):
    gw = Path(project) / "gradlew"
    env = {**os.environ, "ANDROID_HOME": str(SDK)}
    p = subprocess.run(["bash", str(gw), "assembleDebug"], cwd=project, env=env, capture_output=True, text=True, timeout=900)
    return {"rc": p.returncode, "tail": (p.stdout or p.stderr)[-400:]}


def _main(a):
    if not a or a[0] == "check":
        print(check())
    elif a[0] == "devices":
        print(devices())
    elif a[0] == "connect":
        print(connect(a[1]))
    elif a[0] == "shot":
        print(f"wrote {screenshot(a[1])} bytes -> {a[1]}")
    elif a[0] == "build":
        print(build_apk(a[1]))


if __name__ == "__main__":
    _main(sys.argv[1:])
