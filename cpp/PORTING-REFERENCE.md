# C++ port: reference for byte-faithful behaviour

Generated from the Python source. Every entry below is the implementation the
C++ port must reproduce; when behaviour differs, the Python is right until a
deliberate decision says otherwise.

## constants: sync policy

```python
MICRO_DEADBAND = 0.06         # s: inside this the two feeds count as aligned
MICRO_MAX_DRIFT = 0.8         # s: above this, seek instead of trimming
MICRO_MAX_PCT = 0.05         # cap the rate change at +-5%
MICRO_GAIN = 0.10             # rate delta per second of drift
MICRO_HOLD = 8.0             # s: keep a trim applied before re-evaluating

# subtitle file types accepted by drag & drop (mpv sub-add handles all of them)
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".smi", ".sup")
```

## constants: seek modes and scrub

```python
SEEK_MODES = (("precise", "Precise (scrub)"),
              ("direct", "Direct (follow the pointer)"))
DEFAULT_SEEK_MODE = "precise"
SCRUB_BASE_GAIN = 0.5      # s per pixel while the pointer stays on the bar
SCRUB_FINE_GAIN = 0.02     # s per pixel once it is lifted (frame-by-frame)
SCRUB_LIFT_PX = 24         # how far above the bar counts as "lifted"
SCRUB_STEP = 0.02          # s: smallest change worth sending to mpv mid-drag
SCRUB_THROTTLE = 0.05      # s: least time between two live seeks
SCRUB_EXACT_BELOW = 0.15   # s: a movement this small is sent frame-exact
```

## constants: youtube qualities

```python
YOUTUBE_QUALITIES = (("best", "Best available"), ("2160", "2160p (4K)"),
                     ("1440", "1440p"), ("1080", "1080p"), ("720", "720p"),
                     ("480", "480p"), ("360", "360p"))
DEFAULT_YOUTUBE_QUALITY = "1080"
```

## constants: app identity

```python
APP_NAME = "SyncPlayer"
APP_VERSION = "1.6.13"
```

## function: clamp

```python
def clamp(v, lo, hi):
    return max(lo, min(hi, v))


# ---- micro-speed drift correction ------------------------------------------
# Instead of hard-seeking the reaction back into alignment (a visible jump, and
# with a YouTube feed often a fresh buffering stall), a small lag/lead is
# absorbed by running it a fraction of a percent faster or slower. With
# --audio-pitch-correction the pitch is preserved, so the correction is
# inaudible; anything larger than MICRO_MAX_DRIFT is still a seek, because
# trimming a big gap at a few percent would take minutes.
# smallest embedded pane the +/- buttons may reach (matches the floors in
# _pip_calc_rect); move_child's default 160x120 is for the host windows
MIN_PANE_W, MIN_PANE_H = 60, 34

MICRO_DEADBAND = 0.06         # s: inside this the two feeds count as aligned
MICRO_MAX_DRIFT = 0.8         # s: above this, seek instead of trimming
MICRO_MAX_PCT = 0.05         # cap the rate change at +-5%
MICRO_GAIN = 0.10             # rate delta per second of drift
MICRO_HOLD = 8.0             # s: keep a trim applied before re-evaluating

# subtitle file types accepted by drag & drop (mpv sub-add handles all of them)
SUBTITLE_EXTS = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".smi", ".sup")
```

## function: micro_rate

```python
def micro_rate(drift_s, deadband=MICRO_DEADBAND, max_pct=MICRO_MAX_PCT,
               gain=MICRO_GAIN):
    """Playback-rate factor that absorbs `drift_s` without a seek.

    Signed like drift(): positive means the video is AHEAD of where it should be
    (so it must run slower), negative means behind (run faster). Returns 1.0
    inside the dead band, otherwise a factor within [1-max_pct, 1+max_pct].
    """
    try:
        d = float(drift_s)
    except (TypeError, ValueError):
        return 1.0
    if d != d or abs(d) <= deadband:          # NaN guard + dead band
        return 1.0
    delta = max(-max_pct, min(max_pct, -d * gain))
    return 1.0 + delta
```

