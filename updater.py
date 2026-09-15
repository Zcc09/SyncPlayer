"""SyncPlayer updater - checks and installs updates for SyncPlayer + mpv.

Checks the public GitHub releases for BOTH:
  * SyncPlayer (Zcc09/SyncPlayer)  - the app exe
  * mpv (mpv-player/mpv)           - the Windows mpv build (x86_64 mingw)
and installs whatever is missing or outdated. Works anonymously because both
repos are public, so it runs on any computer with no credentials.

Modes:
  python updater.py --check           print a JSON status, exit
  python updater.py --apply [--dir X] apply updates (console)
  python updater.py [--dir X]         GUI mode (windowed build) with progress

Build:
  python -m PyInstaller --noconfirm --clean --onefile --windowed
      --name SyncPlayer-Updater --icon icon.ico updater.py
"""
import ctypes
try:                 # not present on Linux; only get_exe_version needs it
    from ctypes import wintypes
except Exception:    # pragma: no cover
    wintypes = None
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

APP_NAME = "SyncPlayer"
SYNC_REPO = "Zcc09/SyncPlayer"
MPV_REPO = "mpv-player/mpv"
YTDLP_REPO = "yt-dlp/yt-dlp"
# The mpv release tag that our bundled build corresponds to (used as the
# baseline "current" version when install.json has no mpv_version).
MPV_RELEASE_VERSION = "0.41.0"
# Pinned static Windows build used to supply ffmpeg when an installation does
# not have one (older installs predate it being bundled). ffmpeg is what lets
# yt-dlp merge separate video+audio streams - i.e. anything above ~720p.
FFMPEG_URL = ("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip")

# Overridable so the deployment/in-app tests can serve a fake release from
# a local HTTP server instead of GitHub.
API = (os.environ.get("SYNCPLAYER_RELEASES_API")
       or "https://api.github.com/repos/%s/releases/latest")


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


def parse_version(s):
    """Turn 'v0.41.0' / '1.4.0' / '2.0' / '0.41.0-dev-g...' into (major, minor, patch).

    Short versions are padded ("2.0" -> (2, 0, 0)): refusing to parse them meant a
    release tagged that way would never be seen as newer, and the app would
    silently never offer it.
    """
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)*)", str(s))
    if not m:
        return None
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def ver_gt(a, b):
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return False
    return pa > pb


def default_install_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        "~"), APP_NAME)


def find_install_dir(explicit=None):
    """Determine the install dir. Prefer explicit, then install.json, then the
    dir containing a SyncPlayer.exe (next to the updater or in LOCALAPPDATA)."""
    if explicit:
        return os.path.abspath(explicit)
    candidates = [os.path.dirname(os.path.abspath(__file__)),
                  default_install_dir()]
    for c in candidates:
        jp = os.path.join(c, "install.json")
        if os.path.isfile(jp):
            try:
                with open(jp, encoding="utf-8") as f:
                    return os.path.abspath(json.load(f).get(
                        "install_dir", c))
            except Exception:
                pass
        if os.path.isfile(os.path.join(c, APP_NAME + ".exe")):
            return os.path.abspath(c)
    return default_install_dir()


def get_install_state(install_dir):
    state = {
        "install_dir": install_dir,
        "app_version": "0.0.0",
        "mpv_version": None,
        "mpv_present": False,
    }
    jp = os.path.join(install_dir, "install.json")
    if os.path.isfile(jp):
        try:
            with open(jp, encoding="utf-8") as f:
                d = json.load(f)
            state["app_version"] = d.get("app_version") or state["app_version"]
            state["mpv_version"] = d.get("mpv_version")
        except Exception:
            pass
    exe = os.path.join(install_dir, APP_NAME + ".exe")
    if os.path.isfile(exe):
        v = get_exe_version(exe)
        if v:
            state["app_version"] = v
    state["mpv_present"] = os.path.isfile(
        os.path.join(install_dir, "mpv", "mpv.exe"))
    if not state["mpv_version"] and state["mpv_present"]:
        state["mpv_version"] = MPV_RELEASE_VERSION
    return state


