# SyncPlayer

A desktop player for watching two videos at the same time, locked together: a movie you own
and a reaction video, a commentary track, an alternate cut, or the same scene in two
languages.

Each video plays in its own mpv window with its own timeline, so nothing is re-encoded and
one side can never break the other. A control panel keeps them in step: align them once,
lock the sync, and drift is corrected automatically for the rest of the runtime.

Windows 10/11 and Linux (X11 and Wayland).

---

## Quick start

### Windows

1. Download `SyncPlayer-Setup.exe` from the latest release and run it. The wizard is the
   usual flow: **Welcome**, **Destination**, **Start Menu Folder**, **Additional Tasks**,
   **Ready to Install**, **Installing**, **Finish**.

   The default destination is `%LOCALAPPDATA%\Programs\SyncPlayer`, which is per-user, so
   no administrator prompt appears. If an existing install is found, its folder is offered
   so the wizard updates that copy in place.

   On **Additional Tasks** you can keep or drop the bundled **mpv**, **yt-dlp** and
   **ffmpeg**, the update checker, and the Desktop icon.

2. Uninstall any time from Windows **Apps & Features**, or the *Uninstall SyncPlayer*
   entry in the Start Menu folder. Your configuration and screenshots are kept.

3. Unattended installs:

   ```
   SyncPlayer-Setup.exe --silent --install-dir D:\Apps\SyncPlayer ^
       --no-ytdlp --no-desktop-shortcut
   ```

   Other switches: `--no-mpv`, `--no-ffmpeg`, `--no-updater`, `--no-launch`. Setup is
   built with NSIS, so its own switches work as well: `/S` for a silent install, and
   `/D=` for the destination, which has to be the last argument on the line.

### Linux

```bash
git clone https://github.com/Zcc09/SyncPlayer && cd SyncPlayer
./install.sh                     # installs to ~/.local/share/syncplayer
```

`install.sh` checks your dependencies, installs a launcher (`~/.local/bin/syncplayer`),
adds a **SyncPlayer** entry to your application menu with file-manager "Open with"
support, and, if `yt-dlp` is not already present, fetches a private copy so links work
straight away. It needs `mpv`, `python3-tk` and `libX11`; when something is missing the
script prints the exact package command for apt, dnf, pacman or zypper.

```bash
syncplayer --check-env           # report mpv, yt-dlp, window backend and paths
./uninstall.sh                   # remove the app (config and screenshots are kept)
```

On Wayland, SyncPlayer runs the video windows through X11 (Xwayland) so they can be
arranged, made borderless and used for PiP. Setting `SYNCPLAYER_MPV_GPU_CONTEXT=auto`
lets mpv pick its own output instead, at the cost of window arranging on Wayland.

### First sync

1. **Source A** is your movie, **Source B** is the reaction. Use **Browse...**, or paste a
   link (a YouTube URL streams through yt-dlp), or drop files onto the window.
2. Press **Start**. Both videos load paused and open side by side.
3. Press **Play** (transport or Master row, or `Space`).
4. Drag the **Movie** or **Reaction** bar until the two moments line up. That bar moves one
   video only; the other stays where it is.
5. For frame-perfect alignment: pause, then use the step buttons next to a timeline row (or
   `[` and `]`) to move one video a single frame at a time.
6. Press **Lock sync**. The alignment is captured, the per-video bars switch off, and the
   Master bar takes over both videos.
7. Balance the two volume sliders and let it run.

If you already know the delay between the two videos, skip step 4: type it into the
**Offset** box in the Master row and press Enter.

---

## Features

### Sources

- Local files (`mp4`, `mkv`, `mov`, `webm`, `avi`, `ts` and anything else mpv reads) or
  URLs (YouTube and the rest, resolved by yt-dlp).
- Paste a link straight into a source field, or drop files onto the window.
- Your paths, volumes, speed and remembered alignments are saved between sessions.

### Sync and seeking

- **Three seek bars.** The Master bar moves both videos together. The Movie and Reaction
  bars move one side only, which is how alignment is done.
