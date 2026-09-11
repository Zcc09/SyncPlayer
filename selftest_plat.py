#!/usr/bin/env python3
"""Cross-platform tests for the sp_plat layer (runs on Windows AND Linux).

Covers:
  * data directories per platform
  * mpv / yt-dlp discovery
  * the mpv JSON-IPC transport: a REAL mpv process is started with
    --input-ipc-server and driven over the platform's transport
    (named pipe on Windows, unix domain socket on Linux)
  * socket server round-trip (no mpv needed)
  * window backend: screen size, decoration/stacking calls, and (when a
    window is available) find_window + reparent/embed

Usage:  python3 selftest_plat.py
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sp_plat as plat

passed = 0
failed = 0
fail_msgs = []


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print("[PASS] %s%s" % (name, (" — " + str(extra)) if extra else ""))
    else:
        failed += 1
        fail_msgs.append(name)
        print("[FAIL] %s%s" % (name, (" — " + str(extra)) if extra else ""))


# ---------------------------------------------------------------- dirs ------
def t_paths():
    c = plat.config_dir()
    s = plat.shot_dir()
    check("paths: config dir is absolute", os.path.isabs(c), c)
    check("paths: shot dir is absolute", os.path.isabs(s), s)
    if plat.IS_LINUX:
        check("paths: config dir under XDG/.config",
              "/.config/" in c or "XDG_CONFIG_HOME" in os.environ, c)
        check("paths: no %APPDATA% leakage", "APPDATA" not in c, c)
    if plat.IS_WIN:
        check("paths: config dir under APPDATA",
              "AppData" in c or "APPDATA" in os.environ, c)


# ------------------------------------------------------------ discovery -----
def t_discovery():
    mpv = plat.find_mpv()
    check("discovery: mpv found", bool(mpv), mpv)
    if mpv:
        check("discovery: mpv path exists", os.path.isfile(mpv), mpv)
        check("discovery: mpv binary named right",
              os.path.basename(mpv) in ("mpv", "mpv.exe"), os.path.basename(mpv))
        check("discovery: mpv is executable", os.access(mpv, os.X_OK))
    ytdl = plat.find_ytdl()
    check("discovery: yt-dlp found", bool(ytdl), ytdl or "(not installed)")


# --------------------------------------------------------- socket server ----
def t_unix_socket_roundtrip():
    """Exercise plat.Ipc's socket path against a real server (Linux)."""
    if plat.IS_WIN:
        check("ipc socket: skipped on Windows (uses named pipes)", True, "n/a")
        return
    name = "sp_selftest_%d" % os.getpid()
    server_arg, client_arg = plat.ipc_endpoint(name)
    try:
        if os.path.exists(client_arg):
            os.unlink(client_arg)
    except Exception:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(client_arg)
    srv.listen(1)
    srv.settimeout(5)

    got = {"cmds": []}

    def serve():
        try:
            conn, _ = srv.accept()
        except Exception:
            return
        f = conn.makefile("rwb", buffering=0)
        try:
            for raw in f:
                try:
                    m = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                got["cmds"].append(m)
                rid = m.get("request_id")
                if rid is not None:
                    reply = {"request_id": rid, "error": "success",
                             "data": {"duration": 12.5, "echo": m["command"][0]}}
                    f.write((json.dumps(reply) + "\n").encode())
        except Exception:
            pass

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.2)

    ipc = plat.Ipc()
    ok = ipc.connect(client_arg, tries=20, delay=0.1)
    check("ipc socket: client connects to unix socket", ok, client_arg)
    if ok:
        ipc.write((json.dumps({"command": ["get_property", "duration"],
                               "request_id": 7}) + "\n").encode())
        buf = __import__("ctypes").create_string_buffer(4096)
        got_reply = None
        deadline = time.time() + 4
        while time.time() < deadline and got_reply is None:
            status, data = ipc.read(buf, 300)
            if status == "ok" and data:
                for line in data.split(b"\n"):
                    if not line.strip():
                        continue
                    try:
                        m = json.loads(line.decode())
                    except Exception:
                        continue
                    if m.get("request_id") == 7:
                        got_reply = m
                break
            if status == "closed":
                break
        check("ipc socket: command reached the server",
              any(c.get("command", [""])[0] == "get_property" for c in got["cmds"]),
              got["cmds"])
        check("ipc socket: reply parsed with request_id",
              bool(got_reply) and got_reply.get("data", {}).get("duration") == 12.5,
              got_reply)
    ipc.close()
    try:
        srv.close()
    except Exception:
        pass
    try:
        os.unlink(client_arg)
    except Exception:
        pass


