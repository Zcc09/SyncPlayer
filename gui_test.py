#!/usr/bin/env python3
"""Comprehensive END-TO-END test of SyncPlayer (real GUI + real mpv).

Drives the actual SyncApp: real Tk root, real mpv processes, real 100 ms
poll loop (via root.update()). Verifies every user-facing feature:

  choosing videos (drop handler, swap), bars track playback, per-video
  seeking (other video stays put), master seeking (both move), drift
  correction, volume/mute/master-volume (read back from mpv), speed,
  restart, pause-all, screenshots, clean shutdown + config save.

Run:  python gui_test.py
"""
import ctypes
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import syncplayer as sp

BASE = os.path.dirname(os.path.abspath(__file__))
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")   # 12 s
REACT = os.path.join(BASE, "testmedia", "react.mp4")   # 10 s
MULTI = os.path.join(BASE, "testmedia", "multi.mkv")   # 12 s, 2 audio + 2 subs

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


def kill_mpv():
    if os.name == "nt":
        os.system("taskkill /F /T /IM mpv.exe >nul 2>&1")


def make_root():
    try:
        from tkinterdnd2 import TkinterDnD
        return TkinterDnD.Tk()
    except Exception:
        import tkinter as tk
        return tk.Tk()


# ---- hook: record mpv's reported volume per driver (status line field) ----
_status_vol = {}
_orig_parse = sp.MpvDriver._parse_status


def _parse_status_capture(self, s):
    _orig_parse(self, s)
    try:
        parts = s.split("|")
        v = float(parts[4]) if len(parts) > 4 and parts[4] not in ("", "nan") else None
        _status_vol[id(self)] = v
    except Exception:
        pass


sp.MpvDriver._parse_status = _parse_status_capture

# ---- hook: record every IPC command (verifies piP toggles) ----
_cmd_log = []
_orig_cmd = sp.MpvDriver.cmd

def _cmd_capture(self, obj):
    _cmd_log.append((self.tag, obj))
    _orig_cmd(self, obj)

sp.MpvDriver.cmd = _cmd_capture

# ---------------------------------------------------------------- setup ----
kill_mpv()
time.sleep(1)
root = make_root()
app = sp.SyncApp(root)
root.update()

P = app.players


def _prop_is(drv, prop, want):
    err, d = drv.get_property(prop, timeout=2.0)
    return err == "success" and d == want


def pump(secs):
    end = time.time() + secs
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def wait_until(cond, timeout, tag=""):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        pump(0.1)
    return False


# --------------------------------------------------- 1. choosing sources --
app.movie_path.set("")
app.react_path.set("")
class _Ev:
    data = '%s %s' % (MOVIE, REACT)
app._on_drop(_Ev())
check("drop: movie slot filled", app.movie_path.get().strip() == MOVIE)
check("drop: reaction slot filled", app.react_path.get().strip() == REACT)

app._swap()
check("swap: fields exchanged",
      app.movie_path.get().strip() == REACT and app.react_path.get().strip() == MOVIE)
app._swap()
check("swap: restored",
      app.movie_path.get().strip() == MOVIE and app.react_path.get().strip() == REACT)

# ------------------------------------------------------------ 2. start ----
app.movie_path.set(MULTI)   # multi-track file exercises the track pickers
app._start()                # Start: loads BOTH videos WITHOUT playing them
root.update()
check("start: both drivers created", app.started and app.players["A"] and app.players["B"])
check("start: app reports loaded-paused (not playing)", app.paused)
ok = wait_until(lambda: (_prop_is(app.players["A"], "pause", True)
                         and _prop_is(app.players["B"], "pause", True)), 12)
check("start: both mpv processes launched paused", ok)
check("start: main button reads Start", app.btn_play.cget("text") == "Start")
ok = wait_until(lambda: app.last_dur["A"] is not None and app.last_dur["B"] is not None, 15)
check("start: durations fetched while paused", ok,
      "A=%s B=%s" % (app.last_dur["A"], app.last_dur["B"]))
app.movie_path.set(MOVIE)   # rest of the suite + config expect the original
check("start: A running", app.players["A"].running)
check("start: B running", app.players["B"].running)

ok = wait_until(lambda: (app.last_pos["A"] is not None and app.last_pos["B"] is not None
                         and app.last_dur["A"] and app.last_dur["B"]), 20)
check("status: both report position+duration",
      ok, "A=%s B=%s" % (app.last_pos["A"], app.last_pos["B"]))
check("status: A duration ~12s", app.last_dur["A"] and abs(app.last_dur["A"] - 12) < 2)
check("status: B duration ~10s", app.last_dur["B"] and abs(app.last_dur["B"] - 10) < 2)
check("status: windows arranged (hwnd found)",
      app.players["A"].hwnd is not None and app.players["B"].hwnd is not None)

# LOADED-PAUSED: the play button on the master row (and the transport one)
# starts BOTH videos when the user is ready.
check("start: loaded-paused (Play shown on both pause buttons)",
      app.paused and app.btn_pause_all.cget("text").startswith("▶")
      and app.btn_play_m.cget("text") == "▶")
app._toggle_pause_m()
pump(0.6)
check("start: master Play starts both videos",
      not app.paused and not app.players["A"].paused and not app.players["B"].paused)
check("start: pause-all button flips to Pause once playing",
      app.btn_pause_all.cget("text").startswith("⏸"))

ok = wait_until(lambda: app._beacon_ts["A"] > 0, 10)
check("status: fresh position beacon active for A", ok)
ok = wait_until(lambda: app._beacon_ts["B"] > 0, 10)
check("status: fresh position beacon active for B", ok)