def get_ffmpeg_version(path):
    """Read the version from a bundled ffmpeg ("ffmpeg version 9.0.1-...")."""
    if not path or not os.path.isfile(path):
        return None
    try:
        r = subprocess.run([path, "-version"], capture_output=True, text=True,
                           timeout=20, errors="replace",
                           creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                          if os.name == "nt" else 0))
        m = re.search(r"version\s+(\S+)", (r.stdout or "").split("\n")[0])
        return m.group(1).split("-")[0] if m else None
    except Exception:
        return None


def github_latest(owner_repo):
    # read the override per call: tests (and anyone redirecting it) set the
    # variable after this module has been imported
    url = (os.environ.get("SYNCPLAYER_RELEASES_API") or API) % owner_repo
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "SyncPlayer-Updater"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    tag = d.get("tag_name", "")
    assets = {}
    for a in d.get("assets", []):
        assets[a.get("name", "")] = {
            "url": a.get("browser_download_url"),
            "size": a.get("size", 0),
        }
    return {"tag": tag, "version": tag.lstrip("v"), "assets": assets,
            "body": d.get("body") or "", "page": d.get("html_url") or ""}


def pick_mpv_asset(assets):
    names = list(assets.keys())
    # Prefer the smaller mingw x86_64 build, else any x86_64 windows zip.
    for pat in ("x86_64-w64-mingw32", "x86_64-pc-windows-msvc"):
        for n in names:
            if pat in n and n.endswith(".zip"):
                return n
    for n in names:
        if "x86_64" in n and n.endswith(".zip"):
            return n
    return None


def download(url, dest, progress=None):
    req = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream",
        "User-Agent": "SyncPlayer-Updater"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(1024 * 256)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress and total:
                progress(done, total)
    return dest


def extract_mpv(zip_path, dest_dir):
    """Extract a possibly nested mpv release zip into dest_dir."""
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        inner = None
        for n in z.namelist():
            if n.endswith(".zip"):
                inner = n
                break
        if inner:
            tmp = tempfile.mktemp(suffix=".zip")
            with z.open(inner) as src, open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            with zipfile.ZipFile(tmp) as z2:
                z2.extractall(dest_dir)
            os.remove(tmp)
        else:
            z.extractall(dest_dir)


def replace_file(src, dst):
    """Atomically-ish replace dst with src (temp + os.replace)."""
    tmp = dst + ".tmp"
    shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def replace_running_exe(src, dst, backup_suffix=".old"):
    """Replace an executable that may be the one running right now.

    Returns (installed, detail). Windows will not let you overwrite a running image
    but it does let you RENAME it, so the old file is moved aside and the new one
    copied into the free name; the discarded copy is removed by
    sweep_update_leftovers() on the next start. If the file cannot be swapped even
    so, the new build is STAGED as "<dst>.new" and apply_pending_update() finishes
    the job at the next start - a locked file must not fail the whole update.
    """
    old = dst + backup_suffix
    first = None
    try:
        if os.path.isfile(dst):
            if os.path.isfile(old):
                os.remove(old)      # from a previous update, no longer running
            os.replace(dst, old)    # allowed while the image is running
        shutil.copy2(src, dst)
        if os.path.isfile(dst):
            return True, dst
    except Exception as e:
        first = e
    staged = dst + ".new"
    try:
        shutil.copy2(src, staged)
        return False, staged
    except Exception as e:
        return False, "could not replace %s (%s) nor stage it (%s)" % (dst, first, e)


def apply_pending_update(install_dir, log=None):
    """Install a build a previous run could only stage. True if one was installed
    (the caller should mention a restart)."""
    out = log if log is not None else []
    exe = os.path.join(install_dir, APP_NAME + ".exe")
    staged = exe + ".new"
    if not os.path.isfile(staged):
        return False
    installed, _detail = replace_running_exe(staged, exe)
    if installed:
        try:
            os.remove(staged)
        except Exception:
            pass
        out.append("A staged update was installed; restart to use it.")
        return True
    return False


