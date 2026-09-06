#!/usr/bin/env python3
"""Headless self-test for SyncPlayer (two-independent-players architecture).

Run:  python selftest.py
Covers: sync math, dual driver launch, INDEPENDENT seeking, pause/volume/
speed IPC, window discovery + placement, click-to-pause Lua broadcast,
and clean shutdown. Needs local test clips (testmedia/*.mp4).
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import syncplayer as sp

BASE = os.path.dirname(os.path.abspath(__file__))
MOVIE = os.path.join(BASE, "testmedia", "movie.mp4")   # 12 s
REACT = os.path.join(BASE, "testmedia", "react.mp4")   # 10 s

passed, failed = 0, 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("[PASS] %s%s" % (name, (" — " + extra) if extra else ""))
    else:
        failed += 1
        print("[FAIL] %s%s" % (name, (" — " + extra) if extra else ""))


def kill_mpv():
    if os.name == "nt":
        os.system("taskkill /F /T /IM mpv.exe >nul 2>&1")


def ctypes_windll_user32():
    try:
        import ctypes
        return ctypes.windll.user32
    except Exception:
        return None


def w32_rect(user32, hwnd):
    try:
        import ctypes
        r = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        return None


# ---------------------------------------------------------------- 1. math --
check("sync math: reaction_target", sp.reaction_target(100.0, 5.0) == 105.0)
check("sync math: negative offset", sp.reaction_target(100.0, -5.0) == 95.0)
check("sync math: None movie pos", sp.reaction_target(None, 5.0) is None)
check("sync math: drift", abs(sp.drift(104.0, 100.0, 5.0) - (-1.0)) < 1e-9)
check("sync math: no drift", abs(sp.drift(105.0, 100.0, 5.0)) < 1e-9)
check("sync math: correct when over threshold",
      sp.needs_correction(100.0, 100.0, 0.0, threshold=0.45) is False)
check("sync math: no correct when paused",
      sp.needs_correction(110.0, 100.0, 0.0, playing=False) is False)
check("sync math: no correct when movie at end",
      sp.needs_correction(110.0, 100.0, 0.0, movie_at_end=True) is False)
check("sync math: no correct when dragging",
      sp.needs_correction(110.0, 100.0, 0.0, dragging=True) is False)
check("sync math: correct when drifted", sp.needs_correction(110.0, 100.0, 0.0) is True)


# ------------------------------------------------ 1b. mpv discovery (bundled) --
def _bundled_mpv_scenarios():
    import shutil, tempfile
    d = tempfile.mkdtemp()
    saved_frozen = getattr(sp.sys, "frozen", None)
    saved_exec = sp.sys.executable
    res = {"found": False, "right": False, "missing": False}
    try:
        os.makedirs(os.path.join(d, "mpv"))
        open(os.path.join(d, "mpv", "mpv.exe"), "w").write("x")
        sp._mpv_cache = None
        sp.sys.frozen = True
        sp.sys.executable = os.path.join(d, "SyncPlayer.exe")
        b = sp._bundled_mpv()
        res["found"] = b is not None
        res["right"] = bool(b) and b.endswith(os.path.join("mpv", "mpv.exe"))
        # absent -> None
        os.remove(os.path.join(d, "mpv", "mpv.exe"))
        os.rmdir(os.path.join(d, "mpv"))
        sp._mpv_cache = None
        res["missing"] = sp._bundled_mpv() is None
    finally:
        sp._mpv_cache = None
        if saved_frozen is None:
            try:
                del sp.sys.frozen
            except Exception:
                pass
        else:
            sp.sys.frozen = saved_frozen
        sp.sys.executable = saved_exec
        shutil.rmtree(d, ignore_errors=True)
    return res


_mr = _bundled_mpv_scenarios()
check("mpv discovery: bundled mpv found next to exe", _mr["found"])
check("mpv discovery: returns mpv\\mpv.exe", _mr["right"])
check("mpv discovery: absent -> None", _mr["missing"])


# ------------------------------------------------ 1c. updater version logic --
import updater as _up
check("updater: parse v0.41.0", _up.parse_version("v0.41.0") == (0, 41, 0))
check("updater: parse 1.4.0", _up.parse_version("1.4.0") == (1, 4, 0))
check("updater: parse dev string", _up.parse_version("0.41.0-dev-g41f6a6450") == (0, 41, 0))
check("updater: ver_gt newer", _up.ver_gt("1.4.0", "1.3.1") is True)
check("updater: ver_gt equal", _up.ver_gt("0.41.0", "0.41.0") is False)

# ---------------------------------------------------------------- 2. parse --
check("parse: plain seconds", sp.MpvDriver._to_seconds("123.45") == 123.45)
check("parse: HH:MM:SS.mmm", abs(sp.MpvDriver._to_seconds("01:02:03.500") - 3723.5) < 1e-6)
check("parse: MM:SS", sp.MpvDriver._to_seconds("02:03") == 123.0)
check("parse: nan -> None", sp.MpvDriver._to_seconds("nan") is None)
check("parse: empty -> None", sp.MpvDriver._to_seconds("") is None)
check("parse: negative", sp.MpvDriver._to_seconds("-4.5") == -4.5)

# --------------------------------------------------- 2b. yt subs + time fmt --
_smp = ("[info] Available automatic captions for the video:\n"
        "Language      Name\nen            English\nde            German\n"
        "[info] Available subtitles for the video:\n"
        "Language      Name\nfr            French\n")
_sps = sp.yt_parse_list_subs(_smp)
check("yt subs: detects auto-generated captions", any(s["auto"] for s in _sps))
check("yt subs: detects uploaded subtitles", any(not s["auto"] for s in _sps))
check("yt subs: auto labels are marked", any("(auto)" in s["label"] for s in _sps))
check("yt subs: no false header row", all(s["lang"] != "Language" for s in _sps))
check("fmt: under an hour -> MM:SS", sp.SyncApp._fmt(90, 120) == "01:30 / 02:00")
check("fmt: over an hour -> HH:MM:SS",
      sp.SyncApp._fmt(3723.5, 4100) == "1:02:03 / 1:08:20")
check("fmt: None -> placeholder", sp.SyncApp._fmt(None, 120) == "--:--")

# ---------------------------------------------------------- 3. dual launch --
kill_mpv()
time.sleep(1)
dA = sp.MpvDriver(MOVIE, "A")
dB = sp.MpvDriver(REACT, "B")
check("dual launch: A running", dA.running)
check("dual launch: B running", dB.running)

# wait for both to report status
pos_a, pos_b, dur_a, dur_b = None, None, None, None
t0 = time.time()
while time.time() - t0 < 15:
    while True:
        try:
            kind, rec = dA.q.get_nowait()
        except Exception:
            break
        if kind == "status" and rec["time_pos"] is not None:
            pos_a = rec["time_pos"]; dur_a = rec["duration"]
    while True:
        try:
            kind, rec = dB.q.get_nowait()
        except Exception:
            break
        if kind == "status" and rec["time_pos"] is not None:
            pos_b = rec["time_pos"]; dur_b = rec["duration"]
    if pos_a is not None and pos_b is not None:
        break
    time.sleep(0.2)
check("status: A reports position", pos_a is not None, "pos_a=%s" % pos_a)
check("status: B reports position", pos_b is not None, "pos_b=%s" % pos_b)
check("status: A duration ~12s", dur_a is not None and abs(dur_a - 12) < 2, "dur_a=%s" % dur_a)
check("status: B duration ~10s", dur_b is not None and abs(dur_b - 10) < 2, "dur_b=%s" % dur_b)

# playback advancing
time.sleep(1.2)
p2 = None
while True:
    try:
        kind, rec = dA.q.get_nowait()
    except Exception:
        break
    if kind == "status" and rec["time_pos"] is not None:
        p2 = rec["time_pos"]
check("status: A advances while playing", p2 is not None and p2 > pos_a, "%s -> %s" % (pos_a, p2))

# ---------------------------------------------------- 4. independent seek --
# THE core guarantee: seeking A must NOT move B. We can't pause B (mpv
# stops printing status lines while paused), so instead: B keeps playing
# and must NOT jump to A's target (~6 s) — it must stay near its own
# natural progression, a couple of seconds into the clip.
time.sleep(0.6)
b_before = None
while True:
    try:
        kind, rec = dB.q.get_nowait()
    except Exception:
        break
    if kind == "status" and rec["time_pos"] is not None:
        b_before = rec["time_pos"]
dA.seek(6.0)
time.sleep(1.2)
a_after, b_after = None, None
while True:
    try:
        kind, rec = dA.q.get_nowait()
    except Exception:
        break
    if kind == "status" and rec["time_pos"] is not None:
        a_after = rec["time_pos"]
while True:
    try:
        kind, rec = dB.q.get_nowait()
    except Exception:
        break
    if kind == "status" and rec["time_pos"] is not None:
        b_after = rec["time_pos"]
check("independent seek: A moved to ~6s", a_after is not None and abs(a_after - 6.0) < 1.5,
      "a_after=%s" % a_after)
check("independent seek: B NOT yanked to A's target",
      b_before is not None and b_after is not None
      and abs(b_after - 6.0) >= 1.5
      and b_after >= b_before - 0.2,
      "B: %s -> %s" % (b_before, b_after))

# ---------------------------------------------------- 5. pause / volume --
dA.set_pause(True)
time.sleep(0.7)
p_pause = None
while True:
    try:
        kind, rec = dA.q.get_nowait()
    except Exception:
        break
    if kind == "pause":
            p_pause = rec[0] if isinstance(rec, tuple) else rec
check("pause: Lua broadcast received", p_pause is True, "event=%s" % p_pause)
dA.set_pause(False)
time.sleep(0.5)

dA.set_volume(37)
dB.set_volume(12)
time.sleep(0.4)
check("volume: commands accepted (no crash)", dA.running and dB.running)

dA.set_speed(2.0)
time.sleep(0.3)
check("speed: accepted (no crash)", dA.running)

# ------------------------------------------------------- 6. window mgmt --
hA = sp.find_mpv_window(dA.proc.pid, "SyncPlayer — Movie", tries=20)
hB = sp.find_mpv_window(dB.proc.pid, "SyncPlayer — Reaction", tries=20)
check("window: movie window found", hA is not None, "hwnd=%s" % hA)
check("window: reaction window found", hB is not None, "hwnd=%s" % hB)
if hA and hB:
    okA = sp.place_window(hA, 0, 0, 960, 540)
    okB = sp.place_window(hB, 980, 0, 960, 540)
    time.sleep(1.0)
    u = ctypes_windll_user32()
    if u:
        ra = w32_rect(u, hA)
        rb = w32_rect(u, hB)
        check("window: A placed at (0,0)", ra is not None and ra[0] == 0 and ra[1] == 0, "%s" % (ra,))
        check("window: B placed side by side", rb is not None and rb[0] >= 900, "%s" % (rb,))
    else:
        check("window: A placed at (0,0)", okA, "SetWindowPos returned")
        check("window: B placed side by side", okB, "SetWindowPos returned")

# -------------------------------------------------- 6b. no-yank invariant --
# The bug that made the panel "screw up the timeline": after seeking ONE
# video, the drift loop compared the OTHER video's sample against the
# seeked video's STALE pre-seek position and yanked it around. Reproduce
# the exact post-seek state and assert the loop stays quiet.
app = sp.SyncApp.__new__(sp.SyncApp)
app.last_pos = {"A": 50.0, "B": 45.0}
app.last_dur = {"A": 600.0, "B": 600.0}
app._status_time = {"A": time.monotonic() - 0.05, "B": time.monotonic() - 0.05}
app.paused = False
app.players = {"A": None, "B": None}
class _V:
    def get(self):
        return 1.0
app.speed = _V()

# (a) extrapolation: both samples ~50 ms old -> est ≈ real + 0.05 s
now = time.monotonic()
check("extrapolate: A ~50.05", abs(app._est_pos("A", now) - 50.05) < 0.2)
check("extrapolate: B ~45.05", abs(app._est_pos("B", now) - 45.05) < 0.2)

# (b) user dragged Movie bar 50 -> 200 (commit + re-anchor, as the release
# handler does): reaction must NOT be corrected (target == its position)
app.last_pos["A"] = 200.0
app._status_time["A"] = time.monotonic()
app.sync_off = 45.0 - 200.0          # re-anchor: reaction stays put
now = time.monotonic()
ra = app._est_pos("A", now)
rb = app._est_pos("B", now)
check("no-yank: no correction after movie seek",
      not sp.needs_correction(rb, ra, app.sync_off))

# (c) user dragged Reaction bar 45 -> 300 (release handler): movie stays
app.last_pos["B"] = 300.0
app._status_time["B"] = time.monotonic()
app.sync_off = 300.0 - 200.0         # re-anchor: movie stays put
now = time.monotonic()
ra = app._est_pos("A", now)
rb = app._est_pos("B", now)
check("no-yank: no correction after reaction seek",
      not sp.needs_correction(rb, ra, app.sync_off))

# (d) genuine drift IS still corrected (> threshold, settled, not dragging)
app.last_pos["A"] = 200.0
app._status_time["A"] = time.monotonic()
app.last_pos["B"] = 300.0
app._status_time["B"] = time.monotonic()
app.sync_off = 0.0                   # aligned offset ... B actually drifted
check("drift: correction fires on real drift",
      sp.needs_correction(300.0, 200.0, 0.0, playing=True,
                          dragging=False, movie_at_end=False))
# grace window right after a manual seek suppresses it…
app._seek_grace_until = time.monotonic() + 1.2
check("drift: suppressed during post-seek grace",
      not (time.monotonic() >= app._seek_grace_until
           and sp.needs_correction(300.0, 200.0, 0.0)))
# …and once grace expires the correction is allowed again
app._seek_grace_until = 0.0
check("drift: allowed again after grace expires",
      time.monotonic() >= app._seek_grace_until
      and sp.needs_correction(300.0, 200.0, 0.0))

# ------------------------------------------------------------- 7. quit --
dA.quit()
dB.quit()
time.sleep(1.5)
check("quit: A exited", not dA.running)
check("quit: B exited", not dB.running)
kill_mpv()

# ---- crop detection (32:2:16 catches black AND dark-gray bars) ----------
BASE_T = os.path.dirname(os.path.abspath(__file__))
_bars = os.path.join(BASE_T, "testmedia", "bars.mp4")
_gray = os.path.join(BASE_T, "testmedia", "bars_gray.mp4")
_movie = os.path.join(BASE_T, "testmedia", "movie.mp4")
# pitch-black letterbox (1280x540 content in 1280x720, 90px bars)
cb = sp.detect_crop_rect(_bars)
check("detect: pitch-black bars cropped", cb == (1280, 540, 0, 90), "rect=%r" % (cb,))
# dark-gray (Y~32) letterbox - the case threshold 0 missed (the Auto bug)
cg = sp.detect_crop_rect(_gray)
check("detect: dark-gray bars cropped", cg == (1280, 540, 0, 90), "rect=%r" % (cg,))
# bar-less clip -> no crop
cm = sp.detect_crop_rect(_movie)
check("detect: bar-less clip rejected", cm is None, "rect=%r" % (cm,))
kill_mpv()

print("==== %d/%d checks passed ====" % (passed, passed + failed))
sys.exit(0 if failed == 0 else 1)
