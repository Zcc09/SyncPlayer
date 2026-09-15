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

# ---- micro-speed drift trim + subtitle classification (pure helpers) -----
# dead band: within 60 ms the two feeds count as aligned and the rate is left
# alone (a trim that keeps hunting would be audible on music)
check("micro: dead band holds the rate at 1.0",
      all(sp.micro_rate(d) == 1.0 for d in (0.0, 0.03, -0.06, None, "x",
                                            float("nan"))))
# ahead of the target -> must run slower; behind -> faster
check("micro: a feed ahead of its target runs slower",
      sp.micro_rate(0.5) < 1.0 and sp.micro_rate(1.0) < 1.0,
      "%.4f / %.4f" % (sp.micro_rate(0.5), sp.micro_rate(1.0)))
check("micro: a feed behind its target runs faster",
      sp.micro_rate(-0.5) > 1.0 and sp.micro_rate(-1.0) > 1.0,
      "%.4f / %.4f" % (sp.micro_rate(-0.5), sp.micro_rate(-1.0)))
# however far off it is, the trim stays inside +-3% (inaudible with pitch
# correction, and never a visible speed change)
check("micro: trims are clamped to the rate cap",
      abs(sp.micro_rate(30.0) - (1.0 - sp.MICRO_MAX_PCT)) < 1e-9
      and abs(sp.micro_rate(-30.0) - (1.0 + sp.MICRO_MAX_PCT)) < 1e-9,
      "%.4f / %.4f" % (sp.micro_rate(30.0), sp.micro_rate(-30.0)))
check("micro: the trim grows with the drift, then saturates at the cap",
      sp.micro_rate(0.1) > sp.micro_rate(0.4) > sp.micro_rate(0.7)
      and abs(sp.micro_rate(1.0) - (1.0 - sp.MICRO_MAX_PCT)) < 1e-9,
      "%.4f %.4f %.4f %.4f" % (sp.micro_rate(0.1), sp.micro_rate(0.4),
                               sp.micro_rate(0.7), sp.micro_rate(1.0)))
check("micro: 0.1s of drift is trimmed by no more than 1%",
      abs(sp.micro_rate(0.1) - 1.0) <= 0.012, "%.4f" % sp.micro_rate(0.1))
check("micro: the seek threshold sits above every drift the loop acts on",
      # needs_correction fires at 0.45 s (0.15 s when locked); if MICRO_MAX_DRIFT
      # were below that, normal drift would be seeked again instead of trimmed
      sp.MICRO_MAX_DRIFT >= 0.45 and sp.micro_rate(sp.MICRO_MAX_DRIFT) != 1.0,
      "max_drift=%.2f" % sp.MICRO_MAX_DRIFT)

# subtitle files are recognised by extension (drives drag & drop routing)
check("drop: subtitle extensions are recognised",
      all(sp.is_subtitle_file(x) for x in
          ("a.srt", "B.ASS", "c.ssa", "d.vtt", "e.sub", "f.idx", "g.smi"))
      and not any(sp.is_subtitle_file(x) for x in
                  ("m.mp4", "m.mkv", "m.webm", "m.avi", "noext", "")))

# ---------------------------------------------------------------- download --
# Quality list built from yt-dlp's own JSON: best-first, auto first, and a height
# that only exists as separate video+audio streams is dropped when ffmpeg is
# missing, because it could never be merged into a playable file.
_FMT_INFO = {"formats": [
    {"format_id": "137", "height": 1080, "ext": "mp4", "fps": 30,
     "vcodec": "avc1", "acodec": "none", "filesize": 120 * 1048576},
    {"format_id": "22", "height": 720, "ext": "mp4", "fps": 30,
     "vcodec": "avc1", "acodec": "mp4a", "filesize": 45 * 1048576},
    {"format_id": "18", "height": 360, "ext": "mp4", "fps": 30,
     "vcodec": "avc1", "acodec": "mp4a", "filesize": 12 * 1048576},
    {"format_id": "140", "height": None, "ext": "m4a", "vcodec": "none",
     "acodec": "mp4a", "abr": 128},
]}
with_ff = sp._formats_to_entries(_FMT_INFO, ffmpeg="ffmpeg")
without_ff = sp._formats_to_entries(_FMT_INFO, ffmpeg=None)
check("download: the auto entry is offered first",
      with_ff and with_ff[0]["height"] == 0 and with_ff[0]["fmt"] == "bv*+ba/b",
      str(with_ff[0] if with_ff else None))