def sweep_update_leftovers(install_dir, log=None):
    """Delete *.old / *.tmp files a previous self-update could not remove."""
    removed = []
    for name in os.listdir(install_dir):
        if name.endswith((".old", ".tmp")):
            path = os.path.join(install_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                os.remove(path)
                removed.append(name)
            except Exception:
                pass                        # still the running image; try next time
    if removed and log is not None:
        log.append("Cleaned update leftovers: %s" % ", ".join(removed))
    return removed


def apply_linux_tarball(install_dir, check, progress=None, log=None):
    """Install a Linux release by unpacking its tarball over the app directory.

    The Linux build is source + install.sh (nothing to swap atomically), so this
    replaces the source files while keeping the user's config, which lives
    elsewhere. Returns a log list.
    """
    import tarfile
    out = log if log is not None else []
    asset = check.get("app", {}).get("linux")
    if not asset or not asset.get("url"):
        out.append("No Linux tarball in that release.")
        return out
    tmp = tempfile.mktemp(suffix=".tar.gz")
    try:
        download(asset["url"], tmp, progress=progress)
        with tarfile.open(tmp, "r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile()
                       or m.isdir() or m.issym()]
            # strip the top-level "<App>-<ver>-linux/" directory
            roots = {m.name.split("/")[0] for m in members if "/" in m.name}
            prefix = (roots.pop() + "/") if len(roots) == 1 else ""
            for m in members:
                if prefix and not m.name.startswith(prefix):
                    continue
                m.name = m.name[len(prefix):] if prefix else m.name
                if not m.name:
                    continue
                tf.extract(m, install_dir, filter="data")
        out.append("Unpacked %s into %s" % (asset.get("url", "").split("/")[-1],
                                            install_dir))
    except Exception as e:
        out.append("Linux update failed: %s" % e)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return out


def run_check(install_dir):
    """Return a status dict comparing installed vs latest for both apps."""
    st = get_install_state(install_dir)
    out = {"install_dir": install_dir, "app": {}, "mpv": {}}
    # SyncPlayer
    try:
        sp = github_latest(SYNC_REPO)
        out["app"] = {
            "current": st["app_version"],
            "latest": sp["version"],
            "available": ver_gt(sp["version"], st["app_version"]),
            "asset": sp["assets"].get(APP_NAME + ".exe"),
            "linux": sp["assets"].get("%s-%s-linux.tar.gz" % (APP_NAME, sp["version"])),
            "notes": sp.get("body", ""),
            "page": sp.get("page", ""),
        }
    except Exception as e:
        out["app"] = {"error": str(e)}
    # mpv
    try:
        mpv = github_latest(MPV_REPO)
        cur = st["mpv_version"] or "0.0.0"
        asset = pick_mpv_asset(mpv["assets"])
        out["mpv"] = {
            "current": cur,
            "latest": mpv["version"],
            "available": ver_gt(mpv["version"], cur),
            "missing": not st["mpv_present"],
            "asset": mpv["assets"].get(asset) if asset else None,
        }
    except Exception as e:
        out["mpv"] = {"error": str(e)}
    # ffmpeg (bundled alongside mpv; yt-dlp needs it to merge A/V streams)
    try:
        ff_path = os.path.join(install_dir, "mpv", "ffmpeg.exe")
        out["ffmpeg"] = {
            "current": get_ffmpeg_version(ff_path) or ("present" if os.path.isfile(ff_path) else "0"),
            "latest": "bundled",
            "available": False,
            "missing": not os.path.isfile(ff_path),
            "asset": {"url": FFMPEG_URL, "size": 0},
        }
    except Exception as e:
        out["ffmpeg"] = {"error": str(e)}

    # yt-dlp (bundled alongside mpv; needed for YouTube links)
    try:
        yt = github_latest(YTDLP_REPO)
        ytdlp_path = os.path.join(install_dir, "mpv", "yt-dlp.exe")
        present = os.path.isfile(ytdlp_path)
        cur = st.get("ytdlp_version") or ("present" if present else "0")
        out["ytdlp"] = {
            "current": cur,
            "latest": yt["version"],
            "available": ver_gt(yt["version"], cur),
            "missing": not present,
            "asset": yt["assets"].get("yt-dlp.exe"),
        }
    except Exception as e:
        out["ytdlp"] = {"error": str(e)}
    return out


def apply_updates(install_dir, check, progress=None):
    """Download and install whatever is missing or outdated. Returns a log list.

    Each component is isolated: mpv or yt-dlp failing (unreachable asset, no room,
    a held file) must not lose an app update that already succeeded - the log names
    what failed, and "SyncPlayer update failed" is the only line that means the app
    itself did not get installed.
    """
    log = []
    os.makedirs(install_dir, exist_ok=True)

    try:
        _apply_app(install_dir, check.get("app") or {}, progress, log)
    except Exception as e:
        log.append("SyncPlayer update failed: %s" % e)
    try:
        _apply_mpv(install_dir, check.get("mpv") or {}, progress, log)
    except Exception as e:
        log.append("mpv update failed: %s" % e)
    try:
        _apply_ytdlp(install_dir, check.get("ytdlp") or {}, progress, log)
    except Exception as e:
        log.append("yt-dlp update failed: %s" % e)
    try:
        _apply_ffmpeg(install_dir, check.get("ffmpeg") or {}, progress, log)
    except Exception as e:
        log.append("ffmpeg update failed: %s" % e)

    # Persist a fresh install.json so future checks have a baseline. Keep what the
    # check reported for the components we did NOT touch.
    exe = os.path.join(install_dir, APP_NAME + ".exe")
    prev = {}
    try:
        with open(os.path.join(install_dir, "install.json"), encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    state = {
        "app_version": get_exe_version(exe)
        or (check.get("app") or {}).get("current") or "0.0.0",
        "mpv_version": (check.get("mpv") or {}).get("latest")
        or prev.get("mpv_version") or MPV_RELEASE_VERSION,
        "ytdlp_version": (check.get("ytdlp") or {}).get("latest")
        or prev.get("ytdlp_version") or "0",
        "install_dir": os.path.abspath(install_dir),
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with open(os.path.join(install_dir, "install.json"), "w",
              encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    log.append("install.json updated.")
    return log


def _apply_app(install_dir, app, progress, log):
    asset = app.get("asset")
    url = asset.get("url") if asset else None
    if not url:
        log.append("No SyncPlayer asset in that release - app not updated.")
        return
    log.append("Updating SyncPlayer...")
    tmp = tempfile.mktemp(suffix=".exe")
    download(url, tmp, progress=progress)
    ok, detail = replace_running_exe(tmp, os.path.join(install_dir, APP_NAME + ".exe"))
    try:
        os.remove(tmp)
    except Exception:
        pass
    if ok:
        log.append("SyncPlayer updated.")
    else:
        log.append("SyncPlayer staged for the next start (%s)." % detail)


def _apply_mpv(install_dir, mpv, progress, log):
    if not (mpv.get("missing") or mpv.get("available")):
        return
    asset = mpv.get("asset")
    url = asset.get("url") if asset else None
    if not url:
        log.append("mpv update requested but no matching asset found.")
        return
    log.append("Installing mpv..." if mpv.get("missing") else "Updating mpv...")
    tmp = tempfile.mktemp(suffix=".zip")
    download(url, tmp, progress=progress)
    extract_mpv(tmp, os.path.join(install_dir, "mpv"))
    try:
        os.remove(tmp)
    except Exception:
        pass
    log.append("mpv installed/updated.")


def _apply_ffmpeg(install_dir, ffmpeg, progress, log):
    """Fetch ffmpeg when the installation has none (older installs predate it)."""
    mpv_dir = os.path.join(install_dir, "mpv")
    target = os.path.join(mpv_dir, "ffmpeg.exe")
    if os.path.isfile(target) and not ffmpeg.get("missing"):
        return
    url = (ffmpeg.get("asset") or {}).get("url") or FFMPEG_URL
    log.append("Installing ffmpeg (needed for full-quality downloads)...")
    tmp = tempfile.mktemp(suffix=".zip")
    download(url, tmp, progress=progress)
    os.makedirs(mpv_dir, exist_ok=True)
    with zipfile.ZipFile(tmp) as z:
        cand = [n for n in z.namelist()
                if n.lower().endswith("/bin/ffmpeg.exe")]
        if not cand:
            raise RuntimeError("no ffmpeg.exe inside %s" % url.rsplit("/", 1)[-1])
        with z.open(cand[0]) as fsrc, open(target, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
    try:
        os.remove(tmp)
    except Exception:
        pass
    log.append("ffmpeg installed (%s)." % (get_ffmpeg_version(target) or "ok"))


def _apply_ytdlp(install_dir, ytdlp, progress, log):
    if not (ytdlp.get("missing") or ytdlp.get("available")):
        return
    asset = ytdlp.get("asset")
    url = asset.get("url") if asset else None
    if not url:
        log.append("yt-dlp update requested but no asset found.")
        return
    log.append("Updating yt-dlp...")
    tmp = tempfile.mktemp(suffix=".exe")
    download(url, tmp, progress=progress)
    mpv_dir = os.path.join(install_dir, "mpv")
    os.makedirs(mpv_dir, exist_ok=True)
    replace_file(tmp, os.path.join(mpv_dir, "yt-dlp.exe"))
    try:
        os.remove(tmp)
    except Exception:
        pass
    log.append("yt-dlp installed/updated.")


def cli_check(install_dir):
    out = run_check(install_dir)
    print(json.dumps(out, indent=2))
    return 0


def cli_apply(install_dir):
    check = run_check(install_dir)
    if not check["app"].get("available") and not check["mpv"].get("missing") \
            and not check["mpv"].get("available"):
        print("Already up to date.")
        return 0
    log = apply_updates(install_dir, check)
    print("\n".join(log))
    return 0


def _gui_main(install_dir):
    import tkinter as tk
    from tkinter import ttk
    root = tk.Tk()
    root.title(APP_NAME + " Updater")
    root.geometry("460x180")
    root.resizable(False, False)
    status = tk.StringVar(value="Checking for updates...")
    tk.Label(root, textvariable=status, anchor="w").pack(
        fill="x", padx=12, pady=(12, 4))
    bar = ttk.Progressbar(root, maximum=100)
    bar.pack(fill="x", padx=12, pady=4)
    logbox = tk.Text(root, height=5, width=54)
    logbox.pack(fill="both", expand=True, padx=12, pady=(4, 12))

    queue = []

    def poll():
        while queue:
            item = queue.pop(0)
            kind, payload = item
            if kind == "status":
                status.set(payload)
            elif kind == "progress":
                bar["value"] = payload
            elif kind == "log":
                logbox.insert("end", payload + "\n")
                logbox.see("end")
            elif kind == "done":
                root.after(1500, root.destroy)
        root.after(100, poll)

    def worker():
        try:
            queue.append(("status", "Checking for updates..."))
            check = run_check(install_dir)
            if not check["app"].get("available") and \
                    not check["mpv"].get("missing") and \
                    not check["mpv"].get("available"):
                queue.append(("status", "Already up to date."))
                queue.append(("log", "SyncPlayer: %s, mpv: %s" % (
                    check["app"].get("current"), check["mpv"].get("current"))))
                queue.append(("done", None))
                return
            queue.append(("log", "SyncPlayer: %s -> %s" % (
                check["app"].get("current"), check["app"].get("latest"))))
            queue.append(("log", "mpv: %s -> %s" % (
                check["mpv"].get("current"), check["mpv"].get("latest"))))
            queue.append(("status", "Updating..."))
            log = apply_updates(install_dir, check,
                                progress=lambda d, t: queue.append(
                                    ("progress", int(d * 100 / t))))
            for line in log:
                queue.append(("log", line))
            queue.append(("status", "Done."))
            queue.append(("done", None))
        except Exception as e:
            queue.append(("log", "ERROR: %s" % e))
            queue.append(("status", "Error."))

    import threading
    threading.Thread(target=worker, daemon=True).start()
    poll()
    root.mainloop()


def main(argv):
    if not sys.platform.startswith("win"):
        # The self-updater ships Windows binaries (SyncPlayer.exe, mpv.exe,
        # yt-dlp.exe). On Linux everything comes from the distro / the
        # installer script instead, so point the user at the right command
        # rather than downloading a wrong-platform asset.
        print("SyncPlayer self-update is Windows-only.")
        print("On Linux, update with:")
        print("  git -C <repo> pull && ./install.sh     # if installed from a checkout")
        print("  sudo pacman -Syu mpv yt-dlp            # or your distro's package manager")
        print("  yt-dlp -U                             # if you bundled a private yt-dlp")
        return 1
    install_dir = find_install_dir()
    if "--dir" in argv:
        try:
            i = argv.index("--dir")
            install_dir = os.path.abspath(argv[i + 1])
        except Exception:
            pass
    if "--check" in argv:
        return cli_check(install_dir)
    if "--apply" in argv:
        return cli_apply(install_dir)
    # default: GUI
    try:
        _gui_main(install_dir)
        return 0
    except Exception as e:
        print("GUI unavailable (%s); falling back to --apply." % e)
        return cli_apply(install_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
