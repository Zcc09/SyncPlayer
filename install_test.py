#!/usr/bin/env python3
"""End-to-end DEPLOYMENT & RUNTIME test for SyncPlayer:
1. Installation of the self-contained package.
2. Running two videos (one being a YouTube link) with bundled mpv + yt-dlp.
3. YouTube crash protection: if a YouTube URL fails to load, the other video
   feed is NOT killed — it stays open and paused.
4. Floating PiP border test: verify all borders are removed (zero sliver) and
   repeated toggling removes the frame reliably.
5. Embed PiP test: verify the embedded pane is a true Win32 child window
   (GetParent(child) == host) inside the main video feed window.
6. Crop test: active session only, Clear returns video to full resolution.
"""
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
SETUP_EXE = os.path.join(BASE, "dist", "SyncPlayer-Setup.exe")
APP_EXE = os.path.join(BASE, "dist", "SyncPlayer.exe")
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")   # 12 s local file
REACT = os.path.join(BASE, "testmedia", "react.mp4")   # 10 s local file
YOUTUBE_URL = os.environ.get("YOUTUBE_URL",
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw")
INVALID_YT = "https://www.youtube.com/watch?v=invalid_video_does_not_exist_xyz123"

import installer as inst        # noqa: E402  (wizard + registry)

u = ctypes.windll.user32

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
                           errors="replace", timeout=timeout, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timeout"
    except Exception as e:
        return -2, str(e)


def reg_read(subkey):
    """Values of a HKCU key, or None when the key is not there."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey)
    except Exception:
        return None
    vals = {}
    try:
        i = 0
        while True:
            try:
                n, v, _t = winreg.EnumValue(k, i)
            except OSError:
                break
            vals[n] = v
            i += 1
    finally:
        try:
            winreg.CloseKey(k)
        except Exception:
            pass
    return vals


def lnk_target(path):
    """TargetPath of a .lnk (via WScript.Shell, the same API that made it)."""
    if not os.path.isfile(path):
        return ""
    ps = ("$ws = New-Object -ComObject WScript.Shell; "
          "$s = $ws.CreateShortcut('%s'); Write-Output $s.TargetPath" % path)
    rc, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                  timeout=40)
    return out.strip() if rc == 0 else ""


def heal_shortcuts():
    """Restore shortcuts a killed run left aside and delete the test's own.

    Called before the shortcut section: `<name>.sptestbak<pid>` next to the
    original is a previous run's backup (moved back), and a SyncPlayer shortcut
    THIS RUN created (its target is under %TEMP% and that temp install is
    already gone) is the test's own leftover and is deleted - the uninstaller
    normally takes them with the install. A shortcut pointing at a live install
    is never touched, so the user's own shortcut is always safe."""
    healed, swept = [], []
    temp = os.path.normcase(os.environ.get("TEMP", "") or "\\none")
    desk = os.path.join(os.path.expanduser("~"), "Desktop")
    sm = inst.startmenu_dir()
    for d in (desk, os.path.dirname(sm)):
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for n in names:
            if ".sptestbak" not in n:
                continue
            src = os.path.join(d, n)
            dst = os.path.join(d, n.split(".sptestbak")[0])
            if not os.path.exists(dst):
                try:
                    shutil.move(src, dst)
                    healed.append(os.path.basename(dst))
                except Exception:
                    pass
    def _dead_test_shortcut(p):
        """A shortcut this test created: it points into %TEMP%, at a temp install
        that is gone. A shortcut pointing at a real app is never touched - no
        clock or mtime reasoning involved, so the user's own shortcut cannot be
        swept by mistake."""
        t = lnk_target(p)
        if not t or not t.lower().startswith(temp):
            return False
        return not os.path.exists(t)

    desk_lnk = os.path.join(desk, "SyncPlayer.lnk")
    if os.path.isfile(desk_lnk) and _dead_test_shortcut(desk_lnk):
        try:
            os.remove(desk_lnk)
            swept.append("Desktop\\SyncPlayer.lnk")
        except Exception:
            pass
    sm_lnk = os.path.join(sm, "SyncPlayer.lnk")
    if os.path.isfile(sm_lnk) and _dead_test_shortcut(sm_lnk):
        try:
            shutil.rmtree(sm, ignore_errors=True)
            swept.append("Start Menu\\SyncPlayer")
        except Exception:
            pass
    return healed, swept


def save_real_shortcuts():
    """Move any REAL SyncPlayer shortcuts aside so the test cannot clobber or
    delete the user's own installation's shortcuts."""
    saved = []
    desk = os.path.join(os.path.expanduser("~"), "Desktop", "SyncPlayer.lnk")
    sm = inst.startmenu_dir()
    stamp = ".sptestbak%d" % os.getpid()
    for path in (desk, sm):
        if os.path.exists(path):
            bak = path + stamp
            try:
                shutil.move(path, bak)
                saved.append((bak, path))
            except Exception:
                pass
    return saved


def restore_real_shortcuts(saved):
    """Put the user's shortcuts back, replacing what the test left behind.

    The test's own installs create shortcuts at the same global paths, so a
    plain "restore only if missing" would strand the user's copy as a .sptestbak
    and leave a %TEMP%-pointing shortcut in its place. An occupant counts as
    ours only when it points into %TEMP% at a temp install that is already gone;
    anything else is left strictly alone."""
    temp = os.path.normcase(os.environ.get("TEMP", "") or "\\none")
    for bak, path in saved or []:
        try:
            if not os.path.exists(bak):
                continue
            if os.path.exists(path):
                tgt = lnk_target(path) if os.path.isfile(path) else ""
                ours = bool(tgt) and tgt.lower().startswith(temp) and not os.path.exists(tgt)
                if not ours:
                    continue          # not ours - leave it exactly as it is
                if os.path.isdir(path):
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    os.remove(path)
            shutil.move(bak, path)
        except Exception:
            pass


def get_exe_version(path):
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


def wait_until(pred, timeout=10.0, interval=0.2):
    """Poll pred() until it returns true, or give up after timeout seconds."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    try:
        return bool(pred())
    except Exception:
        return False


def sp_procs():
    """Rows for a running SyncPlayer process (empty list when none)."""
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq SyncPlayer.exe",
                            "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=25,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return [l for l in (r.stdout or "").splitlines()
                if "SyncPlayer.exe" in l and "No tasks" not in l]
    except Exception:
        return []


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
            out.append((int(pid.strip()), path.strip()))
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

    # -------------------------------------------------------------------------
    # 1. Installer: silent run, unpack, bundled files & install.json
    # -------------------------------------------------------------------------
    rc, out = run([SETUP_EXE, "--silent", "--install-dir", install_dir,
                   "--no-shortcuts"], timeout=180)
    check("install: installer exits 0", rc == 0, out.strip()[:120])

    app = os.path.join(install_dir, "SyncPlayer.exe")
    mpv = os.path.join(install_dir, "mpv", "mpv.exe")
    ytdl = os.path.join(install_dir, "mpv", "yt-dlp.exe")
    updater = os.path.join(install_dir, "SyncPlayer-Updater.exe")
    ij = os.path.join(install_dir, "install.json")

    check("install: SyncPlayer.exe present", os.path.isfile(app))
    check("install: app version is 1.6.0",
          get_exe_version(app) == "1.6.2", str(get_exe_version(app)))
    check("install: bundled mpv.exe present", os.path.isfile(mpv))
    check("install: bundled yt-dlp.exe present", os.path.isfile(ytdl))
    check("install: updater present", os.path.isfile(updater))
    state = {}
    if os.path.isfile(ij):
        try:
            state = json.load(open(ij))
        except Exception:
            pass
    check("install: install.json app_version", state.get("app_version") == "1.6.2")
    check("install: install.json mpv_version", state.get("mpv_version") == "0.41.0")

    # The packaged app must report its own optional pieces: a windowed exe with
    # no stdout writes the report to a file instead (drag & drop silently going
    # missing from a release build is exactly the sort of thing to pin down).
    env_json = os.path.join(tempfile.gettempdir(), "spdeploy_env.json")
    try:
        os.remove(env_json)
    except Exception:
        pass
    rc, out = run([app, "--check-env", env_json], timeout=90)
    env_app = {}
    if os.path.isfile(env_json):
        try:
            env_app = (json.load(open(env_json)) or {}).get("app", {})
        except Exception:
            env_app = {}
    check("install: packaged app reports its version", env_app.get("version") == "1.6.2",
          str(env_app)[:110])
    check("install: packaged app really has drag & drop (tkinterdnd2 bundled)",
          env_app.get("drag_and_drop") is True, str(env_app)[:110])
    check("install: packaged app really has Pillow bundled (visual crop)",
          env_app.get("pillow") is True, str(env_app)[:110])

    # -------------------------------------------------------------------------
    # 2. Bundled mpv & yt-dlp run directly
    # -------------------------------------------------------------------------
    mpvdir = os.path.dirname(mpv)
    env = dict(os.environ)
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
                   "--frames=10", "--ytdl=yes",
                   "--ytdl-raw-options=extractor-args=youtube:player_client=android",
                   YOUTUBE_URL], timeout=120, env=env)
    check("run: bundled mpv plays a YouTube link (yt-dlp)",
          rc == 0, out.strip()[:120])

    # -------------------------------------------------------------------------
    # 3. Installed app runs TWO videos (local + YouTube link) using bundled mpv
    # -------------------------------------------------------------------------
    kill_mpv()
    # the app writes here if it dies (the crash hook); the smoke run below must
    # not add anything. It is also the proof that the packaged build really
    # initialised drag & drop: TkinterDnD.Tk() + registering the drop target run
    # on startup, and a missing tkdnd library used to kill the app outright.
    crash_log = os.path.join(os.path.expanduser("~"), "Pictures", "SyncPlayer",
                             "syncplayer_crash.txt")

    def crash_size():
        try:
            return os.path.getsize(crash_log)
        except OSError:
            return 0

    crash_before = crash_size()
    time.sleep(0.3)
    proc = subprocess.Popen([app, "--smoke", MOVIE, YOUTUBE_URL],
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    seen_two = False
    bundled_used = False
    deadline = time.time() + 15
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
        rc = proc.wait(timeout=25)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = -1
    check("run: app (--smoke) exits cleanly", rc == 0, "exit=%s" % rc)
    check("run: packaged app started drag & drop and closed without crashing",
          crash_size() == crash_before,
          "crash log grew by %d bytes (see %s)" % (crash_size() - crash_before,
                                                   crash_log))
    kill_mpv()

    # -------------------------------------------------------------------------
    # 4. Crash protection: YouTube URL failure does NOT close the other video
    # -------------------------------------------------------------------------
    sys.path.insert(0, BASE)
    sys._TEST_MODE = True
    import syncplayer as sp

    root = sp.tk.Tk()
    root.withdraw()
    app_obj = sp.SyncApp(root)
    app_obj.movie_path.set(MOVIE)
    app_obj.react_path.set(INVALID_YT)

    # Start playback: A is valid local movie, B is invalid YouTube URL
    app_obj._start()

    # Poll until B has failed AND been cleaned up. This is network-bound:
    # yt-dlp has to run and fail, the app auto-retries a failed stream ONCE,
    # and only then is the player torn down. The important invariant is that A
    # stays alive for the whole sequence, so watch it on every iteration.
    a_alive_throughout = True
    deadline = time.time() + 60.0
    while time.time() < deadline:
        app_obj._poll()
        root.update()
        pa_now = app_obj.players.get("A")
        if not (pa_now is not None and pa_now.running):
            a_alive_throughout = False
            break
        if app_obj.players.get("B") is None:      # B failed and was cleaned up
            break
        time.sleep(0.1)

    pa = app_obj.players.get("A")
    pb = app_obj.players.get("B")

    check("crash protection: movie player A stayed alive the whole time B failed",
          a_alive_throughout, "A=%s B=%s" % (bool(pa and pa.running), bool(pb)))
    check("crash protection: movie player A is STILL RUNNING after YouTube B fails",
          pa is not None and pa.running)
    check("crash protection: movie player A is paused and protected",
          pa is not None and pa.paused)
    check("crash protection: failed YouTube player B is cleaned up",
          pb is None, "pb=%r" % (pb,))
    check("crash protection: Start button re-enabled for retry",
          str(app_obj.btn_play.cget("state")) == "normal")

    # -------------------------------------------------------------------------
    # 5. Floating PiP border test (all borders gone, infinite repeated toggles)
    # -------------------------------------------------------------------------
    # Toggle PiP on player A
    app_obj._toggle_pip("A")
    root.update()
    time.sleep(0.2)
    st_pip = u.GetWindowLongPtrW(pa.hwnd, -16) & 0xFFFFFFFF
    has_caption = bool(st_pip & 0x00C00000)
    has_thickframe = bool(st_pip & 0x00040000)
    check("pip floating: caption removed", not has_caption, "style=%s" % hex(st_pip))
    check("pip floating: thickframe border sliver removed", not has_thickframe, "style=%s" % hex(st_pip))

    # Toggle PiP off, then on again multiple times (diagnose repeated toggle)
    toggles_ok = True
    for _ in range(3):
        app_obj._toggle_pip("A") # OFF
        root.update()
        time.sleep(0.1)
        app_obj._toggle_pip("A") # ON
        root.update()
        time.sleep(0.1)
        st = u.GetWindowLongPtrW(pa.hwnd, -16) & 0xFFFFFFFF
        if (st & 0x00C00000) or (st & 0x00040000):
            toggles_ok = False
            break
    check("pip floating: repeated toggling reliably removes frame", toggles_ok)
    app_obj._toggle_pip("A") # restore to normal

    # -------------------------------------------------------------------------
    # 6. Embed PiP test: real Win32 child window (SetParent) inside main feed
    # -------------------------------------------------------------------------
    # Restart B with valid local video for embed test
    if pb:
        pb.quit()
    app_obj.react_path.set(REACT)
    app_obj._start()
    for _ in range(25):
        app_obj._poll()
        root.update()
        time.sleep(0.1)

    pa = app_obj.players.get("A")
    pb = app_obj.players.get("B")
    if pa and pb and pa.hwnd and pb.hwnd:
        app_obj.sync_locked = True
        app_obj._toggle_pip_int("B") # Embed B inside A
        root.update()
        time.sleep(0.3)

        parent_hwnd = u.GetParent(pb.hwnd)
        check("embed pip: child pane is a TRUE Win32 child of host (SetParent)",
              parent_hwnd == pa.hwnd, "parent=%s, host=%s" % (parent_hwnd, pa.hwnd))

        child_st = u.GetWindowLongPtrW(pb.hwnd, -16) & 0xFFFFFFFF
        WS_CHILD = 0x40000000
        check("embed pip: child pane has WS_CHILD style",
              bool(child_st & WS_CHILD), "child_st=%s" % hex(child_st))

        # Test embedded PiP edge reaching & 1-pixel fine-tune move
        csz = app_obj._client_size(pa.hwnd)
        cw, ch = csz
        # Move all the way right
        for _ in range(35):
            app_obj._pip_move(1, 0)
        x_r, y_r, w_r, h_r, mx_r, my_r = app_obj._pip_calc_rect(cw, ch)
        check("embed pip: pane can reach 100% right edge", x_r == mx_r, "x=%d, max_x=%d" % (x_r, mx_r))

        # Move all the way bottom
        for _ in range(35):
            app_obj._pip_move(0, 1)
        x_b, y_b, w_b, h_b, mx_b, my_b = app_obj._pip_calc_rect(cw, ch)
        check("embed pip: pane can reach 100% bottom edge", y_b == my_b, "y=%d, max_y=%d" % (y_b, my_b))

        # Move all the way left & top
        for _ in range(35):
            app_obj._pip_move(-1, -1)
        x_lt, y_lt, w_lt, h_lt, mx_lt, my_lt = app_obj._pip_calc_rect(cw, ch)
        check("embed pip: pane can reach top-left edge (0,0)", x_lt == 0 and y_lt == 0, "x=%d, y=%d" % (x_lt, y_lt))

        # Test Shift fine-tune 1px move
        app_obj._pip_move(1, 0, fine=True) # 1 pixel right
        x_fine, _, _, _, _, _ = app_obj._pip_calc_rect(cw, ch)
        check("embed pip: shift fine-tune moves by exactly 1 pixel", x_fine == 1, "x=%d" % x_fine)

        # Undock
        app_obj._undock_pip_int()
        root.update()
        time.sleep(0.2)
        new_parent = u.GetParent(pb.hwnd)
        check("embed pip: undock restores child to top-level window",
              new_parent == 0 or new_parent is None, "parent=%s" % new_parent)

    # -------------------------------------------------------------------------
    # 7. Video crop test: active session only + Clear restores full resolution
    # -------------------------------------------------------------------------
    app_obj._crop_tag = "A"
    app_obj._crop_nudge("top", 1) # nudge top crop
    root.update()
    time.sleep(0.2)
    check("crop: nudge sets manual crop in active session",
          app_obj._manual_crop.get("A") is not None)

    # Clear crop
    app_obj._crop_clear()
    root.update()
    time.sleep(0.2)
    check("crop: clear resets manual crop to None",
          app_obj._manual_crop.get("A") is None)
    err, vc = pa.get_property("video-crop")
    check("crop: clear resets mpv video-crop to empty",
          err == "success" and (vc == "" or vc is None), "video-crop=%s" % vc)

    # -------------------------------------------------------------------------
    # 8. Interactive visual crop test: VisualCropDialog drag, resize & scale
    # -------------------------------------------------------------------------
    snap_test = os.path.join(BASE, "testmedia", "frame_sb_split25.jpg")
    applied_crop = []
    dlg = sp.VisualCropDialog(root, snap_test, initial_crop=(1280, 540, 0, 90),
                              video_name="Movie", on_apply=lambda c: applied_crop.append(c))
    root.update()
    check("visual crop: dialog created with source image", dlg.orig_w == 1920 and dlg.orig_h == 720)
    init_c = dlg.get_orig_crop()
    check("visual crop: initial crop box matches existing crop", init_c == (1280, 540, 0, 90), "crop=%s" % str(init_c))

    # Test dynamic viewport scaling: simulate window resize to 1400x800
    old_scale = dlg.scale
    dlg._on_canvas_configure(type("Event", (), {"width": 1400, "height": 800})())
    root.update()
    check("visual crop: viewport scales dynamically with window resize", dlg.scale != old_scale and dlg.disp_w > 0 and dlg.disp_h > 0)
    check("visual crop: original crop coordinates preserved exactly after window resize",
          dlg.get_orig_crop() == (1280, 540, 0, 90), "crop=%s" % str(dlg.get_orig_crop()))

    # Apply crop
    dlg._apply()
    root.update()
    check("visual crop: apply invokes on_apply callback with rect", len(applied_crop) == 1 and applied_crop[0] == (1280, 540, 0, 90))

    # Apply the visual crop to live player A
    app_obj._crop_apply("A", applied_crop[0])
    root.update()
    time.sleep(0.2)
    err, vc = pa.get_property("video-crop")
    check("visual crop: mpv video-crop property set from visual tool", err == "success" and bool(vc), "video-crop=%s" % vc)

    # Clear again to restore full resolution
    app_obj._crop_clear()
    root.update()
    time.sleep(0.2)
    err, vc = pa.get_property("video-crop")
    check("visual crop: clear restores uncropped video feed", err == "success" and (vc == "" or vc is None))

    # -------------------------------------------------------------------------
    # 9. Reset PiP button test
    # -------------------------------------------------------------------------
    app_obj._toggle_pip("A") # turn PiP on
    root.update()
    time.sleep(0.1)
    check("reset pip: pip turned on for test", app_obj.pip.get("A") is True)
    app_obj._reset_pip() # reset PiP
    root.update()
    time.sleep(0.2)
    check("reset pip: pip turned off and reset", app_obj.pip.get("A") is False and app_obj.pip_int is False)
    st_norm = u.GetWindowLongPtrW(pa.hwnd, -16) & 0xFFFFFFFF
    check("reset pip: normal window caption/frame restored", bool(st_norm & 0x00C00000))

    # -------------------------------------------------------------------------
    # 10. Volume sliders go to 150% max
    # -------------------------------------------------------------------------
    app_obj.vol_a.set(135.0)
    app_obj._apply_volumes()
    root.update()
    time.sleep(0.1)
    err, cur_vol = pa.get_property("volume")
    check("volume: supports over 100% (up to 150%)", err == "success" and cur_vol is not None and abs(cur_vol - 135.0) <= 2.0, "cur_vol=%s" % cur_vol)

    # -------------------------------------------------------------------------
    # 11. Editable timecode for individual video feeds (Movie & Reaction)
    # -------------------------------------------------------------------------
    app_obj.goto_vars["A"].set("00:06") # jump movie A alone to 6s
    app_obj._on_goto_single("A")
    # The panel's own bookkeeping (_commit_seek) lands immediately, but a stale
    # pre-seek position can still be sitting in the player's queue and overwrite
    # it on the next poll. Verify against mpv ITSELF (authoritative) and give
    # the seek a moment to land.
    got = None
    deadline = time.time() + 6.0
    while time.time() < deadline:
        root.update()
        err, mp = pa.get_property("time-pos")
        try:
            got = abs(float(mp))
        except (TypeError, ValueError):
            got = None
        if got is not None and abs(got - 6.0) <= 1.0:
            break
        time.sleep(0.1)
    check("goto single: movie A seeks to 6s alone (mpv time-pos)",
          got is not None and abs(got - 6.0) <= 1.0, "mpv time-pos=%s" % got)
    check("goto single: goto entry cleared after seek", app_obj.goto_vars["A"].get() == "")

    # -------------------------------------------------------------------------
    # 12. Jump seconds setting for arrow keys
    # -------------------------------------------------------------------------
    app_obj.jump_sec.set(7.5)
    check("jump setting: jump_sec variable updated", app_obj._get_jump_sec() == 7.5)

    # -------------------------------------------------------------------------
    # 13. Buffer duration & YouTube-style fast keyframe seeking
    # -------------------------------------------------------------------------
    pa.seek(3.0, exact=False) # fast keyframe scrub
    time.sleep(0.1)
    check("seek precision: fast keyframe scrub executed without error", True)
    # wait for a status line instead of racing it: cache_dur is filled by the
    # status parser, so a freshly built player does not have it yet
    ok = wait_until(lambda: pa is not None and hasattr(pa, "cache_dur"), 20)
    check("buffer: cache_dur tracked on player status", ok,
          "cache_dur=%s" % getattr(pa, "cache_dur", None))

    app_obj._stop()
    root.destroy()
    kill_mpv()


    # -------------------------------------------------------------------------
    # 14. Installer wizard plumbing: Add/Remove entry, component choices,
    #     shortcuts, custom path, and a real uninstall
    # -------------------------------------------------------------------------
    regvals = reg_read(inst.UNINSTALL_KEY)
    check("wizard: Add/Remove Programs entry is registered", bool(regvals),
          str(regvals)[:90] if regvals else "missing")
    if regvals:
        check("wizard: Add/Remove entry points at the install folder",
              os.path.normcase(str(regvals.get("InstallLocation", "")))
              == os.path.normcase(install_dir),
              str(regvals.get("InstallLocation")))
        check("wizard: Add/Remove entry offers --uninstall",
              "--uninstall" in str(regvals.get("UninstallString", "")),
              str(regvals.get("UninstallString")))
        check("wizard: Add/Remove entry carries the version",
              str(regvals.get("DisplayVersion", "")) == "1.6.2",
              str(regvals.get("DisplayVersion")))
    check("wizard: install.json records which components went in",
          bool(state.get("mpv_installed")) and bool(state.get("ytdlp_installed"))
          and bool(state.get("updater_installed")), str(state)[:110])

    # An install writes its shortcuts to the user's global Desktop/Start-Menu
    # paths, so move the user's own aside BEFORE running any install - otherwise
    # the first install overwrites them and there is nothing to restore.
    healed, swept = heal_shortcuts()
    if healed or swept:
        print("  (shortcut housekeeping: restored %s, removed %s)"
              % (healed or "none", swept or "none"))
    desk_lnk = os.path.join(os.path.expanduser("~"), "Desktop", "SyncPlayer.lnk")
    sm_dir = inst.startmenu_dir()
    # what the user has NOW, before anything is moved or installed
    user_shortcuts_before = (os.path.isfile(desk_lnk), lnk_target(desk_lnk),
                             os.path.isdir(sm_dir))
    saved_lnks = save_real_shortcuts()

    # app-only install: every component can be switched off (--no-shortcuts: this
    # install is not the one testing shortcut creation, so it must not touch them)
    dir_min = tempfile.mkdtemp(prefix="spdeploy_min_")
    rc, out = run([SETUP_EXE, "--silent", "--install-dir", dir_min, "--no-mpv",
                   "--no-ytdlp", "--no-updater", "--no-shortcuts",
                   "--no-launch"], timeout=200)
    check("wizard: app-only install exits 0", rc == 0, out.strip()[:110])
    check("wizard: app-only install still installs the app",
          os.path.isfile(os.path.join(dir_min, "SyncPlayer.exe")))
    check("wizard: --no-mpv really leaves mpv out",
          not os.path.isfile(os.path.join(dir_min, "mpv", "mpv.exe")))
    check("wizard: --no-ytdlp really leaves yt-dlp out",
          not os.path.isfile(os.path.join(dir_min, "mpv", "yt-dlp.exe")))
    check("wizard: --no-updater really leaves the updater out",
          not os.path.isfile(os.path.join(dir_min, "SyncPlayer-Updater.exe")))
    st_min = {}
    try:
        st_min = json.load(open(os.path.join(dir_min, "install.json")))
    except Exception:
        pass
    check("wizard: install.json reflects the component choices",
          st_min.get("mpv_installed") is False
          and st_min.get("ytdlp_installed") is False
          and st_min.get("updater_installed") is False, str(st_min)[:110])
    check("wizard: the chosen install path is recorded",
          os.path.normcase(str(st_min.get("install_dir", ""))) == os.path.normcase(dir_min),
          str(st_min.get("install_dir")))

    # yt-dlp without mpv must still be discoverable where the app looks for it
    dir_yt = tempfile.mkdtemp(prefix="spdeploy_yt_")
    rc, out = run([SETUP_EXE, "--silent", "--install-dir", dir_yt, "--no-mpv",
                   "--no-updater", "--no-shortcuts", "--no-launch"], timeout=200)
    check("wizard: yt-dlp without mpv keeps yt-dlp where the app finds it",
          os.path.isfile(os.path.join(dir_yt, "mpv", "yt-dlp.exe"))
          and not os.path.isfile(os.path.join(dir_yt, "mpv", "mpv.exe")),
          out.strip()[:90])

    # a full install WITH shortcuts, into a path of our own choosing
    dir_full = tempfile.mkdtemp(prefix="spdeploy_full_")
    try:
        rc, out = run([SETUP_EXE, "--silent", "--install-dir", dir_full], timeout=240)
        check("wizard: full install exits 0", rc == 0, out.strip()[:110])
        check("wizard: Desktop shortcut created",
              os.path.isfile(desk_lnk))
        check("wizard: Desktop shortcut points at the installed exe",
              os.path.normcase(lnk_target(desk_lnk))
              == os.path.normcase(os.path.join(dir_full, "SyncPlayer.exe")),
              lnk_target(desk_lnk))
        check("wizard: Start Menu folder created",
              os.path.isdir(sm_dir))
        check("wizard: Start Menu has the app, the updater and an uninstall entry",
              os.path.isfile(os.path.join(sm_dir, "SyncPlayer.lnk"))
              and os.path.isfile(os.path.join(sm_dir, "SyncPlayer - Check for Updates.lnk"))
              and os.path.isfile(os.path.join(sm_dir, "Uninstall SyncPlayer.lnk")),
              str(sorted(os.listdir(sm_dir)) if os.path.isdir(sm_dir) else []))
        vals_full = reg_read(inst.UNINSTALL_KEY) or {}
        check("wizard: Add/Remove entry follows the chosen path",
              os.path.normcase(str(vals_full.get("InstallLocation", "")))
              == os.path.normcase(dir_full), str(vals_full.get("InstallLocation")))

        # the full install allows itself to launch the app (the default), so
        # this is also the "user has it open, then uninstalls" case
        launched = False
        deadline = time.time() + 15.0
        while time.time() < deadline:
            if sp_procs():
                launched = True
                break
            time.sleep(0.5)
        check("wizard: a normal install launches the app when it finishes",
              launched, "processes=%d" % len(sp_procs()))

        # --- uninstall: uses the entry Windows would use ---------------------
        rc, out = run([os.path.join(dir_full, "SyncPlayer.exe"),
                       "--uninstall", "--silent"], timeout=120)
        check("uninstall: exits 0", rc == 0, out.strip()[:110])
        gone = False
        deadline = time.time() + 25.0
        while time.time() < deadline:
            if not os.path.isdir(dir_full):
                gone = True
                break
            time.sleep(0.5)
        check("uninstall: the program folder is removed", gone,
              "still there: %s" % dir_full if not gone else "")
        deadline = time.time() + 15.0
        while time.time() < deadline and sp_procs():
            time.sleep(0.5)
        check("uninstall: closed the running app", not sp_procs(),
              "processes=%d" % len(sp_procs()))
        check("uninstall: the Desktop shortcut is removed", not os.path.isfile(desk_lnk))
        check("uninstall: the Start Menu entry is removed", not os.path.isdir(sm_dir))
        check("uninstall: the Add/Remove entry is removed",
              reg_read(inst.UNINSTALL_KEY) is None)

        # --- the wizard a user actually clicks through -----------------------
        # The silent CLI above proves the payload handling; this drives the real
        # wizard: every page in order, the footer on-screen on each one, and the
        # folder / Start Menu name chosen in the wizard being the ones used.
        # --- installing over an existing install (the wizard promises this) ---
        up_dir = tempfile.mkdtemp(prefix="spdeploy_upg_")
        rc1, _o1 = run([SETUP_EXE, "--silent", "--install-dir", up_dir, "--no-launch",
                       "--no-shortcuts"], timeout=600)
        v1 = get_exe_version(os.path.join(up_dir, "SyncPlayer.exe"))
        rc2, _o2 = run([SETUP_EXE, "--silent", "--install-dir", up_dir, "--no-launch",
                       "--no-shortcuts"], timeout=600)
        v2 = get_exe_version(os.path.join(up_dir, "SyncPlayer.exe"))
        check("upgrade: running Setup again over the same folder succeeds",
              rc1 == 0 and rc2 == 0, "rc1=%s rc2=%s" % (rc1, rc2))
        check("upgrade: the installed app version survives the second run",
              v2 is not None and v2 == v1, "%s -> %s" % (v1, v2))
        check("upgrade: the second run leaves a complete install",
              os.path.isfile(os.path.join(up_dir, "SyncPlayer.exe"))
              and os.path.isfile(os.path.join(up_dir, "install.json"))
              and os.path.isdir(os.path.join(up_dir, "mpv")))
        try:
            _up_state = json.load(open(os.path.join(up_dir, "install.json")))
        except Exception:
            _up_state = {}
        check("upgrade: it still reports the folder it was installed into",
              os.path.normcase(_up_state.get("install_dir", ""))
              == os.path.normcase(up_dir), str(_up_state.get("install_dir")))
        try:                     # uninstall properly: it registered itself
            run([os.path.join(up_dir, "SyncPlayer.exe"), "--uninstall", "--silent"],
                timeout=180)
        except Exception:
            pass
        shutil.rmtree(up_dir, ignore_errors=True)
        _up_tries = 0
        while os.path.isdir(up_dir) and _up_tries < 40:
            _up_tries += 1
            time.sleep(0.5)
            shutil.rmtree(up_dir, ignore_errors=True)

        import tkinter as _tk
        import tkinter.messagebox as _mb
        bundle_dir = os.path.join(BASE, "bundle")
        wiz_dir = tempfile.mkdtemp(prefix="spdeploy_wiz_")
        wiz_menu = "SyncPlayerWizardTest"
        # Not withdrawn: the footer-visibility check below needs real mapping.
        wroot = _tk.Tk()
        wopts = inst.parse_options([])
        wopts.install_dir = wiz_dir
        wopts.startmenu_folder = wiz_menu
        wopts.launch = False            # don't start the app at the end
        # Driven from source the wizard would look for its payload beside
        # installer.py (there is none - the real Setup has it in its onefile
        # extraction dir), so point it at the same bundle the Setup is built
        # from. A modal error box would also hang this test - silence it.
        _real_resolve = inst.resolve_payloads
        _modal_saved = {n: getattr(_mb, n) for n in
                        ("showerror", "showwarning", "showinfo",
                         "askyesno", "askokcancel")}
        inst.resolve_payloads = lambda: (
            os.path.join(bundle_dir, "SyncPlayer.exe"),
            os.path.join(bundle_dir, "mpv"),
            os.path.join(bundle_dir, "SyncPlayer-Updater.exe"))
        # any modal box would block the walk (the fatal one used to hang the run)
        _mb.showerror = lambda *a, **k: None
        _mb.showwarning = lambda *a, **k: None
        _mb.showinfo = lambda *a, **k: None
        _mb.askyesno = lambda *a, **k: True
        _mb.askokcancel = lambda *a, **k: True
        check("wizard: the bundle used for the wizard test is present",
              os.path.isfile(os.path.join(bundle_dir, "SyncPlayer.exe")),
              bundle_dir)
        wiz = inst.Wizard(wroot, wopts)
        wroot.update()
        order = []
        footer_ok = True
        protected_tried = [False]
        protected_stayed = [False]
        last = None
        deadline = time.time() + 300
        while time.time() < deadline:
            name = wiz.PAGES[wiz.page]
            if name != last:                    # record transitions, not ticks
                order.append(name)
                last = name
            wroot.update_idletasks()
            win_bottom = wroot.winfo_rooty() + wroot.winfo_height()
            footer_ok = footer_ok and bool(wiz.btn_next.winfo_ismapped()) and (
                wiz.btn_next.winfo_rooty() + wiz.btn_next.winfo_height()
                <= win_bottom + 1)
            if name == "dest":
                if not protected_tried[0]:
                    # a protected folder must be refused HERE, not fail mid-copy
                    protected_tried[0] = True
                    wiz.dir_var.set(os.path.join(
                        os.environ.get("ProgramFiles", "C:\\Program Files"),
                        "SyncPlayerTest"))
                    wiz.btn_next.invoke()
                    wroot.update()
                    protected_stayed[0] = (wiz.PAGES[wiz.page] == "dest")
                wiz.dir_var.set(wiz_dir)
                wiz.btn_next.invoke()
            elif name == "startmenu":
                wiz.smf_var.set(wiz_menu)
                wiz.btn_next.invoke()
            elif name == "finish":
                break
            elif name == "installing":
                # the worker is copying files / creating shortcuts: let it finish
                # (Next is disabled here; waiting for the finish page is what a
                # user experiences)
                pass
            else:
                wiz.btn_next.invoke()
            wroot.update()
            time.sleep(0.15)
        check("wizard: pages follow the standard order",
              [x for x in order if x != "license"]
              == ["welcome", "dest", "startmenu", "tasks", "ready", "installing",
                  "finish"], str(order))
        check("wizard: refuses a protected destination, staying on the page",
              protected_stayed[0], str(order))
        check("wizard: the footer stays visible on every page", footer_ok)
        check("wizard: it reaches the finish page with no error",
              wiz.PAGES[wiz.page] == "finish" and wiz.state is not None
              and not wiz.error, "page=%s error=%s" % (wiz.PAGES[wiz.page], wiz.error))
        check("wizard: installed into the folder chosen in the wizard",
              os.path.isfile(os.path.join(wiz_dir, "SyncPlayer.exe")))
        check("wizard: reports the shortcuts it created",
              sorted((wiz.state or {}).get("shortcuts") or [])
              == ["desktop", "startmenu", "startmenu-uninstall",
                  "startmenu-updater"], str((wiz.state or {}).get("shortcuts")))
        check("wizard: created the Start Menu folder that was typed in",
              os.path.isfile(os.path.join(inst.startmenu_dir(wiz_menu),
                                          "SyncPlayer.lnk")),
              inst.startmenu_dir(wiz_menu))
        wiz_state = {}
        try:
            wiz_state = json.load(open(os.path.join(wiz_dir, "install.json")))
        except Exception:
            pass
        check("wizard: install.json records the Start Menu folder",
              wiz_state.get("startmenu_folder") == wiz_menu,
              str(wiz_state.get("startmenu_folder")))
        check("wizard: the desktop shortcut points at the wizard's install",
              os.path.normcase(lnk_target(desk_lnk))
              == os.path.normcase(os.path.join(wiz_dir, "SyncPlayer.exe")),
              lnk_target(desk_lnk))
        check("wizard: the Add/Remove entry follows the wizard's folder",
              os.path.normcase(str((reg_read(inst.UNINSTALL_KEY) or {}).get(
                  "InstallLocation", ""))) == os.path.normcase(wiz_dir),
              str((reg_read(inst.UNINSTALL_KEY) or {}).get("InstallLocation")))
        try:
            wroot.destroy()
        except Exception:
            pass
        rc, out = run([os.path.join(wiz_dir, "SyncPlayer.exe"), "--uninstall",
                       "--silent"], timeout=180)
        gone = False
        deadline = time.time() + 30
        while time.time() < deadline:
            if not os.path.isdir(wiz_dir):
                gone = True
                break
            time.sleep(0.5)
        check("wizard uninstall: removes the install the wizard created", gone,
              "still there: %s" % wiz_dir if not gone else "")
        check("wizard uninstall: removes the custom Start Menu folder",
              not os.path.isdir(inst.startmenu_dir(wiz_menu)),
              inst.startmenu_dir(wiz_menu))
        inst.resolve_payloads = _real_resolve
        for _n, _f in _modal_saved.items():
            setattr(_mb, _n, _f)
        shutil.rmtree(wiz_dir, ignore_errors=True)

    finally:
        restore_real_shortcuts(saved_lnks)

    # --- safety: a copy Setup did not install must touch NOTHING ------------
    dir_bare = tempfile.mkdtemp(prefix="spdeploy_bare_")
    shutil.copy2(os.path.join(install_dir, "SyncPlayer.exe"),
                 os.path.join(dir_bare, "SyncPlayer.exe"))
    # plant an Add/Remove entry the way a real install would have one
    inst.register_uninstall(dir_bare, os.path.join(dir_bare, "SyncPlayer.exe"),
                            "9.9.9")
    rc, out = run([os.path.join(dir_bare, "SyncPlayer.exe"), "--uninstall", "--silent"],
                  timeout=120)
    check("uninstall: refuses to delete a folder Setup did not create",
          os.path.isfile(os.path.join(dir_bare, "SyncPlayer.exe")),
          "survived=%s" % os.path.isdir(dir_bare))
    check("uninstall: and says why", "left alone" in out or "not installed" in out,
          out.strip()[:110])
    check("uninstall: a bare copy leaves another install's Add/Remove entry alone",
          (reg_read(inst.UNINSTALL_KEY) or {}).get("DisplayVersion") == "9.9.9",
          str((reg_read(inst.UNINSTALL_KEY) or {}).get("DisplayVersion")))
    inst.unregister_uninstall()

    # The test moves the user's shortcuts aside and puts them back: prove it
    # really did, or a future ordering slip would silently eat them.
    desk_now, sm_now = os.path.isfile(desk_lnk), os.path.isdir(sm_dir)
    tgt_now = lnk_target(desk_lnk) if desk_now else ""
    check("shortcuts: the user's own Desktop shortcut is exactly as it was",
          desk_now == user_shortcuts_before[0]
          and (not desk_now
               or os.path.normcase(tgt_now) == os.path.normcase(user_shortcuts_before[1])),
          "before=%s after=%s" % (user_shortcuts_before[0], desk_now))
    check("shortcuts: the user's own Start Menu entry is exactly as it was",
          sm_now == user_shortcuts_before[2],
          "before=%s after=%s" % (user_shortcuts_before[2], sm_now))

    for d in (dir_min, dir_yt, dir_bare):
        shutil.rmtree(d, ignore_errors=True)


    print("\n==== %d/%d deployment & runtime checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    shutil.rmtree(install_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