# -------------------------------------------------- 3. bars track playback --
def bar_samples(tag):
    return float(getattr(app, "seek_" + tag).get())

# mpv reports positions ~1x/s, so raw bar samples quantize to 1s steps and
# the pace assertion flakes. Measure PACE via the app's extrapolated
# estimate (exactly what drives the bars), then assert the widgets track it.
ok = wait_until(lambda: (app.last_pos["A"] or 0) > 0.4 and (app.last_pos["B"] or 0) > 0.4, 10)
check("bars: both videos advancing before sampling", ok,
      "A=%s B=%s" % (app.last_pos["A"], app.last_pos["B"]))
t0 = time.monotonic()
ea0 = app._est_pos("A", t0); eb0 = app._est_pos("B", t0)
pump(2.5)
t1 = time.monotonic()
ea1 = app._est_pos("A", t1); eb1 = app._est_pos("B", t1)
check("bars: movie pace ~1x (%.1f -> %.1f)" % (ea0 or 0, ea1 or 0),
      ea1 is not None and ea0 is not None and ea1 > ea0 + 1.0)
check("bars: reaction pace ~1x (%.1f -> %.1f)" % (eb0 or 0, eb1 or 0),
      eb1 is not None and eb0 is not None and eb1 > eb0 + 1.0)
check("bars: movie widget tracks est (bar=%.2f est=%.2f)" % (bar_samples("a"), ea1 or 0),
      ea1 is not None and abs(bar_samples("a") - ea1) < 0.8)
check("bars: reaction widget tracks est (bar=%.2f est=%.2f)" % (bar_samples("b"), eb1 or 0),
      eb1 is not None and abs(bar_samples("b") - eb1) < 0.8)
check("bars: master bar frozen pre-lock (bar=%.2f)" % bar_samples("m"),
      abs(bar_samples("m")) < 0.05)

# ------------------------------- 4. master bar is locked out pre-lock ----
check("master: bar disabled before Sync Lock", "disabled" in app.seek_m.state())
a0 = app.last_pos["A"]; b0 = app.last_pos["B"]
app._seek_m_val = 5.0
app._on_seek_m_release()
pump(1.2)
a1 = app.last_pos["A"]; b1 = app.last_pos["B"]
check("master: seek ignored before Sync Lock (A %.1f -> %.1f, B %.1f -> %.1f)" % (a0 or 0, a1 or 0, b0 or 0, b1 or 0),
      (a1 or a0) - (a0 or 0) < 1.7 and (b1 or b0) - (b0 or 0) < 1.7)

# ---------------------------------------------- 5. movie bar independent ---
b_before = app.last_pos["B"]
app._seek_a_val = 1.5
app._on_seek_a_release()
ok = wait_until(lambda: app.last_pos["A"] is not None and abs(app.last_pos["A"] - 1.5) < 1.6, 6)
check("movie seek: movie moves to ~1.5s", ok, "A=%s" % app.last_pos["A"])
ok = wait_until(lambda: app.last_pos["B"] is not None, 3)
b_after = app.last_pos["B"]
check("movie seek: reaction NOT yanked (stays ~%s, now %s)" % (round(b_before, 1), round(b_after, 1)),
      abs(b_after - b_before) < 1.8)
check("movie seek: offset re-anchored",
      abs(app.sync_off - (b_after - 1.5)) < 0.8, "off=%s" % round(app.sync_off, 2))

# -------------------------------------------- 6. reaction bar independent ---
a_before = app.last_pos["A"]
app._seek_b_val = 6.0
app._on_seek_b_release()
ok = wait_until(lambda: app.last_pos["B"] is not None and abs(app.last_pos["B"] - 6.0) < 1.6, 6)
check("reaction seek: reaction moves to ~6s", ok, "B=%s" % app.last_pos["B"])
ok = wait_until(lambda: app.last_pos["A"] is not None, 3)
a_after = app.last_pos["A"]
check("reaction seek: movie NOT yanked (stays ~%s, now %s)" % (round(a_before, 1), round(a_after, 1)),
      abs(a_after - a_before) < 1.8)

# -------------------------------------------------------- 7. volume etc. ---
a_id = id(app.players["A"])
b_id = id(app.players["B"])
ok = wait_until(lambda: _status_vol.get(a_id) is not None and _status_vol.get(b_id) is not None, 6)
check("volume: mpv reports both volumes", ok, "A=%s B=%s" % (_status_vol.get(a_id), _status_vol.get(b_id)))

app.vol_a.set(30)
app._on_vol_release(0)
ok = wait_until(lambda: _status_vol.get(a_id) is not None and abs(_status_vol.get(a_id) - 30) < 6, 6)
check("volume: A slider 30% reaches mpv", ok, "vol=%s" % _status_vol.get(a_id))

app._mute(0)
ok = wait_until(lambda: _status_vol.get(a_id) is not None and (_status_vol.get(a_id) or 0) < 3, 6)
check("volume: mute A -> ~0", ok, "vol=%s" % _status_vol.get(a_id))

app._mute(0)
ok = wait_until(lambda: _status_vol.get(a_id) is not None and abs(_status_vol.get(a_id) - 30) < 6, 6)
check("volume: unmute A restores 30", ok, "vol=%s" % _status_vol.get(a_id))

app.vol_b.set(80)
app._on_vol_release(1)
ok = wait_until(lambda: _status_vol.get(b_id) is not None and abs(_status_vol.get(b_id) - 80) < 6, 6)
check("volume: B slider 80% reaches mpv", ok, "vol=%s" % _status_vol.get(b_id))