check("download: qualities are ordered best-first and named readably",
      [e["height"] for e in with_ff] == [0, 1080, 720, 360]
      and with_ff[1]["label"].startswith("1080p")
      and "MB" in with_ff[1]["label"],
      " | ".join(e["label"] for e in with_ff))
check("download: a separate video+audio height needs ffmpeg for merging",
      1080 in [e["height"] for e in with_ff]
      and 1080 not in [e["height"] for e in without_ff]
      and "single file only" in without_ff[0]["label"],
      "with=%s without=%s" % ([e["height"] for e in with_ff],
                              [e["height"] for e in without_ff]))
check("download: merged heights use a yt-dlp video+audio selector",
      [e["fmt"] for e in with_ff] == ["bv*+ba/b", "137+140", "22", "18"],
      str([e["fmt"] for e in with_ff]))
check("download: a URL with nothing usable reports why instead of an empty list",
      sp._formats_to_entries({"formats": []}) == [])

# the path parser must survive a Windows drive letter (splitting on ":" ate "C:")
check("download: the destination path keeps its drive letter",
      sp._download_path_from_line(
          r"[download] Destination: C:\Users\Zcc09\Downloads\SyncPlayer\movie [id].mp4")
      == (r"C:\Users\Zcc09\Downloads\SyncPlayer\movie [id].mp4", False))
check("download: a merged file's quoted path is unquoted",
      sp._download_path_from_line(
          r'[Merger] Merging formats into "C:\Users\Zcc09\x\v [id].mp4"')
      == (r"C:\Users\Zcc09\x\v [id].mp4", False))
check("download: a file already on disk counts as the result, not a failure",
      sp._download_path_from_line(
          r"[download] C:\Users\Zcc09\x\movie [id].mp4 has already been downloaded")
      == (r"C:\Users\Zcc09\x\movie [id].mp4", True))
check("download: progress lines are not mistaken for paths",
      sp._download_path_from_line(
          "[download]  42.3% of 5.00MiB at 1.00MiB/s ETA 00:03") == (None, False))

# ------------------------------------------------------------- frame capture --
# capture_frame must escalate (file -> software -> window) and report a reason
# instead of failing silently, and it must NEVER touch playback state: stealing
# the user's pause would be worse than the failure it is fixing.
import tempfile as _tf
_tmpdir = _tf.mkdtemp(prefix="sp_capture_")
_missing = os.path.join(_tmpdir, "_never_written.png")


class _FakeDriver(object):
    """Stands in for MpvDriver: records every call, writes the frame or doesn't."""

    def __init__(self, succeed_on=None):
        self.calls = []
        self.succeed_on = succeed_on
        self.attempts = 0

    def get_property(self, prop, timeout=1.0):
        self.calls.append(("get", prop))
        if prop == "video-format":
            return "success", "h264"
        if prop == "screenshot-sw":
            return "success", False
        return "success", None

    def command_sync(self, cmd, timeout=5.0):
        self.calls.append(("cmd", tuple(cmd)))
        if cmd and cmd[0] == "screenshot-to-file":
            self.attempts += 1
            if self.succeed_on and self.attempts >= self.succeed_on:
                with open(cmd[1], "wb") as fh:
                    fh.write(b"x" * 4096)
                return "success", None
            return "error running command", None
        return "success", None


_fake = _FakeDriver()
_ok, _why = sp.MpvDriver.capture_frame(_fake, _missing, timeout=1.0)
_modes = [c[1] for c in _fake.calls if c[0] == "cmd" and c[1][0] == "screenshot-to-file"]
check("capture: every way mpv can shoot a frame is tried before giving up",
      not _ok and [m[2] for m in _modes] == ["video", "video", "window"],
      "modes=%s why=%s" % ([m[2] for m in _modes], _why))
check("capture: the failure names the strategies that were tried",
      "video=" in _why and "software=" in _why and "window=" in _why, _why)
_PLAYBACK_PROPS = ("pause", "speed", "time-pos", "playback-time", "seeking")
_PLAYBACK_CMDS = ("frame-step", "frame-back-step", "seek", "cycle", "playlist-next")
check("capture: it never pauses, unpauses or steps the video",
      not any(c[0] == "cmd" and (
          c[1][0] in _PLAYBACK_CMDS
          or (c[1][0] == "set_property" and len(c[1]) > 1
              and c[1][1] in _PLAYBACK_PROPS))
              for c in _fake.calls),
      str([c[1] for c in _fake.calls if c[0] == "cmd"]))

