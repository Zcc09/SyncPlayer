#!/usr/bin/env python3
"""End-to-end DEPLOYMENT test for SyncPlayer: installation + running two videos
(one a YouTube link), using the real packaged installer and the bundled mpv +
yt-dlp. NOT about feature behavior — it verifies the artifact you distribute
actually installs and plays.

Run:  python install_test.py
Uses a real YouTube URL (override with YOUTUBE_URL env) and needs network.
"""
import json
import os
import subprocess
import sys
import time
import tempfile
import shutil

BASE = os.path.dirname(os.path.abspath(__file__))
SETUP_EXE = os.path.join(BASE, "dist", "SyncPlayer-Setup.exe")
APP_EXE = os.path.join(BASE, "dist", "SyncPlayer.exe")
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")   # 12 s local file
YOUTUBE_URL = os.environ.get("YOUTUBE_URL",
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw")

passed, failed = 0, 0
fail_msgs = []


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("[PASS] %s%s" % (name, (" — " + extra) if extra else ""))
    else:
        failed += 1
        fail_msgs.append(name)
        print("[FAIL] %s%s" % (name, (" — " + extra) if extra else ""))


def run(cmd, timeout=60, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -2, str(e)


def get_exe_version(path):
    import ctypes
    from ctypes import wintypes
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf)
        val = ctypes.c_void_p()
        vlen = wintypes.UINT()
        ctypes.windll.version.VerQueryValueW(
            buf, "\\", ctypes.byref(val), ctypes.byref(vlen))
        fi = ctypes.cast(val, ctypes.POINTER(ctypes.c_ulong))
        ms, ls = fi[2], fi[3]
        return "%d.%d.%d" % ((ms >> 16) & 0xFFFF, ms & 0xFFFF,
                             (ls >> 16) & 0xFFFF)
    except Exception:
        return None


def mpv_procs():
    """Return list of (pid, exe_path) for running mpv processes."""
    ps = ("Get-Process mpv -ErrorAction SilentlyContinue | "
          "ForEach-Object { \"$($_.Id)|$($_.Path)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            pid, path = line.split("|", 1)
            out.append((pid.strip(), path.strip()))
    return out


def mpv_paths():
    return [p for _, p in mpv_procs()]


def kill_mpv():
    os.system("taskkill /F /T /IM mpv.exe >nul 2>&1")


def main():
    if not os.path.isfile(SETUP_EXE):
        print("FATAL: %s not found - build the installer first." % SETUP_EXE)
        return 1

    install_dir = tempfile.mkdtemp(prefix="spdeploy_")
    print("=== install dir: %s ===" % install_dir)

    # ---- 1. run the real installer (silent) into a temp dir ----
    rc, out = run([SETUP_EXE, "--silent", "--install-dir", install_dir,
                   "--no-shortcuts"], timeout=180)
    check("install: installer exits 0", rc == 0, out.strip()[:120])

    app = os.path.join(install_dir, "SyncPlayer.exe")
    mpv = os.path.join(install_dir, "mpv", "mpv.exe")
    ytdl = os.path.join(install_dir, "mpv", "yt-dlp.exe")
    updater = os.path.join(install_dir, "SyncPlayer-Updater.exe")
    ij = os.path.join(install_dir, "install.json")

    check("install: SyncPlayer.exe present", os.path.isfile(app))
    check("install: app version is 1.4.0",
          get_exe_version(app) == "1.4.0", str(get_exe_version(app)))
    check("install: bundled mpv.exe present", os.path.isfile(mpv))
    check("install: bundled yt-dlp.exe present", os.path.isfile(ytdl))
    check("install: updater present", os.path.isfile(updater))
    state = {}
    if os.path.isfile(ij):
        try:
            state = json.load(open(ij))
        except Exception:
            pass
    check("install: install.json app_version", state.get("app_version") == "1.4.0")
    check("install: install.json mpv_version", state.get("mpv_version") == "0.41.0")

    # ---- 2. the deployed bundle can run (mpv + yt-dlp) ----
    mpvdir = os.path.dirname(mpv)
    env = dict(os.environ)
    # mpv's ytdl_hook finds yt-dlp on PATH -> prepend the bundled mpv dir.
    env["PATH"] = mpvdir + os.pathsep + env.get("PATH", "")

    rc, out = run([mpv, "--version"], timeout=30, env=env)
    check("run: bundled mpv --version works", rc == 0 and "mpv" in out,
          out.splitlines()[0] if out else "")
    rc, out = run([ytdl, "--version"], timeout=30, env=env)
    check("run: bundled yt-dlp --version works", rc == 0 and "20" in out,
          out.strip()[:40])

    rc, out = run([mpv, "--no-config", "--vo=null", "--no-audio",
                   "--frames=10", MOVIE], timeout=60, env=env)
    check("run: bundled mpv plays local file", rc == 0, out.strip()[:100])

    rc, out = run([mpv, "--no-config", "--vo=null", "--no-audio",
                   "--frames=10", "--ytdl=yes", YOUTUBE_URL], timeout=120,
                  env=env)
    check("run: bundled mpv plays a YouTube link (yt-dlp)",
          rc == 0, out.strip()[:120])

    # ---- 3. the installed app runs TWO videos (local + YouTube) ----
    kill_mpv()
    time.sleep(0.3)
    proc = subprocess.Popen([app, "--smoke", MOVIE, YOUTUBE_URL],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    # poll for two mpv processes (A = local, B = YouTube) during the smoke window
    seen_two = False
    bundled_used = False
    deadline = time.time() + 12
    while time.time() < deadline:
        paths = mpv_paths()
        if len(paths) >= 2:
            seen_two = True
            if any(mpv.lower() in p.lower() for p in paths):
                bundled_used = True
            break
        time.sleep(0.4)
    check("run: app spawns TWO mpv processes (local + YouTube)", seen_two,
          "count=%d" % len(mpv_paths()))
    check("run: app uses the BUNDLED mpv", bundled_used,
          str(mpv_paths()[:2]))

    try:
        rc = proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    check("run: app (--smoke) exits cleanly", rc == 0, "exit=%s" % rc)
    kill_mpv()

    print("\n==== %d/%d deployment checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    shutil.rmtree(install_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