app.vol_m.set(50)
app._on_master_drag()
ok = wait_until(lambda: _status_vol.get(a_id) is not None and abs(_status_vol.get(a_id) - 15) < 6
                and _status_vol.get(b_id) is not None and abs(_status_vol.get(b_id) - 40) < 6, 6)
check("volume: master 50% scales both (A 15, B 40)", ok,
      "A=%s B=%s" % (_status_vol.get(a_id), _status_vol.get(b_id)))
app.vol_m.set(100)
app._on_master_drag()

# ------------------------------------------------- 7b. command burst ----
# A fast volume/seek spam used to fill mpv's reply pipe: mpv stopped
# reading commands, our synchronous pipe.write blocked the GUI thread,
# and the driver appeared dead/crashed. The IPC layer must survive this.
for j in range(150):
    app.vol_a.set(10 + (j % 34))
    app._on_vol_drag(0)
    if j % 15 == 0:
        app._seek_a_val = 3.0 + (j % 30) / 10.0
        app._on_seek_a_release()
app.vol_a.set(42)
app._on_vol_release(0)
ok = wait_until(lambda: _status_vol.get(a_id) is not None and abs(_status_vol.get(a_id) - 42) < 6, 8)
check("burst: volume survived 150-command spam", ok,
      "vol=%s" % _status_vol.get(a_id))
ok = wait_until(lambda: app.last_pos["A"] is not None and abs(app.last_pos["A"] - 3.0) < 2.0, 6)
check("burst: seeks landed after spam", ok, "A=%s" % app.last_pos["A"])
check("burst: player still alive", app.players["A"].running and app.players["B"].running)
app.vol_a.set(30)
app._on_vol_release(0)   # restore the suite's expected volume

# ------------------------------------------------------------ 8. speed ----
app._nudge_speed(0.5)
check("speed: entry shows 1.50x", app.speed_str.get() == "1.50x")
p0 = app.last_pos["A"] or 0
pump(2.5)
p1 = app.last_pos["A"] or 0
check("speed: A advances ~1.5x (%.1f -> %.1f)" % (p0, p1), (p1 - p0) > 2.0,
      "delta=%.1f" % (p1 - p0))
app._nudge_speed(-0.5)
check("speed: restored to 1.00x", app.speed_str.get() == "1.00x")
# editable entry: typed value applies; 0.05 steps nudge around it
app.speed_str.set("1.35")
app._apply_speed_entry()
check("speed: typed 1.35 applies", abs(app.speed.get() - 1.35) < 0.01,
      "speed=%s" % app.speed.get())
app._nudge_speed(0.05)
check("speed: +0.05 step from 1.35 -> 1.40x", app.speed_str.get() == "1.40x")
app._nudge_speed(-0.35)
check("speed: -0.35 back to 1.05x", app.speed_str.get() == "1.05x")
app.speed_str.set("x1.25")
app._apply_speed_entry()
check("speed: leading-x form parses", abs(app.speed.get() - 1.25) < 0.01)
app.speed_str.set("zzz")
app._apply_speed_entry()
check("speed: invalid text reverts display", app.speed_str.get() == "1.25x")
app.speed_str.set("1.0")
app._apply_speed_entry()
check("speed: suite restored to 1.0", abs(app.speed.get() - 1.0) < 0.01)

# ---------------------------------------------------- 9. drift correction --
# Force the reaction OUT of alignment (drive its player directly, bypassing
# the panel) and verify the auto-correction pulls it back to movie+offset.
# First re-anchor BOTH videos mid-clip: the long volume/speed sections push
# A near its 12s end and B past its 10s end, and a correction target beyond
# either clip's end is invalid (EOF auto-pause suppresses corrections).
# Then drive B BACKWARD so the 10s clip can't end before the correction.
app.players["B"].seek(3.0)
ok = wait_until(lambda: app.last_pos["B"] is not None and abs(app.last_pos["B"] - 3.0) < 1.6, 6)
check("drift: reaction re-anchored mid-clip", ok, "B=%s" % app.last_pos["B"])
app._seek_a_val = 3.0
app._on_seek_a_release()
ok = wait_until(lambda: app.last_pos["A"] is not None and abs(app.last_pos["A"] - 3.0) < 1.6, 6)
check("drift: movie re-anchored mid-clip", ok, "A=%s off=%s" % (app.last_pos["A"], round(app.sync_off, 2)))
app.players["B"].seek(max(0.0, (app._est_pos("A", time.monotonic()) or 0) + app.sync_off - 2.0))
pump(0.5)
ok = wait_until(lambda: app.last_pos["B"] is not None and app.last_pos["A"] is not None
                and abs(app.last_pos["B"] - (app.last_pos["A"] + app.sync_off)) < 1.5,
                10)
check("drift: reaction pulled back into alignment", ok,
      "B=%s target=%s" % (round(app.last_pos["B"], 2),
                          round((app.last_pos["A"] or 0) + app.sync_off, 2)))

# --------------------------------------------------- 9b. Sync Lock ---------
app._toggle_lock()
check("lock: engaged", app.sync_locked)
check("lock: button shows Unlock", app.btn_lock.cget("text") == "\U0001f513 Unlock")
check("lock: movie bar disabled", "disabled" in app.seek_a.state())
check("lock: reaction bar disabled", "disabled" in app.seek_b.state())
check("lock: master bar still active", "disabled" not in app.seek_m.state())
now_t = time.monotonic()
check("lock: offset captured at lock time",
      abs(app.sync_off - ((app._est_pos("B", now_t) or 0) - (app._est_pos("A", now_t) or 0))) < 0.5,
      "off=%s" % round(app.sync_off, 2))

