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
      --no-updater --no-desktop-shortcut --no-startmenu-shortcut --launch --json
"""
import ctypes
from ctypes import wintypes
import json
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
        self.updater = True          # "Check for Updates"
        self.desktop_shortcut = True
        self.startmenu_shortcut = True
        self.launch = True

    def describe(self):
        comps = [APP_NAME]
        if self.mpv:
            comps.append("mpv")
        if self.ytdlp:
            comps.append("yt-dlp")
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
    if opts.mpv:
        cb(18, "Installing mpv %s..." % MPV_RELEASE_VERSION)
        _copy_tree(src_mpv, mpv_dir,
                   skip=() if opts.ytdlp else ("yt-dlp.exe",),
                   progress=lambda a, b: cb(18 + int(48.0 * a / max(b, 1)),
                                            "Installing mpv... (%d/%d)" % (a, b)))
    elif opts.ytdlp:
        # yt-dlp normally ships inside the mpv payload; keep the app's
        # discovery order (mpv/yt-dlp.exe) working without installing mpv
        os.makedirs(mpv_dir, exist_ok=True)
        src_ytdlp = os.path.join(src_mpv, "yt-dlp.exe")
        if os.path.isfile(src_ytdlp):
            shutil.copy2(src_ytdlp, os.path.join(mpv_dir, "yt-dlp.exe"))

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
        "updater_installed": bool(updater_path),
        "install_dir": install_dir,
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(install_dir, "install.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    return state, app_path, updater_path


def startmenu_dir():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"),
                        "Microsoft", "Windows", "Start Menu", "Programs", APP_NAME)


def make_shortcuts(app_path, install_dir, updater_path, desktop=True,
                   startmenu=True):
    made = []
    if desktop:
        if create_shortcut(os.path.join(os.path.expanduser("~"), "Desktop",
                                        APP_NAME + ".lnk"),
                           app_path, install_dir):
            made.append("desktop")
    if startmenu:
        sm = startmenu_dir()
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


def remove_shortcuts():
    """Delete the shortcuts the installer can create (used by --uninstall)."""
    removed = []
    lnks = [os.path.join(os.path.expanduser("~"), "Desktop", APP_NAME + ".lnk")]
    sm = startmenu_dir()
    try:
        for n in os.listdir(sm):
            lnks.append(os.path.join(sm, n))
    except Exception:
        pass
    for p in lnks:
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed.append(p)
        except Exception:
            pass
    try:
        if os.path.isdir(sm) and not os.listdir(sm):
            os.rmdir(sm)
            removed.append(sm)
    except Exception:
        pass
    return removed


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
                       ("--no-updater", "updater"),
                       ("--no-desktop-shortcut", "desktop_shortcut"),
                       ("--no-startmenu-shortcut", "startmenu_shortcut")):
        if flag in argv:
            setattr(opts, attr, False)
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
        desktop=opts.desktop_shortcut, startmenu=opts.startmenu_shortcut)
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
# ---------------------------------------------------------------------------
BG = "#1f232b"
FG = "#e8e8ea"
DIM = "#9aa0a8"
ACCENT = "#4a9eff"


class Wizard(object):
    """A small multi-page installer: Welcome -> Destination -> Components ->
    Progress -> Finish, with Back/Next, validation and a progress bar."""

    PAGES = ("welcome", "dest", "components", "progress", "finish")

    def __init__(self, root, opts):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.opts = opts
        self.page = 0
        self.error = None
        self.state = None
        self.app_path = None
        self.queue = []

        root.title("%s Setup" % APP_NAME)
        root.geometry("580x460")
        root.resizable(False, False)
        root.configure(bg=BG)
        try:
            root.lift()
            root.attributes("-topmost", True)
            root.after(400, lambda: root.attributes("-topmost", False))
            root.focus_force()
        except Exception:
            pass

        # header
        self.head = tk.Frame(root, bg=BG)
        self.head.pack(fill="x", padx=18, pady=(16, 0))
        self.title_var = tk.StringVar(value="")
        self.sub_var = tk.StringVar(value="")
        tk.Label(self.head, textvariable=self.title_var, fg=FG, bg=BG,
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x")
        tk.Label(self.head, textvariable=self.sub_var, fg=DIM, bg=BG,
                 font=("Segoe UI", 9), anchor="w", justify="left",
                 wraplength=540).pack(fill="x", pady=(2, 0))

        ttk.Separator(root).pack(fill="x", padx=18, pady=(10, 0))

        # body
        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True, padx=18, pady=8)

        # footer
        foot = tk.Frame(root, bg=BG)
        foot.pack(fill="x", padx=18, pady=(4, 14))
        self.step_var = tk.StringVar(value="")
        tk.Label(foot, textvariable=self.step_var, fg=DIM, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        self.btn_next = ttk.Button(foot, text="Next  >", width=12,
                                   command=self.next)
        self.btn_next.pack(side="right")
        self.btn_back = ttk.Button(foot, text="<  Back", width=10,
                                   command=self.back)
        self.btn_back.pack(side="right", padx=(0, 8))
        self.btn_cancel = ttk.Button(foot, text="Cancel", width=10,
                                     command=self.cancel)
        self.btn_cancel.pack(side="right", padx=(0, 8))

        self.frames = {}
        self._build_welcome()
        self._build_dest()
        self._build_components()
        self._build_progress()
        self._build_finish()
        self.show(0)

    # -- page shells ---------------------------------------------------------
    def _new_page(self, name):
        f = self.tk.Frame(self.body, bg=BG)
        self.frames[name] = f
        return f

    def show(self, idx):
        self.page = max(0, min(len(self.PAGES) - 1, idx))
        name = self.PAGES[self.page]
        for n, f in self.frames.items():
            if n == name:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        self.step_var.set("Step %d of 4" % min(self.page + 1, 4))
        titles = {
            "welcome": ("Welcome to the %s setup" % APP_NAME,
                        "This wizard installs %s and the video player it needs on "
                        "your computer." % APP_NAME),
            "dest": ("Choose where to install",
                     "Setup will install %s into the folder below. "
                     "The default needs no administrator rights." % APP_NAME),
            "components": ("Choose what to install",
                           "Everything is selected by default. Uncheck anything "
                           "you do not want; %s needs a player to show video."
                           % APP_NAME),
            "progress": ("Installing", "Please wait while Setup copies the files."),
            "finish": ("Setup complete", "Setup finished installing %s." % APP_NAME),
        }
        t, s = titles[name]
        self.title_var.set(t)
        self.sub_var.set(s)
        last = (name == "finish")
        self.btn_next.config(text="Finish" if last else "Next  >",
                             state="disabled" if name == "progress" else "normal")
        self.btn_back.config(state="disabled" if name in ("progress", "finish")
                             or self.page == 0 else "normal")
        self.btn_cancel.config(state="disabled" if last or name == "progress"
                               else "normal")

    # -- pages ---------------------------------------------------------------
    def _build_welcome(self):
        tk = self.tk
        f = self._new_page("welcome")
        src = resolve_payloads()
        mb = payload_mb(src)
        app_ver = get_exe_version(src[0]) or "?"
        box = tk.Frame(f, bg="#262b34", highlightthickness=1,
                       highlightbackground="#39404b")
        box.pack(fill="both", expand=True, pady=(4, 8))
        lines = [
            "%s %s   -   two videos, two windows, one clock" % (APP_NAME, app_ver),
            "",
            "Setup will install:",
            "    -  %s itself (the control panel + sync engine)" % APP_NAME,
            "    -  mpv %s, the video player that renders the two feeds" % MPV_RELEASE_VERSION,
            "    -  yt-dlp, so pasted YouTube / URL sources work out of the box",
            "    -  the %s update checker" % APP_NAME,
            "",
            "About %d MB of files are copied. You can change what gets installed"
            % int(mb + 0.5),
            "on the next pages.",
        ]
        tk.Label(box, text="\n".join(lines), fg=FG, bg="#262b34",
                 justify="left", anchor="nw", font=("Segoe UI", 9),
                 wraplength=520).pack(fill="both", expand=True, padx=14, pady=12)
        if self.opts.existing[0] and self.opts.existing[1]:
            lines += ["",
                      "%s %s is already installed in:" % (APP_NAME, self.opts.existing[0]),
                      "    " + self.opts.existing[1],
                      "Setup will update that copy in place."]
        tk.Label(f, text="Click Next to continue.", fg=DIM, bg=BG,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x")

    def _build_dest(self):
        tk = self.tk
        f = self._new_page("dest")
        tk.Label(f, text="Install %s to:" % APP_NAME, fg=FG, bg=BG,
                 anchor="w", font=("Segoe UI", 9)).pack(fill="x", pady=(6, 4))
        row = tk.Frame(f, bg=BG)
        row.pack(fill="x")
        self.dir_var = tk.StringVar(value=self.opts.install_dir)
        self.dir_entry = self.ttk.Entry(row, textvariable=self.dir_var, width=58)
        self.dir_entry.pack(side="left", ipady=3)
        self.ttk.Button(row, text="Browse...", width=10,
                        command=self.browse).pack(side="left", padx=(6, 0))
        self.disk_var = tk.StringVar(value="")
        tk.Label(f, textvariable=self.disk_var, fg=DIM, bg=BG, anchor="w",
                 font=("Segoe UI", 8)).pack(fill="x", pady=(4, 0))
        tk.Label(f, text="SyncPlayer is installed for the current user only, so "
                         "no administrator rights are needed. Config and "
                         "screenshots are kept in your own profile, not here.",
                 fg=DIM, bg=BG, anchor="w", justify="left", wraplength=520,
                 font=("Segoe UI", 8)).pack(fill="x", pady=(10, 0))
        if self.opts.existing[0] and self.opts.existing[1]:
            tk.Label(f, text="Updating the existing install (%s)" % self.opts.existing[1],
                     fg=ACCENT, bg=BG, anchor="w", wrap=True,
                     font=("Segoe UI", 8)).pack(fill="x", pady=(6, 0))
        self.dir_var.trace_add("write", lambda *a: self._update_disk())
        self._update_disk()

    def _update_disk(self):
        d = os.path.abspath(self.dir_var.get().strip() or ".")
        free = free_mb(d)
        txt = ""
        if free is not None:
            txt = "Free space: %.0f MB (Setup needs about %d MB)" % (free, REQUIRED_MB)
            if free < REQUIRED_MB:
                txt += "   -  not enough room here"
        self.disk_var.set(txt)

    def browse(self):
        from tkinter import filedialog
        d = filedialog.askdirectory(title="Where should %s be installed?" % APP_NAME,
                                    initialdir=self.dir_var.get() or None)
        if d:
            self.dir_var.set(os.path.abspath(d))

    def _build_components(self):
        tk = self.tk
        f = self._new_page("components")
        self.var_app = tk.BooleanVar(value=True)
        self.var_mpv = tk.BooleanVar(value=self.opts.mpv)
        self.var_ytdlp = tk.BooleanVar(value=self.opts.ytdlp)
        self.var_updater = tk.BooleanVar(value=self.opts.updater)
        self.var_desktop = tk.BooleanVar(value=self.opts.desktop_shortcut)
        self.var_startmenu = tk.BooleanVar(value=self.opts.startmenu_shortcut)
        self.var_launch = tk.BooleanVar(value=self.opts.launch)

        def group(title):
            box = tk.Frame(f, bg="#262b34", highlightthickness=1,
                           highlightbackground="#39404b")
            box.pack(fill="x", pady=(2, 8))
            tk.Label(box, text=title, fg=FG, bg="#262b34", anchor="w",
                     font=("Segoe UI", 9, "bold")).pack(fill="x", padx=12,
                                                        pady=(8, 2))
            return box

        def item(box, var, text, hint="", state="normal"):
            r = tk.Frame(box, bg="#262b34")
            r.pack(fill="x", padx=12, pady=1)
            cb = tk.Checkbutton(r, text=text, variable=var, onvalue=True,
                                offvalue=False, bg="#262b34", fg=FG,
                                activebackground="#262b34",
                                activeforeground=FG, selectcolor="#11151b",
                                anchor="w", font=("Segoe UI", 9),
                                highlightthickness=0, bd=0)
            cb.pack(side="left")
            if state == "disabled":
                cb.config(state="disabled")
            if hint:
                tk.Label(r, text=hint, fg=DIM, bg="#262b34",
                         font=("Segoe UI", 8)).pack(side="left", padx=(8, 0))
            return cb

        box = group("Components")
        item(box, self.var_app, "%s application" % APP_NAME,
             "(required)", state="disabled")
        item(box, self.var_mpv, "mpv video player %s" % MPV_RELEASE_VERSION,
             "(recommended - plays the video windows)")
        self.cb_ytdlp = item(box, self.var_ytdlp, "yt-dlp",
                             "(YouTube / URL sources)")
        item(box, self.var_updater, "Update checker",
             "(%s - Check for Updates)" % APP_NAME)
        tk.Frame(box, bg="#262b34", height=6).pack()

        box2 = group("Shortcuts")
        item(box2, self.var_desktop, "Create a Desktop shortcut")
        item(box2, self.var_startmenu, "Create a Start Menu entry")
        tk.Frame(box2, bg="#262b34", height=6).pack()

        box3 = group("Finish")
        item(box3, self.var_launch, "Launch %s when Setup closes" % APP_NAME)
        tk.Frame(box3, bg="#262b34", height=6).pack()

    def _build_progress(self):
        tk = self.tk
        f = self._new_page("progress")
        self.prog_status = tk.StringVar(value="Preparing...")
        tk.Label(f, textvariable=self.prog_status, fg=FG, bg=BG, anchor="w",
                 font=("Segoe UI", 9)).pack(fill="x", pady=(10, 6))
        self.bar = self.ttk.Progressbar(f, maximum=100, mode="determinate")
        self.bar.pack(fill="x", pady=4)
        self.prog_detail = tk.StringVar(value="")
        tk.Label(f, textvariable=self.prog_detail, fg=DIM, bg=BG, anchor="w",
                 font=("Segoe UI", 8)).pack(fill="x", pady=(2, 8))
        self.log = tk.Text(f, height=8, bg="#171b21", fg=DIM, bd=0,
                           highlightthickness=0, font=("Consolas", 8),
                           wrap="none")
        self.log.pack(fill="both", expand=True)
        self.log.configure(state="disabled")

    def _build_finish(self):
        tk = self.tk
        f = self._new_page("finish")
        self.done_var = tk.StringVar(value="")
        box = tk.Frame(f, bg="#262b34", highlightthickness=1,
                       highlightbackground="#39404b")
        box.pack(fill="both", expand=True, pady=(4, 8))
        tk.Label(box, textvariable=self.done_var, fg=FG, bg="#262b34",
                 justify="left", anchor="nw", font=("Segoe UI", 9),
                 wraplength=520).pack(fill="both", expand=True, padx=14, pady=12)
        self.btn_open = self.ttk.Button(f, text="Open the install folder",
                                        command=self._open_folder)
        self.btn_open.pack(anchor="w")

    def _open_folder(self):
        try:
            os.startfile(self.opts.install_dir)
        except Exception:
            pass

    # -- navigation ----------------------------------------------------------
    def back(self):
        if self.page > 0:
            self.show(self.page - 1)

    def next(self):
        name = self.PAGES[self.page]
        if name == "welcome":
            self.show(1)
        elif name == "dest":
            d = os.path.abspath(self.dir_var.get().strip())
            if not d or len(d) < 4 or d.endswith(":\\"):
                from tkinter import messagebox
                messagebox.showwarning(APP_NAME + " Setup",
                                       "Please choose a folder to install into.")
                return
            low = d.lower()
            if low.startswith(("c:\\windows", "c:\\program files\\windows")):
                from tkinter import messagebox
                messagebox.showwarning(APP_NAME + " Setup",
                                       "That is a system folder - please pick another one.")
                return
            free = free_mb(d)
            if free is not None and free < REQUIRED_MB:
                from tkinter import messagebox
                if not messagebox.askyesno(
                        APP_NAME + " Setup",
                        "Only %.0f MB free in that location. Setup needs about "
                        "%d MB.\n\nInstall anyway?" % (free, REQUIRED_MB)):
                    return
            self.opts.install_dir = d
            self.show(2)
        elif name == "components":
            self.opts.mpv = bool(self.var_mpv.get())
            self.opts.ytdlp = bool(self.var_ytdlp.get())
            self.opts.updater = bool(self.var_updater.get())
            self.opts.desktop_shortcut = bool(self.var_desktop.get())
            self.opts.startmenu_shortcut = bool(self.var_startmenu.get())
            self.opts.launch = bool(self.var_launch.get())
            if not self.opts.mpv and not self.opts.ytdlp:
                pass        # fine: a system mpv is used when present
            self.show(3)
            self.start_install()
        elif name == "finish":
            self.root.destroy()

    def cancel(self):
        from tkinter import messagebox
        if messagebox.askyesno("%s Setup" % APP_NAME,
                               "Cancel %s Setup?" % APP_NAME):
            self.root.destroy()

    def _log(self, text):
        try:
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except Exception:
            pass

    def start_install(self):
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()
        self.root.after(80, self._poll)

    def _poll(self):
        while self.queue:
            kind, payload = self.queue.pop(0)
            if kind == "pct":
                pct, text = payload
                self.bar["value"] = pct
                self.prog_status.set(text)
                self._log("%3d%%  %s" % (pct, text))
            elif kind == "detail":
                self.prog_detail.set(payload)
            elif kind == "done":
                self.bar["value"] = 100
                self._finish_ok(payload)
            elif kind == "error":
                from tkinter import messagebox
                self.error = payload
                self.prog_status.set("Setup failed.")
                self._log("ERROR: %s" % payload)
                self.btn_next.config(state="disabled")
                self.btn_cancel.config(state="normal", text="Close")
                self.btn_cancel.config(command=self.root.destroy)
                messagebox.showerror("%s Setup" % APP_NAME,
                                     "Setup could not finish:\n\n%s" % payload)
        if self.root.winfo_exists():
            if self.PAGES[self.page] == "progress" and not self.error:
                self.root.after(80, self._poll)

    def _worker(self):
        try:
            src = resolve_payloads()
            if not os.path.isfile(src[0]):
                self.queue.append(("error", "SyncPlayer.exe is missing from the "
                                            "Setup payload."))
                return
            if self.opts.mpv and not os.path.isdir(src[1]):
                self.queue.append(("error", "the mpv payload is missing from "
                                            "Setup - rebuild the installer."))
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
                    startmenu=self.opts.startmenu_shortcut)
            state["shortcuts"] = made
            self.queue.append(("pct", (93, "Adding to Add/Remove Programs...")))
            state["uninstall_registered"] = register_uninstall(
                self.opts.install_dir, app_path, state["app_version"])
            self.queue.append(("pct", (100, "Done.")))
            self.queue.append(("done", state))
        except Exception as e:
            self.queue.append(("error", "%s" % e))

    def _finish_ok(self, state):
        self.state = state
        comps = ["%s %s" % (APP_NAME, state.get("app_version"))]
        if state.get("mpv_installed"):
            comps.append("mpv %s" % state.get("mpv_version"))
        if state.get("ytdlp_installed"):
            comps.append("yt-dlp %s" % (state.get("ytdlp_version")))
        if state.get("updater_installed"):
            comps.append("update checker")
        lines = [
            "Installed:  " + ", ".join(comps),
            "Location:   " + state.get("install_dir", ""),
            "Shortcuts:  " + (", ".join(state.get("shortcuts") or []) or "none"),
            "Add/Remove Programs entry: %s" % ("yes" if state.get("uninstall_registered") else "no"),
            "",
            "Run it from the Desktop or Start Menu, or from:",
            "    " + state.get("install_dir", ""),
            "",
            "To remove it later, use Apps & Features in Windows Settings, or the",
            "Uninstall shortcut in the Start Menu.",
        ]
        self.done_var.set("\n".join(lines))
        self.prog_status.set("Setup complete.")
        self.show(4)
        if self.opts.launch and self.app_path:
            try:
                subprocess.Popen([self.app_path])
            except Exception:
                pass


def _gui_main(opts):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TButton", padding=(8, 3))
        style.configure("TEntry", fieldbackground="#171b21", foreground=FG)
    except Exception:
        pass
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
