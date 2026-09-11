#!/usr/bin/env python3
"""
SyncPlayer — dual-video sync player built on mpv (TWO independent players).

Each video plays in its OWN mpv process and window with its OWN timeline,
so seeking one video NEVER touches the other — no filter graphs, no
rebuilds, nothing to glitch or crash. The panel acts as a master clock:

  * Master bar    -> moves BOTH videos together
  * Movie bar     -> moves ONLY the movie (this is how you align)
  * Reaction bar  -> moves ONLY the reaction (this is how you align)
  * auto re-sync  -> while playing, the reaction is gently pulled back
    to its aligned spot whenever it drifts (about a 0.45 s threshold)

Features
  - Two independent video windows, auto-arranged side by side
  - Per-video volume + master volume + mutes, speed 0.25x - 2.5x
  - Master seek, +-n-second jumps, restart, screenshots of both videos
  - Local files or URLs (YouTube etc., resolved via yt-dlp)
  - Drag & drop files onto the panel; config auto-saved

Usage:
  python syncplayer.py                         # open the panel
  python syncplayer.py movie.mkv react.mp4     # prefill and start
  python syncplayer.py movie.mkv "https://youtu.be/..."

Requires: mpv on PATH (or MPV_PATH env / common install dirs).
"""

import ctypes
import ctypes.wintypes  # noqa: F401 (ctypes.wintypes.DWORD etc. used in window helpers)
import glob
import io
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _HAS_DND = True
except Exception:
    _HAS_DND = False
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False
from tkinter import ttk, filedialog, messagebox

APP_NAME = "SyncPlayer"
APP_VERSION = "1.4.0"


class MpvNotFoundError(Exception):
    """Raised when mpv cannot be located (not bundled, not on PATH)."""


if getattr(sys, "frozen", False):
    # packaged exe: keep data out of the exe's folder (e.g. Desktop)
    BASE = os.path.dirname(sys.executable)
    _appdata = os.environ.get("APPDATA") or BASE
    CONFIG_PATH = os.path.join(_appdata, "SyncPlayer", "syncplayer_config.json")
    SHOT_DIR = os.path.join(os.path.expanduser("~"), "Pictures", "SyncPlayer")
else:
    BASE = os.path.dirname(os.path.abspath(__file__))
    CONFIG_PATH = os.path.join(BASE, "syncplayer_config.json")
    SHOT_DIR = os.path.join(BASE, "screenshots")
os.makedirs(SHOT_DIR, exist_ok=True)

STATUS_PREFIX = "SYNCSTATUS|"
STATUS_APPEND = "|${sub-id}|${aid}|${demuxer-cache-duration}"   # includes stream buffer duration
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")

LUA_SCRIPT = """
-- frame-accurate position beacon: mpv 0.41's ${time-pos} in the status
-- line is OSD-cached (~1 Hz, seconds stale) while percent-pos is integer;
-- neither drives the seek bars. observe_property fires on every real
-- position change, so we throttle to ~10 Hz and print fresh positions.
local last_pos = -9
mp.observe_property("time-pos", "number", function(name, pos)
    if pos == nil then return end
    if math.abs(pos - last_pos) >= 0.099 then
        last_pos = pos
        print(string.format("SYNCPOS|%.3f", pos))
    end
end)
mp.observe_property("eof-reached", "bool", function(name, v)
    print("SYNCEOF|" .. tostring(v))
end)
mp.observe_property("pause", "bool", function(name, value)
    if mp.get_property_bool("eof-reached") then
        print("SYNCPAUSE|eof")
    else
        print("SYNCPAUSE|" .. tostring(value))
    end
end)
-- integrated PiP: report left-button drag state so the app can follow the
-- pane while the user drags it (mpv window-dragging moves the window).
local dragging = false
mp.observe_property("mouse-pos", "native", function()
    if mp.get_property_bool("mouse-btn1-down", false) then
        if not dragging then
            dragging = true
            print("SYNCPIPDRAG|start")
        end
    else
        if dragging then
            dragging = false
            print("SYNCPIPDRAG|end")
        end
    end
end)
mp.register_script_message("pip-undock", function()
    print("SYNCPIPDRAG|undock")
end)
-- click-to-pause with double-click discrimination: a left click arms a
-- deferred pause (450 ms); a double-click (MBTN_LEFT_DBL, handled below)
-- cancels it, so double-clicking a video fullscreens it WITHOUT pausing
-- and the two videos never desync (the old MBTN_LEFT cycle pause fired
-- on the first press of a double-click and left one video paused).
local sp_click_t = nil
local sp_click_fs = false
-- nameless bindings: input.conf owns the keys via `script-binding`
mp.add_key_binding("", "syncplayer-click", function()
    if sp_click_t then sp_click_t:kill() end
    sp_click_fs = mp.get_property_bool("fullscreen")
    sp_click_t = mp.add_timeout(0.45, function()
        sp_click_t = nil
        -- double-click safety net: if fullscreen changed since the press
        -- (the DBL handler / property observer may lag under load), this
        -- was a double-click -> do NOT pause
        if mp.get_property_bool("fullscreen") ~= sp_click_fs then
            return
        end
        mp.command("cycle pause")
    end)
end)
-- Double-click handler (dispatched by input.conf via `script-binding`, so
-- it owns the input EVENT - the fullscreen property apply can take ~0.5 s
-- on high-DPI displays, which would race any property-based cancel): it
-- cancels the pending deferred pause and cycles fullscreen.
mp.add_key_binding("", "syncplayer-dbl", function()
    if sp_click_t then sp_click_t:kill() end
    sp_click_t = nil
    mp.command("cycle fullscreen")
end)
-- belt and suspenders: if fullscreen changes through ANY other path,
-- cancel the pending click-pause too
mp.observe_property("fullscreen", "bool", function(name, v)
    if v ~= nil and sp_click_t then
        sp_click_t:kill()
        sp_click_t = nil
    end
end)
"""

# App icon (256x256 PNG, base64) — used for the window/taskbar icon.
ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAAGFUlEQVR42u3du41USxhG0QkCYRED"
    "LsLDwCcZfDLAxkCaGHBxcUmAULBAGCNh8OjuOY+q2uuXdgJ19C1dcWHm7s4555xzzjnnnHPOOeec"
    "c26pe/L02Q/pIYswbAkUBi8BweAlIBi+BAKjl2Bg+BIIDF8CgfFLEDB8CQTGL0HA8CUQGL8EAeOX"
    "IOCjSlEIfEgpioAPKEUR8OGkKAI+mBRFwIeSwgj4SFIUAB9IiiLgw0hRBHwQKYyAjyFFAfAhpCgC"
    "PoAURsDjS1EAPLwURcCDS2EEPLYUBcBDS2EEPLIUBcADS2EEPK4EAEk1ADysFEbAo0pRADyoFEbA"
    "Y0oAkAQASRkAPKQURsAjSgCQBABJGQA8oBRGwONJAFi6V+++LtGLD/eH9vzT2yUCQAAAI4dDGYck"
    "AEYPAxgEATB8EIAgCoDxQwACQQAMHwQgiAJg/BCAQBQA44cABHYCwPiNHwJhBIzf+CGwDgLLAGD8"
    "EIBAFADjhwAEAAAAAACgBoDxQwACUQCMHwIQAAAAAACAGgBvPn5fotefv+iKXn57v0QAAAAAAAAA"
    "ABg1AABg/IIAAAAgAAAAAAIAAAAgAAAAAAIAAAAgAAAAAAAAQBcA4xcEAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAswLw6wAAAACEAVgZAQAAAAAXALAqBAAAAACuAGA1CAAAAADcAMAqEAAAAAB4"
    "BACzIwAAAADgkQDMDAEAAACAjQCYEQIAAAAAGwMwEwQAAAAAdgJgBgQAAAAA7AjA6BAAAAAAOACA"
    "USEAAAAAcCAAo0EAAAAA4AQARkEAAAAAwEkAjAABAAAAgJMBOBMCAAAAAIMAcAYEAAAAAAYD4EgE"
    "AAAAAAwIwFEQAAAAABgYgL0hAAAAADABAHtBAAAAAGAiALZGAAAAAMBkAGwJAQAAAIBJAdgCAgAA"
    "AACTA/AYCAAAAAAsAsAtCAAAAABYCIBrIQAAAACwIACXQgAAAABgYQD+BwEAAACAAAB/QwAAAABA"
    "BIA/QQAAAAAgBsDvEAAAAACIAvBwAAAAAPwXAAAAAAB/BgAAAADA/wUAAAAA4O8BAAAAAPA3AQEA"
    "AAD4twAAAAAA/GtAAAAAAH4eAAAAAAA/EQgAAACAnwkIAAAAYJrhAwAAAPB7AQAAAAD4zUAAAAAA"
    "/G5AAAAAAH47MAAAAIATAThq/AAAAAAGAuDI4QMAAAAYBIAzhg8AAADgZADOHD4AAACAEwEYYfwA"
    "AAAADgZglOEDAAAAOBCA0YYPAAAA4AAARh0+AAAAgJ0BGH38AAAAAHYAYIbhAwAAANgYgJmGDwAA"
    "AGAjAGYcPgAAAIANAJh5/AAAAABuBGD24QMAAAC4AYBVhg8AAADgCgBWGz4AAACACwFYdfwAAAAA"
    "4gEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAGBIACAg4wcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAFgSAAgYv/EDAAAAAAAAAAAAAOQAgIDxGz8AAAAAAFQBgIDxG38cAAgYv/EDAAAAAEAVAAgY"
    "v/HHAYCA8Rt/HAAIGL/x7wwABCBg/OHxzwAABIzf+OMAQMD4jT8OAAgM3/ABAAHjN/46ACAwfMMH"
    "AAyMPj16AMDByEMjB4AkAEjaCAAISOHxA0ACgIeUACApBwAEpPD4ASABwINKAJCUAwACUnj8AJDi"
    "AEBACo8fABIAPLBUBQACUnj8AJDiAEBACo8fAFIcAAhI4fFDQIqPHwBSHAAISOHxQ0CKjx8AUhwA"
    "CEjh8UNAio8fAlJ8/ACQ4gBAQAqPHwJSfPwQkOLjh4AUHz8EpPj4QSDFhw8ByfghINXHDwIpPnwI"
    "SMYPAhm+g4CM34FAhu9AIMN3MJDROxDI8B0QZPAOCDJ4BwoZtnPOOeecc84555xzzjnn3D/vJ1bG"
    "v0h6MjYNAAAAAElFTkSuQmCC"
)

# ---------------------------------------------------------------------------
# mpv discovery
# ---------------------------------------------------------------------------

_mpv_cache = None
_ffprobe_cache = None