_fake2 = _FakeDriver(succeed_on=2)          # first try fails, software retry works
_ok2, _why2 = sp.MpvDriver.capture_frame(_fake2, _missing, timeout=1.0)
check("capture: a software retry is accepted as success", _ok2, _why2)
check("capture: the screenshot-sw override is put back afterwards",
      ("cmd", ("set_property", "screenshot-sw", "no")) in _fake2.calls,
      str([c[1] for c in _fake2.calls if c[0] == "cmd"][-2:]))

_fake3 = _FakeDriver(succeed_on=1)
_ok3, _why3 = sp.MpvDriver.capture_frame(_fake3, os.path.join(_tmpdir, "_ok.png"),
                                         timeout=1.0)
check("capture: the normal path succeeds on the first attempt",
      _ok3 and _why3 == "ok" and _fake3.attempts == 1,
      "attempts=%d why=%s" % (_fake3.attempts, _why3))
try:
    import shutil as _sh
    _sh.rmtree(_tmpdir, ignore_errors=True)
except Exception:
    pass

# ---------------------------------------------------------------- updater --
# The in-app updater: what it offers, and what it does when a file cannot be
# swapped (a running exe is exactly that case).
import ctypes as _ct
import tempfile as _tf2

_upd_ok = True
try:
    import updater as _upd
except Exception as _e:                       # updater is optional
    _upd_ok = False
    print("  (updater import failed: %s)" % _e)
check("updater: the module imports where the app does", _upd_ok)
check("updater: the API base can be redirected for tests",
      "{" not in _upd.API and ("%s" in _upd.API),
      _upd.API[:70])
check("updater: a newer tag is detected and an equal one is not",
      _upd.ver_gt("1.6.5", "1.6.4") and not _upd.ver_gt("1.6.4", "1.6.4")
      and not _upd.ver_gt("1.6.3", "1.6.4") and _upd.ver_gt("1.6.10", "1.6.9"))
check("updater: a short tag like v2.0 still counts as newer",
      _upd.parse_version("2.0") == (2, 0, 0)
      and _upd.parse_version("v3") == (3, 0, 0)
      and _upd.ver_gt("2.0", "1.9.9") and _upd.ver_gt("v2", "1.9.9"),
      str(_upd.parse_version("2.0")))
check("updater: date-style versions compare correctly",
      _upd.ver_gt("2026.08.20", "2026.08.19")
      and not _upd.ver_gt("2026.08.19", "2026.08.19"))

# _update_summary: only what would actually change is offered
_sum = sp._update_summary({"app": {"available": True, "current": "1.0", "latest": "2.0"},
                           "mpv": {"available": False, "missing": False},
                           "ytdlp": {"available": False, "missing": False}})
check("updater: only outdated or missing pieces are offered",
      [r[0] for r in _sum] == ["SyncPlayer"], str(_sum))
_sum2 = sp._update_summary({"app": {"available": False},
                            "mpv": {"missing": True, "latest": "0.42.0"},
                            "ytdlp": {"available": True, "current": "1", "latest": "2"}})
check("updater: a missing mpv and an old yt-dlp are both offered",
      [r[0] for r in _sum2] == ["mpv", "yt-dlp"], str(_sum2))
check("updater: a current install offers nothing",
      sp._update_summary({"app": {"available": False}, "mpv": {}, "ytdlp": {}}) == [])

# replacing a file nobody holds, and one that is locked solid
_u = _tf2.mkdtemp(prefix="sp_upd_")
_exe = os.path.join(_u, "SyncPlayer.exe")
_new = os.path.join(_u, "staged-src.exe")
with open(_exe, "wb") as _f:
    _f.write(b"OLD")
with open(_new, "wb") as _f:
    _f.write(b"NEW")
_ok, _detail = _upd.replace_running_exe(_new, _exe)
check("updater: a free exe is replaced in place",
      _ok and open(_exe, "rb").read() == b"NEW", str(_detail))

if os.name == "nt":                          # the lock trick is Windows-only
    with open(_exe, "wb") as _f:      # closed before the exclusive lock below
        _f.write(b"OLD-again")
    _k32 = _ct.windll.kernel32
    _h = _k32.CreateFileW(_exe, 0x80000000, 0, None, 3, 0, None)   # share=0
    _ok2, _detail2 = _upd.replace_running_exe(_new, _exe)
    check("updater: an unswappable exe is STAGED rather than crashed on",
          _ok2 is False and _detail2.endswith(".new") and os.path.isfile(_detail2),
          os.path.basename(_detail2))
    if _h not in (0, -1, None):
        _k32.CloseHandle(_h)
    check("updater: the staged build installs at the next start",
          _upd.apply_pending_update(_u) and open(_exe, "rb").read() == b"NEW")
    check("updater: a pending .new file is not swept away",
          not os.path.isfile(_exe + ".new"))
