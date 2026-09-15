"""SyncPlayer setup wizard (Windows).

Self-contained: bundles SyncPlayer.exe + the mpv distro (with yt-dlp) + the
update checker. Walks the user through the same steps most Windows installers
do - where to install, what to install, which shortcuts to create - and
registers a proper Add/Remove Programs entry so it can be uninstalled later
(SyncPlayer.exe --uninstall).

Build:
  python -m PyInstaller --noconfirm --clean --onefile --windowed \
      --name SyncPlayer-Setup --icon icon.ico installer.py \
      --add-data "bundle\\SyncPlayer.exe;." \
      --add-data "bundle\\SyncPlayer-Updater.exe;." \
      --add-data "bundle\\mpv;mpv"

Automation (used by install_test.py):
  SyncPlayer-Setup.exe --silent --install-dir <path> --no-mpv --no-ytdlp \
                      --no-ffmpeg \
      --no-updater --no-desktop-shortcut --no-startmenu-shortcut --launch --json
"""
import ctypes
from ctypes import wintypes
import json
import re
import os
import shutil
import subprocess
import sys
import threading
import time

APP_NAME = "SyncPlayer"
MPV_RELEASE_VERSION = "0.41.0"
PUBLISHER = "SyncPlayer"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SyncPlayer"
# room for the app + the mpv distro + yt-dlp, unpacked
REQUIRED_MB = 400

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
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


def _powershell(script, timeout=30):
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            check=True, capture_output=True, timeout=timeout,
            creationflags=CREATE_NO_WINDOW)
        return True
    except Exception:
        return False


def create_shortcut(lnk_path, target, workdir, icon=None, args=None):
    lnk_path = os.path.abspath(lnk_path)
    os.makedirs(os.path.dirname(lnk_path), exist_ok=True)
    icon_arg = ",0" if icon else ""
    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        "$sc = $ws.CreateShortcut('%s'); "
        "$sc.TargetPath = '%s'; "
        "$sc.WorkingDirectory = '%s'; "
        "$sc.Arguments = '%s'; "
        "$sc.IconLocation = '%s%s'; "
        "$sc.Save();"
    ) % (lnk_path, target, workdir, args or "", icon or target, icon_arg)
    return _powershell(script)


def _copy_tree(src, dst, progress=None, skip=()):
    """Copy a directory tree, skipping any top-level names in `skip`."""
    if not os.path.isdir(src):
        return False
    os.makedirs(dst, exist_ok=True)
    names = [n for n in os.listdir(src) if n not in skip]
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


def _close_running():
    """Close a running SyncPlayer so its exe can be replaced."""
    try:
        subprocess.run(["taskkill", "/F", "/IM", APP_NAME + ".exe"],
                       capture_output=True, creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass


def default_install_dir():
    """Per-user default - no administrator prompt, like most per-user setups."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def detect_existing_install():
    """(version, install_dir) of a copy Setup can update, else (None, None).

    Checks the Add/Remove entry Setup writes, then the folders Setup uses (the
    current default and the one earlier versions used). Keeping the user's
    existing folder is what "run Setup again" is expected to do - silently
    installing a second copy somewhere else is not."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            d = winreg.QueryValueEx(k, "InstallLocation")[0]
            v = winreg.QueryValueEx(k, "DisplayVersion")[0]
        if d and os.path.isfile(os.path.join(d, APP_NAME + ".exe")):
            return v, os.path.abspath(d)
    except Exception:
        pass
    la = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    for cand in (default_install_dir(), os.path.join(la, APP_NAME)):
        exe = os.path.join(cand, APP_NAME + ".exe")
        if os.path.isfile(exe):
            ver = None
            try:
                with open(os.path.join(cand, "install.json"), encoding="utf-8") as f:
                    ver = json.load(f).get("app_version")
            except Exception:
                ver = get_exe_version(exe)
            return ver, os.path.abspath(cand)
    return None, None


def resolve_payloads():
    if getattr(sys, "frozen", False):
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    src_exe = os.path.join(base, "SyncPlayer.exe")
    src_mpv = os.path.join(base, "mpv")
    src_updater = os.path.join(base, "SyncPlayer-Updater.exe")
    return src_exe, src_mpv, src_updater


def _protected_reason(path):
    """Why Setup should not write into this folder, or None when it is fine.

    Windows refuses these without elevation, and failing halfway through a 120 MB
    copy with a raw OSError is not something an installer should do.
    """
    p = os.path.abspath(path).lower().rstrip("\\/")
    drive, tail = os.path.splitdrive(p)
    home = os.path.expanduser("~").lower().rstrip("\\/")
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("ProgramData"), os.environ.get("SystemRoot"),
                 os.environ.get("ProgramW6432")):
        b = base.lower().rstrip("\\/")
        if base and (p == b or p.startswith(b + "\\")):
            return ("Setup cannot install %s into a protected system folder:\n\n%s\n\n"
                    "Choose a folder inside your user profile instead - the default\n"
                    "(%%LOCALAPPDATA%%\\Programs\\%s) needs no administrator rights."
                    % (APP_NAME, path, APP_NAME))
    if p == home:
        return ("Please choose a folder for %s rather than your profile root:\n\n%s"
                % (APP_NAME, path))
    if tail in ("", "\\", "/"):
        return ("Please choose a folder for %s rather than a drive root:\n\n%s"
                % (APP_NAME, path))
    return None


def _bytes_text(mb):
    """Human units: installers show a 1.5 TB drive as GB/TB, not 1556377 MB."""
    if mb >= 1024 * 1024:
        return "%.1f TB" % (mb / (1024.0 * 1024.0))
    if mb >= 1024:
        return "%.1f GB" % (mb / 1024.0)
    return "%.1f MB" % mb


