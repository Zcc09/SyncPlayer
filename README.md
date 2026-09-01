# SyncPlayer — dual-video synchronized player (mpv)

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

1. **`SyncPlayer.exe`** (Desktop) — double-click to open the panel.
2. Source A = your movie (Browse… or URL…), Source B = the reaction.
3. Hit **Start** — both videos load **paused** and open side by side.
4. Press **▶ Play** (transport or Master row, or `Space`) when ready.
5. Drag the **Movie** or **Reaction** bar until the moments line up — that
   video moves on its own; the other stays put. For frame-perfect
   alignment: pause, then use the **⏴/⏵** step buttons to move one video
   one frame at a time.
6. Let it run — SyncPlayer gently re-syncs any drift, so they stay locked.
7. Balance the two **volume sliders**, then enjoy.

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
- **🎵 Tracks**: per-video pickers for **audio** and **subtitles**, built
  from the file's own streams (language + title shown; Off disables).
- **Click-to-pause mirrors to both** windows, so they never fight each other.
- **Lengths**: each video ends at its own end and freezes on its last frame
  (`--keep-open`); restart to re-run in sync.
- **Config** lives in `%APPDATA%\SyncPlayer\`, screenshots in
  `%USERPROFILE%\Pictures\SyncPlayer` (keeps the Desktop clean).
- **Rate matching**: if the reaction was recorded at a slightly different
  speed, use Speed (affects both).

## Requirements

- Windows, **mpv** installed (`winget install shinchiro.mpv`), optional
  **yt-dlp** for URLs.
- The exe bundles everything else.

## For developers

```
syncplayer.py        # the whole app (panel + two mpv drivers + sync loop)
selftest.py          # 41-check headless verification (python selftest.py)
gui_test.py          # 134-check END-TO-END test: drives the real GUI + real
                     # mpv processes (python gui_test.py) — bars track, per-
                     # video seeks, drift correction, volume read-back from
                     # mpv, speed, pause, Sync Lock, PiP, frame-step, tracks
make_testclips.sh    # regenerates the demo clips (needs ffmpeg)
make_icon.py         # regenerates icon.png / icon.ico
dist/SyncPlayer.exe  # PyInstaller onefile build
```

Rebuild the exe:

```
python -m PyInstaller --noconfirm --clean --onefile --windowed \
  --name SyncPlayer --icon icon.ico --version-file version_info.txt syncplayer.py
```

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
