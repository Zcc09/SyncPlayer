"""SyncPlayer platform abstraction layer.

Single place where SyncPlayer talks to the host OS:

  * directory locations (config / screenshots / install)
  * mpv + yt-dlp discovery
  * subprocess creation flags
  * mpv JSON-IPC transport
        Windows -> named pipe (overlapped Win32 I/O)
        Linux   -> unix domain socket
  * video-window control (find / move / borderless / always-on-top /
        reparent-embed)
        Windows -> Win32 user32
        Linux   -> libX11 via ctypes (no extra Python packages needed)

Every backend degrades gracefully: if a feature is unavailable the call
returns False/None and `diagnose()` explains why, so the app can keep running
instead of crashing.
"""

import ctypes
import ctypes.util
import functools
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

if sys.platform.startswith("win"):
    import ctypes.wintypes  # noqa: F401 - required for Win32 IPC handles

IS_WIN = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")
IS_MAC = sys.platform == "darwin"

MPV_EXE = "mpv.exe" if IS_WIN else "mpv"
YTDL_EXE = "yt-dlp.exe" if IS_WIN else "yt-dlp"
APP_DIRNAME = "SyncPlayer" if IS_WIN else "syncplayer"


# ---------------------------------------------------------------------------
# X11 thread safety
#
# Xlib is NOT thread safe: two threads sharing one Display connection can eat
# each other's replies, leaving the loser blocked forever inside _XReply - on
# Xwayland this shows up as a hard hang in XGetGeometry. SyncPlayer calls the
# window backend from its arrange / screenshot threads as well as from the Tk
# main thread, so every primitive X call below is serialized on one re-entrant
# lock. XInitThreads() is also called as early as possible (before Tk opens its
# own display) so Xlib's internal locking is active too.
# ---------------------------------------------------------------------------
_X_LOCK = threading.RLock()


def xsync(func):
    """Serialize a primitive X11Backend call on the shared X connection."""
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        with _X_LOCK:
            return func(self, *args, **kwargs)
    return wrapper


def _x_init_threads():
    """XInitThreads() as early as possible; harmless if unavailable."""
    try:
        name = ctypes.util.find_library("X11") or "libX11.so.6"
        lib = ctypes.CDLL(name)
        lib.XInitThreads.restype = ctypes.c_int
        return bool(lib.XInitThreads())
    except Exception:
        return False


X_THREADS_READY = _x_init_threads() if IS_LINUX else False


# ---------------------------------------------------------------------------
# directories
# ---------------------------------------------------------------------------

def _xdg(env_name, default_rel):
    base = os.environ.get(env_name) or os.path.join(os.path.expanduser("~"), default_rel)
    return base


def config_dir():
    """Where syncplayer_config.json lives."""
    if IS_WIN:
        return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIRNAME)
    return os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), APP_DIRNAME)


def shot_dir():
    """Where screenshots / mpv logs / helper lua+input.conf are written."""
    if IS_WIN:
        return os.path.join(os.path.expanduser("~"), "Pictures", "SyncPlayer")
    # Linux: honour XDG_PICTURES_DIR / user-dirs.dirs, else ~/Pictures
    pics = None
    try:
        udd = os.path.join(_xdg("XDG_CONFIG_HOME", ".config"), "user-dirs.dirs")
        with open(udd, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("XDG_PICTURES_DIR="):
                    val = line.split("=", 1)[1].strip().strip('"')
                    val = os.path.expandvars(val.replace("$HOME", os.path.expanduser("~")))
                    if val:
                        pics = val
                    break
    except Exception:
        pics = None
    if not pics:
        pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return os.path.join(pics, APP_DIRNAME)


def install_dir_hint():
    """Where the installer drops the app (used to find a bundled mpv)."""
    if IS_WIN:
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
                            APP_DIRNAME)
    return os.path.join(_xdg("XDG_DATA_HOME", ".local/share"), APP_DIRNAME)