def _bundled_mpv():
    """Return a path to an mpv.exe shipped next to the app (self-contained
    installs), or None. Uses the executable dir when frozen, else the source
    dir. This is the fix for "stuck at starting" on a machine with no mpv
    on PATH - the installer puts mpv in <exe dir>\mpv\."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(base, "mpv", "mpv.exe"),
                 os.path.join(base, "mpv.exe")):
        if os.path.isfile(cand):
            return cand
    return None


def _prepend_path(d):
    """Put d at the front of PATH so the bundled yt-dlp.exe and the FFmpeg
    DLLs next to mpv are discoverable by the mpv subprocess (and its
    ytdl_hook), so a YouTube URL plays on a fresh machine with no yt-dlp
    installed globally."""
    if not d:
        return
    path = os.environ.get("PATH", "")
    if path and d.lower() not in path.lower().split(os.pathsep):
        os.environ["PATH"] = d + os.pathsep + path


_ytdl_cache = None


def find_ytdl():
    """Locate yt-dlp.exe: next to mpv, in %LOCALAPPDATA%\SyncPlayer\mpv, or on PATH."""
    global _ytdl_cache
    if _ytdl_cache and os.path.isfile(_ytdl_cache):
        return _ytdl_cache
    mpv = find_mpv()
    if mpv:
        cand = os.path.join(os.path.dirname(mpv), "yt-dlp.exe")
        if os.path.isfile(cand):
            _ytdl_cache = cand
            return cand
    local = os.path.join(os.environ.get("LOCALAPPDATA") or "", "SyncPlayer", "mpv", "yt-dlp.exe")
    if os.path.isfile(local):
        _ytdl_cache = local
        return local
    w = shutil.which("yt-dlp")
    if w:
        _ytdl_cache = w
        return w
    return None


def find_mpv():
    global _mpv_cache
    if _mpv_cache:
        return _mpv_cache
    # 1) explicit override
    env = os.environ.get("MPV_PATH")
    if env and os.path.isfile(env):
        _mpv_cache = env
        return env
    # 2) mpv bundled next to the app (self-contained install)
    bundled = _bundled_mpv()
    if bundled:
        _prepend_path(os.path.dirname(bundled))
        _mpv_cache = bundled
        return bundled
    # 3) %LOCALAPPDATA%\SyncPlayer\mpv\mpv.exe (installer target)
    inst_mpv = os.path.join(os.environ.get("LOCALAPPDATA") or "", "SyncPlayer", "mpv", "mpv.exe")
    if os.path.isfile(inst_mpv):
        _prepend_path(os.path.dirname(inst_mpv))
        _mpv_cache = inst_mpv
        return inst_mpv
    # 4) PATH
    found = shutil.which("mpv")
    if found:
        if found.lower().endswith(".com"):
            exe = os.path.join(os.path.dirname(found), "mpv.exe")
            if os.path.isfile(exe):
                found = exe
        _mpv_cache = found
        return found
    # 5) common install dirs
    for c in (r"C:\Program Files\MPV Player\mpv.exe",
              r"C:\Tools\mpv\mpv.exe",
              os.path.expanduser(r"~\AppData\Local\Programs\mpv\mpv.exe"),
              os.path.expanduser(r"~\scoop\apps\mpv\current\mpv.exe"),
              r"C:\Program Files\mpv\mpv.exe"):
        if os.path.isfile(c):
            _mpv_cache = c
            return c
    _mpv_cache = None
    return None


def detect_crop_rect(src, duration=None, timeout=30):
    """One-shot black-bar detection for a LOCAL file with headless mpv
    (no audio, no video window). Runs libavfilter's cropdetect over ~2.5 s
    sampled a little into the file and parses the detected crop rect from
    the verbose log. Returns (w, h, x, y) in source pixels, or None when no
    bars are detected / the probe fails (URLs included - never applied).

    The cropdetect args are limit:round:threshold. The old value 24:2:0 put
    the black threshold at 0, so only PITCH-BLACK (0,0,0) letterbox was ever
    detected - real encodes have near-black/dark-gray bars (luma ~16-40)
    that 0 missed, which is why Auto looked dead. 32:2:16 raises the luma
    threshold to 16 and the limit to 32, catching both pure-black and
    dark-gray bars at 1280x540+0+90 while rejecting bar-less clips. A sanity
    guard rejects a "bar" that eats more than 45% of a frame."""
    if not src or src.startswith(("http://", "https://")) or not os.path.isfile(src):
        return None
    start = 1.0
    if duration and duration > 8:
        start = min(2.0, duration * 0.2)
    try:
        mpv = find_mpv()
        if not mpv:
            return None
        proc = subprocess.run(
            [mpv, "--no-config", "--input-terminal=no",
             "--vo=null", "--no-audio", "--keep-open=no",
             "--frames=75", "--start=%.2f" % start, "-v",
             "--vf=lavfi-cropdetect=32:2:16", src],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        out = proc.stdout or ""
    except Exception:
        return None
    rect = None
    for m in re.finditer(r"crop=([0-9]+):([0-9]+):([0-9]+):([0-9]+)", out):
        w, h, x, y = (int(m.group(i)) for i in range(1, 5))
        if w > 0 and h > 0:
            rect = (w, h, x, y)
    if not rect or rect[0] < 16 or rect[1] < 16:
        return None
    m = re.search(r"Decoder format: ([0-9]+)x([0-9]+)", out)
    if m:
        sw, sh = int(m.group(1)), int(m.group(2))
        if rect[0] >= sw - 2 and rect[1] >= sh - 2 and rect[2] == 0 and rect[3] == 0:
            return None        # full frame: no bars
        if (max(rect[3], sh - (rect[1] + rect[3])) > 0.45 * sh
                or max(rect[2], sw - (rect[0] + rect[2])) > 0.45 * sw):
            return None        # a "bar" this big is content, not letterbox
    return rect


def find_probe():
    global _ffprobe_cache
    if _ffprobe_cache is None:
        _ffprobe_cache = shutil.which("ffprobe") or ""
    return _ffprobe_cache or None


def probe_media(path, timeout=20):
    """Best-effort probe: returns (duration_s or None, (w, h) or None)."""
    probe = find_probe()
    if not probe or not path or path.startswith(("http://", "https://")):
        return None, None
    try:
        out = subprocess.run(
            [probe, "-v", "error",
             "-show_entries", "format=duration",
             "-show_entries", "stream=codec_type,width,height",
             "-of", "json", path],
            capture_output=True, text=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW)
        d = json.loads(out.stdout or "{}")
        dur = None
        try:
            dur = float(d["format"]["duration"])
        except Exception:
            pass
        size = None
        for s in d.get("streams", []):
            if s.get("codec_type") == "video" and s.get("width"):
                size = (int(s["width"]), int(s["height"]))
                break
        return dur, size
    except Exception:
        return None, None


def probe_duration(path, timeout=20):
    return probe_media(path, timeout)[0]


def is_youtube(url):
    try:
        u = url.lower()
        return "youtube.com" in u or "youtu.be" in u
    except Exception:
        return False


def yt_parse_list_subs(text):
    """Parse `yt-dlp --list-subs` stdout into [{lang, label, auto}] boxes.

    yt-dlp prints two sections: 'Available automatic captions for the video:'
    (auto-generated ASR) and 'Available subtitles for the video:' (uploaded).
    Each is followed by a 'Language  Name' header row, then lang/name rows."""
    subs = []
    section = None
    for line in (text or "").splitlines():
        s = line.strip()
        low = s.lower()
        if "automatic captions for the video" in low:
            section = "auto"
            continue
        if "subtitles for the video" in low and "automatic" not in low:
            section = "manual"
            continue
        if not s or s.startswith("Language"):
            continue
        parts = s.split(None, 1)
        if len(parts) != 2:
            continue
        lang = parts[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", lang):
            continue
        name = parts[1].strip()
        auto = (section == "auto")
        label = "%s [%s]%s" % (name or lang, lang, " (auto)" if auto else "")
        subs.append({"lang": lang, "auto": auto, "label": label})
    return subs


def yt_subtitle_repo(url):
    """List YouTube subtitles (auto + uploaded) for a URL via the yt-dlp CLI."""
    if not is_youtube(url):
        return []
    ytdl = shutil.which("yt-dlp")
    if not ytdl:
        return []
    try:
        out = subprocess.run(
            [ytdl, "--list-subs", "--no-warnings", "--no-playlist",
             "--skip-download", url],
            capture_output=True, text=True, timeout=180)
        if out.returncode != 0:
            return []
        return yt_parse_list_subs(out.stdout)
    except Exception:
        return []


def resolve_url(url):
    """Non-YouTube URLs: resolve a direct stream URL via yt-dlp (best-effort)."""
    ytdl = shutil.which("yt-dlp")
    if not ytdl:
        return url
    try:
        out = subprocess.run(
            [ytdl, "-f", "best[height<=1080]/best", "--no-warnings",
             "--get-url", "--no-playlist", url],
            capture_output=True, text=True, timeout=120)
        line = out.stdout.strip().splitlines()
        if out.returncode == 0 and line:
            return line[-1]
    except Exception:
        pass
    return url


# ---------------------------------------------------------------------------
# sync math (pure functions — unit-tested in selftest.py)
# ---------------------------------------------------------------------------

def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def reaction_target(movie_pos, sync_off):
    """Where the reaction SHOULD be, given the movie's live position and the
    aligned offset (reaction offset relative to the movie, may be negative)."""
    if movie_pos is None:
        return None
    return movie_pos + sync_off


def drift(react_pos, movie_pos, sync_off):
    """How far the reaction has wandered from its aligned spot (seconds)."""
    if react_pos is None or movie_pos is None:
        return 0.0
    return react_pos - (movie_pos + sync_off)


def needs_correction(react_pos, movie_pos, sync_off, threshold=0.45,
                     playing=True, dragging=False, movie_at_end=False,
                     react_at_end=False):
    """True when the reaction should be gently re-seeked back into alignment."""
    if not playing or dragging or movie_at_end or react_at_end:
        return False
    if react_pos is None or movie_pos is None:
        return False
    return abs(drift(react_pos, movie_pos, sync_off)) > threshold


# ---------------------------------------------------------------------------
# Win32 helpers (window placement; reliable at any DPI scaling)
# ---------------------------------------------------------------------------

_SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010

# ---- mpv IPC pipe plumbing ------------------------------------------------
# Overlapped I/O: the CRT poisons a pipe handle that has been read, and a
# synchronous write blocks forever once mpv's reply buffer (a few KB) is
# full - which a drag-spammed volume/seek burst does in a second. Writes
# go through a dedicated writer thread and replies are drained by a
# reader thread, so the GUI thread can never block on the pipe.
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
_FILE_FLAG_OVERLAPPED = 0x40000000
_ERROR_IO_PENDING = 997
_ERROR_BROKEN_PIPE = 109
_ERROR_OPERATION_ABORTED = 995
_ERROR_INVALID_HANDLE = 6
_WAIT_TIMEOUT = 258


class _OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", ctypes.wintypes.DWORD),
                ("OffsetHigh", ctypes.wintypes.DWORD),
                ("hEvent", ctypes.c_void_p)]


_k32 = ctypes.windll.kernel32
_k32.CreateFileW.restype = ctypes.c_void_p
_k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.wintypes.DWORD,
                             ctypes.wintypes.DWORD, ctypes.c_void_p,
                             ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                             ctypes.c_void_p]
_k32.ReadFile.restype = ctypes.wintypes.BOOL
_k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                          ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.c_void_p]
_k32.WriteFile.restype = ctypes.wintypes.BOOL
_k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.wintypes.DWORD,
                           ctypes.POINTER(ctypes.wintypes.DWORD), ctypes.c_void_p]
_k32.GetOverlappedResult.restype = ctypes.wintypes.BOOL
_k32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(ctypes.wintypes.DWORD),
                                     ctypes.wintypes.BOOL]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]
_k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def screen_size():
    try:
        u = ctypes.windll.user32
        return u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    except Exception:
        return 1920, 1080


def find_mpv_window(pid, title_sub, tries=120, delay=0.25):
    """Find the visible top-level 'mpv' window of a process (by PID + title).
    Returns the HWND or None. The 'mpv smtc' helper window is skipped by
    class-name matching."""
    user32 = ctypes.windll.user32
    found = [0]
    for _ in range(tries):
        found[0] = 0
        cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(h, _l):
            pid2 = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(h, ctypes.byref(pid2))
            if pid2.value == pid and user32.IsWindowVisible(h):
                cls = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(h, cls, 64)
                if cls.value == "mpv":
                    buf = ctypes.create_unicode_buffer(256)
                    user32.GetWindowTextW(h, buf, 256)
                    if title_sub in buf.value:
                        found[0] = h
            return True

        user32.EnumWindows(cb(_cb), 0)
        if found[0]:
            return found[0]
        time.sleep(delay)
    return None


def place_window(hwnd, x, y, w, h):
    try:
        ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, max(160, w), max(120, h),
                                          _SWP_NOZORDER_NOACTIVATE)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# mpv driver (one process + one window per video)
# ---------------------------------------------------------------------------

class MpvDriver:
    def __init__(self, src, tag, on_pause=None, on_exit=None,
                 start_paused=False):
        self.tag = tag
        self.on_pause = on_pause
        self.on_exit = on_exit
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self._h = None               # overlapped pipe handle
        self._cmdq = queue.Queue()   # IPC commands, drained by a writer thread
        self._next_rid = 1           # outgoing get_property request ids
        self.rq = queue.Queue()      # replies: (request_id, error, data)
        self.sub_id = None           # current subtitle track id (status line)
        self.audio_id = None         # current audio track id (status line)
        self.paused = bool(start_paused)
        self.at_end = False
        self._eof_hold = False           # parked in the keep-open EOF hold
        self.hwnd = None
        self.last_seek_ts = 0.0         # any seek path stamps this

        # Windows named pipe for JSON IPC (open() works directly on \\\\.\\pipe\\...)
        pipe_name = "syncplayer-%d-%d-%s" % (os.getpid(), threading.get_ident(), tag)

        # custom input map: SPACE pauses; the mouse keys dispatch the Lua
        # beacon's named bindings (input.conf is the TOP of mpv's binding
        # chain - it wins over the built-in defaults AND Lua key bindings,
        # so the double-click handling is fully deterministic here):
        #   MBTN_LEFT       -> syncplayer-click: arm a 450 ms deferred pause
        #   MBTN_LEFT_DBL   -> syncplayer-dbl: cancel the deferred pause and
        #                      cycle fullscreen (fires on the INPUT EVENT,
        #                      not on the slow high-DPI property apply)
        self.input_conf = os.path.join(SHOT_DIR, "input.conf")
        try:
            with open(self.input_conf, "w") as f:
                f.write("SPACE cycle pause\n")
                f.write("MBTN_LEFT script-binding syncplayer-click\n")
                f.write("MBTN_LEFT_DBL script-binding syncplayer-dbl\n")
        except Exception:
            pass

        self.lua_script = os.path.join(SHOT_DIR, "syncplayer_events.lua")
        try:
            with open(self.lua_script, "w") as f:
                f.write(LUA_SCRIPT)
        except Exception:
            pass

        title = "SyncPlayer — Movie" if tag == "A" else "SyncPlayer — Reaction"
        # capture mpv's output so a crash on another machine is diagnosable
        self._mpv_log = None
        try:
            self._mpv_log = open(os.path.join(SHOT_DIR, "mpv_%s.log" % tag),
                                  "w", encoding="utf-8", errors="replace")
        except Exception:
            self._mpv_log = None
        mpv = find_mpv()
        if not mpv:
            raise MpvNotFoundError(
                "mpv was not found. Install SyncPlayer (it bundles mpv), or "
                "put mpv on PATH, or set the MPV_PATH environment variable.")
        ytdl_bin = find_ytdl()
        if ytdl_bin:
            _prepend_path(os.path.dirname(ytdl_bin))
        args = [mpv,
                "--no-config",
                "--input-ipc-server=%s" % pipe_name,
                "--input-conf=%s" % self.input_conf,
                "--script=%s" % self.lua_script,
                "--input-terminal=no",
                "--terminal=yes",
                "--term-osd=force",
                "--no-term-osd-bar",
                "--term-status-msg=" + STATUS_PREFIX +
                "${time-pos}|${duration}|${percent-pos}|${pause}|${volume}|${playback-time}|${eof-reached}"
                + STATUS_APPEND,
                "--osc=no",
                "--keep-open=yes",
                "--keepaspect=yes",
                "--keepaspect-window=no",   # free-form window resize (no aspect snap)
                "--hwdec=safe",
                "--fs=no",
                "--ytdl=yes",
                "--volume-max=150",
                ]
        if ytdl_bin:
            args.append("--script-opts=ytdl_hook-ytdl_path=%s" % ytdl_bin)
        args.extend([
                "--force-window=yes",
                "--title=%s" % title,
                "--pause=yes" if start_paused else "--pause=no",
                src,
                ])
        self.proc = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        threading.Thread(target=self._connect_pipe, args=(pipe_name,), daemon=True).start()
        threading.Thread(target=self._stdout_reader, daemon=True).start()

    # -- pipe ---------------------------------------------------------------
    # Overlapped connect: open the pipe with FILE_FLAG_OVERLAPPED, then spawn
    # a writer thread (serializes IPC writes; blocks only itself) and a drain
    # thread (consumes mpv's replies so its reply buffer never fills).
    def _connect_pipe(self, name, tries=60, delay=0.25):
        path = r"\\.\pipe\%s" % name
        for _ in range(tries):
            if self.stopped.is_set():
                return
            h = _k32.CreateFileW(path,
                                 _GENERIC_READ | _GENERIC_WRITE, 0, None,
                                 _OPEN_EXISTING, _FILE_FLAG_OVERLAPPED, None)
            if h and h != ctypes.c_void_p(-1).value:
                self._h = h
                threading.Thread(target=self._drain_replies, daemon=True).start()
                threading.Thread(target=self._writer_loop, daemon=True).start()
                return
            time.sleep(delay)

    def _drain_replies(self):
        """Consume and discard mpv reply packets so its server-side buffer
        never fills and mpv keeps reading our commands - a full buffer
        wedges the whole sync loop. Reply LINES carrying a request_id are
        routed to rq so get_property() can match replies to its calls."""
        buf = ctypes.create_string_buffer(8192)
        evt = _k32.CreateEventW(None, False, False, None)
        ov = _OVERLAPPED()
        ov.hEvent = evt
        n = ctypes.wintypes.DWORD(0)
        h = self._h
        acc = b""

        def emit(chunk):
            nonlocal acc
            acc += chunk
            while b"\n" in acc:
                line, rest = acc.split(b"\n", 1)
                acc = rest
                try:
                    m = json.loads(line.decode("utf-8", "replace"))
                    if "request_id" in m and "error" in m:
                        self.rq.put((m["request_id"], m.get("error"),
                                     m.get("data")))
                except Exception:
                    pass

        while not self.stopped.is_set() and h:
            if _k32.ReadFile(h, ctypes.byref(buf), len(buf), ctypes.byref(n),
                             ctypes.byref(ov)):
                emit(buf.raw[:n.value])
                continue
            err = _k32.GetLastError()
            if err == _ERROR_IO_PENDING:
                # Block on the event: NEVER re-issue ReadFile while the
                # OVERLAPPED is still pending (undefined behavior, heap
                # corruption). quit() cancels the I/O to wake us.
                _k32.WaitForSingleObject(evt, 0xFFFFFFFF)
                done = ctypes.wintypes.DWORD(0)
                if not _k32.GetOverlappedResult(h, ctypes.byref(ov),
                                                ctypes.byref(done), False):
                    err = _k32.GetLastError()
                    if err in (_ERROR_BROKEN_PIPE, _ERROR_OPERATION_ABORTED,
                               _ERROR_INVALID_HANDLE):
                        break
                else:
                    emit(buf.raw[:done.value])
            elif err in (_ERROR_BROKEN_PIPE, _ERROR_OPERATION_ABORTED,
                          _ERROR_INVALID_HANDLE):
                break
        if evt:
            _k32.CloseHandle(evt)

    def _writer_loop(self):
        """Serial writer: pops queued commands and writes them. A blocked
        write stalls THIS thread only - never the GUI."""
        while not self.stopped.is_set() and self._h:
            try:
                obj = self._cmdq.get(timeout=0.2)
            except queue.Empty:
                continue
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n"
            wbuf = ctypes.create_string_buffer(data)
            written = ctypes.wintypes.DWORD(0)
            try:
                if not _k32.WriteFile(self._h, ctypes.byref(wbuf), len(data),
                                     ctypes.byref(written), None):
                    time.sleep(0.05)      # pipe busy/broken: back off, drop
            except Exception:
                pass

    # -- status reader ------------------------------------------------------
    def _stdout_reader(self):
        try:
            for line in self.proc.stdout:
                if self._mpv_log:
                    try:
                        self._mpv_log.write(line)
                        self._mpv_log.flush()
                    except Exception:
                        pass
                if line.startswith(STATUS_PREFIX):
                    self._parse_status(line[len(STATUS_PREFIX):].strip())
                elif "SYNCPOS|" in line:
                    try:
                        pos = float(line.rsplit("SYNCPOS|", 1)[1].strip())
                        self.q.put(("pos", pos))
                    except Exception:
                        pass
                elif "SYNCPIPDRAG|start" in line:
                    self.q.put(("pipdrag", "start"))
                elif "SYNCPIPDRAG|end" in line:
                    self.q.put(("pipdrag", "end"))
                elif "SYNCPIPDRAG|undock" in line:
                    self.q.put(("pipdrag", "undock"))
                elif "SYNCEOF|true" in line:
                    self.at_end = True
                    self._eof_hold = True
                elif "SYNCEOF|false" in line:
                    self.at_end = False
                    self._eof_hold = False
                elif "SYNCPAUSE|eof" in line:
                    self.paused = True
                    self.at_end = True
                    self._eof_hold = True
                    self.q.put(("pause", ("eof", time.monotonic())))
                elif "SYNCPAUSE|true" in line:
                    self.paused = True
                    self.q.put(("pause", (True, time.monotonic())))
                elif "SYNCPAUSE|false" in line:
                    self.paused = False
                    self._eof_hold = False
                    self.q.put(("pause", (False, time.monotonic())))
        except Exception:
            pass
        finally:
            if self._mpv_log:
                try:
                    self._mpv_log.close()
                except Exception:
                    pass
            if not self.stopped.is_set():
                self.q.put(("exit", None))
                # NOTE: never call self.on_exit() here — it would run on this
                # worker thread and tkinter calls from non-main threads are
                # unsafe (RuntimeError). The app handles ("exit", None) on the
                # main thread via the queue.

    @staticmethod
    def _to_seconds(x):
        """Parse '123.45' or '01:23:45.678' (mpv HH:MM:SS.mmm format) to float."""
        x = (x or "").strip()
        if not x or x in ("nan", "-nan"):
            return None
        sign = -1.0 if x.startswith("-") else 1.0
        x = x.lstrip("-")
        try:
            if ":" in x:
                parts = [float(p) for p in x.split(":")]
                tail = parts[-3:]
                if len(tail) == 2:
                    sec = tail[0] * 60 + tail[1]
                elif len(tail) == 3:
                    sec = tail[0] * 3600 + tail[1] * 60 + tail[2]
                else:
                    sec = tail[-1]
                return sign * sec
            return sign * float(x)
        except Exception:
            return None

    def _parse_status(self, s):
        parts = s.split("|")
        try:
            rec = {
                "time_pos": MpvDriver._to_seconds(parts[0] if len(parts) > 0 else ""),
                "duration": MpvDriver._to_seconds(parts[1] if len(parts) > 1 else ""),
                "percent": float(parts[2]) if len(parts) > 2 and parts[2] not in ("", "nan", "-nan") else None,
                "pause": parts[3].strip() == "yes" if len(parts) > 3 else False,
                "volume": float(parts[4]) if len(parts) > 4 and parts[4] not in ("", "nan", "-nan") else None,
                "playback_time": MpvDriver._to_seconds(parts[5] if len(parts) > 5 else ""),
                "eof": parts[6].strip() == "yes" if len(parts) > 6 else False,
            }
            if len(parts) > 7:
                self.sub_id = self._to_int_or_none(parts[7])
            if len(parts) > 8:
                self.audio_id = self._to_int_or_none(parts[8])
            if len(parts) > 9:
                try:
                    cval = parts[9].strip()
                    self.cache_dur = float(cval) if cval not in ("", "nan", "-nan") else 0.0
                except Exception:
                    self.cache_dur = 0.0
            else:
                self.cache_dur = 0.0
            rec["cache_dur"] = self.cache_dur
            self.q.put(("status", rec))
        except Exception:
            pass

    @staticmethod
    def _to_int_or_none(x):
        x = (x or "").strip()
        try:
            return int(x)
        except Exception:
            return None

    def get_property(self, prop, timeout=3.0):
        """Synchronous property read over the IPC pipe: queue the command
        with a unique request_id, then wait for the drain thread to route
        the matching reply line into rq. Returns (error, data)."""
        if not (self._h and self.running):
            return None, None
        rid = self._next_rid
        self._next_rid += 1
        self._cmdq.put({"command": ["get_property", prop],
                        "request_id": rid})
        dl = time.monotonic() + timeout
        while time.monotonic() < dl:
            try:
                rid2, err, data = self.rq.get(timeout=0.2)
            except queue.Empty:
                continue
            if rid2 == rid:
                return err, data
            if err == "property-unavailable":
                return err, data         # id-less reply: no match possible
        return None, None

    def track_list(self):
        err, tl = self.get_property("track-list")
        return tl if isinstance(tl, list) else []

    def set_sub(self, n):
        if n is None:
            self.cmd({"command": ["set_property", "sid", "no"]})
        else:
            self.cmd({"command": ["set_property", "sid", int(n)]})

    def set_audio(self, n):
        if n is None:
            self.cmd({"command": ["set_property", "aid", "no"]})
        else:
            self.cmd({"command": ["set_property", "aid", int(n)]})

    def frame_step(self, back=False):
        """Step one video by exactly one frame, then pause again. Verified
        on mpv 0.41: works while PAUSED and moves time-pos by exactly one
        frame duration (+/-0.0333 s at 30 fps). Stamps the seek timestamp so
        the drift corrector leaves this video alone."""
        self.last_seek_ts = time.monotonic()
        self.cmd({"command": ["frame-back-step" if back else "frame-step"]})

    # -- commands -----------------------------------------------------------
    def cmd(self, obj):
        """Queue a JSON IPC command; a writer thread performs the write, so
        the GUI can never block on the pipe (commands sent before the pipe
        connects are simply drained once it is up)."""
        try:
            self._cmdq.put(obj)
        except Exception:
            pass

    def set_volume(self, n):
        # NOTE: mpv 0.41's IPC "set" command silently ignores `volume` —
        # verified empirically; "set_property" is the working form.
        self.cmd({"command": ["set_property", "volume", max(0, min(150, int(round(n))))]})

    def set_speed(self, r):
        self.cmd({"command": ["set_property", "speed", max(0.1, min(4.0, r))]})

    def set_pause(self, pause):
        # mpv 0.41 rejects JSON booleans for pause ("invalid parameter") -
        # use the "yes"/"no" strings. mpv stops printing the term status line
        # while paused, so we track the state locally (single writer).
        self.paused = bool(pause)
        # NOTE: do NOT clear at_end here - resume events from the Lua
        # (SYNCEOF|false / SYNCPAUSE|false) own that state; clearing it on
        # unpause unlocked the drift corrector straight into a manual seek.
        self.cmd({"command": ["set", "pause", "yes" if pause else "no"]})

    def toggle_pause(self):
        self.paused = not self.paused
        self.cmd({"command": ["cycle", "pause"]})

    def seek(self, pos, absolute=True, exact=True):
        self.last_seek_ts = time.monotonic()
        hold = self.at_end or self._eof_hold
        self.at_end = False
        mode = "exact" if exact else "keyframes"
        if hold:
            if absolute:
                self.cmd({"command": ["seek", max(0.0, pos), "absolute+" + mode]})
            else:
                self.cmd({"command": ["seek", pos, "relative+" + mode]})
            self.set_pause(False)
            return
        if absolute:
            self.cmd({"command": ["seek", max(0.0, pos), "absolute+" + mode]})
        else:
            self.cmd({"command": ["seek", pos, "relative+" + mode]})

    def screenshot(self, path):
        self.cmd({"command": ["screenshot-to-file", path, "video"]})

    def quit(self):
        self.stopped.set()
        try:
            self.cmd({"command": ["quit"]})
            if self._h:
                time.sleep(0.1)   # let the writer deliver "quit"
                _k32.CancelIoEx(self._h, None)
                _k32.CloseHandle(self._h)
                self._h = None
        except Exception:
            pass
        self.pipe = None

    @property
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def kill(self):
        try:
            if self.proc:
                self.proc.kill()
        except Exception:
            pass
# ---------------------------------------------------------------------------
# GUI helpers
# ---------------------------------------------------------------------------

class Tooltip:
    """Simple hover tooltip for a widget (500 ms delay)."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        self._after = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def _schedule(self, _event=None):
        self._cancel()
        self._after = self.widget.after(500, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _show(self, _event=None):
        self._after = None
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry("+%d+%d" % (x, y))
        tk.Label(self.tip, text=self.text, justify="left",
                 bg="#2a2f3a", fg="#e8e8ea", padx=10, pady=6,
                 font=("Segoe UI", 9)).pack()

    def _hide(self, _event=None):
        self._cancel()
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


class VisualCropDialog(tk.Toplevel):
    """Interactive visual cropping popup: displays a freeze-frame of the video
    where the user can draw, drag, and resize a crop box with the mouse.
    The viewport and frame dynamically scale with window resizing, allowing
    users to expand the window as large as they want for maximum precision."""
    def __init__(self, parent, image_path, initial_crop=None, video_name="Video", on_apply=None):
        super().__init__(parent)
        self.title("Visual Crop — %s" % video_name)
        self.transient(parent)
        self.grab_set()
        self.configure(bg="#1f232b")
        self.geometry("960x650")
        self.minsize(500, 380)
        self.resizable(True, True)

        self.on_apply = on_apply
        self.orig_img = Image.open(image_path)
        self.orig_w, self.orig_h = self.orig_img.size

        # Crop rectangle in ORIGINAL IMAGE coordinates: ox1, oy1, ox2, oy2
        if initial_crop and len(initial_crop) == 4:
            cw, ch, cx, cy = initial_crop
            self.ox1 = max(0, min(self.orig_w, cx))
            self.oy1 = max(0, min(self.orig_h, cy))
            self.ox2 = max(0, min(self.orig_w, cx + cw))
            self.oy2 = max(0, min(self.orig_h, cy + ch))
        else:
            self.ox1, self.oy1 = 0, 0
            self.ox2, self.oy2 = self.orig_w, self.orig_h

        # Dynamic viewport geometry
        self.scale = 1.0
        self.disp_w = self.orig_w
        self.disp_h = self.orig_h
        self.off_x = 0
        self.off_y = 0
        self.photo = None

        # Header info
        hdr = ttk.Frame(self)
        hdr.pack(fill="x", padx=12, pady=(10, 4))
        self.lbl_info = ttk.Label(hdr, text="Drag to select or resize the crop box. Resize window for larger view.", font=("Segoe UI", 9))
        self.lbl_info.pack(side="left")

        # Canvas container
        self.canv_frame = ttk.Frame(self)
        self.canv_frame.pack(fill="both", expand=True, padx=12, pady=4)

        self.canvas = tk.Canvas(self.canv_frame, bg="#111317", highlightthickness=1,
                                highlightbackground="#333740")
        self.canvas.pack(fill="both", expand=True)

        # Interaction state
        self.mode = None
        self.start_x = 0
        self.start_y = 0
        self.drag_start_orig = (0, 0, 0, 0)

        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_hover)

        # Button bar
        btn_bar = ttk.Frame(self)
        btn_bar.pack(fill="x", padx=12, pady=(8, 12))

        btn_apply = ttk.Button(btn_bar, text="✔ Apply Crop", width=14, command=self._apply)
        btn_apply.pack(side="left", padx=(0, 6))

        btn_reset = ttk.Button(btn_bar, text="Full Frame (Reset)", width=16, command=self._reset_full)
        btn_reset.pack(side="left", padx=6)

        btn_cancel = ttk.Button(btn_bar, text="Cancel", width=10, command=self.destroy)
        btn_cancel.pack(side="right", padx=(6, 0))

        self.bind("<Return>", lambda e: self._apply())
        self.bind("<Escape>", lambda e: self.destroy())

    def _on_canvas_configure(self, event):
        cw = max(50, event.width)
        ch = max(50, event.height)
        scale_w = cw / float(self.orig_w) if self.orig_w > 0 else 1.0
        scale_h = ch / float(self.orig_h) if self.orig_h > 0 else 1.0
        new_scale = min(scale_w, scale_h)
        new_dw = max(10, int(self.orig_w * new_scale))
        new_dh = max(10, int(self.orig_h * new_scale))
        new_ox = (cw - new_dw) // 2
        new_oy = (ch - new_dh) // 2

        self.scale = new_scale
        self.disp_w = new_dw
        self.disp_h = new_dh
        self.off_x = new_ox
        self.off_y = new_oy

        resample = getattr(Image, "Resampling", Image).BILINEAR
        disp_img = self.orig_img.resize((self.disp_w, self.disp_h), resample)
        self.photo = ImageTk.PhotoImage(disp_img)

        self.canvas.delete("img")
        self.canvas.create_image(self.off_x, self.off_y, anchor="nw", image=self.photo, tags="img")
        self._draw_overlay()

    def _to_display(self, ox, oy):
        return int(self.off_x + ox * self.scale), int(self.off_y + oy * self.scale)

    def _to_orig(self, dx, dy):
        if self.scale <= 0:
            return 0, 0
        ox = int((dx - self.off_x) / self.scale)
        oy = int((dy - self.off_y) / self.scale)
        return max(0, min(self.orig_w, ox)), max(0, min(self.orig_h, oy))

    def _get_display_rect(self):
        x1, y1 = min(self.ox1, self.ox2), min(self.oy1, self.oy2)
        x2, y2 = max(self.ox1, self.ox2), max(self.oy1, self.oy2)
        rx1, ry1 = self._to_display(x1, y1)
        rx2, ry2 = self._to_display(x2, y2)
        return rx1, ry1, rx2, ry2

    def _get_handles(self):
        rx1, ry1, rx2, ry2 = self._get_display_rect()
        mx = (rx1 + rx2) // 2
        my = (ry1 + ry2) // 2
        return {
            "nw": (rx1, ry1), "n": (mx, ry1), "ne": (rx2, ry1),
            "w": (rx1, my),                    "e": (rx2, my),
            "sw": (rx1, ry2), "s": (mx, ry2), "se": (rx2, ry2)
        }

    def _hit_test(self, x, y):
        tol = 8
        handles = self._get_handles()
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= tol and abs(y - hy) <= tol:
                return name
        rx1, ry1, rx2, ry2 = self._get_display_rect()
        if rx1 < x < rx2 and ry1 < y < ry2:
            return "move"
        if self.off_x <= x <= self.off_x + self.disp_w and self.off_y <= y <= self.off_y + self.disp_h:
            return "new"
        return None

    def _on_hover(self, event):
        hit = self._hit_test(event.x, event.y)
        cursor_map = {
            "nw": "size_nw_se", "se": "size_nw_se",
            "ne": "size_ne_sw", "sw": "size_ne_sw",
            "n": "size_ns", "s": "size_ns",
            "w": "size_we", "e": "size_we",
            "move": "fleur",
            "new": "crosshair"
        }
        self.canvas.config(cursor=cursor_map.get(hit, ""))

    def _on_press(self, event):
        hit = self._hit_test(event.x, event.y)
        if not hit:
            return
        self.mode = hit
        self.start_x = event.x
        self.start_y = event.y
        self.drag_start_orig = (self.ox1, self.oy1, self.ox2, self.oy2)
        if self.mode == "new":
            ox, oy = self._to_orig(event.x, event.y)
            self.ox1 = ox
            self.oy1 = oy
            self.ox2 = ox
            self.oy2 = oy
            self._draw_overlay()

    def _on_drag(self, event):
        if not self.mode:
            return
        ox1, oy1, ox2, oy2 = self.drag_start_orig
        x1, y1 = min(ox1, ox2), min(oy1, oy2)
        x2, y2 = max(ox1, ox2), max(oy1, oy2)

        cur_ox, cur_oy = self._to_orig(event.x, event.y)
        st_ox, st_oy = self._to_orig(self.start_x, self.start_y)
        d_ox = cur_ox - st_ox
        d_oy = cur_oy - st_oy

        if self.mode == "new":
            self.ox2 = cur_ox
            self.oy2 = cur_oy
        elif self.mode == "move":
            bw = x2 - x1
            bh = y2 - y1
            nx1 = max(0, min(self.orig_w - bw, x1 + d_ox))
            ny1 = max(0, min(self.orig_h - bh, y1 + d_oy))
            self.ox1, self.oy1 = nx1, ny1
            self.ox2, self.oy2 = nx1 + bw, ny1 + bh
        elif self.mode == "nw":
            self.ox1 = min(x2 - 16, max(0, x1 + d_ox))
            self.oy1 = min(y2 - 16, max(0, y1 + d_oy))
            self.ox2, self.oy2 = x2, y2
        elif self.mode == "se":
            self.ox1, self.oy1 = x1, y1
            self.ox2 = max(x1 + 16, min(self.orig_w, x2 + d_ox))
            self.oy2 = max(y1 + 16, min(self.orig_h, y2 + d_oy))
        elif self.mode == "ne":
            self.ox1 = x1
            self.oy1 = min(y2 - 16, max(0, y1 + d_oy))
            self.ox2 = max(x1 + 16, min(self.orig_w, x2 + d_ox))
            self.oy2 = y2
        elif self.mode == "sw":
            self.ox1 = min(x2 - 16, max(0, x1 + d_ox))
            self.oy1 = y1
            self.ox2 = x2
            self.oy2 = max(y1 + 16, min(self.orig_h, y2 + d_oy))
        elif self.mode == "n":
            self.ox1, self.ox2 = x1, x2
            self.oy1 = min(y2 - 16, max(0, y1 + d_oy))
            self.oy2 = y2
        elif self.mode == "s":
            self.ox1, self.ox2 = x1, x2
            self.oy1 = y1
            self.oy2 = max(y1 + 16, min(self.orig_h, y2 + d_oy))
        elif self.mode == "w":
            self.ox1 = min(x2 - 16, max(0, x1 + d_ox))
            self.oy1, self.oy2 = y1, y2
            self.ox2 = x2
        elif self.mode == "e":
            self.ox1 = x1
            self.oy1, self.oy2 = y1, y2
            self.ox2 = max(x1 + 16, min(self.orig_w, x2 + d_ox))

        self._draw_overlay()

    def _on_release(self, event):
        x1, y1 = min(self.ox1, self.ox2), min(self.oy1, self.oy2)
        x2, y2 = max(self.ox1, self.ox2), max(self.oy1, self.oy2)
        if (x2 - x1) < 16 or (y2 - y1) < 16:
            self._reset_full()
        else:
            self.ox1, self.oy1, self.ox2, self.oy2 = x1, y1, x2, y2
            self._draw_overlay()
        self.mode = None

    def _draw_overlay(self):
        self.canvas.delete("overlay")
        rx1, ry1, rx2, ry2 = self._get_display_rect()
        im_x1 = self.off_x
        im_y1 = self.off_y
        im_x2 = self.off_x + self.disp_w
        im_y2 = self.off_y + self.disp_h

        # Semi-transparent shaded strips outside the crop box (clamped to image bounds)
        fill_color = "#000000"
        stipple = "gray50"
        if ry1 > im_y1:
            self.canvas.create_rectangle(im_x1, im_y1, im_x2, ry1, fill=fill_color, stipple=stipple, width=0, tags="overlay")
        if ry2 < im_y2:
            self.canvas.create_rectangle(im_x1, ry2, im_x2, im_y2, fill=fill_color, stipple=stipple, width=0, tags="overlay")
        if rx1 > im_x1:
            self.canvas.create_rectangle(im_x1, ry1, rx1, ry2, fill=fill_color, stipple=stipple, width=0, tags="overlay")
        if rx2 < im_x2:
            self.canvas.create_rectangle(rx2, ry1, im_x2, ry2, fill=fill_color, stipple=stipple, width=0, tags="overlay")

        # Crop box border
        self.canvas.create_rectangle(rx1, ry1, rx2, ry2, outline="#00b4d8", width=2, tags="overlay")

        # Handles
        handles = self._get_handles()
        hs = 5
        for hx, hy in handles.values():
            self.canvas.create_rectangle(hx - hs, hy - hs, hx + hs, hy + hs, fill="#ffffff", outline="#00b4d8", width=1.5, tags="overlay")

        # Update info label
        orig_crop = self.get_orig_crop()
        if orig_crop:
            cw, ch, cx, cy = orig_crop
            asp = cw / float(ch) if ch > 0 else 0
            self.lbl_info.config(text="Crop: %d × %d at (+%d, +%d) — Aspect: %.2f:1 [Window resizable for higher precision]" % (cw, ch, cx, cy, asp))
        else:
            self.lbl_info.config(text="Full Frame: %d × %d (no crop) [Window resizable for higher precision]" % (self.orig_w, self.orig_h))

    def _reset_full(self):
        self.ox1, self.oy1 = 0, 0
        self.ox2, self.oy2 = self.orig_w, self.orig_h
        self._draw_overlay()

    def get_orig_crop(self):
        x1, y1 = min(self.ox1, self.ox2), min(self.oy1, self.oy2)
        x2, y2 = max(self.ox1, self.ox2), max(self.oy1, self.oy2)
        ow = x2 - x1
        oh = y2 - y1
        ox = x1
        oy = y1

        if ow >= self.orig_w - 4 and oh >= self.orig_h - 4 and ox <= 2 and oy <= 2:
            return None
        return (ow, oh, ox, oy)

    def _apply(self):
        crop = self.get_orig_crop()
        if self.on_apply:
            self.on_apply(crop)
        self.destroy()


