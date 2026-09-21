#include "platform/win/window.h"

#ifdef _WIN32
#include <windows.h>
#endif

#include <algorithm>
#include <thread>

namespace sp {
namespace {

#ifdef _WIN32
struct FindState {
  DWORD pid = 0;
  std::string needle_lower;
  HWND best = nullptr;
  bool require_visible = true;
  bool any_title_ok = false;
};

std::string lower_ascii(std::string s) {
  for (auto& c : s) {
    if (c >= 'A' && c <= 'Z') c = static_cast<char>(c - 'A' + 'a');
  }
  return s;
}

std::string title_of(HWND h) {
  const int n = GetWindowTextLengthW(h);
  if (n <= 0) return {};
  std::wstring w(static_cast<size_t>(n) + 1, L'\0');
  const int got = GetWindowTextW(h, w.data(), n + 1);
  if (got <= 0) return {};
  w.resize(static_cast<size_t>(got));
  int need = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                                 nullptr, 0, nullptr, nullptr);
  std::string out(static_cast<size_t>(need), '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()), out.data(), need,
                      nullptr, nullptr);
  return out;
}

std::wstring widen(const std::string& s) {
  if (s.empty()) return {};
  int need = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                 nullptr, 0);
  std::wstring out(static_cast<size_t>(need), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(), need);
  return out;
}

BOOL CALLBACK enum_proc(HWND h, LPARAM param) {
  auto* st = reinterpret_cast<FindState*>(param);
  DWORD pid = 0;
  GetWindowThreadProcessId(h, &pid);
  if (pid != st->pid) return TRUE;
  const std::string title = title_of(h);
  if (title.empty()) return TRUE;
  if (st->require_visible && !IsWindowVisible(h)) return TRUE;
  if (!st->any_title_ok) {
    if (lower_ascii(title).find(st->needle_lower) == std::string::npos) return TRUE;
  }
  st->best = h;
  return FALSE;  // stop
}
#endif

}  // namespace

ScreenInfo screen_info() {
  ScreenInfo out;
#ifdef _WIN32
  RECT work{};
  if (SystemParametersInfoW(SPI_GETWORKAREA, 0, &work, 0)) {
    out.work = {work.left, work.top, work.right - work.left, work.bottom - work.top};
  }
  out.width = GetSystemMetrics(SM_CXSCREEN);
  out.height = GetSystemMetrics(SM_CYSCREEN);
  HDC dc = GetDC(nullptr);
  if (dc) {
    out.dpi = GetDeviceCaps(dc, LOGPIXELSX);
    ReleaseDC(nullptr, dc);
  }
  if (out.work.w <= 0) out.work = {0, 0, out.width, out.height};
#endif
  return out;
}

std::optional<void*> find_window_for_pid(int pid, const std::string& title_sub,
                                         int tries, int delay_ms) {
#ifdef _WIN32
  for (int attempt = 0; attempt < tries; ++attempt) {
    FindState st;
    st.pid = static_cast<DWORD>(pid);
    st.needle_lower = lower_ascii(title_sub);
    st.require_visible = true;
    st.any_title_ok = title_sub.empty();
    EnumWindows(enum_proc, reinterpret_cast<LPARAM>(&st));
    if (st.best) return st.best;
    std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
  }
  return std::nullopt;
#else
  (void)pid; (void)title_sub; (void)tries; (void)delay_ms;
  return std::nullopt;
#endif
}

std::optional<void*> any_window_for_pid(int pid) {
#ifdef _WIN32
  FindState st;
  st.pid = static_cast<DWORD>(pid);
  st.require_visible = false;
  st.any_title_ok = true;
  EnumWindows(enum_proc, reinterpret_cast<LPARAM>(&st));
  if (st.best) return st.best;
  return std::nullopt;
#else
  (void)pid;
  return std::nullopt;
#endif
}

Rect window_rect(void* hwnd) {
  Rect r;
#ifdef _WIN32
  RECT w{};
  if (hwnd && GetWindowRect(static_cast<HWND>(hwnd), &w)) {
    r = {w.left, w.top, w.right - w.left, w.bottom - w.top};
  }
#endif
  return r;
}

bool window_minimised(void* hwnd) {
#ifdef _WIN32
  return hwnd && IsIconic(static_cast<HWND>(hwnd));
#else
  (void)hwnd;
  return false;
#endif
}

bool window_visible(void* hwnd) {
#ifdef _WIN32
  return hwnd && IsWindowVisible(static_cast<HWND>(hwnd));
#else
  (void)hwnd;
  return false;
#endif
}

std::string window_title(void* hwnd) {
#ifdef _WIN32
  return hwnd ? title_of(static_cast<HWND>(hwnd)) : std::string();
#else
  (void)hwnd;
  return {};
#endif
}