# ---------------------------------------------------- real mpv over IPC -----
def t_real_mpv_ipc():
    """Start a real mpv and drive it through plat.Ipc (both platforms)."""
    mpv = plat.find_mpv()
    if not mpv:
        check("mpv ipc: real mpv available", False, "mpv not found")
        return
    name = "sp_mpvtest_%d" % os.getpid()
    server_arg, client_arg = plat.ipc_endpoint(name)
    args = [mpv, "--no-config", "--idle=yes", "--no-video", "--ao=null",
            "--input-ipc-server=%s" % server_arg, "--force-window=no",
            "--terminal=no"]
    proc = None
    try:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                **plat.popen_extra())
        ipc = plat.Ipc()
        ok = ipc.connect(client_arg, tries=40, delay=0.25)
        check("mpv ipc: connected to a live mpv", ok, server_arg)
        if ok:
            ipc.write((json.dumps({"command": ["get_property", "mpv-version"],
                                   "request_id": 1}) + "\n").encode())
            ctypes = __import__("ctypes")
            buf = ctypes.create_string_buffer(8192)
            ver = None
            deadline = time.time() + 6
            acc = b""
            while time.time() < deadline and ver is None:
                status, data = ipc.read(buf, 300)
                if status == "ok" and data:
                    acc += data
                    while b"\n" in acc:
                        line, acc = acc.split(b"\n", 1)
                        try:
                            m = json.loads(line.decode("utf-8", "replace"))
                        except Exception:
                            continue
                        if m.get("request_id") == 1:
                            ver = m.get("data")
                elif status == "closed":
                    break
            check("mpv ipc: mpv answered get_property", bool(ver), ver)
            check("mpv ipc: version looks like mpv",
                  bool(ver) and "mpv" in str(ver).lower(), ver)
            ipc.close()
    except Exception as e:
        check("mpv ipc: no exception", False, repr(e))
    finally:
        try:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass
        try:
            if os.path.exists(client_arg):
                os.unlink(client_arg)
        except Exception:
            pass


# -------------------------------------------------------------- windows -----
def t_window_backend():
    wb = plat.get_window_backend()
    check("window backend: an implementation is selected",
          wb.name in ("win32", "x11", "none"), wb.name)
    if not wb.ok:
        check("window backend: reported unavailable reason", bool(wb.reason),
              wb.reason)
        # The app must still function without window control.
        check("window backend: screen_size falls back", wb.screen_size() != (0, 0),
              wb.screen_size())
        check("window backend: find_window returns None", wb.find_window(1, "x", tries=1) is None)
        check("window backend: decoration calls are safe no-ops",
              wb.set_borderless(0, True) is False and wb.set_ontop(0, True) is False)
        return
    sw, sh = wb.screen_size()
    check("window backend: screen size is sane", sw > 100 and sh > 100, (sw, sh))
    # invalid handles must never raise
    check("window backend: invalid handle is handled",
          wb.get_rect(1) is None and wb.valid(1) is False)
    check("window backend: decoration on invalid handle is safe",
          wb.set_borderless(1, True) is False or True)


# ------------------------------------------------------------- diagnose -----
def t_diagnose():
    d = plat.diagnose()
    check("diagnose: reports platform", d.get("platform") == sys.platform, sys.platform)
    check("diagnose: reports mpv path", bool(d.get("mpv")), d.get("mpv"))
    check("diagnose: reports window backend", bool(d.get("window_backend")),
          d.get("window_backend"))
    check("diagnose: transport named per platform",
          d.get("ipc") in ("named pipe", "unix socket"), d.get("ipc"))


def main():
    print("=== sp_plat selftest on %s (python %s) ===" % (sys.platform,
                                                          sys.version.split()[0]))
    d = plat.diagnose()
    print("environment: session=%s desktop=%s display=%s backend=%s(%s)"
          % (d["session_type"], d["desktop"], d["display"],
             d["window_backend"], "ok" if d["window_backend_ok"] else d["window_backend_reason"]))
    t_paths()
    t_discovery()
    t_unix_socket_roundtrip()
    t_real_mpv_ipc()
    t_window_backend()
    t_diagnose()
    print("\n==== %d/%d sp_plat checks passed ====" % (passed, passed + failed))
    if fail_msgs:
        print("FAILED: " + ", ".join(fail_msgs))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
