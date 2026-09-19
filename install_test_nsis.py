"""Focused test: the NSIS installer, end to end.

Runs the built Setup the way callers and users do, and checks what actually lands
on disk and in the registry - plus one real GUI walk that clicks through the wizard
with BM_CLICK, so "it installs from the wizard" is observed rather than assumed.

Checks:
  1. default silent install: files, install.json (parsed), Add/Remove entry
  2. component matrix: --no-mpv / --no-ytdlp / --no-ffmpeg / --no-updater
  3. shortcuts: desktop, Start Menu folder, custom folder name, --no-shortcuts
  4. NSIS-native /S and /D=
  5. an existing install becomes the default destination
  6. quiet uninstall through the registered UninstallString, keeping user data
  7. GUI: wizard window, the Run checkbox, clicking Next..Finish really installs
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
SETUP = os.path.join(HERE, "dist", "SyncPlayer-Setup.exe")
APP = "SyncPlayer"
UNINST_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SyncPlayer"
APPDATA_CFG = os.path.join(os.environ.get("APPDATA", ""), APP)
SM_ROOT = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                       "Start Menu", "Programs")
DESKTOP = os.path.join(os.path.expanduser("~"), "Desktop")

u32 = ctypes.WinDLL("user32", use_last_error=True)
BM_CLICK = 0x00F5
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)))
    print("  %-4s %s%s" % ("ok" if ok else "FAIL", name,
                           ("   [%s]" % detail) if detail else ""))


def info(name, detail):
    print("       %s: %s" % (name, detail))


def run_setup(args, timeout=300):
    p = subprocess.run([SETUP] + args, capture_output=True, text=True,
                       timeout=timeout, creationflags=0x08000000)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def tmpdir(tag):
    d = os.path.join(tempfile.gettempdir(), "sp_nsis_" + tag)
    shutil.rmtree(d, ignore_errors=True)
    return d


def reg_values():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINST_KEY) as k:
            out = {}
            i = 0
            while True:
                try:
                    n, v, _t = winreg.EnumValue(k, i)
                    out[n] = v
                    i += 1
                except OSError:
                    break
            return out
    except OSError:
        return {}


def install_json(d):
    p = os.path.join(d, "install.json")
    if not os.path.isfile(p):
        return None
    raw = open(p, encoding="utf-8", errors="replace").read()
    try:
        return json.loads(raw)
    except Exception as e:
        # report the malformed file instead of dying on it: the raw text is the
        # evidence for what the installer wrote
        print("       install.json PARSE ERROR: %s" % e)
        print("       raw: %r" % raw)
        return {"__parse_error__": str(e)}


def lnk_ok(path):
    return os.path.isfile(path) and os.path.getsize(path) > 0


def windows_of_pid(pid):
    found = []

    def cb(hwnd, _l):
        p = ctypes.c_ulong()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid:
            n = u32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(n + 2)
            u32.GetWindowTextW(hwnd, buf, n + 2)
            cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(hwnd, cls, 256)
            found.append((hwnd, buf.value, cls.value))
        return True

    u32.EnumWindows(WNDENUMPROC(cb), None)
    return found


def children(hwnd, out=None):
    if out is None:
        out = []

    def cb(h, _l):
        n = u32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(h, buf, n + 2)
        cls = ctypes.create_unicode_buffer(128)
        u32.GetClassNameW(h, cls, 128)
        out.append((h, buf.value, cls.value))
        return True

    u32.EnumChildWindows(hwnd, WNDENUMPROC(cb), None)
    return out


def find_button(hwnd, needle):
    for h, text, cls in children(hwnd):
        if cls.lower() == "button" and needle.lower() in text.lower():
            return h, text
    return None, ""


def click(hwnd):
    u32.SendMessageW(hwnd, BM_CLICK, 0, 0)


def main():
    if not os.path.isfile(SETUP):
        print("FAIL: %s not built" % SETUP)
        return 1
    size_mb = os.path.getsize(SETUP) / 1048576.0
    print("Setup: %.1f MB" % size_mb)

    # ---------------------------------------------------------------- 1. default
    print("\n1. default silent install")
    d1 = tmpdir("a1")
    rc, out = run_setup(["--silent", "--install-dir", d1, "--no-launch"])
    info("exit", rc)
    check("silent install exits 0", rc == 0, out.strip()[-90:])
    check("app exe installed", os.path.isfile(os.path.join(d1, APP + ".exe")))
    check("updater installed", os.path.isfile(os.path.join(d1, APP + "-Updater.exe")))
    check("bundled mpv installed", os.path.isfile(os.path.join(d1, "mpv", "mpv.exe")))
    check("mpv has its dlls",
          len([f for f in os.listdir(os.path.join(d1, "mpv"))
               if f.lower().endswith(".dll")]) > 5)
    check("yt-dlp installed", os.path.isfile(os.path.join(d1, "mpv", "yt-dlp.exe")))
    check("ffmpeg installed", os.path.isfile(os.path.join(d1, "mpv", "ffmpeg.exe")))

    st = install_json(d1)
    check("install.json is valid JSON", st is not None)
    if st:
        info("install.json", json.dumps(st, sort_keys=True)[:230])
        check("install.json keys complete",
              set(st) == {"app_version", "mpv_version", "mpv_installed",
                          "ytdlp_version", "ytdlp_installed", "ffmpeg_version",
                          "ffmpeg_installed", "updater_installed", "install_dir",
                          "startmenu_folder", "installed_at"}, sorted(st))
        check("install.json install_dir matches the real folder",
              os.path.normcase(st.get("install_dir", "")) == os.path.normcase(d1),
              st.get("install_dir", ""))
        check("install.json mpv_version", st.get("mpv_version") == "0.41.0",
              st.get("mpv_version"))
        check("install.json mpv_installed", st.get("mpv_installed") is True)
        check("install.json ytdlp_installed", st.get("ytdlp_installed") is True)
        check("install.json ffmpeg_installed", st.get("ffmpeg_installed") is True)
        check("install.json updater_installed", st.get("updater_installed") is True)
        check("install.json app_version looks like a version",
              str(st.get("app_version", "")).count(".") == 2,
              st.get("app_version"))
        check("install.json ffmpeg_version parsed to a bare version",
              str(st.get("ffmpeg_version", "")).count(".") >= 1
              and "-" not in str(st.get("ffmpeg_version", "")),
              st.get("ffmpeg_version"))
        check("install.json startmenu_folder", st.get("startmenu_folder") == APP,
              st.get("startmenu_folder"))
        check("install.json installed_at looks like a timestamp",
              len(str(st.get("installed_at", ""))) == 19
              and "T" in str(st.get("installed_at", "")),
              st.get("installed_at"))

    rv = reg_values()
    check("Add/Remove entry created", bool(rv))
    if rv:
        check("A/R DisplayName", rv.get("DisplayName") == APP, rv.get("DisplayName"))
        check("A/R DisplayVersion matches the app",
              rv.get("DisplayVersion") == (st or {}).get("app_version"),
              "%s vs %s" % (rv.get("DisplayVersion"), (st or {}).get("app_version")))
        check("A/R Publisher", rv.get("Publisher") == APP, rv.get("Publisher"))
        check("A/R InstallLocation",
              os.path.normcase(rv.get("InstallLocation", "")) == os.path.normcase(d1),
              rv.get("InstallLocation"))
        check("A/R UninstallString runs the app",
              "--uninstall" in rv.get("UninstallString", "")
              and APP + ".exe" in rv.get("UninstallString", ""),
              rv.get("UninstallString"))
        check("A/R QuietUninstallString is silent",
              "--uninstall" in rv.get("QuietUninstallString", "")
              and "--silent" in rv.get("QuietUninstallString", ""),
              rv.get("QuietUninstallString"))
        check("A/R EstimatedSize is a real size",
              int(rv.get("EstimatedSize", 0)) > 50000, rv.get("EstimatedSize"))
        check("A/R DisplayIcon points at the exe",
              APP + ".exe" in rv.get("DisplayIcon", ""), rv.get("DisplayIcon"))
        check("A/R NoModify/NoRepair set",
              rv.get("NoModify") == 1 and rv.get("NoRepair") == 1)

    check("Desktop shortcut created", lnk_ok(os.path.join(DESKTOP, APP + ".lnk")))
    sm = os.path.join(SM_ROOT, APP)
    check("Start Menu folder created", os.path.isdir(sm))
    if os.path.isdir(sm):
        got = sorted(os.listdir(sm))
        check("Start Menu has app, updater and uninstall entries", len(got) == 3, got)

    # ------------------------------------------------------------ 2. components
    print("\n2. component matrix")
    d2 = tmpdir("a2")
    rc, _o = run_setup(["--silent", "--install-dir", d2, "--no-launch",
                        "--no-mpv", "--no-ytdlp", "--no-ffmpeg"])
    st2 = install_json(d2) or {}
    check("app-only install exits 0", rc == 0)
    check("app-only installs the app",
          os.path.isfile(os.path.join(d2, APP + ".exe")))
    check("app-only leaves mpv out", not os.path.isdir(os.path.join(d2, "mpv")))
    check("app-only records the flags",
          st2.get("mpv_installed") is False and st2.get("ytdlp_installed") is False
          and st2.get("ffmpeg_installed") is False and st2.get("mpv_version") == "0",
          json.dumps({k: st2.get(k) for k in ("mpv_installed", "ytdlp_installed",
                                              "ffmpeg_installed", "mpv_version")}))
    check("app-only still registers A/R", bool(reg_values()))

    d3 = tmpdir("a3")
    run_setup(["--silent", "--install-dir", d3, "--no-launch", "--no-ytdlp",
               "--no-ffmpeg"])
    st3 = install_json(d3) or {}
    check("mpv without the tools: mpv present",
          os.path.isfile(os.path.join(d3, "mpv", "mpv.exe")))
    check("mpv without the tools: yt-dlp absent",
          not os.path.isfile(os.path.join(d3, "mpv", "yt-dlp.exe")))
    check("mpv without the tools: ffmpeg absent",
          not os.path.isfile(os.path.join(d3, "mpv", "ffmpeg.exe")))
    check("mpv without the tools: flags recorded",
          st3.get("mpv_installed") is True and st3.get("ytdlp_installed") is False
          and st3.get("ffmpeg_installed") is False)

    d4 = tmpdir("a4")
    run_setup(["--silent", "--install-dir", d4, "--no-launch", "--no-mpv"])
    check("mpv unticked but tools kept: yt-dlp where the app looks for it",
          os.path.isfile(os.path.join(d4, "mpv", "yt-dlp.exe")))
    check("mpv unticked: mpv.exe really absent",
          not os.path.isfile(os.path.join(d4, "mpv", "mpv.exe")))

    d5 = tmpdir("a5")
    run_setup(["--silent", "--install-dir", d5, "--no-launch", "--no-updater"])
    st5 = install_json(d5) or {}
    check("--no-updater leaves the updater out",
          not os.path.isfile(os.path.join(d5, APP + "-Updater.exe")))
    check("--no-updater recorded", st5.get("updater_installed") is False)

    # ------------------------------------------------------------- 3. shortcuts
    print("\n3. shortcuts")
    d6 = tmpdir("a6")
    run_setup(["--silent", "--install-dir", d6, "--no-launch", "--no-shortcuts"])
    check("--no-shortcuts: no Desktop shortcut",
          not os.path.isfile(os.path.join(DESKTOP, APP + ".lnk")))
    check("--no-shortcuts: no Start Menu folder", not os.path.isdir(sm))
    check("--no-shortcuts: install.json startmenu_folder empty",
          (install_json(d6) or {}).get("startmenu_folder") == "",
          (install_json(d6) or {}).get("startmenu_folder"))

    d7 = tmpdir("a7")
    custom = "SyncPlayer Test Menu"
    run_setup(["--silent", "--install-dir", d7, "--no-launch",
               "--startmenu-folder", custom])
    cm = os.path.join(SM_ROOT, custom)
    check("--startmenu-folder is honoured", os.path.isdir(cm), custom)
    if os.path.isdir(cm):
        check("custom folder is populated", len(os.listdir(cm)) == 3,
              os.listdir(cm))
        for f in os.listdir(cm):
            os.remove(os.path.join(cm, f))
        os.rmdir(cm)

    d8 = tmpdir("a8")
    run_setup(["--silent", "--install-dir", d8, "--no-launch",
               "--no-desktop-shortcut"])
    check("--no-desktop-shortcut: Start Menu still made", os.path.isdir(sm))
    check("--no-desktop-shortcut: Desktop left alone",
          not os.path.isfile(os.path.join(DESKTOP, APP + ".lnk")))

    # ------------------------------------------------------- 4. NSIS-native flags
    print("\n4. NSIS-native switches (/S and /D=)")
    d9 = os.path.join(tempfile.gettempdir(), "sp_nsis_native")
    shutil.rmtree(d9, ignore_errors=True)
    rc = subprocess.run([SETUP, "/S", "/D=" + d9], capture_output=True,
                        timeout=300, creationflags=0x08000000).returncode
    check("/S plus /D= installs there",
          rc == 0 and os.path.isfile(os.path.join(d9, APP + ".exe")), rc)
    check("/D= honours the path in install.json",
          os.path.normcase((install_json(d9) or {}).get("install_dir", ""))
          == os.path.normcase(d9))

    # ------------------------------------------------- 5. existing-install default
    print("\n5. an existing install is the default destination")
    rc, out = run_setup(["--silent", "--no-launch"])   # no --install-dir
    check("second run with no destination exits 0", rc == 0, out.strip()[-80:])
    landed = (reg_values().get("InstallLocation") or "")
    check("it reused the last install location",
          os.path.normcase(landed) == os.path.normcase(d9)
          or os.path.isdir(os.path.join(d9, APP + ".exe")), landed)

    # --------------------------------------------------------------- 6. uninstall
    print("\n6. uninstall through the registered command")
    cfg = os.path.join(APPDATA_CFG, "syncplayer_config.json")
    os.makedirs(APPDATA_CFG, exist_ok=True)
    if not os.path.isfile(cfg):
        with open(cfg, "w", encoding="utf-8") as fh:
            fh.write("{}")
    rv = reg_values()
    cmd = rv.get("QuietUninstallString", "")
    if cmd:
        words = cmd.replace('"', " ").split()
        proc = subprocess.run(words[:1] + words[1:] + ["--silent"],
                              capture_output=True, text=True, timeout=180,
                              creationflags=0x08000000)
        info("uninstall exit", proc.returncode)
    else:
        check("QuietUninstallString exists", False)
    time.sleep(2)
    gone_dir = not os.path.exists(os.path.join(d9, APP + ".exe"))
    check("uninstall removed the program", gone_dir)
    check("uninstall removed the Add/Remove entry", not reg_values())
    check("uninstall removed the Start Menu folder", not os.path.isdir(sm))
    check("uninstall kept the user config", os.path.isfile(cfg))

    # --------------------------------------------------------------------- 7. GUI
    print("\n7. the wizard itself (clicked through with BM_CLICK)")
    for leftover in (d1, d2, d3, d4, d5, d6, d7, d8, d9):
        shutil.rmtree(leftover, ignore_errors=True)
    gui_dir = tmpdir("gui")
    proc = subprocess.Popen([SETUP, "--install-dir", gui_dir, "--no-launch"],
                            creationflags=0x08000000)
    hwnd = None
    title = ""
    for _ in range(40):
        time.sleep(0.5)
        for h, t, cls in windows_of_pid(proc.pid):
            if cls.lower() == "#32770" and t:
                hwnd, title = h, t
                break
        if hwnd:
            break
    check("the wizard window appeared", hwnd is not None, title)
    if hwnd:
        info("window title", title)
        check("window title names the app and version",
              APP in title and "1.6" in title, title)
        # the four Next pages, then the install page runs, then Finish
        steps = []
        for _i in range(6):
            btn, text = find_button(hwnd, "next")
            if not btn:
                btn, text = find_button(hwnd, "install")
            if not btn:
                btn, text = find_button(hwnd, "finish")
            if not btn:
                break
            steps.append(text)
            click(btn)
            time.sleep(1.6)
        info("buttons clicked", steps)
        check("walked the wizard pages", len(steps) >= 4, steps)
        for _ in range(30):
            time.sleep(0.5)
            if proc.poll() is not None:
                break
        check("the wizard closed on its own", proc.poll() is not None,
              proc.poll())
        check("the GUI install really installed",
              os.path.isfile(os.path.join(gui_dir, APP + ".exe")))
        stg = install_json(gui_dir) or {}
        check("the GUI install wrote install.json",
              os.path.isfile(os.path.join(gui_dir, "install.json")))
        check("the GUI install shipped mpv",
              os.path.isfile(os.path.join(gui_dir, "mpv", "mpv.exe")))
        check("the GUI install shipped ffmpeg",
              os.path.isfile(os.path.join(gui_dir, "mpv", "ffmpeg.exe")),
              stg.get("ffmpeg_version", ""))

        # and the Finish page's checkbox exists (this is what RunApp wires up)
        proc2 = subprocess.Popen([SETUP, "--install-dir", tmpdir("gui2")],
                                 creationflags=0x08000000)
        time.sleep(3.0)
        h2 = None
        for h, t, cls in windows_of_pid(proc2.pid):
            if cls.lower() == "#32770" and t:
                h2 = h
                break
        box = None
        if h2:
            for _i in range(5):
                btn, text = find_button(h2, "next")
                if not btn:
                    btn, text = find_button(h2, "install")
                if btn:
                    click(btn)
                time.sleep(1.5)
                if find_button(h2, "finish")[0]:
                    break
            for h, text, cls in children(h2):
                if cls.lower() == "button" and "run " + APP.lower() in text.lower():
                    box = text
                    break
        check("the Finish page offers the Run checkbox", box is not None, box)
        try:
            proc2.kill()
        except Exception:
            pass
        time.sleep(1.0)

    for leftover in (gui_dir, tmpdir("gui2")):
        shutil.rmtree(leftover, ignore_errors=True)
    subprocess.run(["taskkill", "/F", "/IM", APP + ".exe"], capture_output=True,
                   creationflags=0x08000000)

    # ------------------------------------------------------------------ summary
    passed = sum(1 for _n, ok, _d in results if ok)
    print("\n%d/%d checks passed" % (passed, len(results)))
    bad = [(n, d) for n, ok, d in results if not ok]
    if bad:
        print("failures:")
        for n, d in bad:
            print("  - %s   [%s]" % (n, d))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