def payload_mb(src):
    """Approximate unpacked size of the payload, for the space check."""
    total = 0
    for p in src:
        if os.path.isfile(p):
            total += os.path.getsize(p)
        elif os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    try:
                        total += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
    return total / (1024.0 * 1024.0)


def free_mb(path):
    try:
        drive = os.path.splitdrive(os.path.abspath(path))[0] or "C:"
        return shutil.disk_usage(drive + "\\").free / (1024.0 * 1024.0)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# what to install
# ---------------------------------------------------------------------------
class Options(object):
    def __init__(self):
        self.install_dir = default_install_dir()
        # updating an existing copy is the common case: keep its folder
        self.existing = detect_existing_install()          # (version, dir)
        if self.existing[0] and self.existing[1]:
            self.install_dir = self.existing[1]
        self.mpv = True              # the player itself (recommended)
        self.ytdlp = True            # YouTube / URL sources
        # ffmpeg is what lets yt-dlp MERGE separate video+audio streams: it is
        # how anything above ~720p is published, so without it downloads are
        # capped at single-file (usually 720p) quality.
        self.ffmpeg = True
        self.updater = True          # "Check for Updates"
        self.desktop_shortcut = True
        self.startmenu_shortcut = True
        self.startmenu_folder = APP_NAME     # Start Menu folder the user picked
        self.launch = True

    def describe(self):
        comps = [APP_NAME]
        if self.mpv:
            comps.append("mpv")
        if self.ytdlp:
            comps.append("yt-dlp")
        if self.ffmpeg:
            comps.append("ffmpeg")
        if self.updater:
            comps.append("updater")
        return ", ".join(comps)


def do_install(opts, src, progress=None):
    """Copy the selected components into opts.install_dir. Returns the state."""
    src_exe, src_mpv, src_updater = src
    cb = progress or (lambda pct, text: None)
    install_dir = os.path.abspath(opts.install_dir)
    os.makedirs(install_dir, exist_ok=True)

    app_path = os.path.join(install_dir, APP_NAME + ".exe")
    cb(6, "Installing %s..." % APP_NAME)
    shutil.copy2(src_exe, app_path)

    mpv_dir = os.path.join(install_dir, "mpv")
    skip_names = []
    if not opts.ytdlp:
        skip_names.append("yt-dlp.exe")
    if not opts.ffmpeg:
        skip_names.append("ffmpeg.exe")
    if opts.mpv:
        cb(18, "Installing mpv %s..." % MPV_RELEASE_VERSION)
        _copy_tree(src_mpv, mpv_dir, skip=tuple(skip_names),
                   progress=lambda a, b: cb(18 + int(48.0 * a / max(b, 1)),
                                            "Installing mpv... (%d/%d)" % (a, b)))
    elif opts.ytdlp or opts.ffmpeg:
        # both tools normally ship inside the mpv payload; keep the app's
        # discovery order (mpv/yt-dlp.exe, mpv/ffmpeg.exe) working without
        # installing mpv itself
        os.makedirs(mpv_dir, exist_ok=True)
        for _name in ("yt-dlp.exe", "ffmpeg.exe"):
            if _name in skip_names:
                continue
            _src = os.path.join(src_mpv, _name)
            if os.path.isfile(_src):
                shutil.copy2(_src, os.path.join(mpv_dir, _name))

    ytdlp_exe = os.path.join(mpv_dir, "yt-dlp.exe")
    ytdlp_ver = "0"
    if opts.ytdlp and os.path.isfile(ytdlp_exe):
        cb(70, "Checking yt-dlp...")
        try:
            r = subprocess.run([ytdlp_exe, "--version"], capture_output=True,
                               text=True, timeout=20,
                               creationflags=CREATE_NO_WINDOW, errors="replace")
            ytdlp_ver = (r.stdout or "").strip() or "0"
        except Exception:
            ytdlp_ver = "0"

    ffmpeg_exe = os.path.join(mpv_dir, "ffmpeg.exe")
    ffmpeg_ver = "0"
    if opts.ffmpeg and os.path.isfile(ffmpeg_exe):
        cb(74, "Checking ffmpeg...")
        try:
            _r = subprocess.run([ffmpeg_exe, "-version"], capture_output=True,
                                text=True, timeout=20,
                                creationflags=CREATE_NO_WINDOW, errors="replace")
            _m = re.search(r"version\s+(\S+)", (_r.stdout or "").split("\n")[0])
            ffmpeg_ver = _m.group(1).split("-")[0] if _m else "0"
        except Exception:
            ffmpeg_ver = "0"

    updater_path = None
    if opts.updater and src_updater and os.path.isfile(src_updater):
        cb(78, "Installing the update checker...")
        updater_path = os.path.join(install_dir, APP_NAME + "-Updater.exe")
        shutil.copy2(src_updater, updater_path)

    app_ver = get_exe_version(app_path) or "0.0.0"
    state = {
        "app_version": app_ver,
        "mpv_version": MPV_RELEASE_VERSION if opts.mpv else "0",
        "mpv_installed": bool(opts.mpv),
        "ytdlp_version": ytdlp_ver if opts.ytdlp else "0",
        "ytdlp_installed": bool(opts.ytdlp and os.path.isfile(ytdlp_exe)),
        "ffmpeg_version": ffmpeg_ver if opts.ffmpeg else "0",
        "ffmpeg_installed": bool(opts.ffmpeg and os.path.isfile(ffmpeg_exe)),
        "updater_installed": bool(updater_path),
        "install_dir": install_dir,
        "startmenu_folder": (opts.startmenu_folder if opts.startmenu_shortcut
                             else ""),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(install_dir, "install.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return state, app_path, updater_path


def startmenu_dir(folder=None):
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "Microsoft", "Windows", "Start Menu", "Programs",
                        folder or APP_NAME)


def make_shortcuts(app_path, install_dir, updater_path, desktop=True,
                   startmenu=True, startmenu_folder=None):
    made = []
    if desktop:
        if create_shortcut(os.path.join(os.path.expanduser("~"), "Desktop",
                                        APP_NAME + ".lnk"),
                           app_path, install_dir):
            made.append("desktop")
    if startmenu:
        sm = startmenu_dir(startmenu_folder)
        if create_shortcut(os.path.join(sm, APP_NAME + ".lnk"),
                           app_path, install_dir):
            made.append("startmenu")
        if updater_path and create_shortcut(
                os.path.join(sm, APP_NAME + " - Check for Updates.lnk"),
                updater_path, install_dir):
            made.append("startmenu-updater")
        if create_shortcut(os.path.join(sm, "Uninstall %s.lnk" % APP_NAME),
                           app_path, install_dir, args="--uninstall"):
            made.append("startmenu-uninstall")
    return made


# --------------------------------------------------------- Add/Remove entry --
def register_uninstall(install_dir, app_path, version):
    """Per-user Add/Remove Programs entry (HKCU: no admin needed)."""
    try:
        import winreg
    except Exception:
        return False
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY, 0,
                                 winreg.KEY_WRITE)
    except Exception:
        return False
    try:
        size_kb = 0
        for root, _dirs, files in os.walk(install_dir):
            for f in files:
                try:
                    size_kb += os.path.getsize(os.path.join(root, f)) // 1024
                except OSError:
                    pass
        q = '"%s"' % app_path
        vals = [
            ("DisplayName", APP_NAME),
            ("DisplayVersion", version or "0.0.0"),
            ("Publisher", PUBLISHER),
            ("InstallLocation", install_dir),
            ("DisplayIcon", app_path + ",0"),
            ("UninstallString", q + " --uninstall"),
            ("QuietUninstallString", q + " --uninstall --silent"),
            ("EstimatedSize", int(size_kb)),
            ("NoModify", 1),
            ("NoRepair", 1),
        ]
        with key:
            for name, value in vals:
                winreg.SetValueEx(key, name, 0,
                                  winreg.REG_DWORD if isinstance(value, int)
                                  else winreg.REG_SZ, value)
        return True
    except Exception:
        return False