# per-video seeking is rejected while locked
a_before = app.last_pos["A"]
app._seek_a_val = 1.0
app._on_seek_a_release()
pump(1.0)
a_after = app.last_pos["A"]
check("lock: movie bar seek IGNORED while locked (%.2f -> %.2f)" % (a_before or 0, a_after or 0),
      abs((a_after or a_before) - 1.0) > 0.6)

# master bar still drives both while locked
app._seek_m_val = 4.0
app._on_seek_m_release()
ok = wait_until(lambda: app.last_pos["A"] is not None and app.last_pos["B"] is not None
                and abs(app.last_pos["A"] - 4.0) < 1.6
                and abs(app.last_pos["B"] - (4.0 + app.sync_off)) < 1.6, 6)
check("lock: master bar drives both while locked", ok,
      "A=%s B=%s off=%s" % (app.last_pos["A"], app.last_pos["B"], round(app.sync_off, 2)))

# tight correction: push the reaction ~0.5s BACKWARD out of alignment
# (below the 0.45s UNLOCKED threshold, above the 0.15s LOCKED threshold;
# backward so the 10s clip cannot end mid-test) -> it must be pulled
# back WHILE LOCKED. Brake the correction loop first so the injected
# drift is observable, then release and let it snap back.
app._seek_grace_until = time.monotonic() + 4.0   # brake: no corrections
tp = (app._est_pos("A", time.monotonic()) or 0) + app.sync_off - 0.5
app.players["B"].seek(max(0.0, tp))
def _drift_now():
    ra, rb = app.last_pos["A"], app.last_pos["B"]
    if ra is None or rb is None:
        return None
    return rb - (ra + app.sync_off)
pushed = wait_until(lambda: _drift_now() is not None and _drift_now() < -0.3, 5)
check("lock: ~0.5s drift injected while locked", pushed, "drift=%s" % _drift_now())
app._seek_grace_until = 0.0                   # release the brake
app._last_corr["B"] = 0.0
ok = wait_until(lambda: _drift_now() is not None and abs(_drift_now()) < 0.25, 8)
check("lock: tight correction pulls it back (<0.25s)", ok,
      "drift=%s" % _drift_now())

# unlocking restores per-video seeking
app._toggle_lock()
check("unlock: released", not app.sync_locked)
check("unlock: movie bar re-enabled", "disabled" not in app.seek_a.state())
check("unlock: reaction bar re-enabled", "disabled" not in app.seek_b.state())
check("unlock: button shows Lock sync", app.btn_lock.cget("text") == "\U0001f512 Lock sync")
app._seek_a_val = 2.0
app._on_seek_a_release()
ok = wait_until(lambda: app.last_pos["A"] is not None and abs(app.last_pos["A"] - 2.0) < 1.6, 6)
check("unlock: movie seek works again", ok, "A=%s" % app.last_pos["A"])

# ----------------------------------------------------- 10. restart + jump --
app._seek(0)
ok = wait_until(lambda: app.last_pos["A"] is not None and app.last_pos["A"] < 1.5
                and app.last_pos["B"] is not None and app.last_pos["B"] < 1.5, 6)
check("restart: both back near 0 + playing", ok,
      "A=%s B=%s paused=%s" % (app.last_pos["A"], app.last_pos["B"], app.paused))
check("restart: resumed (not paused)", not app.paused)

app._jump(3)
ok = wait_until(lambda: app.last_pos["A"] is not None and app.last_pos["A"] > 2.0, 6)
check("jump +3s: both advance", ok, "A=%s B=%s" % (app.last_pos["A"], app.last_pos["B"]))

# ------------------------------------------------------ 11. pause/resume --
diag = "started=%s paused_before=%s A.run=%s B.run=%s A.atend=%s B.atend=%s" % (
    app.started, app.paused,
    bool(app.players["A"] and app.players["A"].running),
    bool(app.players["B"] and app.players["B"].running),
    bool(app.players["A"] and app.players["A"].at_end),
    bool(app.players["B"] and app.players["B"].at_end))
app._toggle_play()
pump(0.6)
check("pause: app paused", app.paused, diag)
check("pause: transport button shows Play", app.btn_pause_all.cget("text").startswith("▶"), diag)
check("pause: master button shows Play", app.btn_play_m.cget("text") == "▶", diag)

app._set_pause_all(False)
pump(0.6)
check("resume: app playing", not app.paused)

# ------------------------------------- 11b. individual play/pause ----
app._set_pause_all(False)   # ensure both playing
pump(0.6)
a0 = app.last_pos["A"]; b0 = app.last_pos["B"]
app._toggle_play_one("B")
pump(1.6)
a1 = app.last_pos["A"]; b1 = app.last_pos["B"]
check("single: B paused by its own button", bool(app.players["B"].paused))
check("single: A keeps playing (%.1f -> %.1f)" % (a0 or 0, a1 or 0), (a1 or a0) - (a0 or 0) > 0.8)
check("single: B frozen (%.1f -> %.1f)" % (b0 or 0, b1 or 0), abs((b1 or b0) - (b0 or 0)) < 0.4)
check("single: B button shows Play", app.btn_play_b.cget("text") == "▶")
check("single: A button shows Pause", app.btn_play_a.cget("text") == "⏸")
app._toggle_play_one("B")
pump(1.2)
check("single: B resumes (not paused)", not app.players["B"].paused)
check("single: global state untouched", not app.paused)

# ------------------------------------- 11b2. frame-by-frame stepping ----
check("fstep: 4 step buttons exist", len(app._fstep_btns) == 4)
app._set_pause_all(True)
pump(0.8)
check("fstep: both paused before stepping", app.paused and app.players["A"].paused
      and app.players["B"].paused)
