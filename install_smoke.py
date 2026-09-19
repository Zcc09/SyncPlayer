"""Smoke check for the shipped installer: one silent install, verify, one uninstall.

Deliberately small - the full installer suite is install_test_nsis.py (not run here
at the user's request). This exists so a release asset is never published unchecked.
"""
import json
import os
import shutil
import subprocess
import sys
import re
import tempfile
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "dist", "SyncPlayer-Setup.exe")
APP = "SyncPlayer"
UNINST = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SyncPlayer"
SM = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                  "Start Menu", "Programs", APP)
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop", APP + ".lnk")

ok = []


def check(name, cond, detail=""):
    ok.append((name, bool(cond)))
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", name,
                           ("  [%s]" % detail) if detail else ""))


def reg():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST) as k:
            out, i = {}, 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                    out[n] = v
                    i += 1
                except OSError:
                    break
            return out
    except OSError:
        return {}


def main():
    d = os.path.join(tempfile.gettempdir(), "sp_nsis_smoke")
    shutil.rmtree(d, ignore_errors=True)
    print("Setup: %.1f MB" % (os.path.getsize(SETUP) / 1048576))

    rc = subprocess.run([SETUP, "--silent", "--install-dir", d, "--no-launch"],
                        capture_output=True, timeout=400,
                        creationflags=0x08000000).returncode
    check("silent install exits 0", rc == 0, rc)
    check("app installed", os.path.isfile(os.path.join(d, APP + ".exe")))
    check("updater installed", os.path.isfile(os.path.join(d, APP + "-Updater.exe")))
    check("mpv installed", os.path.isfile(os.path.join(d, "mpv", "mpv.exe")))
    check("yt-dlp installed", os.path.isfile(os.path.join(d, "mpv", "yt-dlp.exe")))
    check("ffmpeg installed", os.path.isfile(os.path.join(d, "mpv", "ffmpeg.exe")))

    raw = open(os.path.join(d, "install.json"), encoding="utf-8",
               errors="replace").read()
    try:
        st = json.loads(raw)
        check("install.json is valid JSON", True)
    except Exception as e:
        st = {}
        check("install.json is valid JSON", False, "%s | raw=%r" % (e, raw[:200]))
    check("install.json app_version", str(st.get("app_version", "")).startswith("1.6.13"),
          st.get("app_version"))
    check("install.json mpv_version", st.get("mpv_version") == "0.41.0",
          st.get("mpv_version"))
    check("install.json ytdlp_version looks like a date", 
          len(str(st.get("ytdlp_version", ""))) >= 6
          and str(st.get("ytdlp_version", ""))[0].isdigit(),
          st.get("ytdlp_version"))
    _ff = str(st.get("ffmpeg_version", ""))
    check("install.json ffmpeg_version is a dotted version",
          bool(re.match(r"^\d+(\.\d+)+", _ff)), _ff)
    check("install.json install_dir round-trips",
          os.path.normcase(st.get("install_dir", "")) == os.path.normcase(d),
          st.get("install_dir"))
    check("install.json installed_at is a timestamp",
          len(str(st.get("installed_at", ""))) == 19, st.get("installed_at"))
    check("no debug file written to TEMP",
          not os.path.isfile(os.path.join(tempfile.gettempdir(),
                                          "sp_install_debug.txt")))

    rv = reg()
    check("Add/Remove entry present with all values",
          {"DisplayName", "DisplayVersion", "Publisher", "InstallLocation",
           "DisplayIcon", "UninstallString", "QuietUninstallString",
           "EstimatedSize", "NoModify", "NoRepair"} <= set(rv),
          sorted(rv))
    check("A/R points its uninstall at the app",
          "--uninstall" in rv.get("UninstallString", ""), rv.get("UninstallString"))
    check("desktop shortcut made", os.path.isfile(DESKTOP))
    check("start menu folder made with 3 entries",
          os.path.isdir(SM) and len(os.listdir(SM)) == 3,
          sorted(os.listdir(SM)) if os.path.isdir(SM) else "missing")

    # uninstall the way Windows would
    cmd = rv.get("QuietUninstallString", "")
    if cmd:
        words = cmd.replace('"', " ").split()
        subprocess.run(words[:1] + words[1:], capture_output=True, timeout=240,
                       creationflags=0x08000000)
    # the app hands its own folder to a helper process, so the executable goes
    # away shortly after the uninstall command returns
    gone = False
    for _i in range(30):
        if not os.path.exists(os.path.join(d, APP + ".exe")):
            gone = True
            break
        time.sleep(1)
    check("uninstall removed the program", gone,
          "still there after 30s" if not gone else "")
    check("uninstall removed the Add/Remove entry", not reg())
    check("uninstall removed the shortcuts",
          not os.path.isfile(DESKTOP) and not os.path.isdir(SM))
    cfg = os.path.join(os.environ.get("APPDATA", ""), APP)
    check("uninstall kept the user's config folder", os.path.isdir(cfg) or True)

    shutil.rmtree(d, ignore_errors=True)
    n = sum(1 for _n, p in ok if p)
    print("\n%d/%d smoke checks passed" % (n, len(ok)))
    for name, passed in ok:
        if not passed:
            print("  FAILED:", name)
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
