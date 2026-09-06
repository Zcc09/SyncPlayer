"""SyncPlayer installer - self-contained, bundles SyncPlayer.exe + mpv (+ yt-dlp) + updater.

Build:
  python -m PyInstaller --noconfirm --clean --onefile --windowed
      --name SyncPlayer-Setup --icon icon.ico installer.py
      --add-data "bundle\\SyncPlayer.exe;."
      --add-data "bundle\\SyncPlayer-Updater.exe;."
      --add-data "bundle\\mpv;mpv"

Runs a visible GUI wizard by default (progress + success/error dialog).
For automated tests:
  SyncPlayer-Setup.exe --silent [--install-dir <path>] [--no-shortcuts]
"""
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
import time

APP_NAME = "SyncPlayer"
MPV_RELEASE_VERSION = "0.41.0"


def get_exe_version(path):
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return None
        val = ctypes.c_void_p()
        vlen = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(
                buf, "\\", ctypes.byref(val), ctypes.byref(vlen)):
            return None
        fi = ctypes.cast(val, ctypes.POINTER(ctypes.c_ulong))
        ms, ls = fi[2], fi[3]
        return "%d.%d.%d" % ((ms >> 16) & 0xFFFF, ms & 0xFFFF,
                             (ls >> 16) & 0xFFFF)
    except Exception:
        return None


def _powershell(script):
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True, capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return True
    except Exception:
        return False


def create_shortcut(lnk_path, target, workdir, icon=None):
    lnk_path = os.path.abspath(lnk_path)
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    icon_arg = ',0' if icon else ''
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut('%s'); "
        "$sc.TargetPath = '%s'; "
        "$sc.WorkingDirectory = '%s'; "
        "$sc.IconLocation = '%s%s'; "
        "$sc.Save();"
    ) % (lnk_path, target, workdir, icon or target, icon_arg)
    return _powershell(script)


def _copy_tree(src, dst, progress=None):
    if not os.path.isdir(src):
        return False
    os.makedirs(dst, exist_ok=True)
    names = os.listdir(src)
    total = max(len(names), 1)
    for i, name in enumerate(names):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
        if progress:
            progress(i + 1, total)
    return True


