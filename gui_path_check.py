"""Cover the GUI path: the wizard a user actually clicks.

install_smoke.py drives the silent install only. An earlier (pre-fix) run against the
GUI path showed install.json with an empty app_version and the Start Menu folder
recorded as the yt-dlp version, while the silent path was fine - so the wizard needs
its own check. It installs to a temporary destination, never the real one.
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "dist", "SyncPlayer-Setup.exe")
APP = "SyncPlayer"

u32 = ctypes.WinDLL("user32", use_last_error=True)
BM_CLICK = 0x00F5
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
ok = []


def check(name, cond, detail=""):
    ok.append((name, bool(cond)))
    print("  %-4s %s%s" % ("ok" if cond else "FAIL", name,
                           ("   [%s]" % detail) if detail else ""))


def windows_of_pid(pid):
    found = []

    def cb(h, _l):
        p = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid:
            n = u32.GetWindowTextLengthW(h)
            buf = ctypes.create_unicode_buffer(n + 2)
            u32.GetWindowTextW(h, buf, n + 2)
            cls = ctypes.create_unicode_buffer(128)
            u32.GetClassNameW(h, cls, 128)
            found.append((h, buf.value, cls.value))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), None)
    return found


def children(hwnd):
    found = []

    def cb(h, _l):
        n = u32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(h, buf, n + 2)
        cls = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(h, cls, 128)
        found.append((h, buf.value, cls.value, bool(u32.IsWindowEnabled(h))))
        return True

    u32.EnumChildWindows(hwnd, WNDENUMPROC(cb), None)
    return found


def find(hwnd, needle):
    for h, text, cls, enabled in children(hwnd):
        if cls.lower() == "button" and needle in text.lower():
            return h, text, enabled
    return None, "", False


def main():
    dest = os.path.join(tempfile.gettempdir(), "sp_nsis_gui_path")
    shutil.rmtree(dest, ignore_errors=True)
    print("dest: %s\nSetup: %.1f MB" % (dest, os.path.getsize(SETUP) / 1048576))

    proc = subprocess.Popen([SETUP, "--install-dir", dest, "--no-launch"],
                            creationflags=0x08000000)
    hwnd, title = None, ""
    for _ in range(60):
        time.sleep(0.5)
        for h, t, cls in windows_of_pid(proc.pid):
            if cls.lower() == "#32770" and t:
                hwnd, title = h, t
                break
        if hwnd:
            break
    check("the wizard window appeared", hwnd is not None, title)
    if hwnd is None:
        return 1
    print("  window title: %r" % title)

    pages, run_box = [], None
    for _ in range(40):
        # the Run checkbox only exists on the Finish page
        h, text, _en = find(hwnd, "run " + APP.lower())
        if h:
            run_box = text
        fin, ftext, fen = find(hwnd, "finish")
        if fin and fen:
            pages.append(ftext)
            print("  clicking %r" % ftext)
            u32.SendMessageW(fin, BM_CLICK, 0, 0)
            break
        nxt, ntext, nen = find(hwnd, "next")
        if nxt and nen:
            pages.append(ntext)
            print("  clicking %r" % ntext)
            u32.SendMessageW(nxt, BM_CLICK, 0, 0)
            time.sleep(1.2)
            continue
        # MUI labels the last configuration page's button "Install", not "Next"
        ins, itext, ien = find(hwnd, "install")
        if ins and ien:
            pages.append(itext)
            print("  clicking %r (last configuration page)" % itext)
            u32.SendMessageW(ins, BM_CLICK, 0, 0)
            time.sleep(2.0)
            continue
        time.sleep(1.0)
    print("  pages walked: %s" % pages)

    for _ in range(90):
        if proc.poll() is not None:
            break
        time.sleep(1)
    check("the wizard finished (exit 0)", proc.poll() == 0,
          "exit %s - 1 means it was closed or aborted" % proc.poll())
    check("it offered the Run checkbox on the Finish page", run_box is not None,
          run_box)
    check("the GUI install really installed",
          os.path.isfile(os.path.join(dest, APP + ".exe")))
    check("it shipped mpv", os.path.isfile(os.path.join(dest, "mpv", "mpv.exe")))
    check("it shipped ffmpeg", os.path.isfile(os.path.join(dest, "mpv", "ffmpeg.exe")))

    raw = ""
    p = os.path.join(dest, "install.json")
    if os.path.isfile(p):
        raw = open(p, encoding="utf-8", errors="replace").read()
    try:
        st = json.loads(raw)
        check("install.json is valid JSON", True)
    except Exception as e:
        st = {}
        check("install.json is valid JSON", False, "%s | raw=%r" % (e, raw[:220]))
    print("  install.json: %s" % json.dumps(st, sort_keys=True)[:240])
    check("app_version recorded from the GUI path",
          str(st.get("app_version", "")).startswith("1.6."), st.get("app_version"))
    check("ytdlp_version is a version, not a folder name",
          str(st.get("ytdlp_version", "")).replace(".", "").isdigit(),
          st.get("ytdlp_version"))
    check("startmenu_folder is the app name",
          st.get("startmenu_folder") == APP, st.get("startmenu_folder"))
    check("component flags are booleans",
          st.get("mpv_installed") is True and st.get("ytdlp_installed") is True
          and st.get("ffmpeg_installed") is True, 
          {k: st.get(k) for k in ("mpv_installed", "ytdlp_installed",
                                  "ffmpeg_installed")})
    check("install_dir round-trips",
          os.path.normcase(st.get("install_dir", "")) == os.path.normcase(dest),
          st.get("install_dir"))

    shutil.rmtree(dest, ignore_errors=True)
    n = sum(1 for _k, v in ok if v)
    print("\n%d/%d GUI-path checks passed" % (n, len(ok)))
    for k, v in ok:
        if not v:
            print("  FAILED:", k)
    return 0 if n == len(ok) else 1


if __name__ == "__main__":
    sys.exit(main())