## function: reaction_target

```python
def reaction_target(movie_pos, sync_off):
    """Where the reaction SHOULD be, given the movie's live position and the
    aligned offset (reaction offset relative to the movie, may be negative)."""
    if movie_pos is None:
        return None
    return movie_pos + sync_off
```

## function: drift

```python
def drift(react_pos, movie_pos, sync_off):
    """How far the reaction has wandered from its aligned spot (seconds)."""
    if react_pos is None or movie_pos is None:
        return 0.0
    return react_pos - (movie_pos + sync_off)
```

## function: needs_correction

```python
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

_SWP_NOZORDER_NOACTIVATE = 0x0004 | 0x0010   # kept for reference; backends use their own

# ---- mpv IPC pipe plumbing ------------------------------------------------
# Overlapped I/O: the CRT poisons a pipe handle that has been read, and a
# synchronous write blocks forever once mpv's reply buffer (a few KB) is
# full - which a drag-spammed volume/seek burst does in a second. Writes
# go through a dedicated writer thread and replies are drained by a
# reader thread, so the GUI thread can never block on the pipe.
# The mpv JSON-IPC transport is OS specific (named pipe on Windows,
# unix domain socket on Linux) and lives in sp_plat.Ipc.


# ---------------------------------------------------------------------------
# frame capture for visual crop
#
# "mpv said success" is not evidence: a screenshot of a surface that has not
# painted yet is a perfectly valid black PNG. Every route below is therefore
# checked for CONTENT, and a route that returns an empty picture is treated as a
# failure and escalated past. The last route shows the video and reads the screen,
# because a GPU-composited surface is only guaranteed to be readable once it is
# actually on screen.
# ---------------------------------------------------------------------------
```

## function: scrub_target

```python
def scrub_target(pos0, dx_px, lift_px, duration):
    """Where a precise drag points, given how far the pointer has moved.

    Pure on purpose: the behaviour is testable without a player.
    """
    gain = SCRUB_FINE_GAIN if lift_px >= SCRUB_LIFT_PX else SCRUB_BASE_GAIN
    target = float(pos0) + (int(dx_px) * gain)
    if duration:
        return max(0.0, min(target, float(duration)))
    return max(0.0, target)
```

## function: is_youtube

```python
def is_youtube(url):
    try:
        u = url.lower()
        return "youtube.com" in u or "youtu.be" in u
    except Exception:
        return False
```

## function: ytdl_format_expr

```python
def ytdl_format_expr(quality):
    """The yt-dlp format selector for a playback-quality choice."""
    q = str(quality or DEFAULT_YOUTUBE_QUALITY).strip().lower()
    if q in ("best", "auto", "0", ""):
        return "bestvideo+bestaudio/best"
    try:
        h = int(q)
    except ValueError:
        h = int(DEFAULT_YOUTUBE_QUALITY)
    return "bestvideo[height<=%d]+bestaudio/best[height<=%d]" % (h, h)


# MpvDriver builds its own command line, so the current selector lives here and
# the app keeps it in step with the setting instead of threading it through every
# spawn path.
_PLAYBACK_YTDL_FORMAT = [ytdl_format_expr(DEFAULT_YOUTUBE_QUALITY)]
```

## function: is_subtitle_file

```python
def is_subtitle_file(path):
    p = (path or "").strip().lower()
    return p.endswith(SUBTITLE_EXTS)
```

## function: detect_crop_rect

```python
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
            **plat.run_extra())
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
```

## function: probe_media

```python
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
            **plat.run_extra())
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
```

## function: probe_duration

```python
def probe_duration(path, timeout=20):
    return probe_media(path, timeout)[0]


# YouTube playback quality. A URL is handed to mpv, which resolves it through
# yt-dlp; left alone yt-dlp picks the best streams on offer, so a 4K upload is
# what stalls the moment its buffer runs dry. The default here is 1080p and the
# range is selectable in Settings ("best" restores yt-dlp's own choice).
YOUTUBE_QUALITIES = (("best", "Best available"), ("2160", "2160p (4K)"),
                     ("1440", "1440p"), ("1080", "1080p"), ("720", "720p"),
                     ("480", "480p"), ("360", "360p"))
DEFAULT_YOUTUBE_QUALITY = "1080"
```

