#!/usr/bin/env python3
"""Linux/X11 window-feature test for SyncPlayer.

Launches two REAL mpv windows, then exercises every window operation the app
uses: discovery by title, geometry, arrange (move+resize), borderless
(_MOTIF_WM_HINTS), always-on-top (_NET_WM_STATE_ABOVE), and integrated PiP
(reparent/embed via XReparentWindow) including moving the embedded child and
undocking again.

Run inside a graphical session:
    DISPLAY=:0 XAUTHORITY=... python3 selftest_x11.py
"""
import ctypes
import os
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sp_plat as plat   # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")
REACT = os.path.join(BASE, "testmedia", "react.mp4")

passed = 0
failed = 0
fail_msgs = []


class Hung(Exception):
    """Raised by the watchdog so a deadlock FAILS instead of hanging forever."""


def _on_alarm(signum, frame):
    raise Hung("watchdog fired")


def set_alarm(seconds):
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))


def clear_alarm():
    signal.setitimer(signal.ITIMER_REAL, 0)


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("[PASS] %s%s" % (name, (" — " + str(extra)) if extra else ""))
    else:
        failed += 1
        fail_msgs.append(name)
        print("[FAIL] %s%s" % (name, (" — " + str(extra)) if extra else ""))


def launch(title, src, x11_software=False):
    """Start mpv as an X11 (Xwayland) client so the window can be controlled."""
    name = "sptest-%d-%s" % (os.getpid(), title.replace(" ", ""))
    server_arg, _ = plat.ipc_endpoint(name)
    args = [plat.find_mpv(), "--no-config", "--keep-open=yes", "--pause=yes",
            "--osc=no", "--title=%s" % title,
            "--input-ipc-server=%s" % server_arg,
            "--volume-max=150"]
    if x11_software:
        args.append("--vo=x11")
    else:
        args.append("--gpu-context=x11egl")
    args.append(src)
    return subprocess.Popen(args, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            **plat.popen_extra())


def motif_decorations(wb, h):
    """Read back _MOTIF_WM_HINTS decorations value (0 = borderless)."""
    _, fmt, vals, _ = wb._prop(h, "_MOTIF_WM_HINTS", 5)
    if fmt == 32 and len(vals) >= 3:
        return vals[2]
    return None


def net_wm_state(wb, h):
    _, fmt, vals, _ = wb._prop(h, "_NET_WM_STATE", 16)
    return vals or []


