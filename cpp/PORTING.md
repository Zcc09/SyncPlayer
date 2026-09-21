# Porting SyncPlayer to C++

Status: **the core is ported, builds and is covered by a smoke test. The WinUI-style
interface is the next increment.** This file is the working plan and the honest
record of what has and has not moved.

## Why a C++ port, and what "native" means here

The Python build works and ships. The reason to port is the interface: tkinter cannot
be made to look or behave like a Windows application, and the user asked for the
Windows version to be native in the WinUI sense.

Decision: **Win32 + Direct2D/DirectWrite**, drawn by hand, rather than WinUI 3 / XAML.

| | WinUI 3 (XAML, Windows App SDK) | Win32 + D2D/DWrite (chosen) |
|---|---|---|
| Look | The real Fluent controls | Fluent look, implemented here |
| Runtime dependency | Windows App SDK redistributable (tens of MB, bootstrapper or MSIX) | none beyond Windows itself |
| Deployment | Packaged or unpackaged-with-bootstrap; a bundled mpv next to the exe is awkward | one exe plus the payload, exactly like today |
| Windows 10 support | Constrained | Fine |
| Cost | Less drawing code, more packaging risk | More drawing code, no packaging risk |

The machine has VS Build Tools 2026 with the C++ workload, Windows SDK 26100, CMake
and Ninja, and **no** Windows App SDK, so the second column is also the one that can
be built and verified here. If the real XAML controls are wanted later, only `ui/`
changes: the core has no knowledge of the interface.

## Architecture

```
cpp/
  src/core/            portable, no UI - reused by a Linux build unchanged
    json.*             the IPC, config and release-check JSON (no third-party dep)
    paths.*            config/screenshot/download directories, mpv/yt-dlp/ffmpeg discovery
    config.*           settings + remembered alignments, same keys as the Python build
    sync.*             the sync policy: drift, micro-rate trim, thresholds, scrub maths
    mpv.*              one mpv instance: process, stdout beacon, JSON IPC over the pipe
    mpv_assets.cpp     generated: the Lua beacon + input.conf
  src/platform/win/    window discovery, placement, PiP styles, embedding
  src/ui/              the WinUI-flavoured drawing layer (next increment)
  src/app/             the panel: sections, rows, wiring to the core (next increment)
  tests/smoke.cpp      drives two real mpv instances through the IPC
  assets/              the extracted Lua and input.conf, kept readable
  tools/               embed_assets.py, build.bat
```

The core is deliberately UI-free so the Linux port can reuse it: only `platform/` and
`ui/` are per-OS.

## What is ported and verified

Verified by `cpp/build/sp_smoke.exe` (see `tests/smoke.cpp`), which starts real mpv
processes rather than mocking them:

- mpv, yt-dlp and ffmpeg discovery, in the same order as the Python build, with the
  rule that a missing mpv is reported as missing rather than as a bare name
- the Lua beacon and `input.conf` written from the embedded sources, so the same
  script drives mpv: `SYNCPOS`, `SYNCEOF`, `SYNCPAUSE`, `SYNCPIPDRAG`
- the launch flags, including `--input-ipc-server`, `--input-conf`, `--script`,
  `--hwdec=auto-safe`, `--keepaspect-window=no`, `--audio-pitch-correction`, the
  YouTube buffering set, and `--script-opts=ytdl_hook-ytdl_path=`