- **Independent seeking.** A per-video seek never touches the other player.
- **Precise scrubbing (default).** Dragging a bar converts sideways pointer movement into
  time instead of jumping to the pointer: 0.5 s per pixel while the pointer is on the bar,
  and 0.02 s per pixel once it is lifted 24 px above it, which is finer than one frame at
  30 fps. Playback holds while you drag and resumes when you let go, and the release lands
  frame-exactly. A click without movement still seeks to the clicked position. Settings
  offers **Direct** for the older pointer-following behaviour.
- **Seek buttons and arrow keys** move both videos by the **Jump** distance (5 s by
  default, adjustable 0.5-120), and the buttons show the distance in use.
- **Frame stepping.** The buttons beside each timeline row, or `[` and `]`, move one video
  a single frame while paused. The alignment offset is re-anchored after every step.
- **Editable speed.** Type `1.35` and press Enter; the +/- buttons step by 0.05.
- **Go-to box.** Type `90`, `83:45` or `1:23:45` and press Enter to move both videos there.
  Time labels switch to `HH:MM:SS` once a video passes an hour.
- **Remembered alignment.** The offset between the two videos is stored per source pair (up
  to 60 pairs, keyed by both paths or URLs) and restored on Start, so a pair you have
  aligned once comes back aligned.
- **Typed offset.** In the Master row, type a known delay and press Enter: seconds
  (`12.5`), a timecode (`1:05`), a comma decimal (`0,5`), or negative (`-3.25`). Positive
  means the reaction is ahead of the movie. It moves the reaction immediately and is
  remembered for that pair. Typing `0` clears the alignment.
- **Offset indicator.** The status line shows the current offset, so it can be read at a
  glance and typed back in next session.
- **Drift correction without jumps.** While playing, small drift is absorbed by trimming
  the reaction's playback rate by up to 5 percent (pitch-preserved, so no change in speed
  is audible) and the gap closes over a few seconds. Only a real gap, over 0.8 s, such as
  after a buffering stall or a manual seek, is corrected with a seek.

### Picture-in-Picture

- **Floating PiP.** A per-video button makes that window borderless and always on top,
  keeping its resize edges.
- **Integrated PiP.** Embeds one video inside the other's window. Drag the pane with the
  mouse, nudge it with the arrow keys, `Esc` to undock. Requires Sync Lock so the two stay
  aligned.
- **PiP size.** The - and + buttons (or keys) scale the embedded pane by 12 percent per
  press, clamped to 8-95 percent of the host window.
- **Free-form windows.** Video windows do not snap to the video's aspect ratio, so a window
  can be shaped to any ratio, for example 2.35:1, with no black bars.

### Cropping

- **Visual crop.** Opens a snapshot of the current frame where a box can be dragged with
  the mouse, resized with handles, and applied with Apply or Enter. Fine adjustment buttons
  (8 px nudges) and **Clear** are also available.
- **Reading the frame is robust.** Capture escalates until it has a real picture: mpv's own
  screenshots, then ffmpeg reading the file, then a window render that works while the
  player is covered, then a screen grab with the video shown fullscreen. A frame that comes
  back blank is never accepted. The crop window has **Capture frame** and **Grab screen**
  buttons of its own, so a frame can be fetched from inside it, and it reports the frame
  size, brightness and the route that produced it.
- **HDR and Dolby Vision.** The transfer is detected and the capture is tone-mapped for it,
  with the software route first. A frame that still comes back essentially black is lifted
  into a viewable range, since seeing the bars is what matters here.
- **Manual crop.** Top/Bottom/Left/Right nudges, Auto re-detection and Clear, on the normal
  window as well as in PiP. The crop survives window and PiP changes for the session; only
  Clear or a restart returns the video to full resolution.
- **Auto detection** of black letterbox and pillarbox bars is available but is never applied
  automatically: the framing only changes when you change it.

### Subtitles and tracks

- **Per-video track pickers** for audio and subtitles, built from each file's own streams
  (language and title shown; Off disables).