def main():
    wb = plat.get_window_backend()
    print("backend=%s ok=%s" % (wb.name, wb.ok))
    if not wb.ok:
        print("SKIP: no X11 display (%s)" % wb.reason)
        return 2

    mpv = plat.find_mpv()
    if not mpv:
        print("SKIP: mpv not found")
        return 2
    for f in (MOVIE, REACT):
        if not os.path.isfile(f):
            print("SKIP: missing test clip %s" % f)
            return 2

    p1 = p2 = None
    try:
        p1 = launch("SyncPlayer Movie", MOVIE)
        p2 = launch("SyncPlayer Reaction", REACT)
        time.sleep(2.5)

        h1 = wb.find_window(p1.pid, "SyncPlayer Movie", tries=40, delay=0.25)
        h2 = wb.find_window(p2.pid, "SyncPlayer Reaction", tries=40, delay=0.25)
        check("x11: find_window locates the Movie window by title", bool(h1), hex(h1 or 0))
        check("x11: find_window locates the Reaction window by title", bool(h2), hex(h2 or 0))
        if not (h1 and h2):
            # help debug: dump what we see
            for line in (p1.stderr.read(2000).decode("utf-8", "replace")
                         if p1.stderr else "").splitlines()[:10]:
                print("   mpv1:", line)
            return 1

        check("x11: windows are distinct handles", h1 != h2, (hex(h1), hex(h2)))
        check("x11: screen size is sane", wb.screen_size()[0] > 200, wb.screen_size())

        r1 = wb.get_rect(h1)
        check("x11: get_rect returns geometry", bool(r1) and r1[2] > 0 and r1[3] > 0, r1)
        check("x11: client_size returns geometry", bool(wb.client_size(h1)), wb.client_size(h1))

        # ---- arrange (what the ⇦⇨ Arrange button does) --------------------
        ok = wb.place(h1, 0, 0, 960, 540)
        wb.place(h2, 960, 0, 960, 540)
        time.sleep(0.6)
        ra = wb.get_rect(h1)
        check("x11: place() moves+resizes a window", ok and ra is not None
              and abs(ra[2] - 960) < 60, ra)

        # ---- borderless (floating PiP) ------------------------------------
        wb.set_borderless(h1, True)
        time.sleep(0.4)
        check("x11: borderless sets _MOTIF_WM_HINTS decorations=0",
              motif_decorations(wb, h1) == 0, motif_decorations(wb, h1))
        wb.set_borderless(h1, False)
        time.sleep(0.3)
        check("x11: borderless can be undone (decorations=1)",
              motif_decorations(wb, h1) == 1, motif_decorations(wb, h1))

        # ---- always-on-top -------------------------------------------------
        wb.set_ontop(h1, True)
        time.sleep(0.4)
        above = wb.atom("_NET_WM_STATE_ABOVE")
        st = net_wm_state(wb, h1)
        check("x11: set_ontop adds _NET_WM_STATE_ABOVE", above in st,
              "states=%s above=%s" % ([hex(s) for s in st], hex(above)))
        wb.set_ontop(h1, False)
        time.sleep(0.3)

        # ---- integrated PiP (reparent/embed) ------------------------------
        em = wb.embed(h2, h1)
        time.sleep(0.6)
        par = wb.parent_of(h2)
        check("x11: embed() reparents the pane into the host", em and par == h1,
              "parent=%s host=%s" % (hex(par or 0), hex(h1)))
        check("x11: pane is no longer a root child", par != wb.root,
              "parent=%s root=%s" % (hex(par or 0), hex(wb.root)))

        moved = wb.move_child(h2, 40, 30, 320, 180)
        time.sleep(0.5)
        cr = wb.get_rect(h2)
        check("x11: embedded child can be moved/sized inside the host",
              moved and cr is not None and abs(cr[2] - 320) < 60,
              "child rect=%s" % (cr,))

        # reach the far edge like the panel arrows do
        csz = wb.client_size(h1)
        if csz:
            wb.move_child(h2, max(0, csz[0] - 320), max(0, csz[1] - 180), 320, 180)
            time.sleep(0.4)
            cr2 = wb.get_rect(h2)
            check("x11: embedded child reaches the host's far edge",
                  cr2 is not None and cr2[0] > 0, "child rect=%s host client=%s" % (cr2, csz))

        # ---- undock --------------------------------------------------------
        un = wb.unembed(h2)
        time.sleep(0.6)
        par2 = wb.parent_of(h2)
        check("x11: unembed() returns the pane to top level",
              un and par2 == wb.root, "parent=%s root=%s" % (hex(par2 or 0), hex(wb.root)))

        # ---- restore_style / valid ----------------------------------------
        check("x11: valid() true for live windows",
              wb.valid(h1) and wb.valid(h2))

        # ---- thread safety -------------------------------------------------
        # Regression: Xlib is not thread-safe. SyncPlayer calls the backend from
        # its arrange/screenshot threads AND from the Tk main thread, and two
        # threads sharing the connection used to eat each other's replies ->
        # the loser hung forever inside XGetGeometry (XReply) under Xwayland.
        t_started = time.time()
        t_errors = []
        t_stop = threading.Event()

        def _worker():
            try:
                while not t_stop.is_set():
                    wb.get_rect(h1)
                    wb.client_size(h2)
                    wb.parent_of(h2)
                    wb.save_style(h2)
                    wb.screen_size()
                    wb.valid(h1)
                    wb._prop(h1, "_NET_WM_NAME")
            except Exception as exc:            # noqa: BLE001
                t_errors.append(repr(exc))

        workers = [threading.Thread(target=_worker, daemon=True) for _ in range(4)]
        for w in workers:
            w.start()

        # the main thread hammers the same connection too, including an
        # UNWRAPPED retry search (find_window sleeps between attempts)
        def _main_load():
            deadline = time.time() + 4.0
            while time.time() < deadline:
                wb.get_rect(h1)
                wb.client_size(h1)
                wb.find_window(-1, "SyncPlayer — NoSuchWindow", tries=1, delay=0.0)

        set_alarm(25)                           # watchdog: a deadlock must FAIL
        try:
            _main_load()
            t_stop.set()
            for w in workers:
                w.join(timeout=5)
            alive = [w for w in workers if w.is_alive()]
            elapsed = time.time() - t_started
            check("x11: concurrent backend calls from 4 threads + main thread "
                  "do not deadlock", not alive and not t_errors,
                  "elapsed=%.1fs alive=%d errors=%s" % (elapsed, len(alive), t_errors[:2]))
        except Hung:
            t_stop.set()
            check("x11: concurrent backend calls from 4 threads + main thread "
                  "do not deadlock", False, "DEADLOCK (watchdog fired)")
        finally:
            clear_alarm()
        t_stop.set()
    finally:
        for p in (p1, p2):
            try:
                if p and p.poll() is None:
                    p.terminate()
                    try:
                        p.wait(timeout=5)
                    except Exception:
                        p.kill()
            except Exception:
                pass

    print("\n==== %d/%d X11 window checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