def unregister_uninstall():
    try:
        import winreg
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# silent / CLI install (also used by the tests)
# ---------------------------------------------------------------------------
def parse_options(argv):
    opts = Options()
    if "--install-dir" in argv:
        try:
            i = argv.index("--install-dir")
            opts.install_dir = os.path.abspath(argv[i + 1])
        except Exception:
            pass
    env_dir = os.environ.get("SYNCPLAYER_INSTALL_DIR")
    if env_dir:
        opts.install_dir = os.path.abspath(env_dir)
    if "--no-shortcuts" in argv or os.environ.get("SYNCPLAYER_NO_SHORTCUTS"):
        opts.desktop_shortcut = opts.startmenu_shortcut = False
    for flag, attr in (("--no-mpv", "mpv"), ("--no-ytdlp", "ytdlp"),
                       ("--no-ffmpeg", "ffmpeg"),
                       ("--no-updater", "updater"),
                       ("--no-desktop-shortcut", "desktop_shortcut"),
                       ("--no-startmenu-shortcut", "startmenu_shortcut")):
        if flag in argv:
            setattr(opts, attr, False)
    if "--startmenu-folder" in argv:
        try:
            i = argv.index("--startmenu-folder")
            opts.startmenu_folder = argv[i + 1]
        except Exception:
            pass
    if "--no-launch" in argv:
        opts.launch = False
    if "--launch" in argv:
        opts.launch = True
    return opts


def _cli_main(opts, as_json=False):
    src = resolve_payloads()
    if not os.path.isfile(src[0]):
        print("ERROR: SyncPlayer.exe not bundled - rebuild with --add-data.")
        return 1
    if not os.path.isdir(src[1]):
        print("ERROR: mpv bundle missing - rebuild with --add-data.")
        return 1
    _close_running()
    try:
        state, app_path, updater_path = do_install(
            opts, src, progress=lambda pct, text: print("  %3d%%  %s" % (pct, text)))
    except Exception as e:
        print("ERROR installing: %s" % e)
        return 1
    state["shortcuts"] = make_shortcuts(
        app_path, opts.install_dir, updater_path,
        desktop=opts.desktop_shortcut, startmenu=opts.startmenu_shortcut,
        startmenu_folder=opts.startmenu_folder)
    state["uninstall_registered"] = register_uninstall(
        opts.install_dir, app_path, state["app_version"])
    state["components"] = opts.describe()
    if as_json:
        print(json.dumps(state, indent=2))
    else:
        print("Installed %s %s -> %s" % (APP_NAME, state["app_version"],
                                         opts.install_dir))
        print("mpv: %s | yt-dlp: %s | updater: %s | shortcuts: %s"
              % (state["mpv_installed"], state["ytdlp_installed"],
                 state["updater_installed"], ", ".join(state["shortcuts"]) or "none"))
    if opts.launch:
        try:
            subprocess.Popen([app_path])
        except Exception:
            pass
    return 0


# ---------------------------------------------------------------------------
# wizard
#
# A conventional Windows setup wizard: fixed-size window, white content area,
# page title + description in the header, and Back / Next / Cancel in the footer
# in the order every other installer puts them. ttk's native theme is left alone
# so buttons, entries and the progress bar are the system ones, and the page flow
# is the usual one:
#
#   Welcome -> [License] -> Destination -> Start Menu -> Tasks -> Ready to
#   Install -> Installing -> Finish
# ---------------------------------------------------------------------------
WIZ_W, WIZ_H = 500, 366