- **YouTube subtitles.** For a URL source the picker also lists the uploader's subtitles and
  the auto-generated captions; picking one downloads it and attaches it live.
- **Subtitle drag and drop.** Drop a `.srt`, `.ass`, `.ssa`, `.vtt`, `.sub`, `.idx`, `.smi`
  or `.sup` file onto the window to attach it live. A subtitle whose name matches a loaded
  video goes to that video (`movie.mp4` with `movie.srt`); anything else goes to the feed the
  crop panel is pointed at.

### Downloads

- **Download button** in the Reaction row. It lists the qualities yt-dlp offers, saves the
  chosen one to `%USERPROFILE%\Downloads\SyncPlayer`, and repoints the reaction at the local
  file, so a downloaded reaction cannot stall mid-take the way a stream can.
- **Connections** (1-16, default 8) fetches that many parts of the video at once, the way a
  download manager does. The choice is remembered, and the log shows the count used.
- **ffmpeg is bundled** with the installer, which is what lets yt-dlp merge the separate
  video and audio streams that anything above roughly 720p is published as. Installing
  without it (`--no-ffmpeg`) caps downloads at single-file quality.

### Updates

- **Update from inside the app.** The header shows your version and doubles as a manual
  check; when a newer release exists an **Update to X** button appears with the release
  notes, download progress and a restart button. Checks run in the background at most every
  six hours and stay silent when offline. Your configuration, alignments, screenshots and
  downloads are never touched.
- If Windows will not let the running executable be replaced, the new build is staged and
  installed on the next start.
- On Linux, update with `git pull && ./install.sh` plus your package manager for mpv.

---

## Settings

The **Settings** button in the header opens:

| Setting | What it does |
|---|---|
| YouTube playback quality | Resolution mpv asks yt-dlp for: Best available, 2160p, 1440p, **1080p (default)**, 720p, 480p, 360p. Applies to the next video you load, without a restart. |
| Download connections | The default for the Download window's Connections box (1-16). |
| Status readout | Shows or hides the live Movie / Reaction / delta line. Messages still appear while it is hidden. |
| Seek bar dragging | **Precise** (scrub, the default) or **Direct** (the knob follows the pointer). |
| Seek distance (s) | How far the arrow keys and the seek buttons move (0.5-120). |

---

## Keyboard shortcuts

| Key | Action |
|---|---|
| `Space` | Play or pause both videos |
| `Left` / `Right` | Seek both by the Jump distance (5 s by default; nudges the PiP pane instead while integrated PiP is active) |
| `Up` / `Down` | Nudge the PiP pane |
| `[` / `]` | Step the last-touched video back or forward one frame (paused) |
| `Esc` | Undock integrated PiP |
| `Ctrl+Shift+S` | Swap the movie and the reaction |
| Click a video window | Pause or resume both |

---

## Troubleshooting

**The video windows do not appear.**
Check that mpv is found: run `syncplayer --check-env` on Linux, or look at the status line
in the app. The installer bundles mpv, so a missing mpv usually means a bare
`SyncPlayer.exe` was used without installing it.

**The reaction drifts out of sync.**
Small drift is corrected on its own. If it drifts badly, the two files are probably not the
same cut or have a different frame rate; align them once and let the remembered alignment
handle the next session, or trim the reaction's speed with the Speed box.

**A link will not play.**
YouTube changes its side of the contract regularly. Update yt-dlp (the in-app updater keeps
the bundled copy current), and check that the URL plays in a browser.

**Visual crop shows nothing.**
Use the **Grab screen** button inside the crop window, which reads the frame off the screen
and is the most reliable route. The line under the buttons reports the frame size,
brightness and which route produced the frame, which is what to include in a bug report.

**The download is slow.**
Raise **Connections** in the Download window. It is remembered for next time.

---

## Requirements

**Windows**

- Windows 10 or 11, 64-bit.
- Nothing else: mpv, yt-dlp and ffmpeg are bundled by the installer.
- The bare `SyncPlayer.exe` needs mpv and yt-dlp present on the system; the installer is the
  recommended way to get a working app.