bool place_window(void* hwnd, const Rect& r, bool activate) {
#ifdef _WIN32
  if (!hwnd) return false;
  // SWP_NOZORDER keeps the z-order alone; without NOACTIVATE the panel would steal
  // focus from the video the user is watching.
  UINT flags = SWP_NOZORDER | SWP_NOOWNERZORDER;
  if (!activate) flags |= SWP_NOACTIVATE;
  return SetWindowPos(static_cast<HWND>(hwnd), nullptr, r.x, r.y, r.w, r.h, flags) != 0;
#else
  (void)hwnd; (void)r; (void)activate;
  return false;
#endif
}

bool arrange_side_by_side(void* left, void* right, const Rect& work_area, int gap,
                          bool activate) {
  if (!left || !right) return false;
  const int half = (work_area.w - gap) / 2;
  if (half <= 0) return false;
  const Rect l{work_area.x, work_area.y, half, work_area.h};
  const Rect r{work_area.x + half + gap, work_area.y, half, work_area.h};
  const bool a = place_window(left, l, activate);
  const bool b = place_window(right, r, activate);
  return a && b;
}

bool set_borderless(void* hwnd, bool borderless) {
#ifdef _WIN32
  if (!hwnd) return false;
  HWND h = static_cast<HWND>(hwnd);
  LONG_PTR style = GetWindowLongPtrW(h, GWL_STYLE);
  if (borderless) {
    style &= ~static_cast<LONG_PTR>(kStyleCaption | kStyleThickFrame);
  } else {
    style |= static_cast<LONG_PTR>(kStyleCaption | kStyleThickFrame);
  }
  SetWindowLongPtrW(h, GWL_STYLE, style);
  SetWindowPos(h, nullptr, 0, 0, 0, 0,
               SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED);
  return true;
#else
  (void)hwnd; (void)borderless;
  return false;
#endif
}

bool set_always_on_top(void* hwnd, bool on_top) {
#ifdef _WIN32
  if (!hwnd) return false;
  HWND h = static_cast<HWND>(hwnd);
  return SetWindowPos(h, on_top ? HWND_TOPMOST : HWND_NOTOPMOST, 0, 0, 0, 0,
                      SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE) != 0;
#else
  (void)hwnd; (void)on_top;
  return false;
#endif
}

bool set_fullscreen(void* hwnd, bool fullscreen) {
#ifdef _WIN32
  if (!hwnd) return false;
  HWND h = static_cast<HWND>(hwnd);
  if (fullscreen) {
    MONITORINFO mi{};
    mi.cbSize = sizeof mi;
    if (GetMonitorInfoW(MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST), &mi)) {
      SetWindowLongPtrW(h, GWL_STYLE,
                        GetWindowLongPtrW(h, GWL_STYLE) & ~static_cast<LONG_PTR>(kStyleCaption));
      return SetWindowPos(h, HWND_TOP, mi.rcMonitor.left, mi.rcMonitor.top,
                          mi.rcMonitor.right - mi.rcMonitor.left,
                          mi.rcMonitor.bottom - mi.rcMonitor.top,
                          SWP_NOACTIVATE | SWP_FRAMECHANGED) != 0;
    }
  }
  SetWindowLongPtrW(h, GWL_STYLE,
                    GetWindowLongPtrW(h, GWL_STYLE) | static_cast<LONG_PTR>(kStyleCaption));
  ShowWindow(h, SW_RESTORE);
  return true;
#else
  (void)hwnd; (void)fullscreen;
  return false;
#endif
}

bool minimise(void* hwnd) {
#ifdef _WIN32
  return hwnd && ShowWindow(static_cast<HWND>(hwnd), SW_MINIMIZE) != 0;
#else
  (void)hwnd;
  return false;
#endif
}

bool restore(void* hwnd) {
#ifdef _WIN32
  return hwnd && ShowWindow(static_cast<HWND>(hwnd), SW_RESTORE) != 0;
#else
  (void)hwnd;
  return false;
#endif
}

bool embed_child(void* child, void* parent, const Rect& r) {
#ifdef _WIN32
  if (!child || !parent) return false;
  HWND c = static_cast<HWND>(child);
  SetParent(c, static_cast<HWND>(parent));
  LONG_PTR style = GetWindowLongPtrW(c, GWL_STYLE);
  style &= ~static_cast<LONG_PTR>(kStyleCaption | kStyleThickFrame);
  style |= WS_CHILD;
  SetWindowLongPtrW(c, GWL_STYLE, style);
  return SetWindowPos(c, nullptr, r.x, r.y, r.w, r.h,
                      SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED) != 0;
#else
  (void)child; (void)parent; (void)r;
  return false;
#endif
}

bool undock_child(void* child) {
#ifdef _WIN32
  if (!child) return false;
  HWND c = static_cast<HWND>(child);
  SetParent(c, nullptr);
  LONG_PTR style = GetWindowLongPtrW(c, GWL_STYLE);
  style &= ~static_cast<LONG_PTR>(WS_CHILD);
  style |= static_cast<LONG_PTR>(kStyleCaption | kStyleThickFrame);
  SetWindowLongPtrW(c, GWL_STYLE, style);
  return SetWindowPos(c, nullptr, 100, 100, 960, 540,
                      SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED) != 0;
#else
  (void)child;
  return false;
#endif
}

}  // namespace sp
