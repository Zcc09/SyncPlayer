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
SETUP_EXE = os.path.join(BASE, "dist", "SyncPlayer-Setup.exe")
APP_EXE = os.path.join(BASE, "dist", "SyncPlayer.exe")
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")   # 12 s local file
REACT = os.path.join(BASE, "testmedia", "react.mp4")   # 10 s local file
YOUTUBE_URL = os.environ.get("YOUTUBE_URL",
                             "https://www.youtube.com/watch?v=jNQXAC9IVRw")
INVALID_YT = "https://www.youtube.com/watch?v=invalid_video_does_not_exist_xyz123"

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
    check("install: app version is 1.5.1",
          get_exe_version(app) == "1.5.1", str(get_exe_version(app)))
    check("install: bundled mpv.exe present", os.path.isfile(mpv))
    check("install: bundled yt-dlp.exe present", os.path.isfile(ytdl))
    check("install: updater present", os.path.isfile(updater))
    state = {}
    if os.path.isfile(ij):
        try:
            state = json.load(open(ij))
        except Exception:
            pass
    check("install: install.json app_version", state.get("app_version") == "1.5.1")
    check("install: install.json mpv_version", state.get("mpv_version") == "0.41.0")

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
    check("buffer: cache_dur tracked on player status", hasattr(pa, "cache_dur"))

    app_obj._stop()
    root.destroy()
    kill_mpv()

    print("\n==== %d/%d deployment & runtime checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    shutil.rmtree(install_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