class SyncApp:
    def __init__(self, root):
        self.root = root
        self.players = {"A": None, "B": None}
        self.started = False
        self.paused = True
        self.sync_off = 0.0          # reaction's offset vs the movie (can be ±)
        self.last_pos = {"A": None, "B": None}
        self.last_dur = {"A": None, "B": None}
        self.cache_dur = {"A": 0.0, "B": 0.0}
        self.dragging_seek_a = False
        self.dragging_seek_b = False
        self.dragging_seek_m = False
        self._seek_a_val = None
        self._seek_b_val = None
        self._seek_m_val = None
        self._scrub_ts = 0.0
        self.jump_sec = tk.DoubleVar(value=5.0)
        self.goto_vars = {"A": tk.StringVar(), "B": tk.StringVar()}
        self.dragging_vol = [False, False]
        self.dragging_master = False
        self._ctrls = []
        self._status_time = {"A": 0.0, "B": 0.0}   # monotonic clock per stream
        self._beacon_ts = {"A": 0.0, "B": 0.0}       # last fresh Lua position per video
        self._seek_grace_until = 0.0               # no drift-correction until here
        self._last_corr = {"A": 0.0, "B": 0.0}     # last correction time per video
        self._pause_cmd_ts = 0.0               # last explicit pause-command time
        self._frame_step_ts = 0.0             # last frame-step command time
        self._last_active = "A"               # last-touched video (frame-step keys)
        self._fstep_btns = []                 # per-video frame-step buttons
        self._step_pending = {"A": None, "B": None}   # frame-step retry state
        self._prog_set = False          # True while poll code is .set()-ing bars
        self._vol_cmd_ts = 0.0            # last volume-command send time (drag throttle)
        self.sync_locked = False        # Sync Lock: Master bar alone drives both
        self._to_cache = {}            # last scale "to" per bar (A/B/M)
        self._lbl_cache = {}           # last label text per bar
        self._lbl_status_cache = ""    # last status text
        self.pip = {"A": False, "B": False}   # picture-in-picture state
        self._pip_saved = {"A": 0, "B": 0}     # original window styles
        self.pip_int = False          # integrated PiP (embedded pane) state
        self._pip_int_tag = None      # which video is the embedded pane
        self._pip_int_pos = [0.60, 0.60]   # pane top-left, fraction of host
        self._pip_int_size = [0.32, 0.32]  # pane size, fraction of host
        self._pip_int_drag = False    # user is dragging the pane
        self._pip_move_run = False    # SetWindowPos re-layout in flight
        self._pip_int_hwnd = None     # the embedded mpv window handle
        self._pip_int_host = None     # tag of the HOST window (the main feed)
        self._pip_rect_saved = {"A": None, "B": None}  # pre-embed geometry
        self._pip_crop_saved = {"A": None, "B": None}  # pre-crop window rects
        self._srcs = {"A": None, "B": None}   # resolved sources per player
        self._crop_cache = {}      # source path -> (w, h, x, y) or None
        self._crop_busy = set()    # sources with a detection run in flight
        self._pip_int_asp = None   # embedded pane aspect override (cropped)
        self._crop_tag = "A"            # which video the manual crop controls
        self._manual_crop = {"A": None, "B": None}   # user crop (persists across PiP)
        self._crop_auto_pending = None   # (tag, rect|None) worker->main handoff for Auto
        self._status_pin = 0.0   # until this monotonic time the poll must NOT rewrite the status bar
        self._yt_subs = {"A": [], "B": []}     # yt-dlp subtitle options per video
        self._yt_sub_lock = {"A": False, "B": False}  # yt subtitle download in flight
        self._icon_img = None
        try:
            self._icon_img = tk.PhotoImage(data=ICON_B64)
            root.iconphoto(True, self._icon_img)
        except Exception:
            pass

        self.movie_path = tk.StringVar()
        self.react_path = tk.StringVar()
        self.vol_a = tk.DoubleVar(value=100.0)
        self.vol_b = tk.DoubleVar(value=100.0)
        self.vol_m = tk.DoubleVar(value=100.0)
        self.speed = tk.DoubleVar(value=1.0)
        self.speed_str = tk.StringVar(value="1.00x")   # editable speed entry
        self.paused = True
        self.saved_vol_a = 100.0
        self.saved_vol_b = 100.0

        self._load_config()
        self._build_ui()
        self._apply_startup_cli()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(33, self._poll)      # ~30 Hz: smooth bars

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        # The panel is a fixed-size control surface, sized to its content at
        # the end of this method. The videos play in their own mpv windows,
        # which resize smoothly. Drag-resizing this software-rendered panel
        # forces a full Tk re-layout every pixel (230 ms/step on 4K/175%)
        # so the size is locked after the widgets are built.

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        bg = "#16181d"
        fg = "#e8e8ea"
        card = "#1f232b"
        accent = "#4f9cf9"
        for w in (self.root,):
            w.configure(bg=bg)
        style.configure(".", background=bg, foreground=fg, fieldbackground=card)
        style.configure("TFrame", background=bg)
        style.configure("Card.TFrame", background=card)
        style.configure("TLabel", background=bg, foreground=fg)
        style.configure("Card.TLabel", background=card, foreground=fg)
        style.configure("Dim.TLabel", background=bg, foreground="#8a8f9a")
        style.configure("Head.TLabel", background=bg, foreground=accent, font=("Segoe UI", 13, "bold"))
        style.configure("TButton", background="#2a2f3a", foreground=fg, bordercolor="#3a4150",
                        focusthickness=0, padding=(8, 4))
        style.map("TButton", background=[("active", "#38404e"), ("disabled", "#20242c")],
                  foreground=[("disabled", "#5a5f6a")])
        style.configure("Accent.TButton", background="#2c5a9e", foreground=fg)
        style.map("Accent.TButton", background=[("active", "#3a6fc0")])
        style.configure("TScale", background=bg, troughcolor="#2a2f3a")
        style.configure("Horizontal.TScale", background=bg)
        style.configure("TRadiobutton", background=bg, foreground=fg)
        style.configure("TCheckbutton", background=bg, foreground=fg)
        style.configure("TLabelframe", background=bg, foreground=fg, bordercolor="#3a4150")
        style.configure("TLabelframe.Label", background=bg, foreground=fg)
        style.configure("TEntry", fieldbackground=card, foreground=fg,
                        insertcolor=fg, bordercolor="#3a4150")
        style.configure("TCombobox", fieldbackground=card, background=bg,
                        foreground=fg, arrowcolor=fg, bordercolor="#3a4150",
                        lightcolor=card, darkcolor=card, insertcolor=fg)
        style.map("TCombobox",
                  fieldbackground=[("readonly", card), ("disabled", "#20242c")],
                  foreground=[("disabled", "#5a5f6a")],
                  selectbackground=[("readonly", card), ("disabled", "#20242c")],
                  selectforeground=[("readonly", fg), ("disabled", "#5a5f6a")],
                  arrowcolor=[("disabled", "#5a5f6a")])
        # the popdown listbox of a ttk.Combobox is a plain Tk listbox that
        # ignores the theme; without these options it is white-on-white.
        self.root.option_add("*TCombobox*Listbox.background", card)
        self.root.option_add("*TCombobox*Listbox.foreground", fg)
        self.root.option_add("*TCombobox*Listbox.selectBackground", accent)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.option_add("*TCombobox*Listbox.activestyle", "none")
        self.root.option_add("*TCombobox*Listbox.bordercolor", "#2a2f3a")
        self.root.option_add("*TCombobox*Listbox.highlightBackground", card)
        self.root.option_add("*TCombobox*Listbox.highlightColor", "#2a2f3a")

        # ---- header --------------------------------------------------------
        top = ttk.Frame(self.root, padding=(12, 10, 12, 2))
        top.pack(fill="x")
        ttk.Label(top, text="SyncPlayer", style="Head.TLabel").pack(side="left")
        ttk.Label(top, text="two videos · two windows · auto-sync",
                  style="Dim.TLabel").pack(side="left", padx=(10, 0), pady=(4, 0))
        self.btn_help = ttk.Button(top, text="❓ Help", width=7, command=self._show_help)
        self.btn_help.pack(side="right")
        Tooltip(self.btn_help, "Open the full guide: sync workflow, PiP modes, tracks, shortcuts.")

        body = ttk.Frame(self.root, padding=(12, 4, 12, 8))
        body.pack(fill="both", expand=True)

        # ---- sources -------------------------------------------------------
        src = ttk.LabelFrame(body, text=" Sources ", padding=8)
        src.pack(fill="x", pady=(0, 8))

        row = ttk.Frame(src)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Movie / source A", width=16).pack(side="left")
        ttk.Entry(row, textvariable=self.movie_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", width=9, command=lambda: self._browse(0)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="URL…", width=7, command=lambda: self._url(0)).pack(side="left", padx=(4, 0))

        row = ttk.Frame(src)
        row.pack(fill="x", pady=1)
        ttk.Label(row, text="Reaction / source B", width=16).pack(side="left")
        ttk.Entry(row, textvariable=self.react_path).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Browse…", width=9, command=lambda: self._browse(1)).pack(side="left", padx=(6, 0))
        ttk.Button(row, text="URL…", width=7, command=lambda: self._url(1)).pack(side="left", padx=(4, 0))
        ttk.Button(row, text="⇄ Swap", width=7, command=self._swap).pack(side="left", padx=(4, 0))

        # ---- transport ------------------------------------------------------
        trans = ttk.LabelFrame(body, text=" Transport ", padding=(8, 6))
        trans.pack(fill="x", pady=(0, 8))

        self.btn_play = ttk.Button(trans, text="Start", style="Accent.TButton",
                                   width=10, command=self._start)
        self.btn_play.pack(side="left")
        Tooltip(self.btn_play, "Load both videos PAUSED (windows appear side by side). Press Play or Space when ready.")
        self.btn_pause_all = ttk.Button(trans, text="▶  Play", width=8,
                                        command=self._toggle_pause_m)
        self.btn_pause_all.pack(side="left", padx=(5, 0))
        Tooltip(self.btn_pause_all, "Play or pause BOTH videos together (also on the Master row).")
        self._ctrls.append(self.btn_pause_all)
        for txt, cmd, w, tip in (("⏮ Restart", lambda: self._seek(0), 9,
                                  "Jump both videos back to the start"),
                                 ("⏪ 10s", lambda: self._jump(-10), 7,
                                  "Both videos back 10 s (← = 5 s)"),
                                 ("10s ▶", lambda: self._jump(10), 7,
                                  "Both videos forward 10 s (→ = 5 s)"),
                                 ("📷 Shot", self._shot, 8,
                                  "Save screenshots of both videos"),
                                 ("Close", self._stop, 7,
                                  "Close both video windows")):
            b = ttk.Button(trans, text=txt, width=w, command=cmd)
            b.pack(side="left", padx=(5, 0))
            Tooltip(b, tip)
            self._ctrls.append(b)

        sp = ttk.Frame(trans)
        sp.pack(side="right")
        ttk.Label(sp, text="Speed").pack(side="left")
        stxt = ttk.Entry(sp, textvariable=self.speed_str, width=7, justify="center")
        Tooltip(stxt, "Playback speed of BOTH videos - type e.g. 1.35 and press Enter (range 0.25 - 2.5).")
        stxt.bind("<Return>", lambda e: self._apply_speed_entry())
        stxt.bind("<FocusOut>", lambda e: self._apply_speed_entry())
        stxt.pack(side="left", padx=(6, 0))
        self.speed_entry = stxt
        self._ctrls.append(stxt)
        for txt, d, tip in (("−", -0.05, "Slower by 0.05"), ("+", 0.05, "Faster by 0.05")):
            b = ttk.Button(sp, text=txt, width=3, command=lambda d=d: self._nudge_speed(d))
            b.pack(side="left", padx=(4, 0))
            Tooltip(b, tip)
            self._ctrls.append(b)
        ttk.Label(sp, text="Jump:").pack(side="left", padx=(12, 4))
        self.jump_entry = ttk.Entry(sp, textvariable=self.jump_sec, width=4, justify="center")
        self.jump_entry.pack(side="left")
        Tooltip(self.jump_entry, "Arrow key jump distance in seconds (customizable).")
        ttk.Label(sp, text="s").pack(side="left", padx=(2, 0))
        self._ctrls.append(self.jump_entry)

        # ---- timelines ------------------------------------------------------
        tl = ttk.LabelFrame(body, text=" Timelines ", padding=(8, 4))
        tl.pack(fill="x", pady=(0, 8))

        def make_tl(parent, label, tip, tag=None):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=label, width=9, style="Dim.TLabel").pack(side="left")
            slider = ttk.Scale(row, from_=0, to=600)
            slider.pack(side="left", fill="x", expand=True, padx=(4, 8))
            if tag:
                fb = ttk.Button(row, text="⏴", width=3,
                                command=lambda t=tag: self._step_frame(t, back=True))
                fb.pack(side="left", padx=(0, 2))
                Tooltip(fb, "Step THIS video back one frame (pause first; also the [ key).")
                ff = ttk.Button(row, text="⏵", width=3,
                                command=lambda t=tag: self._step_frame(t, back=False))
                ff.pack(side="left", padx=(0, 4))
                Tooltip(ff, "Step THIS video forward one frame (pause first; also the ] key).")
                self._fstep_btns.extend([fb, ff])
                self._ctrls.extend([fb, ff])
            nbtn = ttk.Button(row, text="▶", width=3,
                              command=lambda t=tag: self._toggle_play_one(t))
            nbtn.pack(side="left", padx=(0, 4))
            Tooltip(nbtn, "Play/pause THIS video only - the other one keeps going (handy before you lock the sync).")
            self._ctrls.append(nbtn)
            if tag:
                gt_frame = ttk.Frame(row)
                gt_frame.pack(side="right", padx=(4, 2))
                ge = ttk.Entry(gt_frame, textvariable=self.goto_vars[tag], width=8)
                ge.pack(side="left")
                ge.bind("<Return>", lambda e, t=tag: self._on_goto_single(t))
                Tooltip(ge, "Jump %s alone to typed time: seconds, MM:SS, or HH:MM:SS." % label.rstrip(":"))
                gb = ttk.Button(gt_frame, text="Go", width=3,
                                command=lambda t=tag: self._on_goto_single(t))
                gb.pack(side="left", padx=(2, 0))
                Tooltip(gb, "Jump %s alone to the typed timecode." % label.rstrip(":"))
                self._ctrls.extend([ge, gb])
            lbl = ttk.Label(row, text="00:00 / --:--", width=24, anchor="e")
            lbl.pack(side="right")
            self._ctrls.append(slider)
            Tooltip(slider, tip)
            return slider, lbl, nbtn

        self.seek_a, self.lbl_a, self.btn_play_a = make_tl(tl, "Movie:", "Drag to seek the movie ONLY. This is how you align it to the reaction.", "A")
        self.seek_b, self.lbl_b, self.btn_play_b = make_tl(tl, "Reaction:", "Drag to seek the reaction ONLY. This is how you align it to the movie.", "B")
        # Master row: label + Sync-Lock toggle + play button, then the bar
        mrow = ttk.Frame(tl)
        mrow.pack(fill="x", pady=2)
        mlab = ttk.Frame(mrow)
        mlab.pack(side="left")
        ttk.Label(mlab, text="Master:", width=9, style="Dim.TLabel").pack(side="left")
        self.btn_lock = ttk.Button(mlab, text="🔒 Lock sync", width=11,
                                   command=self._toggle_lock)
        self.btn_lock.pack(side="left", padx=(2, 0))
        Tooltip(self.btn_lock, "Lock the alignment: per-video bars switch off, the Master bar drives BOTH videos, and drift correction gets stricter.")
        self._ctrls.append(self.btn_lock)
        self.btn_play_m = ttk.Button(mlab, text="▶", width=3,
                                     command=self._toggle_pause_m)
        self.btn_play_m.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_play_m, "Play or pause BOTH videos together (master play button).")
        self._ctrls.append(self.btn_play_m)
        self.seek_m = ttk.Scale(mrow, from_=0, to=600)
        self.seek_m.pack(side="left", fill="x", expand=True, padx=(4, 8))
        # editable timecode: type a target (90 / 83:45 / 1:23:45) and hit Enter
        # or Go to seek BOTH videos there (no scrubbing needed)
        goto = ttk.Frame(mrow)
        goto.pack(side="right", padx=(0, 6))
        self.goto_var = tk.StringVar()
        ge = ttk.Entry(goto, textvariable=self.goto_var, width=9)
        ge.pack(side="left")
        ge.bind("<Return>", lambda e: self._on_goto())
        Tooltip(ge, "Jump both videos to a typed time: seconds, MM:SS or HH:MM:SS (Enter/Go).")
        gb = ttk.Button(goto, text="Go", width=3, command=self._on_goto)
        gb.pack(side="left", padx=(2, 0))
        Tooltip(gb, "Jump both videos to the typed timecode.")
        self._ctrls.extend([ge, gb])
        self.lbl_m = ttk.Label(mrow, text="00:00 / --:--", width=20, anchor="e")
        self.lbl_m.pack(side="right")
        self._ctrls.append(self.seek_m)
        Tooltip(self.seek_m, "Locked out until you engage Lock Sync \u2014 then this bar drives BOTH videos together, keeping their alignment. Its play button toggles both videos at any time.")
        self.seek_m.state(["disabled"])   # invisible to input until Sync Lock

        self.seek_a.config(command=self._on_seek_a_drag)
        self.seek_b.config(command=self._on_seek_b_drag)
        self.seek_m.config(command=self._on_seek_m_drag)

        self.seek_a.bind("<Button-1>", lambda e: self._handle_click(e, self.seek_a, "dragging_seek_a", "_seek_a_val", self._on_seek_a_release))
        self.seek_b.bind("<Button-1>", lambda e: self._handle_click(e, self.seek_b, "dragging_seek_b", "_seek_b_val", self._on_seek_b_release))
        self.seek_m.bind("<Button-1>", lambda e: self._handle_click(e, self.seek_m, "dragging_seek_m", "_seek_m_val", self._on_seek_m_release))
        # Releases over the WIDGET (a release over an mpv video window would
        # otherwise leave the dragging_* flags stuck and freeze that bar).
        self._bind_release(self.seek_a, "dragging_seek_a", self._on_seek_a_release)
        self._bind_release(self.seek_b, "dragging_seek_b", self._on_seek_b_release)
        self._bind_release(self.seek_m, "dragging_seek_m", self._on_seek_m_release)

        # ---- tracks (audio + subtitle pickers per video) -------------------
        tr = ttk.Frame(body)
        tr.pack(fill="x", pady=(0, 8))
        self.combo_audio = {}
        self.combo_sub = {}
        self._track_opts = {"A": [], "B": []}
        for col, (label, tag) in enumerate((("Movie", "A"), ("Reaction", "B"))):
            f = ttk.LabelFrame(tr, text=" %s tracks " % label, padding=6)
            f.grid(row=0, column=col, sticky="ew", padx=(0, 8) if col == 0 else (0, 0))
            tr.columnconfigure(col, weight=1)
            ttk.Label(f, text="Audio:").pack(side="left")
            ca = ttk.Combobox(f, state="readonly", width=16)
            ca.pack(side="left", padx=(4, 10))
            ca.bind("<<ComboboxSelected>>", lambda e, t=tag: self._on_track_sel(t, "a"))
            Tooltip(ca, "Audio track of the %s video (from the file's audio streams)." % label)
            self.combo_audio[tag] = ca
            self._ctrls.append(ca)
            ttk.Label(f, text="Subtitles:").pack(side="left")
            cs = ttk.Combobox(f, state="readonly", width=16)
            cs.pack(side="left", padx=(4, 0))
            cs.bind("<<ComboboxSelected>>", lambda e, t=tag: self._on_track_sel(t, "s"))
            Tooltip(cs, "Subtitle track of the %s video (Off disables subtitles)." % label)
            self.combo_sub[tag] = cs
            self._ctrls.append(cs)

        # ---- windows & PiP -------------------------------------------------
        win = ttk.LabelFrame(body, text=" Windows & PiP ", padding=8)
        win.pack(fill="x", pady=(0, 8))
        b = ttk.Button(win, text="⇦⇨ Arrange", width=12, command=self._arrange_windows)
        b.pack(side="left")
        Tooltip(b, "Put the two video windows next to each other on the screen.")
        self._ctrls.append(b)
        b_rst = ttk.Button(win, text="↺ Reset PiP", width=11, command=self._reset_pip)
        b_rst.pack(side="left", padx=(4, 0))
        Tooltip(b_rst, "Reset all windows: exit any PiP mode and restore both video windows with normal borders side by side.")
        self._ctrls.append(b_rst)
        self.btn_pip_a = ttk.Button(win, text="⧉ PiP Movie", width=12,
                                    command=lambda: self._toggle_pip("A"))
        self.btn_pip_a.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_pip_a, "Picture-in-picture: Movie becomes borderless, always on top, still resizable (drag its edges).")
        self._ctrls.append(self.btn_pip_a)
        self.btn_pip_b = ttk.Button(win, text="⧉ PiP Reaction", width=12,
                                    command=lambda: self._toggle_pip("B"))
        self.btn_pip_b.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_pip_b, "Picture-in-picture: Reaction becomes borderless, always on top, still resizable (drag its edges).")
        self._ctrls.append(self.btn_pip_b)
        self.btn_pip_mode_a = ttk.Button(win, text="▦ Movie in Reaction", width=17,
                                         command=lambda: self._toggle_pip_int("A"))
        self.btn_pip_mode_a.pack(side="left", padx=(6, 0))
        Tooltip(self.btn_pip_mode_a, "Integrated PiP: embed the Movie INSIDE the Reaction window, draggable over it (needs Sync Lock).")
        self._ctrls.append(self.btn_pip_mode_a)
        self.btn_pip_mode_b = ttk.Button(win, text="▦ Reaction in Movie", width=17,
                                         command=lambda: self._toggle_pip_int("B"))
        self.btn_pip_mode_b.pack(side="left", padx=(4, 0))
        Tooltip(self.btn_pip_mode_b, "Integrated PiP: embed the Reaction INSIDE the Movie window, draggable over it (needs Sync Lock).")
        self._ctrls.append(self.btn_pip_mode_b)
        # X/Y arrows: move the embedded pane inside the host feed
        pip_pos = ttk.Frame(win)
        pip_pos.pack(side="left", padx=(12, 0))
        ttk.Label(pip_pos, text="PiP move:", style="Dim.TLabel").pack(side="left")
        self._pip_xy_btns = []
        for txt, dx, dy, tip in (("◀", -1, 0, "Move embedded PiP LEFT (Hold Shift for 1px fine-tune)"),
                                 ("▲", 0, -1, "Move embedded PiP UP (Hold Shift for 1px fine-tune)"),
                                 ("▼", 0, 1, "Move embedded PiP DOWN (Hold Shift for 1px fine-tune)"),
                                 ("▶", 1, 0, "Move embedded PiP RIGHT (Hold Shift for 1px fine-tune)")):
            b = ttk.Button(pip_pos, text=txt, width=3,
                           command=lambda dx=dx, dy=dy: self._pip_move(dx, dy, fine=False))
            b.pack(side="left", padx=(1, 0))
            Tooltip(b, tip)
            b.bind("<Shift-Button-1>", lambda e, dx=dx, dy=dy: (self._pip_move(dx, dy, fine=True), "break")[1])
            self._pip_xy_btns.append(b)
            self._ctrls.append(b)
        # PiP size: scale the embedded pane bigger/smaller
        ttk.Label(pip_pos, text="PiP size:", style="Dim.TLabel").pack(side="left", padx=(10, 0))
        self._pip_sz_btns = []
        for txt, d, tip in (("\u2212", -1, "Make the embedded PiP pane SMALLER"),
                            ("+", 1, "Make the embedded PiP pane BIGGER")):
            b = ttk.Button(pip_pos, text=txt, width=3,
                           command=lambda d=d: self._pip_resize(d))
            b.pack(side="left", padx=(1, 0))
            Tooltip(b, tip)
            self._pip_sz_btns.append(b)
            self._ctrls.append(b)

        # ---- crop (manual black-bar removal) ------------------------------
        crop = ttk.LabelFrame(body, text=" Crop (remove black bars) ", padding=8)
        crop.pack(fill="x", pady=(0, 8))
        crow = ttk.Frame(crop)
        crow.pack(fill="x")
        ttk.Label(crow, text="Crop:", style="Dim.TLabel").pack(side="left")
        self._crop_tag_btns = {}
        for txt, tag in (("Movie", "A"), ("Reaction", "B")):
            b = ttk.Button(crow, text=txt, width=8,
                           command=lambda t=tag: self._on_crop_tag(t))
            b.pack(side="left", padx=(2, 0))
            self._crop_tag_btns[tag] = b
            self._ctrls.append(b)
        self._crop_tag_btns["A"].state(["pressed"])
        b = ttk.Button(crow, text="✂ Visual Crop", width=13, command=self._crop_interactive)
        b.pack(side="left", padx=(8, 0))
        Tooltip(b, "Interactive visual crop: drag a box on a video frame snapshot.")
        self._ctrls.append(b)
        b = ttk.Button(crow, text="\u2716 Clear", width=8, command=self._crop_clear)
        b.pack(side="left", padx=(6, 0))
        Tooltip(b, "Remove crop and return the video to its full resolution.")
        self._ctrls.append(b)
        erow = ttk.Frame(crop)
        erow.pack(fill="x", pady=(5, 0))
        ttk.Label(erow, text="Edges:", style="Dim.TLabel").pack(side="left")
        for edge, lbl in (("top", "Top"), ("bottom", "Bottom"),
                          ("left", "Left"), ("right", "Right")):
            ttk.Label(erow, text=lbl, style="Dim.TLabel").pack(side="left", padx=(8, 0))
            for txt, d in (("\u2212", -1), ("+", 1)):
                b = ttk.Button(erow, text=txt, width=3,
                               command=lambda e=edge, d=d: self._crop_nudge(e, d))
                b.pack(side="left", padx=(1, 0))
                Tooltip(b, "Crop the %s edge %s (remove black bars)." % (lbl, "less" if d < 0 else "more"))
                self._ctrls.append(b)

        # ---- volumes -------------------------------------------------------
        vol = ttk.Frame(body)
        vol.pack(fill="x", pady=(0, 8))

        self.vol_sliders = []
        rows = [("Movie volume", self.vol_a, 0, "A"), ("Reaction volume", self.vol_b, 1, "B")]
        for i, (label, var, idx, tag) in enumerate(rows):
            f = ttk.LabelFrame(vol, text=" %s " % label, padding=6)
            f.grid(row=0, column=i, sticky="ew", padx=(0, 8))
            vol.columnconfigure(i, weight=1)
            s = ttk.Scale(f, from_=0, to=150, variable=var,
                          command=lambda v, j=idx: self._on_vol_drag(j))
            s.pack(fill="x")
            s.bind("<ButtonRelease-1>", lambda e, j=idx: self._on_vol_release(j))
            Tooltip(s, "Volume of this video only — the other one is untouched.")
            self.vol_sliders.append(s)
            self._ctrls.append(s)
            r2 = ttk.Frame(f)
            r2.pack(fill="x", pady=(2, 0))
            self.vol_lbls = getattr(self, "vol_lbls", [])
            lbl = ttk.Label(r2, text="100 %", width=8)
            lbl.pack(side="left")
            b = ttk.Button(r2, text="Mute", width=6,
                           command=lambda j=idx: self._mute(j))
            b.pack(side="right")
            Tooltip(b, "Mute / unmute this video")
            self._ctrls.append(b)
            self.vol_lbls.append(lbl)

        fm = ttk.LabelFrame(vol, text=" Master volume ", padding=6)
        fm.grid(row=0, column=2, sticky="ew")
        vol.columnconfigure(2, weight=1)
        s2 = ttk.Scale(fm, from_=0, to=150, variable=self.vol_m,
                       command=lambda v: self._on_master_drag())
        s2.pack(fill="x")
        s2.bind("<ButtonRelease-1>", lambda e: setattr(self, "dragging_master", False))
        Tooltip(s2, "Overall volume: scales BOTH videos together.")
        self.master_lbl = ttk.Label(fm, text="100 %", width=8)
        self.master_lbl.pack(anchor="w", pady=(2, 0))
        self._ctrls.append(s2)

        # ---- status --------------------------------------------------------
        self.status_lbl = ttk.Label(body, text="Ready. Pick two files (or URLs), then press Start.",
                                    style="Dim.TLabel", wraplength=1100, justify="left")
        self.status_lbl.pack(fill="x", pady=(2, 0))
        self.state_lbl = ttk.Label(body, text="Shortcuts: Space ⏯ · ←/→ ±5 s seek · [ ] frame step (last video) · Esc undocks PiP",
                                   style="Dim.TLabel")
        self.state_lbl.pack(fill="x")

        self.root.bind("<space>", lambda e: self._toggle_play())
        self.root.bind("<Left>", lambda e: None if self._pip_nudge(-15, 0) else self._jump(-self._get_jump_sec()))
        self.root.bind("<Right>", lambda e: None if self._pip_nudge(15, 0) else self._jump(self._get_jump_sec()))
        self.root.bind("<Up>", lambda e: self._pip_nudge(0, -15))
        self.root.bind("<Down>", lambda e: self._pip_nudge(0, 15))
        self.root.bind("<Shift-Left>", lambda e: self._pip_nudge(-1, 0))
        self.root.bind("<Shift-Right>", lambda e: self._pip_nudge(1, 0))
        self.root.bind("<Shift-Up>", lambda e: self._pip_nudge(0, -1))
        self.root.bind("<Shift-Down>", lambda e: self._pip_nudge(0, 1))
        self.root.bind("<bracketleft>", lambda e: self._step_frame(self._last_active, back=True))
        self.root.bind("<bracketright>", lambda e: self._step_frame(self._last_active, back=False))
        self.root.bind("<Escape>", lambda e: self._undock_pip_int() if self.pip_int else None)
        self.root.bind("<plus>", lambda e: None if self._pip_resize(1) else None)
        self.root.bind("<minus>", lambda e: None if self._pip_resize(-1) else None)
        self.root.bind("<KP_Add>", lambda e: None if self._pip_resize(1) else None)
        self.root.bind("<KP_Subtract>", lambda e: None if self._pip_resize(-1) else None)
        self.root.bind("<Button-1>", self._on_global_press)
        self.root.bind("<ButtonRelease-1>", self._on_root_release)

        # Size the window to exactly fit its content, then lock it.
        self.root.update_idletasks()
        width = max(900, self.root.winfo_reqwidth())
        height = max(640, self.root.winfo_reqheight())
        self.root.geometry("%dx%d" % (width, height))
        self.root.resizable(False, False)