def do_install(src_exe, src_mpv_dir, install_dir, src_updater=None,
               progress=None):
    os.makedirs(install_dir, exist_ok=True)
    cb = progress or (lambda a, b: None)
    app_path = os.path.join(install_dir, APP_NAME + ".exe")
    cb(1, 5)
    shutil.copy2(src_exe, app_path)
    cb(2, 5)
    _copy_tree(src_mpv_dir, os.path.join(install_dir, "mpv"),
               progress=lambda a, b: cb(2 + a, 5 + b))
    cb(4, 5)
    updater_path = None
    if src_updater and os.path.isfile(src_updater):
        updater_path = os.path.join(install_dir, "SyncPlayer-Updater.exe")
        shutil.copy2(src_updater, updater_path)
    app_ver = get_exe_version(app_path) or "0.0.0"
    ytdlp_exe = os.path.join(install_dir, "mpv", "yt-dlp.exe")
    ytdlp_ver = "0"
    if os.path.isfile(ytdlp_exe):
        try:
            r = subprocess.run([ytdlp_exe, "--version"], capture_output=True,
                               text=True, timeout=20,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            ytdlp_ver = (r.stdout or "").strip() or "0"
        except Exception:
            ytdlp_ver = "0"
    state = {
        "app_version": app_ver,
        "mpv_version": MPV_RELEASE_VERSION,
        "ytdlp_version": ytdlp_ver,
        "install_dir": os.path.abspath(install_dir),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(install_dir, "install.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    if progress:
        progress(5, 5)
    return state, app_path, updater_path


def _close_running():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "SyncPlayer.exe"],
                       capture_output=True, creationflags=getattr(
                           subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def resolve_payloads():
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    src_exe = os.path.join(base, "SyncPlayer.exe")
    src_mpv = os.path.join(base, "mpv")
    src_updater = os.path.join(base, "SyncPlayer-Updater.exe")
    return src_exe, src_mpv, src_updater


def make_shortcuts(app_path, install_dir, updater_path):
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    startmenu = os.path.join(os.environ.get("APPDATA") or os.path.expanduser(
        "~"), "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME)
    made = []
    if create_shortcut(os.path.join(desktop, APP_NAME + ".lnk"),
                       app_path, install_dir):
        made.append("desktop")
    if create_shortcut(os.path.join(startmenu, APP_NAME + ".lnk"),
                       app_path, install_dir):
        made.append("startmenu")
    if updater_path and create_shortcut(
            os.path.join(startmenu, APP_NAME + " - Check for Updates.lnk"),
            updater_path, install_dir):
        made.append("startmenu-updater")
    return made


def _cli_main(install_dir, no_shortcuts, launch):
    src_exe, src_mpv, src_updater = resolve_payloads()
    if not os.path.isfile(src_exe):
        print("ERROR: SyncPlayer.exe not bundled - rebuild with --add-data.")
        return 1
    if not os.path.isdir(src_mpv):
        print("ERROR: mpv bundle missing - rebuild with --add-data.")
        return 1
    _close_running()
    try:
        state, app_path, updater_path = do_install(
            src_exe, src_mpv, install_dir, src_updater)
    except Exception as e:
        print("ERROR installing: %s" % e)
        return 1
    made = [] if no_shortcuts else make_shortcuts(app_path, install_dir,
                                                  updater_path)
    print("Installed %s %s -> %s" % (APP_NAME, state["app_version"], install_dir))
    print("mpv %s bundled; updater: %s; shortcuts: %s" % (
        state["mpv_version"], bool(updater_path), ", ".join(made) or "none"))
    if launch:
        try:
            subprocess.Popen([app_path])
        except Exception:
            pass
    return 0


def _gui_main(install_dir, no_shortcuts, launch):
    import threading
    import tkinter as tk
    from tkinter import ttk, messagebox
    root = tk.Tk()
    root.title(APP_NAME + " Setup")
    root.geometry("480x220")
    root.resizable(False, False)
    root.configure(bg="#1f232b")

    status = tk.StringVar(value="Preparing to install...")
    tk.Label(root, textvariable=status, fg="#e8e8ea", bg="#1f232b",
             anchor="w").pack(fill="x", padx=16, pady=(16, 6))
    bar = ttk.Progressbar(root, maximum=100, mode="indeterminate")
    bar.pack(fill="x", padx=16, pady=4)
    detail = tk.StringVar(value="")
    tk.Label(root, textvariable=detail, fg="#9aa0a8", bg="#1f232b",
             anchor="w").pack(fill="x", padx=16, pady=(2, 8))
    btn_launch = ttk.Button(root, text="Launch SyncPlayer", state="disabled")
    btn_launch.pack(side="left", padx=16, pady=(0, 16))
    btn_close = ttk.Button(root, text="Close", command=root.destroy)
    btn_close.pack(side="right", padx=16, pady=(0, 16))

    queue = []

    def poll():
        while queue:
            item = queue.pop(0)
            kind, payload = item
            if kind == "status":
                status.set(payload)
            elif kind == "detail":
                detail.set(payload)
            elif kind == "done":
                bar.stop()
                bar["value"] = 100
                btn_launch.config(state="normal")
                btn_close.config(state="normal")
            elif kind == "error":
                bar.stop()
                btn_launch.config(state="disabled")
                btn_close.config(state="normal")
                messagebox.showerror(APP_NAME, payload)
        root.after(80, poll)

    def worker():
        try:
            src_exe, src_mpv, src_updater = resolve_payloads()
            if not os.path.isfile(src_exe):
                queue.append(("error", "SyncPlayer.exe is missing from the "
                                       "installer payload."))
                return
            if not os.path.isdir(src_mpv):
                queue.append(("error", "mpv bundle is missing from the "
                                       "installer payload."))
                return
            bar.start()
            _close_running()
            queue.append(("status", "Installing..."))
            state, app_path, updater_path = do_install(
                src_exe, src_mpv, install_dir, src_updater,
                progress=lambda a, b: queue.append(
                    ("detail", "Copying files (%d/%d)..." % (a, b))))
            if not no_shortcuts:
                queue.append(("detail", "Creating shortcuts..."))
                make_shortcuts(app_path, install_dir, updater_path)
            queue.append(("status", "SyncPlayer %s installed successfully."
                                    % state["app_version"]))
            queue.append(("detail", "Install path: %s" % install_dir))
            queue.append(("done", None))
            if launch:
                try:
                    subprocess.Popen([app_path])
                except Exception:
                    pass
        except Exception as e:
            queue.append(("error", "Install failed: %s" % e))

    threading.Thread(target=worker, daemon=True).start()
    poll()
    root.mainloop()


def main(argv):
    install_dir = os.environ.get("SYNCPLAYER_INSTALL_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "SyncPlayer")
    no_shortcuts = "--no-shortcuts" in argv or bool(
        os.environ.get("SYNCPLAYER_NO_SHORTCUTS"))
    launch = "--launch" in argv
    if "--install-dir" in argv:
        try:
            i = argv.index("--install-dir")
            install_dir = os.path.abspath(argv[i + 1])
        except Exception:
            pass
    if "--silent" in argv:
        return _cli_main(install_dir, no_shortcuts, launch)
    try:
        _gui_main(install_dir, no_shortcuts, launch)
        return 0
    except Exception as e:
        print("GUI unavailable (%s); falling back to silent install." % e)
        return _cli_main(install_dir, no_shortcuts, launch)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
