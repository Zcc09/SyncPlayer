// The application window: modern Windows chrome with a Fluent-drawn panel inside.
//
// The frame is removed (WM_NCCALCSIZE) and the title bar is drawn by the panel, which
// is what WinUI apps do. DWM still supplies the rounded corners, the shadow and the
// dark title bar treatment.
#include <windows.h>
#include <windowsx.h>  // GET_X_LPARAM / GET_Y_LPARAM
#include <dwmapi.h>

#include <algorithm>
#include <cstdio>
#include <memory>

#include "app/debug.h"
#include "app/panel.h"

namespace {

using sp::app::dbg;
using sp::app::debug_on;

constexpr wchar_t kClassName[] = L"SyncPlayerMainWindow";
constexpr UINT_PTR kSyncTimer = 1;
constexpr UINT kSyncIntervalMs = 33;

// DWM attributes that are not in every SDK header yet
constexpr DWORD kDwmUseImmersiveDarkMode = 20;
constexpr DWORD kDwmWindowCornerPreference = 33;
constexpr DWORD kDwmSystemBackdropType = 38;
constexpr int kDwmCornerRound = 2;       // DWMWCP_ROUND
constexpr int kDwmBackdropMainWindow = 2;  // DWMSBT_MAINWINDOW (Mica)

std::unique_ptr<sp::app::Panel> g_panel;
sp::ui::InputState g_input;
bool g_tracking_leave = false;

// The panel's minimum, converted to physical pixels for this window's DPI: at 150%
// scaling the same design needs 1.5x the pixels, otherwise the panel would scale itself
// down on a high-DPI screen and the type would come out smaller than the design.
int min_width_for(HWND hwnd) {
  const UINT dpi = hwnd ? GetDpiForWindow(hwnd) : 96;
  const float s = (dpi ? static_cast<float>(dpi) : 96.0f) / 96.0f;
  return static_cast<int>(sp::app::Panel::kMinWidth * s);
}

int min_height_for(HWND hwnd) {
  const UINT dpi = hwnd ? GetDpiForWindow(hwnd) : 96;
  const float s = (dpi ? static_cast<float>(dpi) : 96.0f) / 96.0f;
  return static_cast<int>(sp::app::Panel::kMinHeight * s);
}

// DWM supplies the rounded corners, the dark title bar treatment and, where it exists,
// the Mica backdrop. On Windows 10 the backdrop call simply fails and the panel's own
// gradient is what shows.
void apply_dwm(HWND hwnd, bool dark) {
  const BOOL use_dark = dark ? TRUE : FALSE;
  DwmSetWindowAttribute(hwnd, kDwmUseImmersiveDarkMode, &use_dark, sizeof use_dark);
  const int corner = kDwmCornerRound;
  DwmSetWindowAttribute(hwnd, kDwmWindowCornerPreference, &corner, sizeof corner);
  const int backdrop = kDwmBackdropMainWindow;
  DwmSetWindowAttribute(hwnd, kDwmSystemBackdropType, &backdrop, sizeof backdrop);
}

LRESULT hit_test(HWND hwnd, POINT screen_pt) {
  RECT rc{};
  GetWindowRect(hwnd, &rc);
  const int border = 7;
  const int x = screen_pt.x, y = screen_pt.y;
  const bool left = x < rc.left + border, right = x >= rc.right - border;
  const bool top = y < rc.top + border, bottom = y >= rc.bottom - border;
  if (top && left) return HTTOPLEFT;
  if (top && right) return HTTOPRIGHT;
  if (bottom && left) return HTBOTTOMLEFT;
  if (bottom && right) return HTBOTTOMRIGHT;
  if (left) return HTLEFT;
  if (right) return HTRIGHT;
  if (top) return HTTOP;
  if (bottom) return HTBOTTOM;

  // the title bar drags the window, except where its buttons are
  POINT client = screen_pt;
  ScreenToClient(hwnd, &client);
  if (client.y >= 0 && client.y < 40) {
    if (client.x < rc.right - rc.left - 100) return HTCAPTION;
  }
  return HTCLIENT;
}

LRESULT CALLBACK wnd_proc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp) {
  switch (msg) {
    case WM_NCCALCSIZE:
      if (wp) return 0;  // the client area covers the whole window
      break;

    case WM_NCHITTEST: {
      const LRESULT r = hit_test(hwnd, POINT{GET_X_LPARAM(lp), GET_Y_LPARAM(lp)});
      if (r != HTCLIENT) return r;
      break;
    }

    case WM_GETMINMAXINFO: {
      auto* mmi = reinterpret_cast<MINMAXINFO*>(lp);
      mmi->ptMinTrackSize.x = min_width_for(hwnd);
      mmi->ptMinTrackSize.y = min_height_for(hwnd);
      return 0;
    }

    case WM_ERASEBKGND:
      return 1;  // Direct2D paints every pixel; erasing would flicker

    case WM_SIZE:
      if (g_panel) {
        g_panel->on_resize(static_cast<float>(LOWORD(lp)), static_cast<float>(HIWORD(lp)));
      }
      return 0;

    case WM_DPICHANGED: {
      const UINT dpi = HIWORD(wp);
      if (g_panel) g_panel->on_dpi(static_cast<float>(dpi));
      const RECT* suggested = reinterpret_cast<const RECT*>(lp);
      const int sw = std::max(static_cast<int>(suggested->right - suggested->left),
                              min_width_for(hwnd));
      const int sh = std::max(static_cast<int>(suggested->bottom - suggested->top),
                              min_height_for(hwnd));
      SetWindowPos(hwnd, nullptr, suggested->left, suggested->top, sw, sh,
                   SWP_NOZORDER | SWP_NOACTIVATE);
      return 0;
    }

    case WM_PAINT: {
      PAINTSTRUCT ps{};
      BeginPaint(hwnd, &ps);
      if (g_panel) {
        g_panel->on_input(g_input);
        g_panel->draw();
      }
      EndPaint(hwnd, &ps);
      g_input.clear_transient();  // typing and clicks belong to exactly one frame
      return 0;
    }

    case WM_MOUSEMOVE: {
      g_input.mouse_x = static_cast<float>(GET_X_LPARAM(lp));
      g_input.mouse_y = static_cast<float>(GET_Y_LPARAM(lp));
      if (!g_tracking_leave) {
        TRACKMOUSEEVENT tme{sizeof tme, TME_LEAVE, hwnd, 0};
        TrackMouseEvent(&tme);
        g_tracking_leave = true;
      }
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;
    }

    case WM_MOUSELEAVE:
      g_tracking_leave = false;
      g_input.mouse_x = -1000;
      g_input.mouse_y = -1000;
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_LBUTTONDOWN:
      SetCapture(hwnd);
      g_input.mouse_x = static_cast<float>(GET_X_LPARAM(lp));
      g_input.mouse_y = static_cast<float>(GET_Y_LPARAM(lp));
      g_input.mouse_down = true;
      g_input.mouse_pressed = true;
      dbg("lbuttondown", GET_X_LPARAM(lp), GET_Y_LPARAM(lp));
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_LBUTTONUP:
      ReleaseCapture();
      g_input.mouse_down = false;
      g_input.mouse_released = true;
      dbg("lbuttonup");
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_CHAR:
      if (wp >= 32) g_input.typed.push_back(static_cast<wchar_t>(wp));
      else if (wp == 8 || wp == 13) g_input.typed.push_back(static_cast<wchar_t>(wp));
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_KEYUP:
      if (wp == VK_CONTROL || wp == VK_LCONTROL || wp == VK_RCONTROL) {
        g_input.control = false;
      }
      if (wp == VK_SHIFT || wp == VK_LSHIFT || wp == VK_RSHIFT) g_input.shift = false;
      return 0;

    case WM_KEYDOWN: {
      // both sources, so a posted Ctrl+1 behaves like a typed one
      if (wp == VK_CONTROL || wp == VK_LCONTROL || wp == VK_RCONTROL) {
        g_input.control = true;
      }
      if (wp == VK_SHIFT || wp == VK_LSHIFT || wp == VK_RSHIFT) g_input.shift = true;
      g_input.shift = g_input.shift || (GetKeyState(VK_SHIFT) & 0x8000) != 0;
      g_input.control = g_input.control || (GetKeyState(VK_CONTROL) & 0x8000) != 0;
      g_input.keys.push_back(static_cast<unsigned>(wp));
      dbg("keydown", static_cast<long>(wp), g_input.control ? 1 : 0);
      // shortcuts that are not text editing: space plays, arrows jump
      if (g_panel && !g_panel->editing_text()) {
        switch (wp) {
          case VK_SPACE:
            g_panel->toggle_play();
            g_input.keys.clear();
            break;
          case VK_LEFT:
            g_panel->nudge_jump(-1);
            g_input.keys.clear();
            break;
          case VK_RIGHT:
            g_panel->nudge_jump(+1);
            g_input.keys.clear();
            break;
          case VK_TAB:
            if (g_input.control) {
              g_panel->select_tab((g_panel->current_tab() + 1) % sp::app::kTabCount);
              g_input.keys.clear();
            }
            break;
          case '1':
          case '2':
          case '3':
          case '4':
            if (g_input.control) {
              g_panel->select_tab(static_cast<int>(wp - '1'));
              g_input.keys.clear();
            }
            break;
          default:
            break;
        }
      }
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;
    }

    case WM_TIMER:
      if (wp == kSyncTimer && g_panel) {
        g_panel->tick();
        InvalidateRect(hwnd, nullptr, FALSE);
      }
      return 0;

    case WM_CLOSE: {
      RECT rc{};
      GetWindowRect(hwnd, &rc);
      if (g_panel) {
        g_panel->remember_window(rc.left, rc.top, rc.right - rc.left, rc.bottom - rc.top);
        g_panel->shutdown();
      }
      DestroyWindow(hwnd);
      return 0;
    }

    case WM_DESTROY:
      KillTimer(hwnd, kSyncTimer);
      PostQuitMessage(0);
      return 0;

    default:
      break;
  }
  return DefWindowProcW(hwnd, msg, wp, lp);
}

}  // namespace

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE, LPWSTR, int) {
  // Per-monitor DPI from the start, so the Fluent metrics are crisp on every display.
  SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
  CoInitializeEx(nullptr, COINIT_APARTMENTTHREADED);

  WNDCLASSEXW wc{};
  wc.cbSize = sizeof wc;
  wc.style = CS_HREDRAW | CS_VREDRAW;
  wc.lpfnWndProc = wnd_proc;
  wc.hInstance = instance;
  wc.hCursor = LoadCursor(nullptr, IDC_ARROW);
  wc.hIcon = LoadIconW(instance, L"APPICON");
  wc.lpszClassName = kClassName;
  if (!RegisterClassExW(&wc)) return 1;

  sp::Config cfg = sp::Config::load();
  int x = cfg.window.x, y = cfg.window.y;
  int w = cfg.window.valid() ? cfg.window.w : 880;
  int h = cfg.window.valid() ? cfg.window.h : 780;
  if (!cfg.window.valid()) {
    const int sw = GetSystemMetrics(SM_CXSCREEN), sh = GetSystemMetrics(SM_CYSCREEN);
    x = (sw - w) / 2;
    y = (sh - h) / 2;
  }

  HWND hwnd = CreateWindowExW(0, kClassName, L"SyncPlayer", WS_POPUP | WS_THICKFRAME |
                                  WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_CLIPCHILDREN,
                              x, y, w, h, nullptr, nullptr, instance, nullptr);
  if (!hwnd) return 1;

  apply_dwm(hwnd, sp::ui::system_prefers_dark());

  g_panel = std::make_unique<sp::app::Panel>();
  if (!g_panel->init(hwnd)) return 1;

  RECT rc{};
  GetClientRect(hwnd, &rc);
  g_panel->on_resize(static_cast<float>(rc.right - rc.left),
                     static_cast<float>(rc.bottom - rc.top));

  // No tab is ever clipped: the window cannot be smaller than the panel's own minimum,
  // and it opens at a size that shows every tab comfortably.
  {
    const int screen_w = GetSystemMetrics(SM_CXSCREEN);
    const int screen_h = GetSystemMetrics(SM_CYSCREEN);
    const int min_w = min_width_for(hwnd);
    const int min_h = min_height_for(hwnd);
    if (w < min_w) w = std::min(min_w, screen_w - 40);
    if (h < min_h) h = std::min(min_h, screen_h - 60);
    if (x + w > screen_w) x = std::max(0, screen_w - w - 20);
    if (y + h > screen_h) y = std::max(0, screen_h - h - 40);
    SetWindowPos(hwnd, nullptr, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE);
  }

  ShowWindow(hwnd, SW_SHOW);
  UpdateWindow(hwnd);
  SetTimer(hwnd, kSyncTimer, kSyncIntervalMs, nullptr);

  MSG msg{};
  while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
    TranslateMessage(&msg);
    DispatchMessageW(&msg);
  }

  g_panel.reset();
  CoUninitialize();
  return 0;
}