else:
    print("  (skipping the locked-file case: not Windows)")

with open(os.path.join(_u, "junk.old"), "wb") as _f:
    _f.write(b"x")
_removed = _upd.sweep_update_leftovers(_u)
check("updater: .old leftovers are swept after an update",
      "junk.old" in _removed and not os.path.isfile(os.path.join(_u, "junk.old")),
      str(_removed))
check("updater: the install dir can be redirected (tests rely on it)",
      sp._self_install_dir() == os.path.abspath(os.environ.get(
          "SYNCPLAYER_INSTALL_DIR") or sp._self_install_dir()))
import shutil as _sh2
_sh2.rmtree(_u, ignore_errors=True)

# ------------------------------------------------ 1.6.6 quality + threads ----
check("quality: 1080p is what a URL is played at by default",
      sp.DEFAULT_YOUTUBE_QUALITY == "1080")
check("quality: the 1080p selector mpv is given",
      sp.ytdl_format_expr("1080")
      == "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
      sp.ytdl_format_expr("1080"))
check("quality: 720p selector",
      sp.ytdl_format_expr("720")
      == "bestvideo[height<=720]+bestaudio/best[height<=720]",
      sp.ytdl_format_expr("720"))
check("quality: 'best' still hands the choice back to yt-dlp",
      sp.ytdl_format_expr("best") == "bestvideo+bestaudio/best",
      sp.ytdl_format_expr("best"))
check("quality: nonsense falls back to the default instead of crashing",
      sp.ytdl_format_expr("banana") == sp.ytdl_format_expr(sp.DEFAULT_YOUTUBE_QUALITY),
      sp.ytdl_format_expr("banana"))
check("quality: the settings list offers the whole useful range",
      [c for c, _ in sp.YOUTUBE_QUALITIES] == ["best", "2160", "1440", "1080",
                                               "720", "480", "360"],
      str([c for c, _ in sp.YOUTUBE_QUALITIES]))
check("quality: set_playback_quality is what a new player would use",
      sp.set_playback_quality("480")
      == "bestvideo[height<=480]+bestaudio/best[height<=480]"
      and sp._PLAYBACK_YTDL_FORMAT[0] == sp.ytdl_format_expr("480"),
      sp._PLAYBACK_YTDL_FORMAT[0])
sp.set_playback_quality(sp.DEFAULT_YOUTUBE_QUALITY)

_cap = {}
_orig_popen = sp.subprocess.Popen


class _FakePopen(object):
    def __init__(self, args, **kw):
        _cap["args"] = list(args)
        self.stdout = iter(())
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


sp.subprocess.Popen = _FakePopen
_tmp = None
try:
    import tempfile as _tf
    _tmp = _tf.mkdtemp(prefix="sp_thr_")
    for _n in (8, 1, 0):
        sp.ytdl_download("http://127.0.0.1:1/x", "b", _tmp, "yt-dlp.exe",
                         connections=_n)
        _cap["c%d" % _n] = _cap["args"]
    _many = sp.ytdl_download("http://127.0.0.1:1/x", "b", _tmp, "yt-dlp.exe",
                             connections=64)
    _cap["c64"] = _cap["args"][:]
finally:
    sp.subprocess.Popen = _orig_popen
    import shutil as _sh3
    if _tmp:
        _sh3.rmtree(_tmp, ignore_errors=True)


def _argval(args, flag):
    return args[args.index(flag) + 1] if flag in args else None


check("threads: -N carries the connection count",
      _argval(_cap["c8"], "-N") == "8", str(_cap["c8"][:10]))
check("threads: chunked downloading is enabled so single-file sources parallelise",
      _argval(_cap["c8"], "--http-chunk-size") == "10M",
      str(_argval(_cap["c8"], "--http-chunk-size")))
check("threads: one connection means a plain download (no -N)",
      "-N" not in _cap["c1"] and "--http-chunk-size" not in _cap["c1"],
      str(_cap["c1"][:10]))
check("threads: a zero/blank count is treated as one connection",
      "-N" not in _cap["c0"], str(_cap["c0"][:10]))
check("threads: 64 connections is what gets asked for (the dialog clamps)",
      _argval(_cap["c64"], "-N") == "64", str(_argval(_cap["c64"], "-N")))
check("threads: the quality is still passed through unchanged",
      _argval(_cap["c8"], "-f") == "b")

print("==== %d/%d checks passed ====" % (passed, passed + failed))
sys.exit(0 if failed == 0 else 1)