## function: frame_stats

```python
def frame_stats(path):
    """(mean, stddev, p99) of a captured frame in 0-255, or (None, None, None)."""
    if not _HAS_PIL:
        return None, None, None
    try:
        from PIL import ImageStat
        with Image.open(path) as im:
            g = im.convert("L")
            g.thumbnail((256, 256))
            st = ImageStat.Stat(g)
            px = sorted(g.getdata())
        p99 = px[min(len(px) - 1, int(len(px) * 0.99))] if px else None
        return float(st.mean[0]), float(st.stddev[0]), float(p99)
    except Exception:
        return None, None, None
```

## function: make_frame_viewable

```python
def make_frame_viewable(path):
    """Brighten a frame that would otherwise look like an empty box.

    Judged on the 99th percentile, not the mean: a night scene is mostly dark and
    perfectly readable, while a frame whose BRIGHTEST content sits at 14/255 is not
    readable at all. A linear gain (capped) is applied so colour relationships
    survive; returns (changed, "x2.4") so the caller can say what it did.
    """
    if not _HAS_PIL:
        return False, ""
    mean, sd, p99 = frame_stats(path)
    if p99 is None or p99 >= VIEW_MIN_P99:
        return False, ""
    gain = min(8.0, VIEW_MIN_P99 / max(1.0, float(p99)))
    try:
        lut = [min(255, int(round(i * gain))) for i in range(256)]
        with Image.open(path) as im:
            src = im.convert("RGB")
        out = src.point(lut * 3)
        out.save(path)
        out.close()
        src.close()
        return True, "x%.1f" % gain
    except Exception:
        return False, ""
```

## function: _frame_has_content

```python
def _frame_has_content(path, min_stddev=2.0, min_bytes=4000):
    """True when `path` holds a PNG with an actual picture in it."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) < min_bytes:
            return False
    except Exception:
        return False
    if not _HAS_PIL:
        return True                  # cannot look at it: trust the size
    try:
        with Image.open(path) as im:
            g = im.convert("L")
            g.thumbnail((256, 256))
            px = list(g.getdata())
        if not px:
            return False
        mean = sum(px) / float(len(px))
        var = sum((v - mean) ** 2 for v in px) / float(len(px))
        return (var ** 0.5) >= min_stddev
    except Exception:
        return False
```

## function: find_mpv

```python
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
    # 3) installed by SyncPlayer (the installer's own target dir)
    for cand in plat.installed_mpv_candidates():
        if cand and os.path.isfile(cand):
            _prepend_path(os.path.dirname(cand))
            _mpv_cache = cand
            return cand
    # 4) PATH
    found = shutil.which(plat.MPV_EXE) or shutil.which("mpv")
    if found:
        if found.lower().endswith(".com"):   # Windows ships mpv.com beside mpv.exe
            exe = os.path.join(os.path.dirname(found), "mpv.exe")
            if os.path.isfile(exe):
                found = exe
        _prepend_path(os.path.dirname(found))
        _mpv_cache = found
        return found
    # 5) common install locations (per platform)
    for c in plat.system_mpv_candidates():
        if os.path.isfile(c):
            _mpv_cache = c
            return c
    _mpv_cache = None
    return None
```

## function: find_ytdl

```python
def find_ytdl():
    """Locate yt-dlp: next to mpv, in the SyncPlayer install dir, or on PATH."""
    global _ytdl_cache
    if _ytdl_cache and os.path.isfile(_ytdl_cache):
        return _ytdl_cache
    mpv = find_mpv()
    if mpv:
        cand = os.path.join(os.path.dirname(mpv), plat.YTDL_EXE)
        if os.path.isfile(cand):
            _ytdl_cache = cand
            return cand
    for cand in plat.installed_ytdl_candidates():
        if cand and os.path.isfile(cand):
            _ytdl_cache = cand
            return cand
    w = shutil.which(plat.YTDL_EXE) or shutil.which("yt-dlp")
    if w:
        _ytdl_cache = w
        return w
    return None
```