- the JSON IPC over the named pipe, with the reader thread and serialised writes the
  Python build needed (the CRT poisons a read pipe handle, and a synchronous write
  blocks once mpv's reply buffer fills)
- the stdout beacon parsed into position, duration, pause, end-of-file and PiP drag
- the sync policy: deadband, the ±5% micro-rate trim, the 0.45 s / 0.15 s thresholds,
  end-of-file and dragging suppression, and the scrub gains (0.5 s/px on the bar,
  0.02 s/px lifted)
- aligning over the IPC: seek both sides, measure the drift, confirm the loop would
  leave them alone
- window discovery by pid and title, and side-by-side arrangement without stealing
  focus or reordering windows
- the config round-trip, including a remembered alignment keyed by both sources

## What remains

Ordered by what a user notices first. Each phase is meant to end with something that
runs, not with a pile of code.

1. **The interface** (`ui/`, `app/`): theme and metrics, the Direct2D renderer, the
   controls (button, seek bar, slider, card, toggle, tooltip), then the panel sections
   - sources, transport, timelines, tracks, windows & PiP, volume, crop - and the
   status readout. This is the largest remaining piece.
2. **Dialogs**: Settings, Download, Visual Crop, Update, Help.
3. **Crop and capture**: the capture ladder (mpv screenshot -> ffmpeg -> PrintWindow ->
   hwdec off -> fullscreen screen grab) with the pixel-content verification and the
   HDR tone-mapping, plus the crop overlay and its fine-adjust buttons.
4. **PiP**: floating and integrated, pane sizing, drag following, the black-bar
   detection probe.
5. **Downloads**: yt-dlp quality list, connections, progress, repointing the reaction.
6. **Subtitles and tracks**: pickers, drag & drop, YouTube subtitle fetch.
7. **Updater**: the release check, per-component install, staged apply.
8. **Installer**: the NSIS script already builds a native Setup; it needs to package
   the C++ executable and its payload, and the updater needs to know the new file set.
9. **Tests**: the Python suites are the specification. Port them in the same order:
   the smoke first (done), then the sync/unit checks, then the GUI and deployment
   tests against the new binary.
10. **Linux**: the core is already portable; the platform and UI layers need an X11
    backend and a toolkit decision.

## Deliberate differences

- **No Windows App SDK.** Explained above.
- **Hand-written JSON.** The Python build had no third-party runtime and the app ships
  beside mpv rather than through a package manager; a JSON dependency would be the only
  one in the project.
- **The Lua beacon and input.conf are embedded, not shipped as files.** The Python build
  wrote them at startup from string constants; the C++ build does the same, generated
  from the Python source by `tools/embed_assets.py` so they cannot drift.
- **Configuration stays compatible.** Same file, same keys, so an existing install keeps
  its sources, volumes, speed and remembered alignments.

## Building

```
cpp\tools\build.bat              # app target, once the UI layer lands
cpp\tools\build.bat OFF          # core + tests only (what builds today)
cpp\tools\build.bat OFF test     # and run the smoke test
```

Requires the MSVC C++ workload (VS Build Tools 2026 or newer), Windows SDK 10.0.26100+
and CMake 3.24+. No other dependencies.

## The interface (done)

The window is a Win32 shell drawing its own Fluent surface with Direct2D and DirectWrite,
which is what "WinUI philosophy without the Windows App SDK dependency" comes to in
practice. The frame is removed (WM_NCCALCSIZE) and the title bar is drawn by the panel;
DWM still supplies the rounded corners, the dark title bar treatment and, on Windows 11,
the Mica backdrop where it is available.

Four tabs, grouped the way a WinUI settings page groups them:

- Sources: the two video fields with Browse, and Start / Play.
- Sync: transport, the three timelines with precise scrubbing, go-to, and the typed
  offset.
- Windows: arrange, floating PiP, the three volume sliders, and the shortcut list.
- Settings: dark theme, the status readout, and the About card with the real mpv and
  config paths.

Each tab lays itself out to fill the window it is given, so a short window shows a
smaller panel rather than a clipped one. The window's minimum is the largest tab's
requirement, scaled by the window's DPI - at 150% scaling that is 1200 physical pixels,
otherwise the panel would scale itself down and the type would come out smaller than the
design. The panel re-checks the client rectangle every frame, because a late
WM_DPICHANGED can resize the window after the opening clamp.

Verified by reading the rendered pixels of all four tabs: every card lands within a pixel
of the layout's own numbers, the status strip is inside the window, and the selected-tab
indicator moves across the four tab positions.

### Not yet verified

The panel's controls are wired to the same core the smoke test covers, but driving a
click into the running app from the test harness did not work (posted messages reached
the window as mouse moves but the button messages did not land), so clicking is unproven
and was not exercised. The Ctrl+1..4 and Ctrl+Tab shortcuts are implemented on the same
path and are likewise unverified.