# ------------------------------------------------------------- actions --
    def _browse(self, idx):
        p = filedialog.askopenfilename(
            title="Pick a video" if idx == 0 else "Pick the reaction video",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.webm *.avi *.ts *.m2ts *.flv *.wmv *.m4v"),
                       ("All files", "*.*")])
        if p:
            if idx == 0:
                self.movie_path.set(p)
            else:
                self.react_path.set(p)

    def _url(self, idx):
        cur = self.movie_path.get() if idx == 0 else self.react_path.get()
        win = tk.Toplevel(self.root)
        win.title("Paste URL")
        win.configure(bg="#16181d")
        win.geometry("520x120")
        win.transient(self.root)
        ttk.Label(win, text="YouTube / any URL (resolved via yt-dlp):").pack(pady=(12, 4))
        e = ttk.Entry(win, width=70)
        e.insert(0, cur if cur.startswith("http") else "")
        e.pack(padx=12)
        e.focus_set()

        def ok():
            v = e.get().strip()
            if v:
                if idx == 0:
                    self.movie_path.set(v)
                else:
                    self.react_path.set(v)
            win.destroy()

        def ok_enter(event):
            ok()
        e.bind("<Return>", ok_enter)
        ttk.Button(win, text="Use this URL", command=ok).pack(pady=8)

    def _swap(self):
        a, b = self.movie_path.get(), self.react_path.get()
        self.movie_path.set(b)
        self.react_path.set(a)
        if self.started:
            self._start()

    def _on_drop(self, event):
        """One or more files dropped onto the window -> fill the source slots.

        tkinterdnd2 sends a space-separated list; each path is wrapped in
        {braces} (or quotes) when it contains spaces — parse all forms.
        """
        try:
            raw = event.data or ""
            paths = []
            i2, n = 0, len(raw)
            while i2 < n:
                c = raw[i2]
                if c in " 	":
                    i2 += 1
                    continue
                if c == "{":
                    j2 = raw.find("}", i2 + 1)
                    if j2 == -1:
                        j2 = n
                    paths.append(raw[i2 + 1:j2])
                    i2 = j2 + 1
                elif c == '"':
                    j2 = raw.find('"', i2 + 1)
                    if j2 == -1:
                        j2 = n
                    paths.append(raw[i2 + 1:j2])
                    i2 = j2 + 1
                else:
                    j2 = raw.find(" ", i2)
                    if j2 == -1:
                        j2 = n
                    seg2 = raw[i2:j2]
                    if seg2:
                        paths.append(seg2)
                    i2 = j2 + 1
            paths = [p.strip() for p in paths if p.strip()]
            if not paths:
                return
            # fill empty slots first, in order A then B
            loaded = 0
            if not self.movie_path.get().strip():
                self.movie_path.set(paths.pop(0))
                loaded += 1
            if not self.react_path.get().strip():
                if paths:
                    self.react_path.set(paths.pop(0))
                    loaded += 1
            both = bool(self.movie_path.get().strip() and self.react_path.get().strip())
            self.status_lbl.config(
                text="Loaded %d file(s)%s" % (loaded, " — press ▶ Play to start." if both else ""))
        except Exception:
            pass


    def _set_running_ui(self, playing):
        state = "normal" if playing else "disabled"
        for w in self._ctrls:
            try:
                w.config(state=state)
            except Exception:
                pass
        try:
            self.btn_play.config(state="normal")   # Start always available
        except Exception:
            pass
        # the transport sweep resets combos to editable (state=normal);
        # restore their readonly behaviour, and keep the frame-step buttons
        # following the running state
        if playing:
            for tag in ("A", "B"):
                for cb in (self.combo_audio.get(tag), self.combo_sub.get(tag)):
                    try:
                        cb.config(state="readonly")
                    except Exception:
                        pass
        for b in getattr(self, "_fstep_btns", []):
            try:
                b.config(state=state)
            except Exception:
                pass
        # the master bar only ever works under Sync Lock: re-assert it
        # after the generic enable/disable sweep above
        if playing and not self.sync_locked:
            self.seek_m.state(["disabled"])

    def _start(self):
        a = self.movie_path.get().strip()
        b = self.react_path.get().strip()
        if not a:
            messagebox.showwarning(APP_NAME, "Pick a movie / source A first.")
            return
        if not b:
            messagebox.showwarning(APP_NAME, "Pick a reaction / source B too (both videos are needed).")
            return
        for t in ("A", "B"):
            p = self.players.get(t)
            if p:
                p.quit()
                time.sleep(0.15)
        self.btn_play.config(state="disabled")
        self.status_lbl.config(text="Starting…")
        self.root.update_idletasks()

        # Decide how each source is fed to mpv.
        ra = a
        rb = b
        if a.startswith("http") and not is_youtube(a):
            ra = resolve_url(a)
        if b.startswith("http") and not is_youtube(b):
            rb = resolve_url(b)
        # YouTube URLs are passed straight to mpv (--ytdl=yes).

        try:
            self.players["A"] = MpvDriver(ra, "A", on_pause=self._on_player_pause,
                                          on_exit=self._on_exit,
                                          start_paused=True)
            self.players["B"] = MpvDriver(rb, "B", on_pause=self._on_player_pause,
            on_exit=self._on_exit, start_paused=True)
        except MpvNotFoundError as _e:
            self.btn_play.config(state="normal")
            self.status_lbl.config(text="mpv not found")
            messagebox.showerror(APP_NAME, str(_e))
            return
        self.started = True
        self.paused = True   # loaded PAUSED: Play/Space starts both videos
        self.sync_off = 0.0
        self.last_pos = {"A": None, "B": None}
        self.last_dur = {"A": None, "B": None}
        self._status_time = {"A": 0.0, "B": 0.0}
        self._beacon_ts = {"A": 0.0, "B": 0.0}
        self._seek_grace_until = 0.0
        self._last_corr = {"A": 0.0, "B": 0.0}
        self._set_running_ui(True)
        self.btn_pause_all.config(state="normal", text="▶  Play")
        self.btn_play_m.config(state="normal", text="▶")
        self.status_lbl.config(
            text="Loaded PAUSED. Press Play (or Space) when ready.")
        self.root.after(1500, self._refresh_tracks)
        self.root.after(4000, self._refresh_tracks)
        self.root.after(2500, self._fetch_meta)
        if is_youtube(ra):
            self.root.after(2500, lambda: self._list_yt_subs("A"))
        if is_youtube(rb):
            self.root.after(2500, lambda: self._list_yt_subs("B"))
        self._apply_volumes()
        self._apply_speed()
        self._srcs = {"A": ra, "B": rb}
        # Crop is active-session only: reset on start (not persistent across sessions/restarts)
        self._manual_crop = {"A": None, "B": None}
        self._pip_crop_saved = {"A": None, "B": None}
        # arrange the two mpv windows side by side once they appear
        threading.Thread(target=self._arrange_thread, daemon=True).start()

    def _arrange_thread(self):
        """Find both mpv windows, then place them side by side (half screen each)."""
        try:
            wa, ha = screen_size()
            half = wa // 2
            gap = 4
            titles = {"A": "SyncPlayer — Movie", "B": "SyncPlayer — Reaction"}
            handles = {"A": None, "B": None}
            for _ in range(160):  # up to ~40 s (URLs take a while)
                done = True
                for t in ("A", "B"):
                    p = self.players.get(t)
                    if not p or not p.running:
                        continue
                    if handles[t] is None:
                        handles[t] = find_mpv_window(p.proc.pid, titles[t])
                    if handles[t] is None:
                        done = False
                if done:
                    break
                if not self.started:
                    return
                time.sleep(0.25)
            if handles["A"]:
                place_window(handles["A"], 0, 0, half - gap, ha - 100)
            if handles["B"]:
                place_window(handles["B"], half + gap, 0, half - gap, ha - 100)
            for t in ("A", "B"):
                p = self.players.get(t)
                if p and handles[t]:
                    p.hwnd = handles[t]
        except Exception:
            pass

    def _arrange_windows(self):
        threading.Thread(target=self._arrange_thread, daemon=True).start()

    def _on_player_exit(self, tag):
        """Handle exit of player `tag` without killing the other video feed (crash protection)."""
        name = "Movie" if tag == "A" else "Reaction"
        other_tag = "B" if tag == "A" else "A"
        other_name = "Reaction" if tag == "A" else "Movie"
        other_p = self.players.get(other_tag)
        p = self.players.get(tag)
        if p:
            p.stopped.set()
        self.players[tag] = None
        self.last_pos[tag] = None

        if other_p and other_p.running:
            other_p.set_pause(True)
            self.paused = True
            self.btn_play_m.config(text="▶")
            self.btn_pause_all.config(text="▶  Play")
            self.btn_play.config(state="normal", text="Start")
            src = self._srcs.get(tag, "")
            is_yt = is_youtube(src) or src.startswith("http")
            self._status_pin = time.monotonic() + 10.0
            if is_yt:
                self.status_lbl.config(
                    text="%s (YouTube stream) failed or closed. %s paused & kept open." % (name, other_name))
                if "--smoke" not in sys.argv and not getattr(sys, "_TEST_MODE", False):
                    try:
                        messagebox.showwarning(
                            APP_NAME,
                            "The YouTube stream for %s failed to load or closed unexpectedly.\n\n"
                            "URL: %s\n\n"
                            "The %s video has been paused and kept open.\n"
                            "You can adjust the URL and press Start again." % (name, src, other_name))
                    except Exception:
                        pass
            else:
                self.status_lbl.config(
                    text="%s window closed. %s is paused and kept open." % (name, other_name))
        else:
            self._stop()

    def _stop(self):
        if self.pip_int:
            try:
                self._undock_pip_int()
            except Exception:
                pass
        for t in ("A", "B"):
            p = self.players.get(t)
            if p:
                p.quit()
        self.players = {"A": None, "B": None}
        self.started = False
        self.paused = True
        self._set_running_ui(False)
        self.btn_play.config(text="Start")
        self.btn_play_m.config(text="▶")
        self.btn_pause_all.config(text="▶  Play")
        self.status_lbl.config(text="Closed. Press Start to load again.")
        for lbl in (self.lbl_a, self.lbl_b, self.lbl_m):
            lbl.config(text="00:00 / --:--")

    # -- transport ----------------------------------------------------------
    def _toggle_play(self):
        # Space handler: a pure play/pause of BOTH videos. Loading is the
        # Start button only; if nothing is loaded there is nothing to toggle.
        if not self.started:
            return
        self._set_pause_all(not self.paused)

    def _set_pause_all(self, pause):
        self._pause_cmd_ts = time.monotonic()
        self.paused = bool(pause)
        for t in ("A", "B"):
            p = self.players.get(t)
            if p and p.running:
                p.set_pause(pause)
        self.btn_play_m.config(text="▶" if pause else "⏸")
        self.btn_pause_all.config(text="▶  Play" if pause else "⏸  Pause")

    def _on_player_pause(self, tag, packed):
        """A pause event arrived from one player.

        Click-to-pause (via the video window) mirrors to BOTH, so they never
        fight and the sync stays locked. BUT an END-OF-FILE auto-pause (mpv
        --keep-open pauses on the last frame) only pauses THAT video — the
        other one must keep playing, or the movie would freeze whenever the
        (usually shorter) reaction ends."""
        value, ts = packed
        p = self.players.get(tag)
        if value == "eof":
            if p:
                p.at_end = True
            return  # EOF auto-pause: do not mirror, do not unpause
        if ts - self._pause_cmd_ts < 0.35:
            return  # our own command's echo (mpv flaps false+true) or stale
        if time.monotonic() - self._frame_step_ts < 1.0:
            return  # flutter around a frame step: never mirror, never unpause
        if value and p and p.at_end:
            return  # EOF auto-pause: do not mirror
        if value != self.paused:
            self.root.after(0, lambda: self._set_pause_all(value))

    def _on_exit(self):
        self.root.after(0, lambda: self.status_lbl.config(text="A video window was closed."))

    def _seek(self, pos):
        """Restart: both videos to `pos` with their alignment intact."""
        if not self.started:
            return
        for t in ("A", "B"):
            p = self.players.get(t)
            if p and p.running:
                p.seek(max(0.0, pos))
                self._commit_seek(t, pos)
        if pos == 0:
            self.sync_off = 0.0          # restart resets the alignment
            for t in ("A", "B"):         # and resumes playback from the top
                p = self.players.get(t)
                if p:
                    p.at_end = False
            self._set_pause_all(False)

    def _jump(self, delta):
        """Jump both videos by delta seconds (keeps their alignment)."""
        if not self.started:
            return
        for t in ("A", "B"):
            p = self.players.get(t)
            if p and p.running:
                p.seek(delta, absolute=False)
        # local bookkeeping so bars don't jump mid-flight
        for t in ("A", "B"):
            if self.last_pos[t] is not None:
                self._commit_seek(t, max(0.0, self.last_pos[t] + delta))

    def _get_jump_sec(self):
        try:
            val = float(self.jump_sec.get())
            if val > 0:
                return val
        except Exception:
            pass
        return 5.0

    def _on_goto_single(self, tag):
        """Jump THIS video alone to typed timecode, adjusting sync offset so other stays put."""
        if not self.started:
            return
        p = self.players.get(tag)
        if not (p and p.running):
            return
        txt = self.goto_vars[tag].get().strip()
        if not txt:
            return
        pos = MpvDriver._to_seconds(txt)
        name = "Movie" if tag == "A" else "Reaction"
        if pos is None or pos < 0:
            self.status_lbl.config(text="Enter a timecode for %s like 1:23:45, 83:45 or 90." % name)
            return
        dur = self.last_dur.get(tag)
        if dur:
            pos = max(0.0, min(pos, dur))
        p.seek(pos, exact=True)
        self._commit_seek(tag, pos)
        now = time.monotonic()
        other_tag = "B" if tag == "A" else "A"
        other_pos = self._est_pos(other_tag, now)
        if other_pos is not None:
            if tag == "A":
                self.sync_off = other_pos - pos
            else:
                self.sync_off = pos - other_pos
        self.goto_vars[tag].set("")
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(text="Seek %s to %s." % (name, self._fmt(pos, dur)))

    def _reset_pip(self):
        """Reset all video windows back to normal bordered windows side by side."""
        if self.pip_int:
            self._undock_pip_int()
        u = ctypes.windll.user32
        for tag in ("A", "B"):
            p = self.players.get(tag)
            if p and p.running:
                p.cmd({"command": ["set_property", "ontop", "no"]})
                p.cmd({"command": ["set_property", "border", "yes"]})
                p.cmd({"command": ["set_property", "window-dragging", "yes"]})
                if p.hwnd:
                    saved = self._pip_saved.get(tag) or (0x10000000 | 0x00CF0000)
                    u.SetWindowLongPtrW(p.hwnd, -16, saved if saved < 0x80000000 else saved - 0x100000000)
                    u.SetWindowPos(p.hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
                    self._pip_saved[tag] = None
            self.pip[tag] = False
        self._arrange_windows()
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(text="Reset PiP: both video windows restored with normal borders.")

    def _on_goto(self):
        """Jump both videos to a typed timecode (seconds / MM:SS / HH:MM:SS)."""
        if not self.started:
            return
        txt = self.goto_var.get().strip()
        if not txt:
            return
        try:
            pos = MpvDriver._to_seconds(txt)
        except Exception:
            pos = None
        if pos is None or pos < 0:
            self.status_lbl.config(text="Enter a timecode like 1:23:45, 83:45 or 90.")
            return
        durs = [d for d in (self.last_dur.get("A"), self.last_dur.get("B")) if d]
        if durs:
            pos = min(pos, max(durs))
        self._seek(pos)
        self.goto_var.set("")
        self.status_lbl.config(text="Seek to %s." % self._fmt(pos, None))

    # -- help / tracks / master play ----------------------------------------
    def _show_help(self):
        help_w = getattr(self, "_help_win", None)
        try:
            if help_w is not None and help_w.winfo_exists():
                help_w.deiconify()
                help_w.lift()
                return
        except Exception:
            pass
        w = tk.Toplevel(self.root)
        self._help_win = w
        w.title("SyncPlayer Help")
        w.configure(bg="#16181d")
        w.resizable(False, False)
        txt = (
            "HOW SYNC WORKS" + chr(10) +
            "1.  Start loads both videos PAUSED, each in its own window." + chr(10) +
            "2.  Play (or Space, or the master play button) starts BOTH." + chr(10) +
            "3.  Drag the Movie or Reaction bar until the moments line" + chr(10) +
            "    up - that video moves on its own." + chr(10) +
            "4.  Lock sync captures the alignment: per-video bars switch" + chr(10) +
            "    off, the Master bar drives BOTH videos, drift is" + chr(10) +
            "    auto-corrected (tighter while locked)." + chr(10) +
            "" + chr(10) +
            "TIPS" + chr(10) +
            "- Drag a video file onto this window to fill a slot." + chr(10) +
            "- The play button on each row plays that ONE video alone." + chr(10) +
            "- Click a video window to pause/resume both." + chr(10) +
            "- PiP Movie/Reaction: borderless, always-on-top, resizable." + chr(10) +
            "- Integrated PiP: PIP MODE Movie/Reaction embeds that video" + chr(10) +
            "  inside the other window. Drag the pane with the mouse," + chr(10) +
            "  arrow keys nudge it, Escape or PIP MODE again returns the" + chr(10) +
            "  video to its own window. Needs Sync Lock (both videos stay" + chr(10) +
            "  aligned inside one window)." + chr(10) +
            "- Double-click a video window to fullscreen it - both videos" + chr(10) +
            "  keep playing (one click = pause/resume both)." + chr(10) +
            "- PiP size: - / + buttons (or - / + keys) make the embedded" + chr(10) +
            "  pane bigger or smaller." + chr(10) +
            "- Windows can be resized freely (no aspect lock) - shape a" + chr(10) +
            "  window to the movie's aspect ratio and the black bars go away." + chr(10) +
            "- Tracks: choose audio and subtitles per video in the" + chr(10) +
            "  Tracks panel (Off turns subtitles off). For a YouTube URL the" + chr(10) +
            "  picker also lists the video's uploaded subtitles AND the" + chr(10) +
            "  auto-generated captions (picking one downloads + attaches it)." + chr(10) +
            "- Crop: remove baked-in black bars by hand - pick Movie or" + chr(10) +
            "  Reaction, then nudge Top/Bottom/Left/Right with - / + (Auto" + chr(10) +
            "  re-detects, Clear restores the full frame)." + chr(10) +
            "- Go-to: type a timecode (90 / 83:45 / 1:23:45) in the Master" + chr(10) +
            "  row and press Enter to jump both videos there. Time labels" + chr(10) +
            "  show HH:MM:SS once a video exceeds an hour." + chr(10) +
            "- Shortcuts: Space play/pause both, Left/Right seek 5 s" + chr(10) +
            "  both, arrow keys nudge the PiP pane while dragging.")
        lbl = tk.Label(w, text=txt, bg="#16181d", fg="#e8e8ea",
                       justify="left", font=("Segoe UI", 10))
        lbl.pack(padx=14, pady=(12, 6))
        tk.Button(w, text="Close", command=w.destroy,
                  bg="#2a2f3a", fg="#e8e8ea", relief="flat").pack(pady=(0, 12))

    def _fetch_meta(self, attempt=0):
        """While loaded-paused, mpv prints no status line and the position
        beacon is silent (no property changes), so pull duration/position
        over the IPC once the pipes are up: bars and labels get real ranges
        before the first Play. Re-arms while paused and data is missing."""
        if not self.started or not self.paused:
            return
        missing = False
        for t in ("A", "B"):
            p = self.players.get(t)
            if not (p and p.running):
                continue
            if self.last_dur.get(t) is None:
                missing = True

                def _grab(drv=p, tag=t):
                    err, dur = drv.get_property("duration", timeout=2.0)
                    if err == "success" and dur:
                        self.last_dur[tag] = dur
                    err2, pos = drv.get_property("time-pos", timeout=2.0)
                    if err2 == "success" and pos is not None:
                        self.last_pos[tag] = pos
                        self._status_time[tag] = time.monotonic()
                threading.Thread(target=_grab, daemon=True).start()
        if missing and attempt < 6:
            self.root.after(2500,
                            lambda a=attempt + 1: self._fetch_meta(a))

    def _refresh_tracks(self):
        for tag in ("A", "B"):
            p = self.players.get(tag)
            ca = self.combo_audio[tag]
            cs = self.combo_sub[tag]
            if not (p and p.running):
                continue
            src = self._srcs.get(tag)
            tl = p.track_list()
            audio = [t for t in tl if t.get("type") == "audio"]
            subs = [t for t in tl if t.get("type") == "sub"]
            aopts = []
            amap = {}
            for t in audio:
                lbl = self._track_label(t)
                aopts.append(lbl)
                amap[lbl] = t.get("id")
            aopts.append("Off")
            amap["Off"] = None
            sopts = ["Off"]
            smap = {"Off": None}
            for t in subs:
                lbl = self._track_label(t)
                sopts.append(lbl)
                smap[lbl] = t.get("id")
            # YouTube: merge fetched external subtitle options (incl. the
            # auto-generated ASR captions) so they show in the track picker
            for sub in (self._yt_subs.get(tag) or []):
                lbl = sub["label"]
                if lbl not in sopts:
                    sopts.append(lbl)
                    smap[lbl] = {"yt": True, "lang": sub["lang"],
                                 "auto": sub["auto"], "url": src,
                                 "label": lbl}
            cur_a = p.audio_id
            cur_s = p.sub_id
            ca["values"] = aopts
            cs["values"] = sopts
            ca.set(next((o for o in aopts if amap.get(o) == cur_a),
                        aopts[0] if aopts else ""))
            cs.set(next((o for o in sopts if smap.get(o) == cur_s), "Off"))
            self._track_opts[tag] = {"audio": amap, "sub": smap}

    @staticmethod
    def _track_label(t):
        bits = []
        title = t.get("title")
        lang = t.get("lang")
        if title:
            bits.append(str(title))
        if lang:
            bits.append("[%s]" % str(lang))
        if not bits:
            bits.append("Track %s" % t.get("id"))
        if t.get("type") == "audio" and t.get("demux-channel-count"):
            bits.append("(%s ch)" % t.get("demux-channel-count"))
        return " ".join(bits)

    def _on_track_sel(self, tag, kind):
        p = self.players.get(tag)
        if not (p and p.running):
            return
        opts = self._track_opts.get(tag) or {}
        name = "Movie" if tag == "A" else "Reaction"
        if kind == "a":
            lbl = self.combo_audio[tag].get()
            p.set_audio(opts.get("audio", {}).get(lbl))
            self.status_lbl.config(text="Audio track set on %s." % name)
        else:
            lbl = self.combo_sub[tag].get()
            sel = opts.get("sub", {}).get(lbl)
            if isinstance(sel, dict) and sel.get("yt"):
                threading.Thread(target=self._load_yt_sub,
                                 args=(tag, sel), daemon=True).start()
            else:
                p.set_sub(sel)
                self.status_lbl.config(text="Subtitle track set on %s." % name)

    def _toggle_pause_m(self):
        if not self.started:
            self._start()
            return
        self._set_pause_all(not self.paused)

    # -- seek bars ----------------------------------------------------------
    def _toggle_play_one(self, tag):
        self._last_active = tag
        """Play/pause ONLY this video - the other one keeps playing. This
        lets the user watch a single video before committing to the sync
        (Lock). We stamp the command time so the SYNCPAUSE echo of OUR OWN
        toggle is suppressed and never mirrors to the other video."""
        p = self.players.get(tag)
        if not (self.started and p and p.running):
            return
        pause = not p.paused
        self._pause_cmd_ts = time.monotonic()
        p.set_pause(pause)
        name = "Movie" if tag == "A" else "Reaction"
        self.status_lbl.config(text="%s %s - the other video is untouched. (Lock sync takes over both.)"
                               % (name, "paused" if pause else "playing"))

    # -- frame-by-frame stepping ---------------------------------------------
    def _step_frame(self, tag, back=False):
        """Step ONE video by one frame (works while paused; mpv steps
        exactly one frame and pauses again - verified). Re-anchors the
        offset from a fresh IPC read afterwards: the beacon throttles at
        0.099 s and would miss a 0.033 s step."""
        if not self.started:
            self.status_lbl.config(text="Start playback first so the video window exists.")
            return
        if self.sync_locked:
            self.status_lbl.config(
                text="Unlock sync first - frame stepping belongs to alignment (or use the Master bar).")
            return
        p = self.players.get(tag)
        if not (p and p.running and p.hwnd):
            self.status_lbl.config(text="Video not ready yet.")
            return
        if not back and (p.at_end or p._eof_hold):
            self.status_lbl.config(
                text="This video is at its end - only one frame back is possible.")
            return
        self._frame_step_ts = time.monotonic()
        self._pause_cmd_ts = time.monotonic()   # mpv flutters pause around a step
        self._last_active = tag
        # reference = TRUE mpv position (the panel's last_pos lags by up to
        # ~0.1 s, so comparing against it would misjudge a 1/30 s step)
        pre = self._fresh_pos(tag)
        self._step_pending[tag] = (back, pre if pre is not None
                                   else self.last_pos.get(tag))
        p.frame_step(back)
        name = "Movie" if tag == "A" else "Reaction"
        self.status_lbl.config(
            text="%s: one frame %s (frame-perfect alignment - use per-video play to compare)."
                 % (name, "back" if back else "forward"))
        self._step_pending[tag] = (back, self.last_pos.get(tag))
        self.root.after(150, lambda: self._reanchor_after_step(tag, 0))

    def _reanchor_after_step(self, tag, tries=0):
        """After a frame step the position moved by one frame; refresh the
        panel bookkeeping from the drivers over IPC and re-anchor the offset.
        Retries: the step may still be landing (frame-back-step decodes
        backward), so keep re-reading until the position changes."""
        if not self.started:
            return
        pending = self._step_pending.get(tag)
        if pending:
            back, pre = pending
            self._step_pending[tag] = None
            ok = False
            for _ in range(8):
                ra = self._fresh_pos("A")
                rb = self._fresh_pos("B")
                if ra is not None and rb is not None:
                    self.sync_off = rb - ra
                cur = self.last_pos.get(tag)
                if cur is None or pre is None:
                    return   # nothing to compare: give up cleanly
                if (back and cur < pre - 0.005) or (not back and cur > pre + 0.005):
                    ok = True
                    break
                time.sleep(0.15)   # step still in flight: read again
            if not ok:
                ra = self._fresh_pos("A")
                rb = self._fresh_pos("B")
                if ra is not None and rb is not None:
                    self.sync_off = rb - ra
        self._commit_seek(tag, self.last_pos.get(tag) or 0.0)

    def _fresh_pos(self, t):
        """Synchronous one-shot position read over IPC (accurate to the
        frame; used after frame steps the beacon would miss)."""
        p = self.players.get(t)
        if not (p and p.running):
            return None
        err, pos = p.get_property("time-pos", timeout=2.0)
        if err == "success" and pos is not None:
            pos = float(pos)
            dur = self.last_dur.get(t)
            if dur and pos > dur:
                pos = dur
            self.last_pos[t] = pos
            self._status_time[t] = time.monotonic()
            return pos
        return None

    # -- PiP black-bar removal -------------------------------------------
    def _crop_preheat(self, tag, src):
        """Background detection of baked-in letterbox/pillarbox bars for a
        source. Caches the rect per path; applies it live if PiP is on."""
        if src in self._crop_cache or src in self._crop_busy:
            return
        if not src or src.startswith(("http://", "https://")):
            self._crop_cache[src or ""] = None
            return
        self._crop_busy.add(src)
        try:
            rect = detect_crop_rect(src, self.last_dur.get(tag))
        except Exception:
            rect = None
        self._crop_cache[src] = rect
        self._crop_busy.discard(src)
        if rect and (self.pip.get(tag) or
                     (self.pip_int and self._pip_int_tag == tag)):
            self._pip_crop_apply(tag, rect)

    def _pip_crop_apply(self, tag, rect):
        """Apply a detected bar-crop to a PiP player and (embedded mode)
        re-fit the pane to the cropped video's display aspect."""
        p = self.players.get(tag)
        if not (p and p.running):
            return
        w, h, x, y = rect
        asp = None
        vp = None
        try:
            e, vp = p.get_property("video-params", timeout=2.5)
            if e == "success" and isinstance(vp, dict):
                par = vp.get("par") or 1.0
                asp = (float(w) * float(par)) / float(h)
        except Exception:
            pass
        # Guard: mpv silently ignores an out-of-bounds video-crop
        # (property stays empty) - e.g. a cached detection left over
        # after the file was swapped for a smaller one. Drop the bad
        # entry and fit the pane to the video's own aspect instead.
        if isinstance(vp, dict):
            vw = max(float(vp.get("dw") or 0), float(vp.get("w") or 0))
            vh = max(float(vp.get("dh") or 0), float(vp.get("h") or 0))
            if vw > 0 and vh > 0 and (w + x > vw + 3 or h + y > vh + 3):
                src = self._srcs.get(tag)
                if src and self._crop_cache.get(src) == rect:
                    self._crop_cache.pop(src, None)
                if self.pip_int and self._pip_int_tag == tag:
                    threading.Thread(target=self._pip_asp_from_video,
                                     args=(tag,), daemon=True).start()
                return
        p.cmd({"command": ["set_property", "video-crop",
                           "%dx%d+%d+%d" % (w, h, x, y)]})
        if self.pip_int and self._pip_int_tag == tag:
            self._set_pip_asp(asp)
        elif asp and p.hwnd and not self.pip_int:
            # keepaspect-window=no: mpv no longer refits the window to the
            # cropped aspect in the free window either - do it here.
            if not self._pip_crop_saved.get(tag):
                self._pip_crop_saved[tag] = self._win_rect(p.hwnd)
            self._fit_pip_window(tag, asp)

    def _fit_pip_window(self, tag, asp):
        """keepaspect-window=no: mpv no longer snaps the window to the
        video's (cropped) aspect - the app refits it: keep width, set height."""
        p = self.players.get(tag)
        if not (p and p.running and p.hwnd) or not asp:
            return
        r = self._win_rect(p.hwnd)
        if not r or r[2] <= 0:
            return
        h = max(34, int(r[2] / asp))
        ctypes.windll.user32.SetWindowPos(p.hwnd, 0, r[0], r[1], r[2], h,
                                          0x0004 | 0x0010)

    def _pip_crop_on(self, tag):
        """PiP engage hook: reapply this player's saved crop (manual first,
        else the auto-detected bars) so it survives the free-window <-> PiP
        switch; a cache miss starts the probe."""
        p = self.players.get(tag)
        manual = self._manual_crop.get(tag)
        if manual and p and p.running:
            self._pip_crop_apply(tag, manual)
            return
        src = self._srcs.get(tag)
        if src and src not in self._crop_cache and src not in self._crop_busy:
            threading.Thread(target=self._crop_preheat,
                             args=(tag, src), daemon=True).start()
        rect = self._crop_cache.get(src) if src else None
        if rect:
            self._pip_crop_apply(tag, rect)
        elif self.pip_int and self._pip_int_tag == tag:
            threading.Thread(target=self._pip_asp_from_video,
                             args=(tag,), daemon=True).start()

    def _pip_crop_off(self, tag):
        """PiP-mode-off hook. A MANUAL crop (set with the Crop panel) is app
        state that PERSISTS across the free-window <-> PiP switch, so it is
        reapplied rather than cleared. Only an AUTO crop (no manual rect yet)
        is lost here, and the pre-crop window shape is restored."""
        p = self.players.get(tag)
        manual = self._manual_crop.get(tag)
        if manual and p and p.running:
            self._pip_crop_apply(tag, manual)
            return
        if p and p.running:
            p.cmd({"command": ["set_property", "video-crop", ""]})
        saved = self._pip_crop_saved.get(tag)
        if (saved and p and p.running and p.hwnd and not self.pip_int):
            u = ctypes.windll.user32
            u.SetWindowPos(p.hwnd, 0, saved[0], saved[1],
                           max(60, saved[2]), max(34, saved[3]),
                           0x0004 | 0x0010)
            self._pip_crop_saved[tag] = None
        self._pip_int_asp = None

    def _cur_crop(self, tag):
        """Return the player's current video-crop rect (w,h,x,y) or None."""
        p = self.players.get(tag)
        if not (p and p.running):
            return None
        err, vc = p.get_property("video-crop", timeout=2.0)
        if err == "success" and vc:
            m = re.match(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$", str(vc).strip())
            if m:
                return tuple(int(g) for g in m.groups())
        return None

    def _crop_video_dims(self, tag):
        """(w, h) of the video's decoded frame (from video-params)."""
        p = self.players.get(tag)
        if not (p and p.running):
            return (0, 0)
        try:
            e, vp = p.get_property("video-params", timeout=2.0)
            if e == "success" and isinstance(vp, dict):
                w = max(float(vp.get("dw") or 0), float(vp.get("w") or 0))
                h = max(float(vp.get("dh") or 0), float(vp.get("h") or 0))
                return (int(w), int(h))
        except Exception:
            pass
        return (0, 0)

    def _crop_apply(self, tag, rect):
        """Apply a crop rect (w,h,x,y) via the user's crop panel (nudge/Auto).
        Remembers it so it PERSISTS across the free-window <-> PiP switch;
        Clear drops it."""
        if not rect:
            self._crop_clear()
            return
        self._manual_crop[tag] = rect      # remember it: survives PiP switches
        self._pip_crop_apply(tag, rect)
        name = "Movie" if tag == "A" else "Reaction"
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(
            text="%s cropped to %dx%d+%d+%d (black bars removed). Clear to undo."
                 % (name, rect[0], rect[1], rect[2], rect[3]))

    def _crop_nudge(self, edge, delta):
        """Move one crop edge ~8px (delta +1 = crop MORE of that edge)."""
        tag = self._crop_tag
        p = self.players.get(tag)
        if not (p and p.running):
            self.status_lbl.config(text="Start playback first, then crop.")
            return
        vw, vh = self._crop_video_dims(tag)
        if not vw or not vh:
            return
        cur = self._cur_crop(tag) or (vw, vh, 0, 0)
        w, h, x, y = cur
        d = 8
        if edge == "top":
            if delta > 0:
                dy = min(d, vh - (y + h)); y += dy; h -= dy
            else:
                dy = min(d, y); y -= dy; h += dy
        elif edge == "bottom":
            if delta > 0:
                h = max(16, h - d)
            else:
                h = min(vh - y, h + d)
        elif edge == "left":
            if delta > 0:
                dx = min(d, vw - (x + w)); x += dx; w -= dx
            else:
                dx = min(d, x); x -= dx; w += dx
        elif edge == "right":
            if delta > 0:
                w = max(16, w - d)
            else:
                w = min(vw - x, w + d)
        w = max(8, min(int(w), vw - x))
        h = max(8, min(int(h), vh - y))
        if w < 8 or h < 8 or x < 0 or y < 0:
            return
        self._crop_apply(tag, (int(w), int(h), int(x), int(y)))

    def _crop_interactive(self):
        """Interactive visual crop: freeze-frame snapshot popup where the user
        can click and drag a crop box directly on the picture with the mouse."""
        if not _HAS_PIL:
            messagebox.showinfo(APP_NAME, "Pillow is required for the visual crop tool.")
            return
        tag = self._crop_tag
        p = self.players.get(tag)
        if not (p and p.running):
            self.status_lbl.config(text="Start playback first, then crop.")
            return

        name = "Movie" if tag == "A" else "Reaction"
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(text="Capturing frame from %s for visual crop..." % name)
        self.root.update_idletasks()

        snap_path = os.path.join(SHOT_DIR, "_crop_frame_%s_%d.png" % (tag, int(time.time())))
        if os.path.isfile(snap_path):
            try:
                os.remove(snap_path)
            except Exception:
                pass

        # Temporarily clear video-crop in mpv so the snapshot captures the full uncropped frame
        cur_crop = self._manual_crop.get(tag)
        if cur_crop:
            p.cmd({"command": ["set_property", "video-crop", ""]})

        p.screenshot(snap_path)

        for _ in range(30):
            if os.path.isfile(snap_path) and os.path.getsize(snap_path) > 1000:
                break
            time.sleep(0.08)

        # Restore previous crop in mpv while dialog is open
        if cur_crop:
            p.cmd({"command": ["set_property", "video-crop", "%dx%d+%d+%d" % cur_crop]})

        if not (os.path.isfile(snap_path) and os.path.getsize(snap_path) > 1000):
            self.status_lbl.config(text="Could not capture frame from %s." % name)
            return

        def on_apply(rect):
            if rect:
                self._crop_apply(tag, rect)
            else:
                self._crop_clear()

        try:
            dlg = VisualCropDialog(self.root, snap_path, initial_crop=cur_crop,
                                   video_name=name, on_apply=on_apply)
            dlg.wait_window()
        finally:
            try:
                if os.path.isfile(snap_path):
                    os.remove(snap_path)
            except Exception:
                pass

    def _crop_clear(self):
        """Remove the crop on current video and return it to full resolution/aspect."""
        tag = self._crop_tag
        self._manual_crop[tag] = None
        p = self.players.get(tag)
        if p and p.running:
            p.cmd({"command": ["set_property", "video-crop", ""]})
            p.cmd({"command": ["set_property", "video-zoom", 0]})
            p.cmd({"command": ["set_property", "video-pan-x", 0]})
            p.cmd({"command": ["set_property", "video-pan-y", 0]})
            u = ctypes.windll.user32
            saved = self._pip_crop_saved.get(tag)
            if saved and p.hwnd and not self.pip_int:
                u.SetWindowPos(p.hwnd, 0, saved[0], saved[1],
                               max(160, saved[2]), max(120, saved[3]),
                               0x0004 | 0x0010)
                self._pip_crop_saved[tag] = None
            else:
                vw, vh = self._crop_video_dims(tag)
                if vw > 0 and vh > 0 and p.hwnd and not self.pip_int:
                    self._fit_pip_window(tag, float(vw) / float(vh))
        self._pip_crop_saved[tag] = None
        name = "Movie" if tag == "A" else "Reaction"
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(text="Crop cleared on %s (returned to full resolution)." % name)

    def _on_crop_tag(self, tag):
        self._crop_tag = tag
        for t, b in self._crop_tag_btns.items():
            if t == tag:
                b.state(["pressed"])
            else:
                b.state(["!pressed"])
        name = "Movie" if tag == "A" else "Reaction"
        self.status_lbl.config(text="Cropping %s." % name)

    def _list_yt_subs(self, tag):
        """Fetch YouTube subtitle options for a source (background)."""
        src = self._srcs.get(tag)
        p = self.players.get(tag)
        if not (src and is_youtube(src) and p and p.running):
            return
        if self._yt_subs.get(tag):
            return
        try:
            subs = yt_subtitle_repo(src)
        except Exception:
            subs = []
        if subs:
            self._yt_subs[tag] = subs
            try:
                self.root.after(0, self._refresh_tracks)
            except Exception:
                pass

    def _load_yt_sub(self, tag, sub):
        """Download a YouTube subtitle (auto or uploaded) and attach to mpv."""
        p = self.players.get(tag)
        src = self._srcs.get(tag)
        name = "Movie" if tag == "A" else "Reaction"
        if not (p and p.running and src):
            return
        if self._yt_sub_lock.get(tag):
            self.status_lbl.config(text="A subtitle is still downloading for %s." % name)
            return
        self._yt_sub_lock[tag] = True
        ok = False
        try:
            lbl = sub.get("label") or sub.get("lang") or "subtitle"
            self.status_lbl.config(text="Downloading %s subtitle for %s..." % (lbl, name))
            ytdl = shutil.which("yt-dlp")
            if not ytdl:
                return
            outdir = os.path.join(tempfile.gettempdir(), "syncplayer_subs")
            os.makedirs(outdir, exist_ok=True)
            tmpl = os.path.join(outdir, "%(id)s.%(ext)s")
            args = [ytdl, "--skip-download",
                    "--write-auto-subs" if sub.get("auto") else "--write-subs",
                    "--subs-langs", sub["lang"], "--subs-format", "vtt",
                    "--no-warnings", "--no-playlist", "--output", tmpl, src]
            out = subprocess.run(args, capture_output=True, text=True, timeout=300)
            if out.returncode == 0:
                cand = sorted(glob.glob(os.path.join(outdir, "*.vtt")),
                              key=os.path.getmtime, reverse=True)
                if cand and os.path.isfile(cand[0]):
                    path = cand[0]
                    p.cmd({"command": ["sub-add", path, "select", lbl, sub["lang"]]})
                    ok = True
        except Exception:
            ok = False
        finally:
            self._yt_sub_lock[tag] = False
            if ok:
                # drop the option so the picker shows the newly added track
                self._yt_subs[tag] = [s for s in self._yt_subs.get(tag, [])
                                      if not (s.get("lang") == sub["lang"]
                                              and s.get("auto") == sub["auto"])]
                try:
                    self.root.after(700, self._refresh_tracks)
                except Exception:
                    pass
                self.status_lbl.config(text="Subtitle '%s' attached to %s."
                                          % (sub.get("label") or sub.get("lang"), name))
            else:
                self.status_lbl.config(text="Couldn't fetch that subtitle for %s." % name)

    def _set_pip_asp(self, asp):
        self._pip_int_asp = asp
        self._pip_update_pane(force=True)

    def _pip_asp_from_video(self, tag):
        """No detected bars: still fit the embedded pane to the video's
        own aspect so a 4:3 / 21:9 source does not letterbox in the pane."""
        p = self.players.get(tag)
        if not (p and p.running):
            return
        try:
            e1, vp = p.get_property("video-params", timeout=2.0)
        except Exception:
            e1 = "error"
            vp = None
        if e1 == "success" and isinstance(vp, dict):
            w = float(vp.get("dw") or vp.get("w") or 0)
            h = float(vp.get("dh") or vp.get("h") or 0)
            if w > 0 and h > 0:
                self._set_pip_asp(w / h)

    def _toggle_pip(self, tag):
        """Picture-in-picture for THIS video: all borders completely removed,
        pure video feed with zero top sliver, always on top. Can be toggled
        repeatedly without frame-removal degradation."""
        p = self.players.get(tag)
        if not (p and p.running):
            self.status_lbl.config(text="Start playback first so the video window exists.")
            return
        if self.pip_int:
            self.status_lbl.config(
                text="Integrated PiP is active - exit PIP MODE first.")
            return
        u = ctypes.windll.user32
        if not self.pip.get(tag):
            p.cmd({"command": ["set_property", "ontop", "yes"]})
            p.cmd({"command": ["set_property", "border", "no"]})
            p.cmd({"command": ["set_property", "window-dragging", "yes"]})
            if p.hwnd:
                cur_style = (u.GetWindowLongPtrW(p.hwnd, -16) or 0) & 0xFFFFFFFF
                if cur_style & 0x00C00000:  # has WS_CAPTION: only save normal window style
                    self._pip_saved[tag] = cur_style
                # Pure borderless popup: WS_POPUP | WS_VISIBLE | WS_CLIPSIBLINGS, NO thickframe sliver
                bare = (0x80000000 | 0x10000000 | 0x04000000)
                u.SetWindowLongPtrW(p.hwnd, -16, bare if bare < 0x80000000 else bare - 0x100000000)
                u.SetWindowPos(p.hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
            self.pip[tag] = True
            self._pip_crop_on(tag)
            self._status_pin = time.monotonic() + 3.0
            self.status_lbl.config(text="PiP on: pure borderless video feed, always on top.")
        else:
            p.cmd({"command": ["set_property", "ontop", "no"]})
            p.cmd({"command": ["set_property", "border", "yes"]})
            if p.hwnd:
                saved = self._pip_saved.get(tag) or (0x10000000 | 0x00CF0000)
                u.SetWindowLongPtrW(p.hwnd, -16, saved if saved < 0x80000000 else saved - 0x100000000)
                u.SetWindowPos(p.hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020)
                self._pip_saved[tag] = None  # reset so subsequent PiP re-reads fresh normal style
            self._pip_crop_off(tag)
            self.pip[tag] = False
            self._status_pin = time.monotonic() + 3.0
            self.status_lbl.config(text="PiP off - window frame restored.")

    # -- integrated PiP (true video-in-video overlay) -------------------------
    def _toggle_pip_int(self, tag):
        """Integrated PiP: the `tag` video window becomes a CHILD window of
        the other video's window (SetParent) - a real video-over-video
        overlay rendered INSIDE the host's feed, not a separate floating
        window. The pane follows the host's video area (client space) and
        the 30 Hz poll keeps it glued; X/Y arrows in the panel (or the
        arrow keys) move it, Escape or toggling again undocks it. Requires
        Sync Lock so the two feeds stay aligned."""
        if self.pip_int:
            self._undock_pip_int()
            return
        if not self.started:
            self.status_lbl.config(text="Start playback first so both video windows exist.")
            return
        p = self.players.get(tag)
        host_tag = "B" if tag == "A" else "A"
        hp = self.players.get(host_tag)
        if not (p and hp and p.running and hp.running and p.hwnd and hp.hwnd):
            self.status_lbl.config(text="Video windows not ready yet.")
            return
        if not self.sync_locked:
            self.status_lbl.config(
                text="Integrated PiP needs Sync Lock (Lock sync) first - both videos must stay aligned.")
            return
        self.pip_int = True
        self._pip_int_tag = tag
        self._pip_int_host = host_tag
        self._pip_int_hwnd = p.hwnd
        self._pip_int_drag = False
        self._embed_pane(p, hp, tag)
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(
            text="Integrated PiP: %s is embedded INSIDE the %s feed. X/Y arrows (or the arrow keys) position it; Escape undocks (crop kept)." %
                 ("Movie" if tag == "A" else "Reaction",
                  "Reaction" if tag == "A" else "Movie"))

    def _embed_pane(self, pane, host, tag):
        """Embed the pane as a TRUE Win32 child window inside the host's video window
        using SetParent. With WS_CLIPCHILDREN on the host and WS_CHILD on the pane,
        the video feeds render together in ONE window rather than separate windows."""
        u = ctypes.windll.user32
        self._pip_rect_saved[tag] = self._win_rect(pane.hwnd)
        st = (u.GetWindowLongPtrW(pane.hwnd, -16) or 0) & 0xFFFFFFFF
        if st and (st & 0x00C00000):
            self._pip_saved[tag] = st

        # Enable WS_CLIPCHILDREN (0x02000000) on the host so host swapchain does not overdraw child
        host_st = (u.GetWindowLongPtrW(host.hwnd, -16) or 0) & 0xFFFFFFFF
        u.SetWindowLongPtrW(host.hwnd, -16, host_st | 0x02000000)

        # Reparent pane as a child of host
        u.SetParent(pane.hwnd, host.hwnd)
        # WS_CHILD (0x40000000) | WS_VISIBLE (0x10000000) | WS_CLIPSIBLINGS (0x04000000)
        child_style = 0x40000000 | 0x10000000 | 0x04000000
        u.SetWindowLongPtrW(pane.hwnd, -16, child_style)
        pane.cmd({"command": ["set_property", "border", "no"]})
        pane.cmd({"command": ["set_property", "window-dragging", "no"]})
        self._pip_crop_on(tag)
        self._pip_update_pane(force=True)

    @staticmethod
    def _win_rect(hwnd):
        u = ctypes.windll.user32
        r = ctypes.wintypes.RECT()
        if u.GetWindowRect(hwnd, ctypes.byref(r)):
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
        return None

    @staticmethod
    def _client_size(hwnd):
        u = ctypes.windll.user32
        r = ctypes.wintypes.RECT()
        if u.GetClientRect(hwnd, ctypes.byref(r)):
            return (r.right - r.left, r.bottom - r.top)
        return None

    def _undock_pip_int(self):
        """Detach child pane from host window and restore as normal top-level window."""
        tag = self._pip_int_tag
        p = self.players.get(tag) if tag else None
        u = ctypes.windll.user32
        if p and p.running and p.hwnd:
            u.SetParent(p.hwnd, 0)  # Detach from host parent
            p.cmd({"command": ["set_property", "border", "yes"]})
            saved = self._pip_saved.get(tag) or (0x10000000 | 0x00CF0000)
            u.SetWindowLongPtrW(p.hwnd, -16,
                                saved if saved < 0x80000000 else saved - 0x100000000)
            rect = self._pip_rect_saved.get(tag)
            if rect:
                u.SetWindowPos(p.hwnd, 0, rect[0], rect[1],
                               max(160, rect[2]), max(120, rect[3]),
                               0x0004 | 0x0020)
            p.cmd({"command": ["set_property", "ontop", "no"]})
            p.cmd({"command": ["set_property", "window-dragging", "yes"]})
            self._pip_crop_off(tag)
        self.pip_int = False
        self._pip_int_tag = None
        self._pip_int_host = None
        self._pip_int_hwnd = None
        self._pip_int_drag = False
        self._status_pin = time.monotonic() + 3.0
        self.status_lbl.config(text="Integrated PiP off - video restored to its own window.")

    def _pip_calc_rect(self, cw, ch):
        """Calculate the exact (x, y, w, h) in pixels for the embedded pane.
        Guarantees that the pane can always reach all 4 edges (left x=0, top y=0,
        right x=cw-w, bottom y=ch-h) regardless of scaling, cropping, or aspect ratio."""
        w = max(60, int(self._pip_int_size[0] * cw))
        h = max(34, int(self._pip_int_size[1] * ch))
        asp = self._pip_int_asp
        if asp and asp > 0:
            h = max(34, int(w / asp))
            maxh = max(34, int(0.98 * ch))
            if h > maxh:
                h = maxh
                w = max(60, int(h * asp))
            maxw = max(60, int(0.98 * cw))
            if w > maxw:
                w = maxw
                h = max(34, int(w / asp))

        max_x = max(0, cw - w)
        max_y = max(0, ch - h)
        x = max(0, min(max_x, int(round(self._pip_int_pos[0] * max_x))))
        y = max(0, min(max_y, int(round(self._pip_int_pos[1] * max_y))))
        return x, y, w, h, max_x, max_y

    def _pip_update_pane(self, force=False):
        """Update the position of the embedded child pane inside the host client area."""
        if not (self.pip_int and self._pip_int_hwnd):
            return
        tag = self._pip_int_tag
        host = self.players.get(self._pip_int_host) if self._pip_int_host else None
        pane = self.players.get(tag)
        if not (host and pane and host.running and pane.running
                and host.hwnd and pane.hwnd):
            return
        if pane.hwnd != self._pip_int_hwnd:
            self._embed_pane(pane, host, tag)
            self._pip_int_hwnd = pane.hwnd
            return
        u = ctypes.windll.user32
        csz = self._client_size(host.hwnd)
        if not csz or csz[0] <= 0 or csz[1] <= 0:
            return
        cw, ch = csz
        x, y, w, h, max_x, max_y = self._pip_calc_rect(cw, ch)
        hwnd = self._pip_int_hwnd
        if hwnd:
            u.SetWindowPos(hwnd, 0, x, y, w, h, 0x0004 | 0x0020)

    def _pip_resize(self, direction):
        """PiP size buttons (+/- keys): grow/shrink the embedded pane
        (10% per press, clamped to 8%..96% of the host feed)."""
        if not self.pip_int:
            return False
        f = 1.10 if direction > 0 else (1.0 / 1.10)
        s = self._pip_int_size
        s[0] = min(0.96, max(0.08, s[0] * f))
        s[1] = min(0.96, max(0.08, s[1] * f))
        self._pip_update_pane(force=True)
        return True

    def _pip_move(self, dx, dy, fine=False):
        """Panel X/Y arrows: move the pane inside the host video feed.
        If fine=True (or Shift held), moves pixel-by-pixel (1 px).
        Otherwise moves by 16 px. Can move right up to all 4 edges."""
        if not self.pip_int:
            return False
        host = self.players.get(self._pip_int_host) if self._pip_int_host else None
        csz = self._client_size(host.hwnd) if (host and host.hwnd) else None
        if not csz or csz[0] <= 0 or csz[1] <= 0:
            return False
        cw, ch = csz
        x, y, w, h, max_x, max_y = self._pip_calc_rect(cw, ch)

        step_px = 1 if fine else max(12, int(0.035 * max(max_x, max_y, 100)))
        nx = max(0, min(max_x, x + dx * step_px))
        ny = max(0, min(max_y, y + dy * step_px))

        self._pip_int_pos[0] = (nx / max_x) if max_x > 0 else 0.0
        self._pip_int_pos[1] = (ny / max_y) if max_y > 0 else 0.0
        self._pip_update_pane(force=True)

        mode_str = "1px fine" if fine else "%dpx" % step_px
        self._status_pin = time.monotonic() + 1.5
        self.status_lbl.config(text="PiP pos: (%d, %d) [%s step]" % (nx, ny, mode_str))
        return True

    def _pip_nudge(self, dx, dy):
        """Arrow keys: nudge the pane by px amounts (host-relative)."""
        if not self.pip_int:
            return False
        host = self.players.get(self._pip_int_host) if self._pip_int_host else None
        csz = self._client_size(host.hwnd) if (host and host.hwnd) else None
        if not csz or csz[0] <= 0 or csz[1] <= 0:
            return False
        cw, ch = csz
        x, y, w, h, max_x, max_y = self._pip_calc_rect(cw, ch)

        nx = max(0, min(max_x, x + dx))
        ny = max(0, min(max_y, y + dy))

        self._pip_int_pos[0] = (nx / max_x) if max_x > 0 else 0.0
        self._pip_int_pos[1] = (ny / max_y) if max_y > 0 else 0.0
        self._pip_update_pane(force=True)
        return True

    def _pip_start_drag(self):
        # mpv window-dragging is disabled on the embedded pane; all
        # positioning happens through the panel arrows / the arrow keys.
        return False

    def _pip_end_drag(self):
        self._pip_int_drag = False

    def _on_global_press(self, event):
        # Presses on the Tk panel are ordinary UI handling.
        pass

    def _on_root_release(self, event):
        self._pip_end_drag()
        self._on_release(event)

    def _toggle_lock(self):

        """Sync Lock: freeze the current alignment, then drive both
        videos from the Master bar alone. Per-video bars grey out and
        drift correction tightens (0.45s -> 0.15s, cooldown 2s -> 1s)."""
        self.sync_locked = not self.sync_locked
        if self.sync_locked:
            now = time.monotonic()
            ra = self._est_pos("A", now)
            rb = self._est_pos("B", now)
            if ra is not None and rb is not None:
                self.sync_off = rb - ra     # capture alignment as it is NOW
            self.btn_lock.config(text="🔓 Unlock", style="Accent.TButton")
            for sbar in (self.seek_a, self.seek_b):
                sbar.state(["disabled"])
            self.seek_m.state(["!disabled"])
            self.btn_play_a.state(["disabled"])
            self.btn_play_b.state(["disabled"])
            self.status_lbl.config(
                text="SYNC LOCKED \u2014 Master bar drives both videos. Unlock to re-align.")
        else:
            self.btn_lock.config(text="🔒 Lock sync", style="TButton")
            for sbar in (self.seek_a, self.seek_b):
                sbar.state(["!disabled"])
            self.seek_m.state(["disabled"])
            self.btn_play_a.state(["!disabled"])
            self.btn_play_b.state(["!disabled"])
            self.status_lbl.config(
                text="Sync unlocked \u2014 Movie / Reaction bars adjust one video at a time.")

    def _on_seek_a_drag(self, v):
        if self._prog_set or self.sync_locked:
            return
        self._last_active = "A"
        self.dragging_seek_a = True
        pos = float(v)
        self._seek_a_val = pos
        dur = self.last_dur.get("A") or 600
        self.lbl_a.config(text=self._fmt(pos, dur))
        now = time.monotonic()
        if now - self._scrub_ts > 0.08:
            self._scrub_ts = now
            p = self.players.get("A")
            if p and p.running:
                p.seek(pos, exact=False)

    def _on_seek_b_drag(self, v):
        if self._prog_set or self.sync_locked:
            return
        self._last_active = "B"
        self.dragging_seek_b = True
        pos = float(v)
        self._seek_b_val = pos
        dur = self.last_dur.get("B") or 600
        self.lbl_b.config(text=self._fmt(pos, dur))
        now = time.monotonic()
        if now - self._scrub_ts > 0.08:
            self._scrub_ts = now
            p = self.players.get("B")
            if p and p.running:
                p.seek(pos, exact=False)

    def _on_seek_m_drag(self, v):
        if self._prog_set or not self.sync_locked:
            return
        self.dragging_seek_m = True
        pos = float(v)
        self._seek_m_val = pos
        m_dur = max(self.last_dur.get("A") or 0, self.last_dur.get("B") or 0) or 600
        self.lbl_m.config(text=self._fmt(pos, m_dur))
        now = time.monotonic()
        if now - self._scrub_ts > 0.08:
            self._scrub_ts = now
            for t, off_key in (("A", None), ("B", "B")):
                p = self.players.get(t)
                if p and p.running:
                    spos = pos if off_key is None else pos + self.sync_off
                    p.seek(max(0.0, spos), exact=False)

    def _on_seek_a_release(self):
        self.dragging_seek_a = False
        if self.sync_locked:
            return
        if not (self.started and self.players["A"] and self.players["A"].running):
            return
        target = getattr(self, "_seek_a_val", None)
        if target is None:
            target = float(self.seek_a.get())
        target = max(0.0, min(target, self.last_dur["A"] or target))
        self.players["A"].seek(target)
        # movie moved alone -> re-anchor the reaction's offset so it stays put
        now = time.monotonic()
        rb = self._est_pos("B", now)
        if rb is not None:
            self.sync_off = rb - target
        self._commit_seek("A", target)
        self._seek_a_val = None

    def _on_seek_b_release(self):
        self.dragging_seek_b = False
        if self.sync_locked:
            return
        if not (self.started and self.players["B"] and self.players["B"].running):
            return
        target = getattr(self, "_seek_b_val", None)
        if target is None:
            target = float(self.seek_b.get())
        target = max(0.0, min(target, self.last_dur["B"] or target))
        self.players["B"].seek(target)
        # reaction moved alone -> re-anchor its offset so the movie stays put
        now = time.monotonic()
        ra = self._est_pos("A", now)
        if ra is not None:
            self.sync_off = target - ra
        self._commit_seek("B", target)
        self._seek_b_val = None

    def _on_seek_m_release(self):
        self.dragging_seek_m = False
        if not self.sync_locked:
            self._seek_m_val = None
            return
        if not self.started:
            return
        target = getattr(self, "_seek_m_val", None)
        if target is None:
            target = float(self.seek_m.get())
        for t, off_key in (("A", None), ("B", "B")):
            p = self.players.get(t)
            if not (p and p.running):
                continue
            pos = target
            if off_key == "B":
                pos = target + self.sync_off
            p.seek(max(0.0, pos))
            self._commit_seek(t, max(0.0, pos))
        self._seek_m_val = None

    def _handle_click(self, event, slider, drag_attr, val_attr, release_func):
        try:
            w = slider.winfo_width()
            to = float(slider.cget("to"))
            if w > 0 and to > 0:
                pos = max(0.0, min(to, event.x / w * to))
                slider.set(pos)
                setattr(self, drag_attr, True)
                setattr(self, val_attr, pos)
                release_func()
        except Exception:
            pass

    # -- volume -------------------------------------------------------------
    def _apply_volumes(self):
        """Master volume scales both; per-video sliders set their own share."""
        m = self.vol_m.get() / 100.0
        for t, var in (("A", self.vol_a), ("B", self.vol_b)):
            p = self.players.get(t)
            if p and p.running:
                p.set_volume(max(0, min(150, var.get() * m)))

    def _on_vol_drag(self, idx):
        self.dragging_vol[idx] = True
        var = self.vol_a if idx == 0 else self.vol_b
        self.vol_lbls[idx].config(text="%d %%" % int(round(var.get())))
        now = time.monotonic()
        if now - self._vol_cmd_ts > 0.07:   # throttle the drag flood
            self._vol_cmd_ts = now
            self._apply_volumes()

    def _on_vol_release(self, idx):
        self.dragging_vol[idx] = False
        var = self.vol_a if idx == 0 else self.vol_b
        self.vol_lbls[idx].config(text="%d %%" % int(round(var.get())))
        self._apply_volumes()

    def _mute(self, idx):
        var = self.vol_a if idx == 0 else self.vol_b
        if var.get() > 0:
            if idx == 0:
                self.saved_vol_a = var.get()
            else:
                self.saved_vol_b = var.get()
            var.set(0)
        else:
            var.set(self.saved_vol_a if idx == 0 else self.saved_vol_b)
        self.vol_lbls[idx].config(text="%d %%" % int(round(var.get())))
        self._apply_volumes()

    def _on_master_drag(self):
        self.dragging_master = True
        self.master_lbl.config(text="%d %%" % int(round(self.vol_m.get())))
        now = time.monotonic()
        if now - self._vol_cmd_ts > 0.07:   # throttle the drag flood
            self._vol_cmd_ts = now
            self._apply_volumes()

    # -- speed / misc -------------------------------------------------------
    def _nudge_speed(self, d):
        s = round(max(0.25, min(2.5, self.speed.get() + d)), 2)
        self.speed.set(s)
        self._sync_speed_str()
        self._apply_speed()

    def _sync_speed_str(self):
        try:
            self.speed_str.set("%.2fx" % round(float(self.speed.get()), 2))
        except Exception:
            self.speed_str.set("1.00x")

    def _apply_speed_entry(self):
        """Entry accepted: parses e.g. '1.35', 'x1.35', '1.35x';
        clamps to [0.25, 2.5]; applies on Return or FocusOut."""
        raw = self.speed_str.get().strip().lower()
        if raw.endswith("x"):
            raw = raw[:-1].strip()
        if raw.startswith("x"):
            raw = raw[1:].strip()
        try:
            s = round(max(0.25, min(2.5, float(raw))), 2)
        except Exception:
            s = 0.0   # invalid text: revert display
        if s <= 0.0:
            self._sync_speed_str()
            self.status_lbl.config(text="Speed not changed - type a number like 1.35.")
            return
        self.speed.set(s)
        self._sync_speed_str()
        self._apply_speed()
        self.status_lbl.config(text="Speed set to %.2fx (both videos)." % s)

    def _apply_speed(self):
        for t in ("A", "B"):
            p = self.players.get(t)
            if p and p.running:
                p.set_speed(self.speed.get())

    def _shot(self):
        if not self.started:
            return
        os.makedirs(SHOT_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        names = []
        for t, lbl in (("A", "movie"), ("B", "reaction")):
            p = self.players.get(t)
            if p and p.running:
                path = os.path.join(SHOT_DIR, "%s_%s_%s.png" % (stamp, lbl, t))
                p.screenshot(path)
                names.append(path)
        if names:
            self.status_lbl.config(text="Screenshots → %s" % ", ".join(names))
        else:
            self.status_lbl.config(text="Nothing playing — nothing to shoot.")

    # -- polling ------------------------------------------------------------
    def _on_release(self, event):
        if self.dragging_seek_a:
            self._on_seek_a_release()
        if self.dragging_seek_b:
            self._on_seek_b_release()
        if self.dragging_seek_m:
            self._on_seek_m_release()
        for i in (0, 1):
            if self.dragging_vol[i]:
                self._on_vol_release(i)
        if self.dragging_master:
            self.dragging_master = False

    def _set_bar(self, slider, key, pos, dur):
        """Set a seek bar, skipping redundant range reconfigs so the 30 Hz
        poll does not force a full widget redraw every tick."""
        to = max(1, int(dur or 1))
        if self._to_cache.get(key) != to:
            slider.config(to=to)
            self._to_cache[key] = to
        slider.set(round(pos, 2))

    def _bind_release(self, widget, drag_attr, release_func):
        """Clear the drag flag on release over the widget itself. A release
        outside the Tk window (e.g. over an mpv video window) would otherwise
        leave dragging_* stuck True and freeze that bar."""
        def _rel(_e):
            if getattr(self, drag_attr):
                setattr(self, drag_attr, False)
                release_func()
        widget.bind("<ButtonRelease-1>", _rel)

    def _poll(self):

        for t in ("A", "B"):
            p = self.players.get(t)
            if not p:
                continue
            while True:
                try:
                    kind, rec = p.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "pos":
                    pos = rec
                    dur = self.last_dur.get(t)
                    if dur and pos > dur:
                        pos = dur    # EOF-hold overshoot clamp
                    self.last_pos[t] = pos
                    self._status_time[t] = time.monotonic()
                    self._beacon_ts[t] = time.monotonic()
                elif kind == "status":
                    now = time.monotonic()
                    if now - self._beacon_ts[t] > 3.0:
                        # beacon silent: fall back to the (stale) status position
                        pos = rec["time_pos"]
                        dur = self.last_dur.get(t)
                        if pos is not None and dur and pos > dur:
                            pos = dur
                        self.last_pos[t] = pos
                        self._status_time[t] = now
                    if rec["duration"]:
                        self.last_dur[t] = rec["duration"]
                    if "cache_dur" in rec:
                        self.cache_dur[t] = rec["cache_dur"]
                    # The status line's eof field is mpv's own eof-reached
                    # (~1 Hz). Make it authoritative BOTH ways: a stale
                    # "yes" must not latch at_end after a restart, or a real
                    # click-pause would be misread as an EOF pause and never
                    # mirror to the other video. SYNCEOF still corroborates.
                    p.at_end = bool(rec["eof"])
                elif kind == "pause":
                    self._on_player_pause(t, rec)
                elif kind == "pipdrag":
                    if rec == "start" and not self._pip_int_drag:
                        self._pip_start_drag()
                    elif rec == "end":
                        self._pip_end_drag()
                    elif rec == "undock" and self.pip_int:
                        self._undock_pip_int()
                elif kind == "exit":
                    self._on_player_exit(t)
        # (auto crop feature removed)
        if self.started:
            a = self.combo_audio["A"]
            b = self.combo_audio["B"]
            if (a and b and not a.cget("values") and not b.cget("values")):
                self._refresh_tracks()
        if self.pip_int:
            try:
                self._pip_update_pane()
            except Exception:
                pass
        self._sync_tick()
        self.root.after(33, self._poll)      # ~30 Hz: smooth bars

    def _est_pos(self, tag, now):
        """Best guess of a video's CURRENT position, on a common time base.

        mpv statuses arrive ~1x/s and the two streams are NOT phase-locked.
        Comparing two raw samples directly shows phantom drift up to ~1 s
        (sample skew), which made the correction loop yank videos around.
        We timestamp every sample and extrapolate each to 'now' with the
        playback speed, so both are compared at the same instant.
        """
        pos = self.last_pos.get(tag)
        if pos is None:
            return None
        dt = now - self._status_time.get(tag, now)
        if dt < 0 or dt > 5.0:      # stale/no sample: trust the raw value
            return pos
        p = self.players.get(tag)
        if self.paused or (p and p.at_end) or not (p and p.running):
            return pos              # frozen: no extrapolation
        est = pos + dt * self.speed.get()
        dur = self.last_dur.get(tag)
        return min(est, dur) if dur else est

    def _commit_seek(self, tag, pos):
        """After SENDING a seek, make the panel's bookkeeping agree at once,
        so the drift loop never reasons about a stale pre-seek position."""
        self.last_pos[tag] = max(0.0, pos)
        self._status_time[tag] = time.monotonic()
        self._seek_grace_until = time.monotonic() + 1.2  # let mpv land & report

    def _sync_tick(self):
        """Master-clock work: correct drift, then refresh bars/status."""
        if not self.started:
            return
        now = time.monotonic()
        ra = self._est_pos("A", now)
        rb = self._est_pos("B", now)
        pa, pb = self.players.get("A"), self.players.get("B")
        pa_paused = pa.paused if (pa and pa.running) else True
        pb_paused = pb.paused if (pb and pb.running) else True
        playing = not (self.paused or pa_paused or pb_paused)
        # gentle drift correction: pull the reaction back to its aligned spot.
        # Suppressed right after a manual seek (grace) and rate-limited so a
        # correction can land and be observed before the next one.
        cooldown = 1.0 if self.sync_locked else 2.0
        thr = 0.15 if self.sync_locked else 0.45
        b_dur = self.last_dur.get("B")
        pb_at_end = pb.at_end if (pb and pb.running) else True
        react_at_end = pb_at_end or (b_dur and rb is not None and rb >= b_dur - 0.5)
        if (pa and pb and pa.running and pb.running and playing
                and now - pb.last_seek_ts > 1.2     # grace for ANY seek path
                and now - self._frame_step_ts > 1.2  # grace after frame steps
                and now >= self._seek_grace_until
                and now - self._last_corr["B"] > cooldown
                and not (self.dragging_seek_a or self.dragging_seek_b
                         or self.dragging_seek_m)):
            target = reaction_target(ra, self.sync_off)
            pa_at_end = pa.at_end if (pa and pa.running) else True
            if needs_correction(rb, ra, self.sync_off, threshold=thr,
                                playing=True, dragging=False,
                                movie_at_end=pa_at_end,
                                react_at_end=react_at_end):
                if b_dur and target is not None and target > b_dur - 0.05:
                    # the aligned spot is past the reaction's own end: it
                    # cannot be there. Parking at its end IS aligned.
                    self._last_corr["B"] = now
                else:
                    pb.seek(target)
                    self._commit_seek("B", target)
                    self._last_corr["B"] = now

        # ---- bars (skip whichever one the user is dragging) ----------------
        # NOTE: ttk.Scale.set() synchronously fires the command callback, which
        # sets dragging_seek_* — so every programmatic update is wrapped in the
        # _prog_set guard, or the bars would freeze on the first poll tick.
        m_dur = max(self.last_dur["A"] or 0, self.last_dur["B"] or 0)
        self._prog_set = True
        try:
            if not self.dragging_seek_a and ra is not None:
                self._set_bar(self.seek_a, "A", ra, self.last_dur["A"] or m_dur)
            if not self.dragging_seek_b and rb is not None:
                self._set_bar(self.seek_b, "B", rb, self.last_dur["B"] or m_dur)
            # Master bar is a frozen placeholder until Sync Lock is engaged
            if self.sync_locked and not self.dragging_seek_m:
                mpos = ra if ra is not None else rb
                if mpos is not None:
                    self._set_bar(self.seek_m, "M", mpos, m_dur)
        except tk.TclError:
            pass
        finally:
            self._prog_set = False

        # ---- labels (with stream buffer indicator) --------------------------
        buf_a = self.cache_dur.get("A", 0.0)
        buf_b = self.cache_dur.get("B", 0.0)
        ma_base = self._fmt(ra, self.last_dur["A"])
        ma = ("%s  [Buf: %ds]" % (ma_base, int(buf_a))) if (buf_a > 1.0) else ma_base
        rb_base = self._fmt(rb, self.last_dur["B"])
        rb_fmt = ("%s  [Buf: %ds]" % (rb_base, int(buf_b))) if (buf_b > 1.0) else rb_base
        mm = self._fmt(ra if ra is not None else rb, self.last_dur["A"] or self.last_dur["B"])
        if self._lbl_cache.get("A") != ma:
            self.lbl_a.config(text=ma)
            self._lbl_cache["A"] = ma
        if self._lbl_cache.get("B") != rb_fmt:
            self.lbl_b.config(text=rb_fmt)
            self._lbl_cache["B"] = rb_fmt
        if self._lbl_cache.get("M") != mm:
            self.lbl_m.config(text=mm)
            self._lbl_cache["M"] = mm
        for tag, btn in (("A", self.btn_play_a), ("B", self.btn_play_b)):
            p = self.players.get(tag)
            want = "⏸" if (p and p.running and not p.paused) else "▶"
            if btn.cget("text") != want:
                btn.config(text=want)

        d = drift(rb, ra, self.sync_off) if (ra is not None and rb is not None) else 0.0
        d_txt = "Δ %+.1fs" % d if abs(d) >= 0.05 else "Δ 0.0s"
        lock_txt = " · SYNC LOCKED" if self.sync_locked else ""
        if ra is not None:
            txt = "Movie %s  ·  Reaction %s  ·  %s%s" % (ma, rb_fmt, d_txt, lock_txt)
            if time.monotonic() >= self._status_pin and self._lbl_status_cache != txt:
                self.status_lbl.config(text=txt)
                self._lbl_status_cache = txt

    @staticmethod
    def _fmt(pos, dur):
        if pos is None:
            return "--:--"

        def _hms(v):
            v = max(0.0, float(v))
            if v >= 3600.0:
                return "%d:%02d:%02d" % (int(v // 3600), int(v // 60 % 60), int(v % 60))
            return "%02d:%02d" % (int(v // 60), int(v % 60))

        s = _hms(pos)
        if dur:
            s += " / " + _hms(dur)
        return s

    # -- config -------------------------------------------------------------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                c = json.load(f)
            self.movie_path.set(c.get("movie", ""))
            self.react_path.set(c.get("reaction", ""))
            self.vol_a.set(float(c.get("vol_a", 100.0)))
            self.vol_b.set(float(c.get("vol_b", 100.0)))
            self.vol_m.set(float(c.get("vol_m", 100.0)))
            self.speed.set(float(c.get("speed", 1.0)))
            self.jump_sec.set(float(c.get("jump_sec", 5.0)))
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({
                    "movie": self.movie_path.get(),
                    "reaction": self.react_path.get(),
                    "vol_a": self.vol_a.get(),
                    "vol_b": self.vol_b.get(),
                    "vol_m": self.vol_m.get(),
                    "speed": self.speed.get(),
                    "jump_sec": self.jump_sec.get(),
                }, f, indent=2)
        except Exception:
            pass

    def _apply_startup_cli(self):
        args = [a for a in sys.argv[1:] if not a.startswith("-")]
        if len(args) == 2:
            self.movie_path.set(args[0])
            self.react_path.set(args[1])
            self.root.after(300, self._start)

    def _on_close(self):
        try:
            if self.pip_int:
                self._undock_pip_int()
        except Exception:
            pass
        self._save_config()
        for t in ("A", "B"):
            p = self.players.get(t)
            if p:
                p.quit()
        self.root.destroy()


def _install_crash_hook():
    import traceback
    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        logp = os.path.join(SHOT_DIR, "syncplayer_crash.txt")
        try:
            with open(logp, "a", encoding="utf-8") as f:
                f.write("\n===== %s =====\n%s\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass
        if "--smoke" in sys.argv:
            return   # don't block the auto-close in packaged smoke tests
        try:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME,
                "SyncPlayer hit an error and closed.\n\n%s\n\nLog: %s"
                % (msg[-400:], logp))
        except Exception:
            pass
    sys.excepthook = hook


def main():
    _install_crash_hook()
    try:  # keep the GUI sharp on HiDPI
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if _HAS_DND:
        root = TkinterDnD.Tk()
    else:
        root = tk.Tk()
    app = SyncApp(root)
    if _HAS_DND:
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", app._on_drop)
    if "--smoke" in sys.argv:  # packaged-exe sanity test: auto-close (cleanly)
        root.after(6000, app._on_close)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        try:
            with open(os.path.join(SHOT_DIR, "syncplayer_crash.txt"), "w") as f:
                traceback.print_exc(file=f)
        except Exception:
            pass