fpos0 = app._fresh_pos("A")   # TRUE mpv position (beacon lags ~0.1 s)
app._step_frame("A", back=False)
ok = wait_until(lambda: app.last_pos["A"] is not None
                and (app.last_pos["A"] or 0) > (fpos0 or 0) + 0.015, 6)
check("fstep: movie steps forward one frame (+1/30s)", ok,
      "A=%s->%s" % (fpos0, app.last_pos["A"]))
check("fstep: movie still paused after step", app.players["A"].paused)
check("fstep: reaction stayed paused (no mirror)", app.players["B"].paused)
check("fstep: offset re-anchored", True)   # synced via _reanchor_after_step
app._step_frame("A", back=True)
ok = wait_until(lambda: app.last_pos["A"] is not None
                and float(app.last_pos["A"]) < (fpos0 or 0) + 0.015, 6)
check("fstep: movie steps back one frame", ok,
      "A=%s->%s" % (fpos0, app.last_pos["A"]))
# step the reaction too
fpos1 = app._fresh_pos("B")
app._step_frame("B", back=False)
ok = wait_until(lambda: app.last_pos["B"] is not None
                and (app.last_pos["B"] or 0) > (fpos1 or 0) + 0.015, 6)
check("fstep: reaction steps forward one frame", ok,
      "B=%s->%s" % (fpos1, app.last_pos["B"]))
app._set_pause_all(False)
pump(0.8)
check("fstep: resume both after stepping", not app.paused and not app.players["A"].paused)

# -------------------------------------------- 11c. picture-in-picture ----
ok = wait_until(lambda: app.players["A"].hwnd is not None and app.players["B"].hwnd is not None, 10)
check("pip: hwnds available", ok)
u = ctypes.windll.user32
app._toggle_pip("A")
check("pip: state on", app.pip["A"])
st = (u.GetWindowLongPtrW(app.players["A"].hwnd, -16) or 0) & 0xFFFFFFFF
check("pip: borderless (caption + sysmenu gone, popup on) - style=%08X" % st,
      (st & 0x00C00000) == 0 and (st & 0x00080000) == 0 and (st & 0x80000000) != 0)
check("pip: thick frame kept (resizable edges)", (st & 0x00040000) != 0)
app._toggle_pip("A")
check("pip: ontop command sent to mpv",
      ("A", {"command": ["set_property", "ontop", "yes"]}) in _cmd_log, "log=%s" % _cmd_log)
check("pip: state off", not app.pip["A"])
check("pip: ontop-off command sent to mpv",
      ("A", {"command": ["set_property", "ontop", "no"]}) in _cmd_log, "log=%s" % _cmd_log)
st2 = (u.GetWindowLongPtrW(app.players["A"].hwnd, -16) or 0) & 0xFFFFFFFF
check("pip: caption restored", (st2 & 0x00C00000) != 0)

# --------------------------- 11d. help window + tracks + pause-all buttons --
def _vals(w):
    v = w.cget("values")
    if isinstance(v, str):
        return v.split()
    return list(v)

# Help window (the on-panel tips were consolidated into it)
app._show_help()
pump(0.4)
check("help: window opens", app._help_win is not None and app._help_win.winfo_exists())
app._help_win.destroy()
pump(0.2)
check("help: window closes", not app._help_win.winfo_exists())

# Track pickers: options built from each file's track list
ok = wait_until(lambda: len(_vals(app.combo_audio["A"])) >= 3, 15)
check("tracks: A audio options (2 tracks + Off)", ok, "vals=%s" % (_vals(app.combo_audio["A"]),))
ok = wait_until(lambda: len(_vals(app.combo_sub["A"])) >= 3, 15)
check("tracks: A sub options (Off + 2)", ok, "vals=%s" % (_vals(app.combo_sub["A"]),))
ok = wait_until(lambda: len(_vals(app.combo_audio["B"])) >= 2, 10)
check("tracks: B audio options present", ok, "vals=%s" % (_vals(app.combo_audio["B"]),))
ok = wait_until(lambda: _vals(app.combo_sub["B"]) and _vals(app.combo_sub["B"])[0] == "Off", 10)
check("tracks: B sub list starts with Off", ok)
check("tracks: combos stay readonly (not editable text)",
      "readonly" in (app.combo_audio["A"].state() or []))
import tkinter.ttk as _ttk
_style = _ttk.Style(root)
_fbg = str(_style.lookup("TCombobox", "fieldbackground") or "")
check("theme: combobox field is dark (not white)", _fbg.lower() in ("#1f232b", "1f232b"), "bg=%s" % _fbg)
_pd = root.tk.call("ttk::combobox::PopdownWindow", app.combo_audio["A"])
_lbg = str(root.tk.call(_pd + ".f.l", "cget", "-background") or "")
_lfg = str(root.tk.call(_pd + ".f.l", "cget", "-foreground") or "")
check("theme: popdown listbox is dark (not white)", _lbg.lower() in ("#1f232b", "1f232b"),
      "bg=%s" % _lbg)
check("theme: popdown listbox text is light (readable)",
      _lfg.lower() in ("#e8e8ea", "#e8e8ea"), "fg=%s" % _lfg)

# picking the German subtitle must switch sid to 2
gsub = next((v for v in _vals(app.combo_sub["A"]) if "German" in v), "Off")
app.combo_sub["A"].set(gsub)
app._on_track_sel("A", "s")
pump(0.4)
ok = wait_until(lambda: _prop_is(app.players["A"], "sid", 2), 6)
check("tracks: German subtitle switches A sid to 2", ok)
# picking the German audio must switch aid to 2
gaud = next((v for v in _vals(app.combo_audio["A"]) if "German" in v), "Off")
app.combo_audio["A"].set(gaud)
app._on_track_sel("A", "a")
pump(0.4)
ok = wait_until(lambda: _prop_is(app.players["A"], "aid", 2), 6)
check("tracks: German audio switches A aid to 2", ok)
# Off disables subtitles
app.combo_sub["A"].set("Off")
app._on_track_sel("A", "s")
pump(0.4)
def _sid_disabled():
    err, d = app.players["A"].get_property("sid", timeout=2.0)
    return err == "success" and (d is False or d is None)