- Setup registers itself in Apps & Features under `HKCU`, so it never asks for administrator
  rights. Removing it leaves your configuration and screenshots alone.

**Linux**

- `python3` (3.8 or newer) with tkinter, `mpv` (tested on 0.41) and `libX11`.
- `yt-dlp` is optional, but URL sources need it.
- Tested on CachyOS / Arch with KDE Plasma 6 (Wayland through Xwayland). Any X11 or Wayland
  desktop should work.
- On a pure Wayland session with no Xwayland, an application cannot move or stack other
  programs' windows, so arranging, floating PiP and embedded PiP are unavailable. The two
  videos and every sync feature still work.

---

## For developers

```
syncplayer.py         the application: control panel, two mpv drivers, sync loop
sp_plat.py            platform layer: Win32 and X11 window backends, mpv and yt-dlp
                      discovery, IPC transport (named pipe or Unix socket), config and
                      screenshot locations
installer.py          Setup wizard (destination, components, shortcuts) plus silent
                      install and Add/Remove Programs registration
updater.py            release checks and installation, shared by the in-app updater and
                      the standalone SyncPlayer-Updater.exe
selftest.py           headless checks: parsing, helpers, sync maths, updater logic
selftest_plat.py      platform layer: paths, discovery, a real mpv IPC round trip
                      (Windows and Linux)
selftest_x11.py       Linux/X11 window features against two real mpv windows
gui_test.py           end-to-end: drives the real panel and real mpv processes
install_test.py       deployment (Windows): drives the wizard page by page, then
                      installs and plays two videos with the bundled mpv
install_test_linux.py deployment (Linux): install.sh, then playback and window features
install.sh            Linux installer (dependency check, launcher, .desktop, yt-dlp)
uninstall.sh          Linux uninstaller
make_testclips.sh     regenerates the demo clips (needs ffmpeg)
make_icon.py          regenerates icon.png and icon.ico
```

### Building

```bash
# 1. the application
#    --collect-all tkinterdnd2 is required: no PyInstaller hook ships for it, and
#    without it the packaged app loses drag and drop. Build with pillow and
#    tkinterdnd2 installed, or they will not be bundled either.
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer --icon icon.ico --version-file version_info.txt \
  --collect-all tkinterdnd2 syncplayer.py

# 2. the updater
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer-Updater --icon icon.ico updater.py

# 3. the installer, which bundles the two executables above plus mpv and ffmpeg
cp dist/SyncPlayer.exe bundle/SyncPlayer.exe
cp dist/SyncPlayer-Updater.exe bundle/SyncPlayer-Updater.exe
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer-Setup --icon icon.ico installer.py \
  --add-data "bundle/SyncPlayer.exe;." \
  --add-data "bundle/SyncPlayer-Updater.exe;." \
  --add-data "bundle/mpv;mpv" \
  --add-data "icon.png;."
```

`bundle/` holds the extracted mpv distribution and the static ffmpeg binary. It is not
committed; it is large and is rebuilt for the installer.

### How the sync works

```
movie.mp4  ->  mpv process A  ->  its own window   -\
                                                    |  control via JSON IPC
react.mp4  ->  mpv process B  ->  its own window   -/      (named pipes)

Control panel (tkinter) - master clock
   Master bar          seeks A and B together
   Movie/Reaction bars seek one player only, re-anchoring the offset
   drift loop (33 ms)  trims B's rate for small gaps, seeks for large ones
   status              each mpv's stdout plus frame-accurate Lua beacons
```

mpv is driven with `--input-ipc-server` (a named pipe per player, with a dedicated reader
thread so replies cannot block the writer), `--force-window` and `--title` (its own
findable window for side-by-side placement), `--term-status-msg` (position, duration,
volume, track), and an embedded Lua script that mirrors pause clicks and broadcasts a
10 Hz frame-accurate position beacon, because the `${time-pos}` status line is cached and
too stale to drive the bars.