## function: yt_parse_list_subs

```python
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
```

## function: _hms_or

```python
def _hms_or(secs):
    """mm:ss (or h:mm:ss) for a number of seconds; blank when unknown."""
    try:
        v = float(secs)
    except Exception:
        return "--:--"
    if v >= 3600:
        return "%d:%02d:%02d" % (v // 3600, v // 60 % 60, v % 60)
    return "%02d:%02d" % (v // 60, v % 60)
```

## platform: config_dir

```python
def config_dir():
    """Where syncplayer_config.json lives."""
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIRNAME)
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), APP_DIRNAME)
```

## platform: shot_dir

```python
def shot_dir():
    """Where screenshots / mpv logs / helper lua+input.conf are written."""
    if IS_WIN:
        return os.path.join(os.path.expanduser("~"), "Pictures", "SyncPlayer")
    # Linux: honour XDG_PICTURES_DIR / user-dirs.dirs, else ~/Pictures
    pics = None
    try:
        udd = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "user-dirs.dirs")
        with open(udd, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_PICTURES_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    val = os.path.expandvars(val.replace("$HOME", os.path.expanduser("~")))
                    if val:
                        pics = val
                    break
    except Exception:
        pics = None
    if not pics:
        pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return os.path.join(pics, APP_DIRNAME)
```

## platform: downloads_dir

```python
def downloads_dir():
    """Where the Download button puts a finished reaction video."""
    if IS_WIN:
        base = os.path.join(os.path.expanduser("~"), "Downloads")
        if not os.path.isdir(base):
            base = os.path.expanduser("~")
        return os.path.join(base, "SyncPlayer")
    try:
        with open(os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "user-dirs.dirs"),
                  encoding="utf-8", errors="replace") as f:
            for line in f:
                if line.startswith("XDG_DOWNLOAD_DIR"):
                    val = line.split("=", 1)[1].strip().strip('"')
                    val = os.path.expandvars(val).replace("$HOME", os.path.expanduser("~"))
                    return os.path.join(val, "SyncPlayer")
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads", "SyncPlayer")
```

## platform: ipc_endpoint

```python
def ipc_endpoint(name):
    """Return the value for mpv's --input-ipc-server plus the client address.

    Windows: a named pipe  (\\\\.\\pipe\\<name>)
    Linux:   a unix socket (<tmp>/<name>.sock)
    """
    if IS_WIN:
        path = r"\\.\pipe\%s" % name
        return path, path
    if IS_MAC:
        path = os.path.join(tempfile.gettempdir(), "%s.sock" % name)
    else:
        # Linux: keep it short (sun_path is 108 bytes) and per-user
        rundir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        path = os.path.join(rundir, "%s.sock" % name)
    return path, path
```

## platform: bundled_mpv_candidates

```python
def bundled_mpv_candidates():
    """mpv bundled next to the app (installer layout)."""
    return bundled_mpv_candidates_for(app_base_dir())
```

## platform: system_mpv_candidates

```python
def system_mpv_candidates():
    """Well-known system install locations (before falling back to PATH)."""
    if IS_WIN:
        return [
            r"C:\Program Files\MPV Player\mpv.exe",
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Tools\mpv\mpv.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\mpv\mpv.exe"),
            os.path.expanduser(r"~\scoop\apps\mpv\current\mpv.exe"),
        ]
    home = os.path.expanduser("~")
    return [
        "/usr/bin/mpv",
        "/usr/local/bin/mpv",
        "/snap/bin/mpv",
        "/var/lib/flatpak/exports/bin/io.mpv.Mpv",
        os.path.join(home, ".local/bin/mpv"),
        "/usr/bin/mpv.bin",
        "/opt/mpv/bin/mpv",
    ]
```