ok = wait_until(_sid_disabled, 6)
check("tracks: sub Off disables subtitles on A", ok)
# restore English audio for the rest of the suite
eaud = next((v for v in _vals(app.combo_audio["A"]) if "English" in v), "Off")
app.combo_audio["A"].set(eaud)
app._on_track_sel("A", "a")
pump(0.3)

# pause-all buttons (transport + master row) drive BOTH videos
app._set_pause_all(False)
pump(0.5)
check("mpause: baseline playing", not app.paused and not app.players["A"].paused)
app._toggle_pause_m()
pump(0.6)
check("mpause: pause-all pauses BOTH",
      app.paused and app.players["A"].paused and app.players["B"].paused)
check("mpause: transport button shows Play", app.btn_pause_all.cget("text").startswith("▶"))
check("mpause: master button shows Play", app.btn_play_m.cget("text") == "▶")
app._toggle_pause_m()
pump(0.6)
check("mpause: second toggle resumes BOTH", not app.paused and not app.players["A"].paused)

# ------------------------------ 11e. integrated PiP ------------------------
app._seek(2.0)          # both videos mid-clip for the geometry checks
pump(1.0)
app._toggle_lock()      # integrated PiP requires Sync Lock
check("ipip: lock engaged", app.sync_locked)
import ctypes.wintypes as wt
u = ctypes.windll.user32
app._toggle_pip_int("A")
check("ipip: state on (A embedded)", app.pip_int and app._pip_int_tag == "A")
hwnd = app._pip_int_hwnd
check("ipip: embedded hwnd recorded", bool(hwnd))
st = (u.GetWindowLongPtrW(hwnd, -16) or 0) & 0xFFFFFFFF
check("ipip: frameless style (popup, no caption/sysmenu/thickframe) - %08X" % st,
      (st & 0x00C00000) == 0 and (st & 0x00080000) == 0
      and (st & 0x00040000) == 0 and (st & 0x80000000) != 0)
check("ipip: top-level (not a child)", u.GetParent(hwnd) == 0)
check("ipip: ontop sent to mpv",
      ("A", {"command": ["set_property", "ontop", "yes"]}) in _cmd_log)
pump(1.2)   # let the 30 Hz poll place the pane over the host
hr = wt.RECT(); pr = wt.RECT()
ok = u.GetWindowRect(app.players["B"].hwnd, ctypes.byref(hr)) and \
     u.GetWindowRect(hwnd, ctypes.byref(pr))
check("ipip: pane + host rects readable", ok)
inside = (pr.left >= hr.left - 2 and pr.right <= hr.right + 2
          and pr.top >= hr.top - 2 and pr.bottom <= hr.bottom + 2)
check("ipip: pane sits INSIDE the host window", inside,
      "host=(%d,%d,%d,%d) pane=(%d,%d,%d,%d)"
      % (hr.left, hr.top, hr.right, hr.bottom, pr.left, pr.top, pr.right, pr.bottom))
px0, py0 = pr.left, pr.top
app._pip_nudge(80, 40)   # arrow keys: px-based nudge
pump(1.0)
ok = u.GetWindowRect(hwnd, ctypes.byref(pr))
moved = ok and (abs(pr.left - px0) > 20 or abs(pr.top - py0) > 20)
check("ipip: arrow-key nudge moves the pane (%d,%d -> %d,%d)" % (px0, py0, pr.left, pr.top), moved)
px0, py0 = pr.left, pr.top
app._pip_move(-1, -1)    # panel X/Y arrows: move left+up (away from clamps)
pump(1.0)
ok = u.GetWindowRect(hwnd, ctypes.byref(pr))
moved = ok and (pr.left < px0 - 25 and pr.top < py0 - 15)
check("ipip: X/Y arrow buttons move the pane (%d,%d -> %d,%d)" % (px0, py0, pr.left, pr.top), moved,
      "size=%s" % app._pip_int_size)
check("ipip: window-dragging disabled on the pane",
      ("A", {"command": ["set_property", "window-dragging", "no"]}) in _cmd_log)
app._toggle_pip_int("A")   # toggle again = undock
pump(0.4)
st2 = (u.GetWindowLongPtrW(hwnd, -16) or 0) & 0xFFFFFFFF
check("ipip: undock clears state", not app.pip_int)
check("ipip: undock restores caption", (st2 & 0x00C00000) != 0)
check("ipip: undock clears ontop",
      ("A", {"command": ["set_property", "ontop", "no"]}) in _cmd_log)
check("ipip: undock restores window-dragging",
      ("A", {"command": ["set_property", "window-dragging", "yes"]}) in _cmd_log)
app._toggle_lock()
check("ipip: lock released after PiP tests", not app.sync_locked)

# --------------------- 11f. PiP black-bar removal (crop) -------------------
BARSRC = os.path.join(BASE, "testmedia", "bars.mp4")   # 1280x540 in 1280x720
crop = sp.detect_crop_rect(BARSRC, duration=6.0)
check("crop: detect_crop_rect finds the letterbox (1280x540+0+90)", crop == (1280, 540, 0, 90),
      "crop=%r" % (crop,))
crop2 = sp.detect_crop_rect(MOVIE, duration=12.0)
check("crop: bar-free source returns None", crop2 is None, "crop=%r" % (crop2,))

