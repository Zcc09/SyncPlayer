"""SyncPlayer installer - self-contained, bundles SyncPlayer.exe + mpv.

Build:  python -m PyInstaller --noconfirm --clean --onefile --windowed
        --name SyncPlayer-Setup --icon icon.ico installer.py
        --add-data "bundle\\SyncPlayer.exe;." --add-data "bundle\\mpv;mpv"

Installs to %LOCALAPPDATA%\\SyncPlayer\\ (override with SYNCPLAYER_INSTALL_DIR),
bundles the mpv distribution alongside the exe so the app is self-contained
(no system mpv required), writes install.json for the updater, and creates
Desktop + Start-Menu shortcuts.
"""
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

APP_NAME = "SyncPlayer"
# The mpv release tag the bundled mpv build corresponds to (v0.41.0 mingw).
MPV_RELEASE_VERSION = "0.41.0"


def get_exe_version(path):
    """Read the FileVersion from a PE file via Win32 version APIs."""
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
        ms = fi[2]            # dwFileVersionMS
        ls = fi[3]            # dwFileVersionLS
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


def _copy_tree(src, dst):
    if not os.path.isdir(src):
        return False
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
    return True


def do_install(src_exe, src_mpv_dir, install_dir, src_updater=None):
    """Copy the app + mpv (+ optional updater) into install_dir, write install.json."""
    os.makedirs(install_dir, exist_ok=True)
    app_path = os.path.join(install_dir, APP_NAME + ".exe")
    shutil.copy2(src_exe, app_path)
    _copy_tree(src_mpv_dir, os.path.join(install_dir, "mpv"))
    updater_path = None
    if src_updater and os.path.isfile(src_updater):
        updater_path = os.path.join(install_dir, "SyncPlayer-Updater.exe")
        shutil.copy2(src_updater, updater_path)
    app_ver = get_exe_version(app_path) or "0.0.0"
    state = {
        "app_version": app_ver,
        "mpv_version": MPV_RELEASE_VERSION,
        "install_dir": os.path.abspath(install_dir),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(install_dir, "install.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return state, app_path


def _close_running():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "SyncPlayer.exe"],
                       capture_output=True, creationflags=getattr(
                           subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        pass


def main():
    # Resolve bundled payloads (frozen -> _MEIPASS; source -> script dir).
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    src_exe = os.path.join(base, "SyncPlayer.exe")
    src_mpv = os.path.join(base, "mpv")
    src_updater = os.path.join(base, "SyncPlayer-Updater.exe")

    if not os.path.isfile(src_exe):
        print("ERROR: SyncPlayer.exe not bundled - rebuild with --add-data.")
        return 1
    if not os.path.isdir(src_mpv):
        print("ERROR: mpv bundle missing - rebuild with --add-data.")
        return 1

    install_dir = os.environ.get("SYNCPLAYER_INSTALL_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "SyncPlayer")

    _close_running()
    try:
        state, app_path = do_install(src_exe, src_mpv, install_dir, src_updater)
    except Exception as e:
        print("ERROR installing: %s" % e)
        return 1

    updater_path = os.path.join(install_dir, "SyncPlayer-Updater.exe")
    has_updater = os.path.isfile(updater_path)

    # Shortcuts (skippable for testing via SYNCPLAYER_NO_SHORTCUTS=1).
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    startmenu = os.path.join(os.environ.get("APPDATA") or os.path.expanduser(
        "~"), "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME)
    made = []
    if not os.environ.get("SYNCPLAYER_NO_SHORTCUTS"):
        if create_shortcut(os.path.join(desktop, APP_NAME + ".lnk"),
                           app_path, install_dir):
            made.append("desktop")
        if create_shortcut(os.path.join(startmenu, APP_NAME + ".lnk"),
                           app_path, install_dir):
            made.append("startmenu")
        if has_updater and create_shortcut(
                os.path.join(startmenu, APP_NAME + " - Check for Updates.lnk"),
                updater_path, install_dir):
            made.append("startmenu-updater")

    print("Installed %s %s -> %s" % (APP_NAME, state["app_version"], install_dir))
    print("mpv %s bundled; updater: %s; shortcuts: %s" % (
        state["mpv_version"], has_updater, ", ".join(made) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