def _find_asset(name):
    """icon.png / LICENSE beside the script or inside the onefile payload."""
    cands = []
    for base in (getattr(sys, "_MEIPASS", None),
                 os.path.dirname(os.path.abspath(__file__)),
                 os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None):
        if base:
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return None


class Wizard(object):
    """Welcome -> [License] -> Destination -> Start Menu -> Tasks -> Ready ->
    Installing -> Finish (the License page only appears when one is shipped)."""

    def __init__(self, root, opts):
        import tkinter as tk
        from tkinter import ttk, font as tkfont
        self.tk, self.ttk, self.tkfont = tk, ttk, tkfont
        self.root, self.opts = root, opts
        self.page = 0
        self.error = None
        self.state = None
        self.app_path = None
        self.queue = []
        self._poll_id = None
        self.src = resolve_payloads()
        self.version = get_exe_version(self.src[0]) or ""

        self.BG = "#f0f0f0"          # window face
        self.PANEL = "#ffffff"       # content area
        self.FG = "#1a1a1a"
        self.DIM = "#4d4d4d"
        self.ACCENT = "#0a3d91"      # group-box headings

        base = tkfont.nametofont("TkDefaultFont")
        self.F_SUB = base.copy()
        self.F_TITLE = base.copy()
        self.F_TITLE.configure(size=base.cget("size") + 2, weight="bold")
        self.F_HEAD = base.copy()
        self.F_HEAD.configure(weight="bold")
        self.F_MONO = None
        try:
            self.F_MONO = tkfont.nametofont("TkFixedFont").copy()
        except Exception:
            pass

        # ttk widgets paint the theme's background, which would show as grey
        # patches on the white content area - give them a white-bodied style
        style = ttk.Style()
        for base in ("TCheckbutton", "TRadiobutton", "TLabel", "TFrame",
                     "TSeparator"):
            try:
                style.configure("Body." + base, background=self.PANEL)
                style.map("Body." + base,
                          background=[("active", self.PANEL),
                                      ("disabled", self.PANEL)])
            except Exception:
                pass
        try:
            style.configure("Body.TEntry", fieldbackground="#ffffff")
        except Exception:
            pass

        self.license_text = self._read_license()
        self.PAGES = tuple(["welcome"] + (["license"] if self.license_text else [])
                           + ["dest", "startmenu", "tasks", "ready", "installing",
                              "finish"])

        root.title("Setup - %s%s" % (APP_NAME, (" " + self.version) if self.version else ""))
        root.resizable(False, False)
        root.configure(bg=self.BG)
        self._set_icon()
        root.protocol("WM_DELETE_WINDOW", self.cancel)
        root.bind("<Return>", lambda e: self._default())
        root.bind("<Escape>", lambda e: self.cancel())
        # whoever destroys the window, our progress poll must not outlive it
        root.bind("<Destroy>", self._on_destroy)

        # The footer is packed FIRST (side=bottom) so a tall page can never
        # push Back / Next / Cancel out of the window.
        foot = tk.Frame(root, bg=self.BG)
        foot.pack(side="bottom", fill="x", padx=12, pady=(0, 10))
        self.foot_rule = ttk.Separator(root)
        self.foot_rule.pack(side="bottom", fill="x", pady=(6, 0))
        self.btn_cancel = ttk.Button(foot, text="Cancel", width=10, command=self.cancel)
        self.btn_cancel.pack(side="right")
        self.btn_next = ttk.Button(foot, text="Next >", width=10, command=self.next)
        self.btn_next.pack(side="right", padx=(0, 8))
        self.btn_back = ttk.Button(foot, text="< Back", width=10, command=self.back)
        self.btn_back.pack(side="right", padx=(0, 8))

        head = tk.Frame(root, bg=self.PANEL)
        head.pack(side="top", fill="x")
        self.head_icon = self._load_icon(head)
        self.head_icon_pad = 34 if self.head_icon is not None else 0
        if self.head_icon is None:
            tk.Frame(head, bg=self.PANEL, height=44, width=28).pack(side="left",
                                                                    padx=(16, 12))
        ht = tk.Frame(head, bg=self.PANEL)
        ht.pack(side="left", fill="both", expand=True, pady=(12, 10))
        self.hdr_title = tk.Label(ht, text="", bg=self.PANEL, fg=self.FG,
                                  anchor="w", font=self.F_TITLE)
        self.hdr_title.pack(fill="x")
        self.hdr_sub = tk.Label(ht, text="", bg=self.PANEL, fg=self.DIM, anchor="w",
                                justify="left", font=self.F_SUB,
                                wraplength=WIZ_W - 110)
        self.hdr_sub.pack(fill="x", pady=(1, 0))
        ttk.Separator(root).pack(fill="x")

        self.body = tk.Frame(root, bg=self.PANEL)
        self.body.pack(side="top", fill="both", expand=True, padx=16, pady=(10, 4))

        self.frames = {}
        self._build_welcome()
        if self.license_text:
            self._build_license()
        self._build_dest()
        self._build_startmenu()
        self._build_tasks()
        self._build_ready()
        self._build_installing()
        self._build_finish()

        # Fit the window to the tallest page rather than guessing a height: a
        # page that needs more room than the others must not clip its controls.
        need = WIZ_H
        for n, f in self.frames.items():
            f.pack(fill="both", expand=True)
            root.update_idletasks()
            need = max(need, f.winfo_reqheight() + self._chrome_height())
            f.pack_forget()
        w, h = WIZ_W, need
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2),
                                       max(0, (sh - h) // 3)))
        self.show(0)

    def _chrome_height(self):
        """Header + footer + padding the body does not include."""
        try:
            return (self.hdr_title.winfo_reqheight() + self.hdr_sub.winfo_reqheight()
                    + self.btn_next.winfo_reqheight()
                    + getattr(self, "head_icon_pad", 34) + 40)
        except Exception:
            return 120

    # -- helpers -------------------------------------------------------------
    def _read_license(self):
        """The project's own licence text, when there is one to show."""
        for name in ("LICENSE", "LICENSE.txt", "LICENSE.md", "COPYING"):
            p = _find_asset(name)
            if p:
                try:
                    with open(p, encoding="utf-8", errors="replace") as f:
                        return f.read().strip()
                except Exception:
                    pass
        return ""

    def _set_icon(self):
        """Window icon: the exe's own icon when frozen, icon.ico from source."""
        try:
            ico = _find_asset("icon.ico")
            if ico:
                self.root.iconbitmap(default=ico)
            elif getattr(sys, "frozen", False):
                self.root.iconbitmap(default=sys.executable)
        except Exception:
            pass

    def _load_icon(self, parent):
        """32x32 header icon (Tk 8.6 reads PNG without extra deps)."""
        p = _find_asset("icon.png")
        if not p:
            return None
        try:
            img = self.tk.PhotoImage(file=p)
            if img.width() > 32:
                img = img.subsample(max(1, int(round(img.width() / 32.0))))
            lbl = self.tk.Label(parent, image=img, bg=self.PANEL)
            lbl.pack(side="left", padx=(16, 12), pady=(12, 10))
            return img
        except Exception:
            return None

    def _page(self, name):
        f = self.tk.Frame(self.body, bg=self.PANEL)
        self.frames[name] = f
        return f

    def _group(self, parent, title, pady=(6, 10)):
        """Etched group box with a bold caption, like the Windows wizards use."""
        box = self.tk.Frame(parent, bg=self.PANEL)
        box.pack(fill="x", pady=pady)
        self.tk.Label(box, text=" " + title + " ", bg=self.PANEL, fg=self.ACCENT,
                      font=self.F_HEAD, anchor="w").pack(fill="x")
        inner = self.tk.Frame(box, bg=self.PANEL, highlightthickness=1,
                              highlightbackground="#c8c8c8")
        inner.pack(fill="x", pady=(2, 0))
        return inner

    def _check(self, parent, var, text, hint="", state="normal"):
        row = self.tk.Frame(parent, bg=self.PANEL)
        row.pack(fill="x", padx=8, pady=2)
        cb = self.ttk.Checkbutton(row, text=text, variable=var, onvalue=True,
                                  offvalue=False, state=state,
                                  style="Body.TCheckbutton")
        cb.pack(side="left")
        if hint:
            self.tk.Label(row, text=hint.strip(), bg=self.PANEL, fg=self.DIM,
                          font=self.F_SUB).pack(side="left")
        return cb

    def _hint(self, parent, text, pady=(0, 8)):
        self.tk.Label(parent, text=text, bg=self.PANEL, fg=self.FG, anchor="w",
                      justify="left", wraplength=WIZ_W - 70,
                      font=self.F_SUB).pack(fill="x", pady=pady)

    # -- pages ---------------------------------------------------------------
    def _build_welcome(self):
        tk = self.tk
        f = self._page("welcome")
        left = WIZ_W - 110
        box = tk.Frame(f, bg=self.PANEL)
        box.pack(fill="both", expand=True)
        tk.Label(box, text="This will install %s%s on your computer."
                           % (APP_NAME, (" " + self.version) if self.version else ""),
                 bg=self.PANEL, fg=self.FG, anchor="nw", justify="left",
                 wraplength=left, font=self.F_SUB).pack(fill="x", pady=(2, 10))
        tk.Label(box, text="It is recommended that you close all other applications "
                           "before continuing.", bg=self.PANEL, fg=self.FG,
                 anchor="nw", justify="left", wraplength=left,
                 font=self.F_SUB).pack(fill="x", pady=(0, 10))
        if self.opts.existing[0] and self.opts.existing[1]:
            tk.Label(box, text="%s %s is already installed in %s - Setup will update "
                               "that copy in place."
                               % (APP_NAME, self.opts.existing[0], self.opts.existing[1]),
                     bg=self.PANEL, fg=self.DIM, anchor="nw", justify="left",
                     wraplength=left, font=self.F_SUB).pack(fill="x", pady=(0, 10))
        tk.Label(box, text="Click Next to continue, or Cancel to exit Setup.",
                 bg=self.PANEL, fg=self.FG, anchor="nw", justify="left",
                 wraplength=left, font=self.F_SUB).pack(fill="x")

    def _build_license(self):
        tk = self.tk
        f = self._page("license")
        self._hint(f, "Please read the following important information before "
                      "continuing.")
        wrap = tk.Frame(f, bg=self.PANEL)
        wrap.pack(fill="both", expand=True)
        txt = tk.Text(wrap, height=9, wrap="word", relief="solid", bd=1,
                      bg="#ffffff", fg=self.FG, font=self.F_MONO or self.F_SUB)
        sb = self.ttk.Scrollbar(wrap, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", self.license_text)
        txt.configure(state="disabled")
        self.var_accept = tk.BooleanVar(value=False)
        row = tk.Frame(f, bg=self.PANEL)
        row.pack(fill="x", pady=(8, 0))
        self.ttk.Radiobutton(row, text="I accept the agreement", value=True,
                             variable=self.var_accept, style="Body.TRadiobutton",
                             command=self._refresh_buttons).pack(anchor="w")
        self.ttk.Radiobutton(row, text="I do not accept the agreement", value=False,
                             variable=self.var_accept, style="Body.TRadiobutton",
                             command=self._refresh_buttons).pack(anchor="w")

    def _build_dest(self):
        tk = self.tk
        f = self._page("dest")
        self._hint(f, "Setup will install %s in the following folder.\n\n"
                      "To install in a different folder, click Browse and select "
                      "another folder." % APP_NAME)
        inner = self._group(f, "Destination Folder")
        row = tk.Frame(inner, bg=self.PANEL)
        row.pack(fill="x", padx=8, pady=8)
        self.dir_var = tk.StringVar(value=self.opts.install_dir)
        self.ttk.Entry(row, textvariable=self.dir_var, width=44).pack(
            side="left", ipady=2)
        self.ttk.Button(row, text="Browse...", width=10,
                        command=self.browse).pack(side="left", padx=(8, 0))
        sp = tk.Frame(f, bg=self.PANEL)
        sp.pack(fill="x", pady=(2, 0))
        self.space_req = tk.StringVar(value="")
        self.space_av = tk.StringVar(value="")
        tk.Label(sp, textvariable=self.space_req, bg=self.PANEL, fg=self.DIM,
                 anchor="e", font=self.F_SUB).pack(fill="x")
        tk.Label(sp, textvariable=self.space_av, bg=self.PANEL, fg=self.DIM,
                 anchor="e", font=self.F_SUB).pack(fill="x")
        self.dir_var.trace_add("write", lambda *a: self._update_space())
        self._update_space()

    def _update_space(self):
        need = payload_mb(self.src)
        self.space_req.set("Space required: %s" % _bytes_text(need))
        free = free_mb(self.dir_var.get().strip() or ".")
        self.space_av.set("Space available: %s"
                          % (_bytes_text(free) if free is not None else "unknown"))

    def browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Select the folder to install %s in"
                                         % APP_NAME,
                                    initialdir=self.dir_var.get() or None)
        if d:
            self.dir_var.set(os.path.abspath(d))

    def _build_startmenu(self):
        tk = self.tk
        f = self._page("startmenu")
        self._hint(f, "Setup will add program shortcuts to the Start Menu folder "
                      "listed below.\n\nTo use a different folder, enter it below.")
        inner = self._group(f, "Start Menu Folder")
        row = tk.Frame(inner, bg=self.PANEL)
        row.pack(fill="x", padx=8, pady=8)
        self.smf_var = tk.StringVar(value=self.opts.startmenu_folder or APP_NAME)
        self.smf_entry = self.ttk.Entry(row, textvariable=self.smf_var, width=44)
        self.smf_entry.pack(side="left", ipady=2)
        self.var_no_startmenu = tk.BooleanVar(value=not self.opts.startmenu_shortcut)
        self._check(f, self.var_no_startmenu,
                    "Don't create a Start Menu folder",
                    state="normal").configure(command=self._toggle_smf)
        self._toggle_smf()

    def _toggle_smf(self):
        if getattr(self, "smf_entry", None) is None:
            return
        self.smf_entry.configure(
            state="disabled" if self.var_no_startmenu.get() else "normal")

    def _build_tasks(self):
        tk = self.tk
        f = self._page("tasks")
        self.var_app = tk.BooleanVar(value=True)
        self.var_mpv = tk.BooleanVar(value=self.opts.mpv)
        self.var_ytdlp = tk.BooleanVar(value=self.opts.ytdlp)
        self.var_ffmpeg = tk.BooleanVar(value=self.opts.ffmpeg)
        self.var_updater = tk.BooleanVar(value=self.opts.updater)
        self.var_desktop = tk.BooleanVar(value=self.opts.desktop_shortcut)
        self.var_launch = tk.BooleanVar(value=self.opts.launch)

        inner = self._group(f, "Components")
        self._check(inner, self.var_app, "%s application" % APP_NAME,
                    " - required", state="disabled")
        self._check(inner, self.var_mpv, "mpv video player %s" % MPV_RELEASE_VERSION,
                    " - recommended, plays the two video feeds")
        self._check(inner, self.var_ytdlp, "yt-dlp",
                    " - YouTube and other URL sources")
        self._check(inner, self.var_ffmpeg, "ffmpeg",
                    " - merges video+audio, so downloads reach full quality")
        self._check(inner, self.var_updater, "Update checker",
                    " - %s - Check for Updates" % APP_NAME)
        tk.Frame(inner, bg=self.PANEL, height=6).pack()

        inner2 = self._group(f, "Shortcuts")
        self._check(inner2, self.var_desktop, "Create a desktop icon")
        tk.Frame(inner2, bg=self.PANEL, height=6).pack()

    def _build_ready(self):
        tk = self.tk
        f = self._page("ready")
        self.ready_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.ready_var, bg=self.PANEL, fg=self.FG, anchor="nw",
                 justify="left", wraplength=WIZ_W - 70,
                 font=self.F_SUB).pack(fill="x", pady=(0, 10))
        self._hint(f, "Click Install to continue with the installation, or click Back "
                      "if you want to review or change any settings.", pady=(0, 0))

    def _build_installing(self):
        tk = self.tk
        f = self._page("installing")
        self._hint(f, "Please wait while Setup installs %s on your computer."
                      % APP_NAME, pady=(0, 6))
        self.bar = self.ttk.Progressbar(f, maximum=100, mode="determinate",
                                        length=WIZ_W - 70)
        self.bar.pack(fill="x", pady=(0, 6))
        self.status_var = tk.StringVar(value="Preparing...")
        tk.Label(f, textvariable=self.status_var, bg=self.PANEL, fg=self.FG, anchor="w",
                 font=self.F_SUB).pack(fill="x", pady=(0, 6))
        wrap = tk.Frame(f, bg=self.PANEL)
        wrap.pack(fill="both", expand=True)
        self.log = tk.Text(wrap, height=6, relief="solid", bd=1, bg="#ffffff",
                           fg=self.DIM, wrap="none",
                           font=self.F_MONO or self.F_SUB)
        sb = self.ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.configure(state="disabled")

    def _build_finish(self):
        tk = self.tk
        f = self._page("finish")
        self.done_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.done_var, bg=self.PANEL, fg=self.FG, anchor="nw",
                 justify="left", wraplength=WIZ_W - 70,
                 font=self.F_SUB).pack(fill="x", pady=(2, 10))
        self.var_launch_ci = tk.BooleanVar(value=self.opts.launch)
        self._check(f, self.var_launch_ci, "Launch %s" % APP_NAME)

    # -- navigation ----------------------------------------------------------
    def _headers(self):
        return {
            "welcome": ("Welcome to the %s Setup Wizard" % APP_NAME,
                        "This part of Setup will guide you through the installation "
                        "of %s." % APP_NAME),
            "license": ("License Agreement",
                        "Please read the following important information before "
                        "continuing."),
            "dest": ("Select Destination Location",
                     "Where should %s be installed?" % APP_NAME),
            "startmenu": ("Select Start Menu Folder",
                          "Where should Setup place the program's shortcuts?"),
            "tasks": ("Select Additional Tasks",
                      "Which additional tasks should be performed?"),
            "ready": ("Ready to Install",
                      "Setup is now ready to begin installing %s on your computer."
                      % APP_NAME),
            "installing": ("Installing",
                           "Please wait while Setup installs %s on your computer."
                           % APP_NAME),
            "finish": ("Completing the %s Setup Wizard" % APP_NAME,
                       "Setup has finished installing %s on your computer."
                       % APP_NAME),
        }

    def show(self, idx):
        self.page = max(0, min(len(self.PAGES) - 1, idx))
        name = self.PAGES[self.page]
        for n, f in self.frames.items():
            if n == name:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        title, sub = self._headers()[name]
        self.hdr_title.config(text=title)
        self.hdr_sub.config(text=sub)
        self._refresh_buttons()

    def _refresh_buttons(self):
        name = self.PAGES[self.page]
        if name == "finish":
            self.btn_next.config(text="Finish", state="normal")
        elif name == "ready":
            self.btn_next.config(text="Install", state="normal")
        elif name == "installing":
            self.btn_next.config(text="Install", state="disabled")
        else:
            self.btn_next.config(text="Next >", state="normal")
        if name == "license" and not self.var_accept.get():
            self.btn_next.config(state="disabled")
        self.btn_back.config(state="normal" if self.page > 0
                             and name not in ("installing", "finish") else "disabled")
        self.btn_cancel.config(state="disabled" if name in ("installing", "finish")
                               else "normal")

    def _default(self):
        """Enter presses the default button (Next / Install / Finish)."""
        if str(self.btn_next.cget("state")) == "disabled":
            return
        self.next()

    def back(self):
        if self.page > 0:
            self.show(self.page - 1)

    def next(self):
        name = self.PAGES[self.page]
        if name == "welcome":
            self.show(self.page + 1)
        elif name == "license":
            self.show(self.page + 1)
        elif name == "dest":
            d = os.path.abspath(self.dir_var.get().strip())
            if not d or len(d) < 4 or d.endswith(":\\"):
                from tkinter import messagebox
                messagebox.showwarning("Setup", "Please choose a folder to install into.")
                return
            why = _protected_reason(d)
            if why:
                from tkinter import messagebox
                messagebox.showwarning("Setup", why)
                return
            try:                    # a read-only folder would fail mid-copy
                os.makedirs(d, exist_ok=True)
                probe = os.path.join(d, ".sp-write-test")
                with open(probe, "w"):
                    pass
                os.remove(probe)
            except Exception as e:
                from tkinter import messagebox
                messagebox.showerror("Setup",
                                     "Setup cannot write to that folder:\n\n%s\n\n%s"
                                     % (d, e))
                return
            self.opts.install_dir = d
            free = free_mb(d)
            if free is not None and free < payload_mb(self.src) + 20:
                from tkinter import messagebox
                if not messagebox.askyesno(
                        "Setup",
                        "There may not be enough space in that folder.\n\n"
                        "Setup needs about %.0f MB and only %.0f MB is free.\n\n"
                        "Continue anyway?" % (payload_mb(self.src) + 20, free)):
                    return
            self._update_space()
            self.show(self.page + 1)
        elif name == "startmenu":
            if not self.var_no_startmenu.get() and not self.smf_var.get().strip():
                from tkinter import messagebox
                messagebox.showwarning("Setup",
                                       "Enter a Start Menu folder name, or tick "
                                       "'Don't create a Start Menu folder'.")
                return
            self.opts.startmenu_folder = self.smf_var.get().strip() or APP_NAME
            self.opts.startmenu_shortcut = not self.var_no_startmenu.get()
            self.show(self.page + 1)
        elif name == "tasks":
            self.opts.mpv = bool(self.var_mpv.get())
            self.opts.ytdlp = bool(self.var_ytdlp.get())
            self.opts.ffmpeg = bool(self.var_ffmpeg.get())
            self.opts.updater = bool(self.var_updater.get())
            self.opts.desktop_shortcut = bool(self.var_desktop.get())
            self.opts.launch = bool(self.var_launch.get())
            self._fill_ready()
            self.show(self.page + 1)
        elif name == "ready":
            self.show(self.page + 1)
            self.start_install()
        elif name == "finish":
            self._stop_poll()
            self.root.destroy()

    def _fill_ready(self):
        comps = [APP_NAME]
        if self.opts.mpv:
            comps.append("mpv %s" % MPV_RELEASE_VERSION)
        if self.opts.ytdlp:
            comps.append("yt-dlp")
        if self.opts.updater:
            comps.append("update checker")
        tasks = []
        if self.opts.desktop_shortcut:
            tasks.append("Create a desktop icon")
        if self.opts.startmenu_shortcut:
            tasks.append("Start Menu folder '%s'" % (self.opts.startmenu_folder or APP_NAME))
        else:
            tasks.append("No Start Menu folder")
        self.ready_var.set(
            "Destination location:\n    %s\n\nComponents to install:\n    %s\n\n"
            "Additional shortcuts:\n    %s"
            % (self.opts.install_dir, ", ".join(comps), ", ".join(tasks)))

    def cancel(self):
        from tkinter import messagebox
        name = self.PAGES[self.page]
        if name == "installing":
            return                                  # cancel is disabled while installing
        if name == "finish":
            return
        if messagebox.askyesno("Setup",
                               "Setup is not complete. If you exit now, %s will not "
                               "be installed.\n\nExit Setup?" % APP_NAME):
            self._stop_poll()
            self.root.destroy()

    # -- install -------------------------------------------------------------
    def _log(self, text):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def start_install(self):
        import threading
        threading.Thread(target=self._worker, daemon=True).start()
        self._poll_id = self.root.after(80, self._poll)

    def _on_destroy(self, event=None):
        """A destroyed window must not leave a pending progress poll behind
        (Tk would log "invalid command name ..._poll" afterwards)."""
        if event is None or getattr(event, "widget", None) is self.root:
            self._stop_poll()

    def _stop_poll(self):
        """Cancel a pending progress poll (called before the window goes away)."""
        if getattr(self, "_poll_id", None) is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except Exception:
                pass
            self._poll_id = None

    def _poll(self):
        self._poll_id = None
        while self.queue:
            kind, payload = self.queue.pop(0)
            if kind == "pct":
                pct, text = payload
                self.bar["value"] = pct
                self.status_var.set(text)
                self._log("%3d%%  %s" % (pct, text))
            elif kind == "done":
                self.bar["value"] = 100
                self._finish_ok(payload)
            elif kind == "error":
                from tkinter import messagebox
                self.error = payload
                self.status_var.set("Setup failed.")
                self._log("ERROR: %s" % payload)
                messagebox.showerror("Setup",
                                     "Setup could not finish:\n\n%s\n\nNothing else "
                                     "was changed." % payload)
                self.btn_cancel.config(state="normal", text="Close",
                                       command=self.root.destroy)
        try:
            if self.root.winfo_exists() and self.PAGES[self.page] == "installing" \
                    and not self.error:
                self._poll_id = self.root.after(80, self._poll)
        except Exception:
            pass

    def _worker(self):
        try:
            src = self.src
            if not os.path.isfile(src[0]):
                self.queue.append(("error", "SyncPlayer.exe is missing from the Setup "
                                            "payload."))
                return
            if self.opts.mpv and not os.path.isdir(src[1]):
                self.queue.append(("error", "The mpv payload is missing from Setup - "
                                            "rebuild the installer."))
                return
            self.queue.append(("pct", (3, "Preparing...")))
            _close_running()
            state, app_path, updater_path = do_install(
                self.opts, src,
                progress=lambda p, t: self.queue.append(("pct", (p, t))))
            self.state, self.app_path = state, app_path
            made = []
            if self.opts.desktop_shortcut or self.opts.startmenu_shortcut:
                self.queue.append(("pct", (88, "Creating shortcuts...")))
                made = make_shortcuts(
                    app_path, self.opts.install_dir, updater_path,
                    desktop=self.opts.desktop_shortcut,
                    startmenu=self.opts.startmenu_shortcut,
                    startmenu_folder=self.opts.startmenu_folder)
            state["shortcuts"] = made
            self.queue.append(("pct", (93, "Adding to Add/Remove Programs...")))
            state["uninstall_registered"] = register_uninstall(
                self.opts.install_dir, app_path, state["app_version"])
            self.queue.append(("pct", (100, "Finished.")))
            self.queue.append(("done", state))
        except Exception as e:
            self.queue.append(("error", "%s" % e))

    def _finish_ok(self, state):
        self._stop_poll()
        self.state = state
        comps = ["%s %s" % (APP_NAME, state.get("app_version"))]
        if state.get("mpv_installed"):
            comps.append("mpv %s" % state.get("mpv_version"))
        if state.get("ytdlp_installed"):
            comps.append("yt-dlp %s" % state.get("ytdlp_version"))
        if state.get("updater_installed"):
            comps.append("update checker")
        self.done_var.set(
            "Installed:  %s\n"
            "Location:   %s\n\n"
            "The application may be launched by selecting the installed icons."
            % (", ".join(comps), state.get("install_dir", "")))
        self.show(self.PAGES.index("finish"))
        if self.var_launch_ci.get() and self.app_path:
            try:
                subprocess.Popen([self.app_path])
            except Exception:
                pass


def _gui_main(opts):
    import tkinter as tk
    root = tk.Tk()
    Wizard(root, opts)
    root.mainloop()


def main(argv):
    opts = parse_options(argv)
    as_json = "--json" in argv
    if "--silent" in argv or os.environ.get("SYNCPLAYER_SILENT"):
        return _cli_main(opts, as_json=as_json)
    try:
        _gui_main(opts)
        return 0
    except Exception as e:
        print("GUI unavailable (%s); falling back to silent install." % e)
        return _cli_main(opts, as_json=as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