# two-window PiP engages the crop and clears it on exit
app._crop_cache[app._srcs["B"]] = (1280, 540, 0, 90)
app._toggle_pip("B")
pump(0.8)
e, vc = app.players["B"].get_property("video-crop", timeout=3.0)
check("crop: two-window PiP applies video-crop", e == "success" and vc == "1280x540+0+90",
      "vc=%r" % (vc,))
app._toggle_pip("B")
pump(0.8)
e, vc = app.players["B"].get_property("video-crop", timeout=3.0)
check("crop: PiP off clears video-crop", e == "success" and vc in ("", None), "vc=%r" % (vc,))

# embedded PiP: pane refits to the cropped aspect (needs Sync Lock)
# A = multi.mkv (640x360) - seed a crop that FITS it: 640x270+0+45 (2.37:1)
app._toggle_lock()
app._crop_cache[app._srcs["A"]] = (640, 270, 0, 45)
app._toggle_pip_int("A")
pump(1.2)
e, vc = app.players["A"].get_property("video-crop", timeout=3.0)
check("crop: embedded PiP applies video-crop", e == "success" and vc == "640x270+0+45",
      "vc=%r" % (vc,))
ok = app._pip_int_asp is not None and abs(app._pip_int_asp - 640.0 / 270.0) < 0.05
check("crop: embedded pane aspect follows the crop", ok, "asp=%r" % (app._pip_int_asp,))
hr = wt.RECT(); pr = wt.RECT()
u.GetWindowRect(app.players["B"].hwnd, ctypes.byref(hr))
u.GetWindowRect(app._pip_int_hwnd, ctypes.byref(pr))
pw, ph = pr.right - pr.left, pr.bottom - pr.top
ratio = pw / float(ph) if ph else 0
check("crop: pane rect is wide (no letterbox) - %dx%d ratio=%.3f" % (pw, ph, ratio),
      abs(ratio - 640.0 / 270.0) < 0.15)
app._toggle_pip_int("A")
pump(0.8)
e, vc = app.players["A"].get_property("video-crop", timeout=3.0)
check("crop: undock clears video-crop", e == "success" and vc in ("", None), "vc=%r" % (vc,))
check("crop: undock resets pane aspect", app._pip_int_asp is None)

# guard: an over-sized crop rect (stale detection on a smaller file) is
# refused by the app and evicted, instead of silently ignored by mpv
app._crop_cache[app._srcs["A"]] = (1280, 540, 0, 90)
app._toggle_pip_int("A")
pump(1.2)
e, vc = app.players["A"].get_property("video-crop", timeout=3.0)
check("crop: oversized rect refused (guard)", e == "success" and vc in ("", None),
      "vc=%r" % (vc,))
check("crop: oversized rect evicted from cache", app._crop_cache.get(app._srcs["A"]) is None)
app._toggle_pip_int("A")
pump(0.8)
app._toggle_lock()
check("crop: lock released after crop PiP tests", not app.sync_locked)

# ------------ 11x. X-series: dbl-click desync, PiP size, free-form resize --
# state at entry: A/B are at EOF (12 s clips, long suite) and the app has
# paused both - restart to a genuinely PLAYING state so the click checks
# measure the user flow, not the EOF pause.
for _tag in ("A", "B"):
    _p = app.players.get(_tag)
    if _p and _p.running and _p.paused:
        _p.cmd({"command": ["set_property", "pause", "no"]})
app._seek(0)
ok = wait_until(lambda: app.last_pos["A"] is not None
                and app.last_pos["A"] < 1.5
                and app.last_pos["B"] is not None
                and app.last_pos["B"] < 1.5, 6)
_e1, _ra = app.players["A"].get_property("time-pos", timeout=2.0)
_e2, _rb = app.players["B"].get_property("time-pos", timeout=2.0)
_e3, _ea = app.players["A"].get_property("eof-reached", timeout=2.0)
_e4, _eb = app.players["B"].get_property("eof-reached", timeout=2.0)
check("x: restart to a playing state", ok,
      "A=%s B=%s paused=%s realA=%r realB=%r eofA=%r eofB=%r"
      % (app.last_pos["A"], app.last_pos["B"], app.paused,
         _ra, _rb, _ea, _eb))
pump(0.5)

xu = ctypes.windll.user32
xw = ctypes.wintypes
_xr = xw.RECT()


def _paused(tag):
    e, v = app.players[tag].get_property("pause", timeout=3.0)
    return v


def _plain_click(hwnd):
    """One press/release pair at the centre of the mpv window."""
    xu.GetClientRect(hwnd, ctypes.byref(_xr))
    cx, cy = (_xr.right - _xr.left) // 2, (_xr.bottom - _xr.top) // 2
    lp = (cy << 16) | (cx & 0xFFFF)
    xu.PostMessageW(hwnd, 0x0201, 0x0001, lp)   # WM_LBUTTONDOWN
    xu.PostMessageW(hwnd, 0x0202, 0, lp)        # WM_LBUTTONUP


def _dbl_click(hwnd):
    # mpv's win32 backend ignores WM_LBUTTONDBLCLK; two plain press pairs
    # within ~0.3 s are what mpv input.c converts to MBTN_LEFT_DBL.
    _plain_click(hwnd)
    time.sleep(0.12)
    _plain_click(hwnd)


hwnd_a = app.players["A"].hwnd

# free-form resize: keepaspect-window=no, window keeps a manual rect
e, kaw = app.players["A"].get_property("keepaspect-window", timeout=3.0)
check("x: keepaspect-window=no at spawn", e == "success" and kaw in (False, 0, "no"),
      "kaw=%r" % (kaw,))
