# SyncPlayer — dual-video synchronized player (mpv)

*Windows and Linux (X11 and Wayland).*

**Sync a movie you own with a YouTube reaction video, and keep them locked
together for the whole runtime.**

| | |
|---|---|
| ⚡ **How it syncs** | Each video plays in its **own mpv instance and window** (independent timelines). The panel is a master clock: seeking one video **never** touches the other, and a gentle drift-correction loop pulls the reaction back to its aligned spot whenever it wanders (> 0.45 s). No filter graphs, no rebuilds → **nothing to glitch or crash**. |
| 🎬 **Sources** | Local files (`mp4/mkv/mov/webm/avi/ts/…`) or URLs (YouTube etc., resolved via yt-dlp). |
| 🖼 **Windows** | Two mpv windows, auto-arranged side by side (re-arrange anytime). |
| 🎚 **Three seek bars** | **Master** moves both together. **Movie** and **Reaction** bars move one side only — drag one to align the two, and it stays locked. |
| 🎞 **Frame-step** | ⏴/⏵ next to each timeline's play button (or `[` / `]`) step THAT video one frame at a time while paused — align the two pictures at 30 fps precision. |
| 🖱 **Drag & drop** | Drop one or two video files straight onto the window to fill the source slots. |
| 🔊 **Audio** | Independent volume slider + mute per video, plus a master volume scaling both. |
| 🎵 **Tracks** | Per-video **audio** and **subtitle** pickers (built from each file's own track list; Off disables). |
| 🎛 **Transport** | ±10 s jumps, restart, close, screenshots of both videos; **speed** editable (type 1.35 and Enter) with ±0.05 nudge buttons. |
| 💾 **Persistence** | Paths, volumes, speed saved between sessions. |

## Quick start

### Windows

1. **Install** with `SyncPlayer-Setup.exe` (installs the app **plus a bundled
   mpv**, so nothing else is needed on the machine). Then launch
   **SyncPlayer** from the Desktop / Start-Menu shortcut.

### Linux

```bash
git clone https://github.com/Zcc09/SyncPlayer && cd SyncPlayer
./install.sh                     # installs to ~/.local/share/syncplayer
```

`install.sh` checks your dependencies, installs a launcher
(`~/.local/bin/syncplayer`), adds a **SyncPlayer** entry to your app menu
(with file-manager "Open with" support), and — if `yt-dlp` is not already on
your system — fetches a private copy so YouTube links work out of the box.
It needs `mpv`, `python3-tk` and `libX11` from your distro; the script prints
the exact package command for apt/dnf/pacman/zypper when something is missing.

```bash
syncplayer --check-env           # verify mpv, yt-dlp, window backend, paths
./uninstall.sh                   # remove the app (config + screenshots are kept)
```

> Wayland sessions are supported: SyncPlayer runs the video windows through
> **X11 (Xwayland)** so they can be arranged, made borderless and used for
> PiP. Set `SYNCPLAYER_MPV_GPU_CONTEXT=auto` to let mpv pick its own output
> (window arranging is then unavailable on Wayland).

### Both platforms

1. Source A = your movie (Browse… or URL…), Source B = the reaction.
2. Hit **Start** — both videos load **paused** and open side by side.
3. Press **▶ Play** (transport or Master row, or `Space`) when ready.
4. Drag the **Movie** or **Reaction** bar until the moments line up — that
   video moves on its own; the other stays put. For frame-perfect
   alignment: pause, then use the **⏴/⏵** step buttons to move one video
   one frame at a time.
6. Let it run — SyncPlayer gently re-syncs any drift, so they stay locked.
7. Balance the two **volume sliders**, then enjoy.

> **Updates**: run **SyncPlayer — Check for Updates** (Start Menu) — it
> fetches the latest **SyncPlayer** and **mpv** releases and installs
> whatever is missing or outdated. No account or token needed.

> URLs: paste a YouTube link into either source. It streams via mpv's
> built-in yt-dlp (no download needed).

## Keyboard shortcuts (panel)

| Key | Action |
|---|---|
| `Space` | Play / pause both |
| `←` / `→` | Seek both ±5 s *(nudge the PiP pane instead while integrated PiP is active)* |
| `[` / `]` | Frame-step the last-touched video back / forward (paused) |
| `↑` / `↓` | Nudge the PiP pane (integrated PiP) |
| `Esc` | Exit integrated PiP (video returns to its own window) |
| Click a video window | Pause / resume both (click-to-pause) |

## Details worth knowing

- **Start loads paused**: *Start* opens both videos frozen on frame 1 — press
  Play (or `Space`) when you are ready. This lets you line up the two
  timelines before anything moves.
- **Independent seeking**: the Movie/Reaction bars seek *that one player
  only* — a plain mpv seek on its own process. The other video keeps
  playing exactly where it was. This is what makes it crash-proof.
- **Auto re-sync**: while playing, the panel compares the reaction's live
  position with its aligned spot and gently re-seeks it when drift exceeds
  ~0.45 s — so a 2-hour movie stays locked.
- **🔒 Sync Lock**: once you've aligned both videos with their own bars,
  hit *Lock sync* — the alignment is captured, the per-video bars switch
  off, the Master bar wakes up and drives BOTH videos, and drift
  correction tightens (0.15 s) so nothing can wander. Unlock to re-align
  anytime.
- **Master bar is locked out until you lock**: until *Lock sync* is
  engaged the Master bar sits frozen and disabled — only the per-video
  bars can move anything.
- **▶/⏸ per video**: each timeline row has its own play/pause button — play
  ONE video alone (e.g. watch the movie before the reaction is ready) while
  the other stays frozen. The correction loop respects it and won't chase a
  paused video.
- **Frame-perfect alignment**: the ⏴/⏵ buttons (and `[`/`]`) step one video
  exactly one frame while it is paused — the offset is re-anchored after
  every step, so Lock sync captures the new alignment. mpv was verified to
  step `+/-1/30 s` per press on these clips.
- **⧉ Picture-in-Picture (window)**: per-video *PiP* button makes that video
  window borderless and always on top (mpv's own `ontop`) while keeping its
  resize edges — drag an edge to resize, drag the video to move.
- **🗔 Integrated PiP (inside one window)**: *PIP MODE* (in the Help menu)
  embeds one video **inside** the other's window — borderless, always on
  top. Drag the small pane with the mouse, nudge with the arrow keys,
  `Esc` to undock. Requires Sync Lock so both stay aligned.
- **⬛ Black-bar removal (both PiP modes)**: when a PiP engages, the app
  probes that file once (headless mpv crop-detection, no extra deps) and,
  if black letterbox/pillarbox bars are found, applies mpv's `video-crop`
  and re-fits the embedded pane to the cropped aspect — so neither the
  borderless window nor the embedded pane shows bars. Cleared on exit.
  Bar-free sources are untouched; an out-of-date crop (e.g. the file was
  swapped for a smaller one) is refused and re-detected.
- **↕ PiP size (− / + buttons or − / + keys)**: the integrated PiP pane
  grows/shrinks 12 % per press (clamped 8–95 % of the host window) so a
  reaction cam can be made big or small without leaving the panel.
- **🖼 Free-form window resize**: the video windows no longer snap to the
  video's aspect ratio (`keepaspect-window=no`) — drag any edge to give
  the window exactly the shape you want, e.g. a 2.35:1 window for a
  widescreen movie, with **no black bars**. The app re-fits the window
  itself when the black-bar crop turns on/off.
- **🎵 Tracks**: per-video pickers for **audio** and **subtitles**, built
  from the file's own streams (language + title shown; Off disables).
- **🌐 YouTube subtitles**: for a URL source, the Tracks picker also lists
  the video's **uploaded subtitles AND auto-generated (ASR) captions**
  (fetched via yt-dlp); picking one downloads it and attaches it live.
- **⬛ Visual & Manual crop**: a dedicated *Crop* section lets you kill baked-in
  black bars with ease — click **✂ Visual Crop** to open an interactive snapshot
  popup of the current video frame where you can simply drag a box with the mouse,
  resize handles, and click Apply (or hit Enter). Fine-tuning buttons (Top/Bottom/Left/Right
  8 px nudges) and **✖ Clear** are also available. The crop is remembered during
  the **active session** (survives normal window ↔ PiP switches), and **Clear**
  immediately restores the video to its exact full original resolution and aspect ratio.
- **🎯 Editable timecode**: the master seek row has a **Go-to** box — type
  `90`, `83:45` or `1:23:45` and hit Enter to jump both videos there
  (no scrubbing). The time labels show **HH:MM:SS** once a video is over
  an hour (MM:SS below it).
- **Click-to-pause mirrors to both** windows, so they never fight each
  other; **double-clicking a video fullscreens it and never pauses** (the
  deferred single-click pause is cancelled by the double-click), so both
  videos stay in lock-step while the clicked one goes fullscreen.
- **Lengths**: each video ends at its own end and freezes on its last frame
  (`--keep-open`); restart to re-run in sync.
- **Config** lives in `%APPDATA%\SyncPlayer\`, screenshots in
  `%USERPROFILE%\Pictures\SyncPlayer` (keeps the Desktop clean).
- **Rate matching**: if the reaction was recorded at a slightly different
  speed, use Speed (affects both).

## Requirements

**Windows**

- Windows 10/11. **mpv and yt-dlp are bundled** by the installer — nothing
  else to install, and **YouTube links work out of the box** (the updater
  keeps mpv + yt-dlp current too). Optional **yt-dlp** not needed separately.
- The bare `SyncPlayer.exe` (a standalone asset) still needs mpv + yt-dlp
  present; the **installer is the recommended** way to get a working app.

**Linux**

- `python3` (3.8+; tested on 3.14) **with tkinter**, `mpv` (tested on 0.41),
  and `libX11`. `yt-dlp` is optional (install it or let `install.sh` fetch a
  private copy) but required for URL sources.
- Tested on **CachyOS / Arch** (KDE Plasma 6, Wayland + Xwayland). Any X11 or
  Wayland desktops should work; the app drives windows through X11, so on pure
  Wayland (no Xwayland) the player still works but read the note below.
- On a **pure Wayland** session with no Xwayland there is no supported way for
  an app to move or stack other programs' windows, so *arrange*, *floating
  PiP* and *embedded PiP* are unavailable — the two videos and all sync
  features (bars, drift correction, frame-step, volume, crop) still work.
- The Windows self-updater is Windows-only; on Linux update with `git pull &&
  ./install.sh` plus your package manager (`mpv`, `yt-dlp`).

## For developers

```
syncplayer.py        # the whole app (panel + two mpv drivers + sync loop)
sp_plat.py           # platform layer: Win32 + X11 window backends, mpv/yt-dlp
                     # discovery, IPC transport (named pipe / unix socket),
                     # config + screenshot locations
selftest.py          # 59-check headless verification (python selftest.py)
selftest_plat.py     # 20-check platform layer: paths, discovery, real mpv IPC
                     # round trip (runs on Windows AND Linux)
selftest_x11.py      # 16-check Linux/X11 window features against two REAL mpv
                     # windows (find/arrange/borderless/ontop/embed/undock)
gui_test.py          # 179-check END-TO-END test: drives the real GUI + real
                     # mpv processes (python gui_test.py) — bars track, per-
                     # video seeks, drift correction, volume read-back from
                     # mpv, speed, pause, Sync Lock, PiP, frame-step, tracks,
                     # PiP black-bar crop & guard, double-click fullscreen,
                     # PiP size, free-form resize, manual crop, Go-to timecode,
                     # yt subtitle merge, crop persistence + Auto re-probe
installer.py         # self-contained installer (bundles app + mpv + yt-dlp + updater)
updater.py           # checks/installs latest SyncPlayer + mpv + yt-dlp releases
install_test.py      # DEPLOYMENT test (Windows): runs the real installer, then
                     # verifies the installed app plays TWO videos (one a
                     # YouTube link) using the bundled mpv + yt-dlp
install.sh           # Linux installer (deps check, launcher, .desktop, yt-dlp)
uninstall.sh         # Linux uninstaller
install_test_linux.py# DEPLOYMENT test (Linux): install.sh, then TWO videos
                     # (one YouTube) + playback, window features, crop, volume
make_testclips.sh    # regenerates the demo clips (needs ffmpeg)
make_icon.py         # regenerates icon.png / icon.ico
dist/SyncPlayer.exe        # PyInstaller onefile build
dist/SyncPlayer-Setup.exe  # installer (primary download)
dist/SyncPlayer-Updater.exe# updater (GUI)
```

Build the app exe, then the installer and updater:

```
# 1. app exe
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer --icon icon.ico --version-file version_info.txt syncplayer.py

# 2. updater (no bundled data)
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer-Updater --icon icon.ico updater.py

# 3. installer (bundles the app exe + mpv distro + updater)
cp dist/SyncPlayer.exe bundle/SyncPlayer.exe
cp dist/SyncPlayer-Updater.exe bundle/SyncPlayer-Updater.exe
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer-Setup --icon icon.ico installer.py \
  --add-data "bundle/SyncPlayer.exe;." \
  --add-data "bundle/SyncPlayer-Updater.exe;." \
  --add-data "bundle/mpv;mpv"
```

The `bundle/mpv` dir is the extracted mpv Windows distro (not committed — it
is large and rebuilt by the installer build; `bundle/` is gitignored).

### How the sync works (architecture)

```
movie.mp4 ──▶ mpv process A ──▶ its own window (idle)      ─┐
                                                            ├─ control via
react.mp4 ──▶ mpv process B ──▶ its own window (idle)      ─┘   JSON IPC
                                                            │   (named pipes)
Control panel (tkinter) ── master clock ────────────────────┘
   · Master bar → seeks A and B together
   · Movie/Reaction bars → seek one player only (re-anchor offset)
   · drift loop (33 ms) → if |reaction − target| > 0.45 s, gently re-seek
   · status via each mpv's stdout (`--term-status-msg`) + Lua beacons
```

mpv flags of note: `--input-ipc-server` (named-pipe JSON control per
player, with a dedicated reader thread so replies never block the writer),
`--force-window` + `--title` (its own window, findable by the panel for
side-by-side placement), `--term-status-msg` (position/duration/volume/
track stream), and an embedded Lua script that broadcasts pause clicks and
a ~10 Hz frame-accurate position beacon (the `${time-pos}` status line is
OSD-cached and too stale to drive the bars).
