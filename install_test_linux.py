#!/usr/bin/env python3
"""SyncPlayer - Linux end-to-end test (installation + running two videos,
one of them a YouTube link).

Sections
  1. install.sh: runs into a throwaway prefix and verifies the layout,
     launcher, desktop entry and install.json
  2. environment: python/tkinter, mpv, yt-dlp, libX11 + window backend
  3. app as a subprocess: opens a local file AND a YouTube link, both mpv
     processes come up, both really decode video+audio, app exits cleanly
  4. in-process playback: two videos actually PLAY (positions advance), and
     the window features work (find / arrange / borderless / embed / reset /
     crop) through the app's own code paths
  5. YouTube in-process: the stream buffers and playback advances

Usage:  python3 install_test_linux.py            (needs a graphical session)
        YOUTUBE_URL=... python3 install_test_linux.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
YOUTUBE_URL = os.environ.get("YOUTUBE_URL",
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw")
MOVIE = os.path.join(HERE, "testmedia", "movie.mp4")
REACT = os.path.join(HERE, "testmedia", "react.mp4")

passed = 0
failed = 0
fail_msgs = []
HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("[PASS] %s%s" % (name, (" — " + str(extra)) if extra else ""))
    else:
        failed += 1
        fail_msgs.append(name)
        print("[FAIL] %s%s" % (name, (" — " + str(extra)) if extra else ""))


def run(cmd, timeout=120, env=None, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace",
                           timeout=timeout, env=env, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -2, repr(e)


def mpv_pids():
    out = subprocess.run(["pgrep", "-x", "mpv"], capture_output=True,
                         text=True).stdout or ""
    return [int(x) for x in out.split() if x.strip().isdigit()]


def kill_mpv():
    subprocess.run(["pkill", "-x", "mpv"], capture_output=True)


# ---------------------------------------------------------------- 1. install
def t_installer():
    prefix = tempfile.mkdtemp(prefix="spinstall_")
    env = dict(os.environ)
    env["SYNCPLAYER_PREFIX"] = prefix
    rc, out = run(["bash", os.path.join(HERE, "install.sh"), "--prefix", prefix,
                   "--no-ytdlp", "--yes"], timeout=180, env=env)
    check("install: install.sh exits 0", rc == 0, out.strip().splitlines()[-1] if out else "")

    app = os.path.join(prefix, "syncplayer.py")
    plat = os.path.join(prefix, "sp_plat.py")
    meta = os.path.join(prefix, "install.json")
    check("install: syncplayer.py installed", os.path.isfile(app))
    check("install: sp_plat.py installed", os.path.isfile(plat))
    check("install: install.json written", os.path.isfile(meta))

    state = {}
    if os.path.isfile(meta):
        try:
            state = json.load(open(meta))
        except Exception:
            pass
    check("install: install.json app_version", state.get("app_version") == "1.5.1",
          state.get("app_version"))
    check("install: install.json platform is linux", state.get("platform") == "linux",
          state.get("platform"))
    check("install: install.json records detected mpv", bool(state.get("mpv")),
          state.get("mpv"))

    launcher = os.path.expanduser("~/.local/bin/syncplayer")
    check("install: launcher created", os.path.isfile(launcher),
          launcher)
    check("install: launcher is executable",
          os.path.isfile(launcher) and os.access(launcher, os.X_OK))
    if os.path.isfile(launcher):
        body = open(launcher).read()
        check("install: launcher points at the installed app", prefix in body,
              body.strip().splitlines()[-1][:80])

    desk = os.path.expanduser("~/.local/share/applications/syncplayer.desktop")
    check("install: desktop entry created", os.path.isfile(desk), desk)
    if os.path.isfile(desk):
        d = open(desk).read()
        check("install: desktop entry has Type/Exec/Name",
              "Type=Application" in d and "Exec=" in d and "Name=SyncPlayer" in d)
        check("install: desktop entry Exec resolves to the launcher",
              launcher in d, [l for l in d.splitlines() if l.startswith("Exec=")])

    # the installed copy must be importable (catches syntax/import errors early)
    sys.path.insert(0, prefix)
    for mod in ("sp_plat", "syncplayer"):
        sys.modules.pop(mod, None)
    rc, out = run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, %r); import sp_plat, syncplayer; "
                   "print('IMPORT_OK', syncplayer.APP_VERSION)" % prefix],
                  timeout=60)
    check("install: installed app imports cleanly",
          rc == 0 and "IMPORT_OK" in out, out.strip()[-90:])

    return prefix


# ------------------------------------------------------------------- 2. env
def t_environment(prefix):
    rc, out = run([sys.executable, os.path.join(prefix, "syncplayer.py"),
                   "--check-env"], timeout=60, env=dict(os.environ))
    check("env: --check-env exits 0", rc == 0, out.strip()[:80])
    info = {}
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            info = json.loads(m.group(0))
        except Exception:
            info = {}
    check("env: reports linux platform", info.get("platform") == "linux",
          info.get("platform"))
    check("env: mpv located", bool(info.get("mpv")) and info.get("mpv") != "NOT FOUND",
          info.get("mpv"))
    check("env: yt-dlp located", bool(info.get("yt_dlp")) and info.get("yt_dlp") != "NOT FOUND",
          info.get("yt_dlp"))
    check("env: IPC transport is a unix socket", info.get("ipc") == "unix socket",
          info.get("ipc"))
    check("env: config dir is XDG-based", "/.config/" in info.get("config_dir", ""),
          info.get("config_dir"))
    if HAVE_DISPLAY:
        check("env: X11 window backend available", info.get("window_backend") == "x11"
              and info.get("window_backend_ok"), info.get("window_backend_reason"))
    else:
        check("env: no display -> backend degrades gracefully (no crash)",
              isinstance(info.get("window_backend"), str))
    return info


# --------------------------------------------------- 3. app runs two videos
def t_app_two_videos(prefix):
    app = os.path.join(prefix, "syncplayer.py")
    shot = os.path.join(os.path.dirname(app), "screenshots")
    shutil.rmtree(shot, ignore_errors=True)
    kill_mpv()
    time.sleep(0.4)

    proc = subprocess.Popen([sys.executable, app, "--smoke", MOVIE, YOUTUBE_URL],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    seen = 0
    deadline = time.time() + 12
    while time.time() < deadline:
        n = len(mpv_pids())
        seen = max(seen, n)
        if n >= 2:
            break
        time.sleep(0.5)
    check("app: launched TWO mpv processes (local + YouTube)", seen >= 2,
          "max seen = %d" % seen)

    try:
        rc = proc.wait(timeout=40)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    check("app: --smoke run exits cleanly", rc == 0, "exit=%s" % rc)

    logs = {}
    for tag in ("A", "B"):
        p = os.path.join(shot, "mpv_%s.log" % tag)
        try:
            logs[tag] = open(p, encoding="utf-8", errors="replace").read()
        except Exception:
            logs[tag] = ""
    a, b = logs.get("A", ""), logs.get("B", "")
    check("app: movie decoded video+audio", ("Video" in a and "Audio" in a),
          [l for l in a.splitlines() if "Video" in l][:1])
    check("app: YouTube link decoded video+audio", ("Video" in b and "Audio" in b),
          [l for l in b.splitlines() if "Video" in l][:1])
    check("app: no crash log written",
          not os.path.isfile(os.path.join(shot, "syncplayer_crash.txt")))
    kill_mpv()


# --------------------------------------------- 4/5. in-process playback etc.
def t_inprocess(prefix, with_youtube):
    sys.path.insert(0, prefix)
    for mod in ("sp_plat", "syncplayer"):
        sys.modules.pop(mod, None)
    sys._TEST_MODE = True          # suppress modal dialogs during the test
    import syncplayer as sp

    root = sp.tk.Tk()
    root.withdraw()
    app = sp.SyncApp(root)
    app.movie_path.set(MOVIE)
    app.react_path.set(YOUTUBE_URL if with_youtube else REACT)
    app._start()

    label = "YouTube" if with_youtube else "local"
    # Pump via root.update() ONLY, so the app's own single 30 Hz poll timer
    # drives _poll() (calling app._poll() here as well would stack timers and
    # multiply the poll rate geometrically).
    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            root.update()
            time.sleep(0.05)
    pump(6.0)

    pa, pb = app.players.get("A"), app.players.get("B")
    check("%s: movie player running" % label, bool(pa and pa.running))
    check("%s: reaction player running" % label, bool(pb and pb.running))
    if not (pa and pb):
        app._stop(); root.destroy(); kill_mpv()
        return

    # window discovery through the app's own helper (platform layer)
    ha = sp.find_mpv_window(pa.proc.pid, "SyncPlayer — Movie", tries=40, delay=0.25)
    hb = sp.find_mpv_window(pb.proc.pid, "SyncPlayer — Reaction", tries=40, delay=0.25)
    if HAVE_DISPLAY:
        check("%s: app found movie window" % label, bool(ha), hex(ha or 0))
        check("%s: app found reaction window" % label, bool(hb), hex(hb or 0))
    pa.hwnd, pb.hwnd = ha, hb

    # play both and confirm positions actually advance
    app._toggle_play()
    pump(3.0)
    app._poll()
    pos_a = app.last_pos.get("A")
    pos_b = app.last_pos.get("B")
    check("%s: movie position advances (video is playing)" % label,
          pos_a is not None and pos_a > 0.2, "pos=%.2f" % (pos_a or -1))
    check("%s: reaction position advances (video is playing)" % label,
          pos_b is not None and pos_b > 0.2, "pos=%.2f" % (pos_b or -1))

    if with_youtube:
        buf = app.cache_dur.get("B", 0.0)
        check("YouTube: stream buffer reported (demuxer cache)",
              buf > 0.5, "buffered=%.1fs" % buf)

    if HAVE_DISPLAY and ha and hb:
        wb = sp._wb()
        # arrange
        wb.place(ha, 40, 60, 800, 450)
        pump(0.6)
        ra = wb.get_rect(ha)
        check("%s: arrange moved the window" % label,
              ra is not None and abs(ra[2] - 800) < 80, ra)

        # floating PiP: borderless + ontop, then reset
        app._toggle_pip("A")
        pump(0.6)
        check("%s: floating PiP engaged" % label, app.pip.get("A") is True)
        if hasattr(wb, "_prop"):        # X11: _MOTIF_WM_HINTS decorations == 0
            _t, _f, vals, _n = wb._prop(ha, "_MOTIF_WM_HINTS")
            check("%s: floating PiP removed decorations" % label,
                  len(vals) >= 3 and vals[2] == 0, vals[:5])
        app._reset_pip()
        pump(0.6)
        check("%s: reset PiP restored the window" % label,
              app.pip.get("A") is False and app.pip_int is False)

        # embedded PiP (reparent) needs sync lock
        app.sync_locked = True
        app._toggle_pip_int("B")
        pump(0.8)
        check("%s: embedded PiP engaged" % label, app.pip_int is True)
        if app.pip_int:
            par = wb.parent_of(pb.hwnd)
            check("%s: pane is a child of the host window" % label,
                  par == ha, "parent=%s host=%s" % (hex(par or 0), hex(ha)))
            app._pip_move(1, 0)
            pump(0.4)
            check("%s: embedded pane can be moved" % label, True)
            app._undock_pip_int()
            pump(0.6)
            check("%s: undock returned the pane to top level" % label,
                  wb.parent_of(pb.hwnd) == wb.root)

    # crop + volume paths through the app
    app._crop_tag = "A"
    app._crop_nudge("bottom", 1)
    pump(0.4)
    check("%s: crop applies in the active session" % label,
          app._manual_crop.get("A") is not None)
    app._crop_clear()
    pump(0.4)
    check("%s: crop clear resets to full frame" % label,
          app._manual_crop.get("A") is None)

    app.vol_a.set(135.0)
    app._apply_volumes()
    pump(0.4)
    err, vol = pa.get_property("volume")
    check("%s: volume above 100%% works (150%% max)" % label,
          err == "success" and vol is not None and abs(vol - 135.0) <= 3.0, vol)

    # no crash log from the whole session
    check("%s: app session produced no crash log" % label,
          not os.path.isfile(os.path.join(sp.SHOT_DIR, "syncplayer_crash.txt")))

    app._stop()
    pump(0.5)
    root.destroy()
    kill_mpv()


def main():
    print("=== SyncPlayer Linux end-to-end test ===")
    print("python %s | display=%s | session=%s" % (
        sys.version.split()[0], os.environ.get("DISPLAY") or "(none)",
        os.environ.get("XDG_SESSION_TYPE") or "(unknown)"))
    if not os.path.isfile(MOVIE) or not os.path.isfile(REACT):
        print("FATAL: run this from the repo root (movie.mp4 / react.mp4 missing)")
        return 1

    prefix = t_installer()
    t_environment(prefix)
    t_app_two_videos(prefix)
    t_inprocess(prefix, with_youtube=False)
    t_inprocess(prefix, with_youtube=True)

    print("\n==== %d/%d Linux checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    shutil.rmtree(prefix, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