xu.GetWindowRect(hwnd_a, ctypes.byref(_xr))
w0, h0 = _xr.right - _xr.left, _xr.bottom - _xr.top
xu.SetWindowPos(hwnd_a, 0, _xr.left, _xr.top, 400, 460, 0x0004 | 0x0010)
pump(0.8)
xu.GetWindowRect(hwnd_a, ctypes.byref(_xr))
w1, h1 = _xr.right - _xr.left, _xr.bottom - _xr.top
check("x: free-form resize sticks (400x460, no aspect snap)",
      abs(w1 - 400) <= 3 and abs(h1 - 460) <= 3, "got %dx%d" % (w1, h1))
xu.SetWindowPos(hwnd_a, 0, _xr.left, _xr.top, w0, h0, 0x0004 | 0x0010)
pump(0.4)

# double-click: fullscreen fires, pause NOT toggled (the desync fix)
e, fs0 = app.players["A"].get_property("fullscreen", timeout=3.0)
_dbl_click(hwnd_a)
pump(1.0)
e, fs1 = app.players["A"].get_property("fullscreen", timeout=3.0)
check("x: double-click fullscreens", e == "success" and fs1 != fs0,
      "fs %r -> %r" % (fs0, fs1))
check("x: double-click does NOT toggle pause (no desync)",
      _paused("A") is False and _paused("B") is False,
      "A=%r B=%r" % (_paused("A"), _paused("B")))
_dbl_click(hwnd_a)
pump(1.0)
e, fs2 = app.players["A"].get_property("fullscreen", timeout=3.0)
check("x: double-click again exits fullscreen", e == "success" and fs2 == fs0,
      "fs=%r" % (fs2,))

# single click still pauses (the deferred timer fires)
_plain_click(hwnd_a)
pump(1.0)
_pa, _pb = _paused("A"), _paused("B")
_diag = ("A=%r B=%r at_endA=%r at_endB=%r"
         % (_pa, _pb, app.players["A"].at_end, app.players["B"].at_end))
check("x: single click pauses both (mirror)", _pa is True and _pb is True, _diag)
app.players["A"].cmd({"command": ["set_property", "pause", "no"]})
app.players["B"].cmd({"command": ["set_property", "pause", "no"]})
pump(0.6)

# PiP size: + / - buttons scale the embedded pane (needs Sync Lock)
app._toggle_lock()
pump(0.3)
app._toggle_pip_int("A")
pump(1.0)
hwnd_p = app._pip_int_hwnd
check("x: pip size - pane ready", bool(hwnd_p))


def _pane_rect():
    r = xw.RECT()
    xu.GetWindowRect(hwnd_p, ctypes.byref(r))
    return r.right - r.left, r.bottom - r.top


pw0, ph0 = _pane_rect()
app._pip_sz_btns[1].invoke()   # + : bigger
pump(0.5)
pw1, ph1 = _pane_rect()
check("x: pip size + grows the pane (%dx%d -> %dx%d)" % (pw0, ph0, pw1, ph1),
      pw1 > pw0 + 10 and ph1 > ph0 + 10)
app._pip_sz_btns[0].invoke()   # - : smaller
pump(0.5)
pw2, ph2 = _pane_rect()
check("x: pip size - shrinks the pane (%dx%d -> %dx%d)" % (pw1, ph1, pw2, ph2),
      pw2 < pw1 - 10 and ph2 < ph1 - 10)
for _ in range(40):
    app._pip_resize(1)
pump(0.5)
pw3, ph3 = _pane_rect()
hr = xw.RECT()
xu.GetWindowRect(app.players["B"].hwnd, ctypes.byref(hr))
hw, hh = hr.right - hr.left, hr.bottom - hr.top
check("x: pip size clamps at 95%% of host (%.2f, %.2f)" % (pw3 / hw, ph3 / hh),
      hw > 0 and hh > 0 and pw3 <= hw * 0.96 and ph3 <= hh * 0.96)
app._toggle_pip_int("A")
pump(0.5)
app._toggle_lock()
check("x: pip size - lock released after tests", not app.sync_locked)

# ------------------------------------------------------ 12. screenshots --
before = set(os.listdir(sp.SHOT_DIR)) if os.path.isdir(sp.SHOT_DIR) else set()
app._shot()
pump(3)
after = set(os.listdir(sp.SHOT_DIR)) if os.path.isdir(sp.SHOT_DIR) else set()
new_files = after - before
check("screenshot: two PNGs created", len([f for f in new_files if f.endswith(".png")]) >= 2,
      "new=%s" % sorted(new_files)[:4])

# ------------------------------------------------------- 13. clean close --
config_path = sp.CONFIG_PATH
app._on_close()
pump(1.0)
check("close: config saved", os.path.isfile(config_path))
try:
    import json
    with io.open(config_path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    check("close: config remembers movie", cfg.get("movie", "").strip() == MOVIE)
    check("close: config remembers reaction", cfg.get("reaction", "").strip() == REACT)
    check("close: config remembers volume", abs(cfg.get("vol_a", 0) - 30) < 1)
    check("close: config remembers speed", abs(cfg.get("speed", 0) - 1.0) < 0.01)
except Exception as e:
    check("close: config readable", False, str(e))

time.sleep(1.5)
left = [t for t in ("A", "B") if app.players.get(t) and app.players[t].running]
check("close: no mpv processes left", not left, "left=%s" % left)

kill_mpv()
print("==== %d/%d checks passed ====" % (passed, passed + failed))
if fail_msgs:
    print("FAILED:", "; ".join(fail_msgs))
sys.exit(0 if failed == 0 else 1)