def app_base_dir():
    """Folder holding the running app (frozen exe/dir, or the source folder)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# subprocess helpers
# ---------------------------------------------------------------------------

def popen_extra():
    """Extra kwargs for subprocess.Popen: keep child consoles hidden on Windows."""
    if IS_WIN:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def run_extra():
    """Extra kwargs for subprocess.run (same idea as popen_extra)."""
    return popen_extra()


def path_prepend(d):
    """Put `d` first on PATH so child processes find bundled tools/DLLs."""
    if not d:
        return
    path = os.environ.get("PATH", "")
    parts = [p for p in path.split(os.pathsep) if p]
    if d.lower() not in [p.lower() for p in parts]:
        os.environ["PATH"] = d + os.pathsep + path


# ---------------------------------------------------------------------------
# mpv / yt-dlp discovery
# ---------------------------------------------------------------------------

def bundled_mpv_candidates_for(base):
    """mpv bundled next to a given app folder (installer layout)."""
    names = ["mpv.exe"] if IS_WIN else ["mpv", "mpv.bin"]
    out = []
    for n in names:
        out.append(os.path.join(base, "mpv", n))
        out.append(os.path.join(base, n))
    return out


def bundled_mpv_candidates():
    """mpv bundled next to the app (installer layout)."""
    return bundled_mpv_candidates_for(app_base_dir())


def installed_mpv_candidates():
    """mpv from a previous SyncPlayer install."""
    d = install_dir_hint()
    names = ["mpv.exe"] if IS_WIN else ["mpv"]
    return [os.path.join(d, "mpv", n) for n in names]


def system_mpv_candidates():
    """Well-known system install locations (before falling back to PATH)."""
    if IS_WIN:
        return [
            r"C:\Program Files\MPV Player\mpv.exe",
            r"C:\Program Files\mpv\mpv.exe",
            r"C:\Tools\mpv\mpv.exe",
            os.path.expanduser(r"~\AppData\Local\Programs\mpv\mpv.exe"),
            os.path.expanduser(r"~\scoop\apps\mpv\current\mpv.exe"),
        ]
    home = os.path.expanduser("~")
    return [
        "/usr/bin/mpv",
        "/usr/local/bin/mpv",
        "/snap/bin/mpv",
        "/var/lib/flatpak/exports/bin/io.mpv.Mpv",
        os.path.join(home, ".local/bin/mpv"),
        "/usr/bin/mpv.bin",
        "/opt/mpv/bin/mpv",
    ]


def bundled_ytdl_candidates():
    base = app_base_dir()
    names = [YTDL_EXE]
    out = []
    for n in names:
        out.append(os.path.join(base, "mpv", n))
        out.append(os.path.join(base, n))
    return out


def installed_ytdl_candidates():
    d = install_dir_hint()
    return [os.path.join(d, "mpv", YTDL_EXE)]


def _which(name):
    return shutil.which(name)


# ---------------------------------------------------------------------------
# IPC transport
# ---------------------------------------------------------------------------

def ipc_endpoint(name):
    """Return the value for mpv's --input-ipc-server plus the client address.

    Windows: a named pipe  (\\\\.\\pipe\\<name>)
    Linux:   a unix socket (<tmp>/<name>.sock)
    """
    if IS_WIN:
        path = r"\\.\pipe\%s" % name
        return path, path
    if IS_MAC:
        path = os.path.join(tempfile.gettempdir(), "%s.sock" % name)
    else:
        # Linux: keep it short (sun_path is 108 bytes) and per-user
        rundir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        path = os.path.join(rundir, "%s.sock" % name)
    return path, path


class Ipc:
    """mpv JSON-IPC client: named pipe on Windows, unix socket on Linux.

    Contract used by the drivers:
        connect(path, tries, delay) -> bool
        read(buf, timeout_ms=None)  -> (status, data_bytes)
             status in {"ok", "timeout", "closed"}
        write(data)                 -> bool
        close()
    """

    def __init__(self):
        self.mode = "pipe" if IS_WIN else "sock"
        self._h = None          # Windows handle
        self._sock = None       # unix socket
        self._evt = None        # Windows overlapped event
        self._ov = None
        self._closed = False
        if IS_WIN:
            self._setup_win32()
        # NOTE: no epoll/select needed on the socket path: we use a timeout.

    # -- win32 plumbing ---------------------------------------------------
    def _setup_win32(self):
        k32 = ctypes.windll.kernel32
        self._k32 = k32
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.wintypes.DWORD,
                                    ctypes.wintypes.DWORD, ctypes.c_void_p,
                                    ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
                                    ctypes.c_void_p]
        k32.ReadFile.restype = ctypes.wintypes.BOOL
        k32.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.wintypes.DWORD,
                                 ctypes.POINTER(ctypes.wintypes.DWORD),
                                 ctypes.c_void_p]
        k32.WriteFile.restype = ctypes.wintypes.BOOL
        k32.WriteFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                  ctypes.wintypes.DWORD,
                                  ctypes.POINTER(ctypes.wintypes.DWORD),
                                  ctypes.c_void_p]
        k32.GetOverlappedResult.restype = ctypes.wintypes.BOOL
        k32.GetOverlappedResult.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.wintypes.DWORD),
                                            ctypes.wintypes.BOOL]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        k32.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

        class _OVERLAPPED(ctypes.Structure):
            _fields_ = [("Internal", ctypes.c_void_p),
                        ("InternalHigh", ctypes.c_void_p),
                        ("Offset", ctypes.wintypes.DWORD),
                        ("OffsetHigh", ctypes.wintypes.DWORD),
                        ("hEvent", ctypes.c_void_p)]

        self._OVERLAPPED = _OVERLAPPED

    # -- connect ----------------------------------------------------------
    def connect(self, path, tries=60, delay=0.25):
        for _ in range(tries):
            if self._closed:
                return False
            if self.mode == "pipe":
                h = self._k32.CreateFileW(path, 0x80000000 | 0x40000000, 0,
                                          None, 3, 0x40000000, None)
                if h and h != ctypes.c_void_p(-1).value:
                    self._h = h
                    evt = self._k32.CreateEventW(None, False, False, None)
                    self._evt = evt
                    ov = self._OVERLAPPED()
                    ov.hEvent = evt
                    self._ov = ov
                    return True
            else:
                try:
                    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    s.settimeout(1.0)
                    s.connect(path)
                    s.settimeout(None)
                    self._sock = s
                    return True
                except OSError:
                    try:
                        s.close()
                    except Exception:
                        pass
            time.sleep(delay)
        return False

    # -- read -------------------------------------------------------------
    def read(self, buf, timeout_ms=None):
        """Returns (status, data). status: 'ok' | 'timeout' | 'closed'."""
        if self.mode == "pipe":
            return self._read_pipe(buf, timeout_ms)
        return self._read_sock(buf, timeout_ms)

    def _read_sock(self, buf, timeout_ms):
        if self._sock is None:
            return ("closed", b"")
        try:
            if timeout_ms is None:
                self._sock.settimeout(None)
            else:
                self._sock.settimeout(max(0.05, timeout_ms / 1000.0))
            data = self._sock.recv(len(buf))
            if not data:
                return ("closed", b"")
            return ("ok", data)
        except socket.timeout:
            return ("timeout", b"")
        except OSError:
            return ("closed", b"")

    def _read_pipe(self, buf, timeout_ms):
        if not self._h:
            return ("closed", b"")
        n = ctypes.wintypes.DWORD(0)
        if self._k32.ReadFile(self._h, ctypes.byref(buf), len(buf),
                              ctypes.byref(n), ctypes.byref(self._ov)):
            return ("ok", buf.raw[:n.value])
        err = self._k32.GetLastError()
        if err == 997:  # ERROR_IO_PENDING
            if timeout_ms is None:
                wait = self._k32.WaitForSingleObject(self._evt, 0xFFFFFFFF)
            else:
                wait = self._k32.WaitForSingleObject(self._evt, int(timeout_ms))
            if wait == 258:  # WAIT_TIMEOUT
                self._k32.CancelIoEx(self._h, ctypes.byref(self._ov))
                return ("timeout", b"")
            done = ctypes.wintypes.DWORD(0)
            if not self._k32.GetOverlappedResult(self._h, ctypes.byref(self._ov),
                                                 ctypes.byref(done), False):
                err = self._k32.GetLastError()
                if err in (109, 995, 6):   # broken pipe / aborted / invalid handle
                    return ("closed", b"")
                return ("timeout", b"")
            return ("ok", buf.raw[:done.value])
        if err in (109, 995, 6):
            return ("closed", b"")
        return ("timeout", b"")

    # -- write ------------------------------------------------------------
    def write(self, data):
        if self.mode == "pipe":
            if not self._h:
                return False
            wbuf = ctypes.create_string_buffer(data)
            written = ctypes.wintypes.DWORD(0)
            try:
                return bool(self._k32.WriteFile(self._h, ctypes.byref(wbuf),
                                                len(data), ctypes.byref(written),
                                                None))
            except Exception:
                return False
        if self._sock is None:
            return False
        try:
            self._sock.sendall(data)
            return True
        except OSError:
            return False

    # -- close ------------------------------------------------------------
    def close(self):
        self._closed = True
        try:
            if self.mode == "pipe":
                if self._h:
                    self._k32.CancelIoEx(self._h, None)
                    self._k32.CloseHandle(self._h)
                    self._h = None
                if self._evt:
                    self._k32.CloseHandle(self._evt)
                    self._evt = None
            else:
                if self._sock:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    self._sock.close()
                    self._sock = None
        except Exception:
            pass

    @property
    def connected(self):
        return bool(self._h) if self.mode == "pipe" else (self._sock is not None)


# ---------------------------------------------------------------------------
# window backend
# ---------------------------------------------------------------------------

class WindowBackend(object):
    """Base class: every method is a no-op returning a failure value."""

    name = "none"
    ok = False
    reason = "not initialised"

    # discovery
    def screen_size(self):
        return 1920, 1080

    def find_window(self, pid, title_sub, tries=120, delay=0.25):
        return None

    # geometry
    def get_rect(self, h):
        return None

    def client_size(self, h):
        return None

    def place(self, h, x, y, w, hh):
        return False

    def move_child(self, h, x, y, w, hh):
        return False

    def valid(self, h):
        return False

    # decoration / stacking
    def save_style(self, h):
        return None

    def restore_style(self, h, style):
        return False

    def set_borderless(self, h, on):
        return False

    def is_frameless(self, h):
        """True/False when the backend can tell whether a window currently has
        no decorations, or None when it cannot. Callers use this to re-assert a
        decoration state without flickering the window every poll tick."""
        return None

    def set_ontop(self, h, on):
        return False

    # embedding
    def embed(self, child, host):
        return False

    def unembed(self, child):
        return False

    def parent_of(self, h):
        return 0


# --------------------------------- Win32 ----------------------------------

class Win32Backend(WindowBackend):
    name = "win32"

    # SetWindowPos flag sets
    SWP_STYLE = 0x0001 | 0x0002 | 0x0004 | 0x0020   # NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED
    SWP_MOVE = 0x0004 | 0x0010                      # NOZORDER|NOACTIVATE
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    WS_CLIPSIBLINGS = 0x04000000
    WS_OVERLAPPEDWINDOW = 0x10000000 | 0x00CF0000
    WS_CLIPCHILDREN = 0x02000000

    def __init__(self):
        try:
            self.u = ctypes.windll.user32
            self.ok = True
            self.reason = ""
        except Exception as e:      # pragma: no cover - only on non-Windows
            self.ok = False
            self.reason = "user32 unavailable: %s" % e

    def screen_size(self):
        try:
            return (self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1))
        except Exception:
            return 1920, 1080

    def find_window(self, pid, title_sub, tries=120, delay=0.25):
        found = [0]
        for _ in range(tries):
            found[0] = 0
            cb = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def _cb(h, _l):
                pid2 = ctypes.wintypes.DWORD()
                self.u.GetWindowThreadProcessId(h, ctypes.byref(pid2))
                if pid2.value == pid and self.u.IsWindowVisible(h):
                    cls = ctypes.create_unicode_buffer(64)
                    self.u.GetClassNameW(h, cls, 64)
                    if cls.value == "mpv":
                        buf = ctypes.create_unicode_buffer(256)
                        self.u.GetWindowTextW(h, buf, 256)
                        if title_sub in buf.value:
                            found[0] = h
                return True

            self.u.EnumWindows(cb(_cb), 0)
            if found[0]:
                return found[0]
            time.sleep(delay)
        return None

    def get_rect(self, h):
        try:
            r = ctypes.wintypes.RECT()
            if not self.u.GetWindowRect(h, ctypes.byref(r)):
                return None                 # invalid handle -> no geometry
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
        except Exception:
            return None

    def client_size(self, h):
        try:
            r = ctypes.wintypes.RECT()
            if not self.u.GetClientRect(h, ctypes.byref(r)):
                return None
            return (r.right - r.left, r.bottom - r.top)
        except Exception:
            return None

    def place(self, h, x, y, w, hh):
        try:
            self.u.SetWindowPos(h, 0, x, y, max(160, w), max(120, hh), self.SWP_MOVE)
            return True
        except Exception:
            return False

    def move_child(self, h, x, y, w, hh):
        return self.place(h, x, y, w, hh)

    def valid(self, h):
        try:
            return bool(self.u.IsWindow(h))
        except Exception:
            return False

    def save_style(self, h):
        """Return the style worth restoring. Only a NORMAL captioned window is
        worth remembering - saving an already-borderless style is exactly what
        made repeated PiP toggling eventually stop removing the frame."""
        try:
            st = (self.u.GetWindowLongPtrW(h, -16) or 0) & 0xFFFFFFFF
            if st & self.WS_CAPTION:
                return st
            return self.WS_OVERLAPPEDWINDOW
        except Exception:
            return self.WS_OVERLAPPEDWINDOW

    def _set_style(self, h, style):
        self.u.SetWindowLongPtrW(h, -16,
                                 style if style < 0x80000000 else style - 0x100000000)
        self.u.SetWindowPos(h, 0, 0, 0, 0, 0, self.SWP_STYLE)

    def restore_style(self, h, style):
        try:
            self._set_style(h, style or self.WS_OVERLAPPEDWINDOW)
            return True
        except Exception:
            return False

    def set_borderless(self, h, on):
        try:
            if on:
                self._set_style(h, self.WS_POPUP | self.WS_VISIBLE | self.WS_CLIPSIBLINGS)
            else:
                self._set_style(h, self.WS_OVERLAPPEDWINDOW)
            return True
        except Exception:
            return False

    def set_ontop(self, h, on):
        # mpv's own `ontop` property drives the real stacking; this keeps the
        # style flag tidy so repeated toggles never drift.
        try:
            HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
            self.u.SetWindowPos(h, HWND_TOPMOST if on else HWND_NOTOPMOST,
                                0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            return True
        except Exception:
            return False

    def is_frameless(self, h):
        """Frameless = no caption AND no resize frame. mpv re-applies its own
        window style asynchronously when `border` flips (OR-ing sysmenu /
        thickframe back in), which is exactly the drift this detects.
        Returns None when the handle is invalid or the style cannot be read, so
        callers never act on a bogus "looks frameless" answer (a failed style
        read is 0, which would otherwise read as frameless)."""
        try:
            if not self.valid(h):
                return None
            st = (self.u.GetWindowLongPtrW(h, -16) or 0) & 0xFFFFFFFF
            if st == 0:
                return None
            return (st & (self.WS_CAPTION | self.WS_THICKFRAME)) == 0
        except Exception:
            return None

    def embed(self, child, host):
        try:
            # host needs WS_CLIPCHILDREN so its video does not overdraw the pane
            hs = (self.u.GetWindowLongPtrW(host, -16) or 0) & 0xFFFFFFFF
            hs2 = hs | self.WS_CLIPCHILDREN
            self.u.SetWindowLongPtrW(host, -16, hs2 if hs2 < 0x80000000 else hs2 - 0x100000000)
            self.u.SetParent(child, host)
            self._set_style(child, self.WS_CHILD_STYLE)
            return True
        except Exception:
            return False

    WS_CHILD_STYLE = 0x40000000 | 0x10000000 | 0x04000000

    def unembed(self, child):
        try:
            self.u.SetParent(child, 0)
            self._set_style(child, self.WS_OVERLAPPEDWINDOW)
            return True
        except Exception:
            return False

    def parent_of(self, h):
        try:
            return self.u.GetParent(h) or 0
        except Exception:
            return 0

    def set_window_title(self, h, title):
        try:
            self.u.SetWindowTextW(h, title)
            return True
        except Exception:
            return False


# ---------------------------------- X11 ------------------------------------

class _XWindowAttributes(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_int), ("y", ctypes.c_int),
        ("width", ctypes.c_int), ("height", ctypes.c_int),
        ("border_width", ctypes.c_int),
        ("depth", ctypes.c_int),
        ("visual", ctypes.c_void_p),
        ("root", ctypes.c_ulong),
        ("klass", ctypes.c_int),
        ("bit_gravity", ctypes.c_int),
        ("win_gravity", ctypes.c_int),
        ("backing_store", ctypes.c_int),
        ("backing_planes", ctypes.c_ulong),
        ("backing_pixel", ctypes.c_ulong),
        ("save_under", ctypes.c_int),
        ("colormap", ctypes.c_ulong),
        ("map_installed", ctypes.c_int),
        ("map_state", ctypes.c_int),
        ("all_event_masks", ctypes.c_long),
        ("your_event_mask", ctypes.c_long),
        ("do_not_propagate_mask", ctypes.c_long),
        ("override_redirect", ctypes.c_int),
        ("screen", ctypes.c_void_p),
    ]


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data", ctypes.c_long * 5),
    ]


class _XEvent(ctypes.Union):
    _fields_ = [("type", ctypes.c_int),
                ("xclient", _XClientMessageEvent),
                ("pad", ctypes.c_long * 24)]


class _XErrorEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int),
                ("display", ctypes.c_void_p),
                ("resourceid", ctypes.c_ulong),
                ("serial", ctypes.c_ulong),
                ("error_code", ctypes.c_ubyte),
                ("request_code", ctypes.c_ubyte),
                ("minor_code", ctypes.c_ubyte)]


# Xlib's DEFAULT protocol-error handler prints the message and EXITS THE
# PROCESS. A stale window handle (a video window destroyed a moment ago, a
# window closed while the arrange thread was iterating) must never take the
# whole app down, so the backend installs a handler that records the error and
# returns, leaving our own return-value checks (get_rect -> None, valid ->
# False) to report the failure normally.
# This handler is process-wide (Xlib has one), so it also sees Tk's protocol
# errors - they are recorded, never fatal.
X_ERRORS = []                     # [(error_code, request_code, minor, resource)]


def _x_error_handler(dpy, ev_ptr):
    try:
        if ev_ptr:
            ev = ctypes.cast(ev_ptr, ctypes.POINTER(_XErrorEvent)).contents
            X_ERRORS.append((int(ev.error_code), int(ev.request_code),
                             int(ev.minor_code), int(ev.resourceid)))
            del X_ERRORS[:-50]    # keep only recent history
    except Exception:
        pass
    return 0                      # handled - Xlib must not abort the process


_X_ERROR_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)(
    _x_error_handler)


class _MotifWmHints(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_ulong),
                ("functions", ctypes.c_ulong),
                ("decorations", ctypes.c_ulong),
                ("input_mode", ctypes.c_long),
                ("status", ctypes.c_ulong)]


_IS_VIEWABLE = 2
_CLIENT_MESSAGE = 33


class X11Backend(WindowBackend):
    """Window control through libX11 (ctypes) - works on Xorg and on
    Wayland sessions for X11 clients running under XWayland."""

    name = "x11"

    def __init__(self):
        self.ok = False
        self.reason = ""
        self.display_name = os.environ.get("DISPLAY", "") or "(unset)"
        try:
            libname = ctypes.util.find_library("X11") or "libX11.so.6"
            self.x = ctypes.CDLL(libname)
        except OSError as e:
            self.reason = "libX11 not found (%s)" % e
            return
        try:
            # Xlib must know it will be used from several threads. Called again
            # here (idempotent) so a standalone backend instance is safe even
            # if the module-level early call did not run.
            try:
                self.x.XInitThreads.restype = ctypes.c_int
                self.x.XInitThreads()
            except Exception:
                pass
            try:
                # never let a BadWindow on a stale handle abort the process
                self.x.XSetErrorHandler.restype = ctypes.c_void_p
                self.x.XSetErrorHandler.argtypes = [ctypes.c_void_p]
                self.x.XSetErrorHandler(
                    ctypes.cast(_X_ERROR_CB, ctypes.c_void_p))
            except Exception:
                pass
            self.x.XOpenDisplay.restype = ctypes.c_void_p
            self.x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            self.x.XDefaultScreen.restype = ctypes.c_int
            self.x.XDefaultScreen.argtypes = [ctypes.c_void_p]
            self.x.XRootWindow.restype = ctypes.c_ulong
            self.x.XRootWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.x.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.x.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.x.XInternAtom.restype = ctypes.c_ulong
            self.x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            self.x.XFree.argtypes = [ctypes.c_void_p]
            self.x.XGetWindowProperty.restype = ctypes.c_int
            self.x.XGetWindowProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_void_p)]
            self.x.XGetWindowAttributes.restype = ctypes.c_int
            self.x.XGetWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                   ctypes.POINTER(_XWindowAttributes)]
            self.x.XQueryTree.restype = ctypes.c_int
            self.x.XQueryTree.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint)]
            self.x.XGetGeometry.restype = ctypes.c_int
            self.x.XGetGeometry.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
            self.x.XMoveResizeWindow.restype = ctypes.c_int
            self.x.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                                 ctypes.c_int, ctypes.c_int,
                                                 ctypes.c_uint, ctypes.c_uint]
            self.x.XMoveWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                           ctypes.c_int, ctypes.c_int]
            self.x.XResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                             ctypes.c_uint, ctypes.c_uint]
            self.x.XReparentWindow.restype = ctypes.c_int
            self.x.XReparentWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                               ctypes.c_ulong, ctypes.c_int, ctypes.c_int]
            self.x.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            self.x.XChangeProperty.restype = ctypes.c_int
            self.x.XChangeProperty.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
            self.x.XSendEvent.restype = ctypes.c_int
            self.x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                          ctypes.c_int, ctypes.c_long,
                                          ctypes.POINTER(_XEvent)]
            self.x.XFlush.argtypes = [ctypes.c_void_p]
            self.x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        except Exception as e:
            self.reason = "libX11 symbol binding failed: %s" % e
            return
        dpy = self.x.XOpenDisplay(None)
        if not dpy:
            self.reason = ("cannot open X display %s (X11/XWayland not reachable; "
                           "displays, arranging and PiP need one)" % self.display_name)
            return
        self.dpy = ctypes.c_void_p(dpy)
        self.scr = self.x.XDefaultScreen(self.dpy)
        self.root = self.x.XRootWindow(self.dpy, self.scr)
        self._atoms = {}
        self.ok = True
        self.reason = ""

    # -- helpers ----------------------------------------------------------
    def atom(self, name):
        a = self._atoms.get(name)
        if a is None:
            a = self.x.XInternAtom(self.dpy, name.encode("ascii"), False)
            self._atoms[name] = a
        return a

    def _prop(self, win, prop_name, limit=512):
        """Read a window property; returns (type_atom, format, values, nbytes)."""
        prop = self.atom(prop_name)
        if not prop:
            return 0, 0, [], 0
        actual_type = ctypes.c_ulong()
        actual_fmt = ctypes.c_int()
        nitems = ctypes.c_ulong()
        after = ctypes.c_ulong()
        ptr = ctypes.c_void_p()
        st = self.x.XGetWindowProperty(self.dpy, win, prop, 0, limit, False,
                                       0,  # AnyPropertyType
                                       ctypes.byref(actual_type),
                                       ctypes.byref(actual_fmt),
                                       ctypes.byref(nitems),
                                       ctypes.byref(after),
                                       ctypes.byref(ptr))
        if st != 0 or not ptr:
            return 0, 0, [], 0
        vals = []
        try:
            if actual_fmt.value == 8:
                raw = ctypes.string_at(ptr, nitems.value)
                vals = [raw]
            elif actual_fmt.value == 32:
                arr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ulong))
                vals = [arr[i] for i in range(nitems.value)]
            elif actual_fmt.value == 16:
                arr = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_ushort))
                vals = [arr[i] for i in range(nitems.value)]
        finally:
            self.x.XFree(ptr)
        return actual_type.value, actual_fmt.value, vals, after.value

    def _text(self, win, prop_name):
        t, f, vals, _ = self._prop(win, prop_name, 256)
        if f == 8 and vals:
            raw = vals[0]
            if isinstance(raw, bytes):
                return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
        return ""

    def _cardinals(self, win, prop_name):
        t, f, vals, _ = self._prop(win, prop_name, 32)
        if f == 32:
            return vals
        return []

    def _attrs(self, win):
        a = _XWindowAttributes()
        if not self.x.XGetWindowAttributes(self.dpy, win, ctypes.byref(a)):
            return None
        return a

    def _children(self, win):
        r = ctypes.c_ulong()
        p = ctypes.c_ulong()
        kids = ctypes.POINTER(ctypes.c_ulong)()
        n = ctypes.c_uint(0)
        if not self.x.XQueryTree(self.dpy, win, ctypes.byref(r), ctypes.byref(p),
                                 ctypes.byref(kids), ctypes.byref(n)):
            return []
        out = []
        try:
            for i in range(n.value):
                out.append(int(kids[i]))
        finally:
            if kids:
                self.x.XFree(kids)
        return out

    def _walk(self, win, depth=0, max_depth=8):
        yield win
        if depth >= max_depth:
            return
        for c in self._children(win):
            for w in self._walk(c, depth + 1, max_depth):
                yield w

    # -- discovery --------------------------------------------------------
    def screen_size(self):
        try:
            return (self.x.XDisplayWidth(self.dpy, self.scr),
                    self.x.XDisplayHeight(self.dpy, self.scr))
        except Exception:
            return 1920, 1080

    def find_window(self, pid, title_sub, tries=120, delay=0.25):
        for _ in range(tries):
            for w in self._walk(self.root):
                a = self._attrs(w)
                if a is None or a.map_state != _IS_VIEWABLE:
                    continue
                if a.width < 80 or a.height < 60:
                    continue
                name = self._text(w, "_NET_WM_NAME") or self._text(w, "WM_NAME")
                if title_sub not in name:
                    continue
                # prefer an exact PID match; accept WM_CLASS mpv as fallback
                wpid = self._cardinals(w, "_NET_WM_PID")
                if wpid and pid and int(wpid[0]) == int(pid):
                    return w
                cls = self._text(w, "WM_CLASS")
                if (not wpid or not pid) and "mpv" in cls.lower():
                    return w
                if "mpv" in cls.lower():
                    return w
            time.sleep(delay)
        return None

    # -- geometry ---------------------------------------------------------
    def get_rect(self, h):
        try:
            root = ctypes.c_ulong()
            x = ctypes.c_int(); y = ctypes.c_int()
            w = ctypes.c_uint(); hh = ctypes.c_uint()
            bw = ctypes.c_uint(); d = ctypes.c_uint()
            if not self.x.XGetGeometry(self.dpy, h, ctypes.byref(root),
                                       ctypes.byref(x), ctypes.byref(y),
                                       ctypes.byref(w), ctypes.byref(hh),
                                       ctypes.byref(bw), ctypes.byref(d)):
                return None
            return (x.value, y.value, w.value, hh.value)
        except Exception:
            return None

    def client_size(self, h):
        r = self.get_rect(h)
        if not r:
            return None
        return (r[2], r[3])

    def place(self, h, x, y, w, hh):
        try:
            self.x.XMoveResizeWindow(self.dpy, h, int(x), int(y),
                                     max(160, int(w)), max(120, int(hh)))
            self.x.XFlush(self.dpy)
            return True
        except Exception:
            return False

    move_child = place

    def valid(self, h):
        try:
            return self._attrs(h) is not None
        except Exception:
            return False

    # -- decoration / stacking -------------------------------------------
    def save_style(self, h):
        _, f, vals, _ = self._prop(h, "_MOTIF_WM_HINTS", 5)
        return list(vals) if f == 32 and vals else None

    def restore_style(self, h, style):
        """Restore a saved decoration state.

        Accepts whatever save_style() returned: a motif-hints list, a Win32
        style int (treated as "decorated"), or None/0 for "never touched ->
        normal window". Never raises on an unexpected type.
        """
        dec = 1                                  # default: normal decorations
        if isinstance(style, (list, tuple)):
            try:
                dec = int(style[2]) if len(style) > 2 else 1
            except Exception:
                dec = 1
        elif isinstance(style, int):
            dec = 1                              # a Win32 style int = decorated
        if dec not in (0, 1):
            dec = 1
        return self._motif(h, dec)

    def _motif(self, h, decorations):
        """_MOTIF_WM_HINTS decorations=0 -> no titlebar/border (KDE/GNOME/XFCE honour it)."""
        try:
            hints = _MotifWmHints()
            hints.flags = 1 << 1          # MWM_HINTS_DECORATIONS
            hints.functions = 0
            hints.decorations = decorations
            hints.input_mode = 0
            hints.status = 0
            self.x.XChangeProperty(self.dpy, h, self.atom("_MOTIF_WM_HINTS"),
                                   self.atom("_MOTIF_WM_HINTS"), 32, 0,
                                   ctypes.byref(hints), 5)
            self.x.XFlush(self.dpy)
            return True
        except Exception:
            return False

    def set_borderless(self, h, on):
        return self._motif(h, 0 if on else 1)

    def is_frameless(self, h):
        """_MOTIF_WM_HINTS decorations == 0 means no titlebar/border.
        None when the window is gone (never report a stale handle as frameless)."""
        try:
            if self._attrs(h) is None:
                return None
            _t, fmt, vals, _n = self._prop(h, "_MOTIF_WM_HINTS", 5)
            if fmt != 32 or len(vals) < 3:
                return False            # no hint set -> window manager default
            return int(vals[2]) == 0
        except Exception:
            return None

    def set_ontop(self, h, on):
        """EWMH _NET_WM_STATE_ABOVE via a client message to the root window."""
        try:
            ev = _XEvent()
            ev.xclient.type = _CLIENT_MESSAGE
            ev.xclient.serial = 0
            ev.xclient.send_event = 1
            ev.xclient.display = ctypes.cast(self.dpy, ctypes.c_void_p)
            ev.xclient.window = ctypes.c_ulong(h)
            ev.xclient.message_type = self.atom("_NET_WM_STATE")
            ev.xclient.format = 32
            ev.xclient.data[0] = 1 if on else 0        # 1=add, 0=remove
            ev.xclient.data[1] = self.atom("_NET_WM_STATE_ABOVE")
            ev.xclient.data[2] = 0
            ev.xclient.data[3] = 1                      # source: application
            ev.xclient.data[4] = 0
            mask = (1 << 20) | (1 << 19)   # SubstructureRedirect | SubstructureNotify
            self.x.XSendEvent(self.dpy, self.root, False, mask, ctypes.byref(ev))
            self.x.XFlush(self.dpy)
            if on:
                self.x.XRaiseWindow(self.dpy, h)
                self.x.XFlush(self.dpy)
            return True
        except Exception:
            return False

    # -- embedding (reparent) --------------------------------------------
    def embed(self, child, host):
        try:
            self.x.XReparentWindow(self.dpy, child, host, 0, 0)
            self.x.XFlush(self.dpy)
            return True
        except Exception:
            return False

    def unembed(self, child):
        try:
            self.x.XReparentWindow(self.dpy, child, self.root, 0, 0)
            self.x.XFlush(self.dpy)
            return True
        except Exception:
            return False

    def parent_of(self, h):
        try:
            r = ctypes.c_ulong(); p = ctypes.c_ulong()
            kids = ctypes.POINTER(ctypes.c_ulong)(); n = ctypes.c_uint(0)
            if not self.x.XQueryTree(self.dpy, h, ctypes.byref(r), ctypes.byref(p),
                                     ctypes.byref(kids), ctypes.byref(n)):
                return 0
            if kids:
                self.x.XFree(kids)
            return int(p.value)
        except Exception:
            return 0


# Serialize every primitive X call on the shared connection (see the X11
# thread-safety note at the top of this file). find_window is deliberately NOT
# wrapped: it retries with sleeps, and holding the lock across those sleeps
# would freeze the Tk main thread while an arrange thread searched.
# The lock is re-entrant, so nested calls (_text -> _prop) are safe.
for _meth in ("atom", "_prop", "_text", "_cardinals", "_attrs", "_children",
              "_walk", "screen_size", "get_rect", "client_size", "place",
              "move_child", "valid", "save_style", "restore_style", "_motif",
              "set_borderless", "set_ontop", "embed", "unembed", "parent_of"):
    _fn = getattr(X11Backend, _meth, None)
    if callable(_fn):
        setattr(X11Backend, _meth, xsync(_fn))
del _meth, _fn


# ---------------------------------------------------------------------------
# backend selection
# ---------------------------------------------------------------------------

_WIN_CACHE = None
_WIN_REASON = ""


def get_window_backend():
    """Return a singleton window backend for this platform."""
    global _WIN_CACHE, _WIN_REASON
    if _WIN_CACHE is not None:
        return _WIN_CACHE
    if IS_WIN:
        _WIN_CACHE = Win32Backend()
        return _WIN_CACHE
    b = X11Backend()
    if b.ok:
        _WIN_CACHE = b
        return _WIN_CACHE
    _WIN_REASON = b.reason
    # fall back to a no-op backend so the app still runs (positioning disabled)
    fb = WindowBackend()
    fb.name = "none"
    fb.reason = b.reason
    _WIN_CACHE = fb
    return _WIN_CACHE


def window_backend_reason():
    b = get_window_backend()
    return b.reason if not b.ok else ""


# ---------------------------------------------------------------------------
# process helpers
# ---------------------------------------------------------------------------

def list_mpv_pids():
    """PIDs of running mpv processes."""
    try:
        if IS_WIN:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq mpv.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, errors="replace",
                timeout=20, **run_extra()).stdout or ""
            pids = []
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 2 and parts[0].lower().startswith("mpv"):
                    try:
                        pids.append(int(parts[1]))
                    except ValueError:
                        pass
            return pids
        out = subprocess.run(["pgrep", "-x", "mpv"], capture_output=True,
                             text=True, errors="replace", timeout=15).stdout or ""
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def kill_all_mpv():
    """Best-effort: kill every mpv process (used by tests / 'Close')."""
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/F", "/T", "/IM", "mpv.exe"],
                           capture_output=True, timeout=25, **run_extra())
        else:
            subprocess.run(["pkill", "-x", "mpv"], capture_output=True,
                           timeout=15)
            subprocess.run(["pkill", "-f", "mpv/mpv"], capture_output=True,
                           timeout=15)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# compatibility aliases (historic names used across the codebase)
# ---------------------------------------------------------------------------

def find_mpv():
    """Locate mpv: bundled next to the app > installed by SyncPlayer > PATH >
    standard system locations. Returns None when nothing is found."""
    env = os.environ.get("MPV_PATH")
    if env and os.path.isfile(env):
        return env
    for c in (bundled_mpv_candidates() + installed_mpv_candidates()
              + system_mpv_candidates()):
        if c and os.path.isfile(c):
            path_prepend(os.path.dirname(c))
            return c
    w = _which(MPV_EXE) or _which("mpv")
    if w:
        path_prepend(os.path.dirname(w))
        return w
    return None


def find_ytdl():
    """Locate yt-dlp: bundled next to mpv > SyncPlayer install > PATH."""
    env = os.environ.get("YTDLP_PATH")
    if env and os.path.isfile(env):
        return env
    for c in bundled_ytdl_candidates() + installed_ytdl_candidates():
        if c and os.path.isfile(c):
            return c
    w = _which(YTDL_EXE) or _which("yt-dlp")
    if w:
        return w
    return None


def diagnose():
    """Report what this machine can do - surfaced by `--check-env`."""
    b = get_window_backend()
    mpv = find_mpv()
    ytdl = find_ytdl()
    d = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "session_type": os.environ.get("XDG_SESSION_TYPE", "n/a") if IS_LINUX else "n/a",
        "desktop": os.environ.get("XDG_CURRENT_DESKTOP", "n/a") if IS_LINUX else "n/a",
        "display": os.environ.get("DISPLAY", "") if IS_LINUX else "n/a",
        "window_backend": b.name,
        "window_backend_ok": bool(b.ok),
        "window_backend_reason": b.reason or "",
        "screen": "%dx%d" % b.screen_size(),
        "mpv": mpv or "NOT FOUND",
        "yt_dlp": ytdl or "NOT FOUND",
        "config_dir": config_dir(),
        "shot_dir": shot_dir(),
        "ipc": "named pipe" if IS_WIN else "unix socket",
    }
    if b.ok and b.name == "x11":
        d["x11_embedding"] = "reparent (XReparentWindow)"
        d["x11_borderless"] = "_MOTIF_WM_HINTS"
        d["x11_ontop"] = "_NET_WM_STATE_ABOVE"
    return d
