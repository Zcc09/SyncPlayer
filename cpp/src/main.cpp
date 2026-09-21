// The application window: modern Windows chrome with a Fluent-drawn panel inside.
//
// The frame is removed (WM_NCCALCSIZE) and the title bar is drawn by the panel, which
// is what WinUI apps do. DWM still supplies the rounded corners, the shadow and the
// dark title bar treatment.
#include <windows.h>
#include <windowsx.h>  // GET_X_LPARAM / GET_Y_LPARAM
#include <dwmapi.h>

#include <algorithm>
#include <memory>

#include "app/panel.h"

namespace {

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

void apply_dwm(HWND hwnd, bool dark) {
  const BOOL use_dark = dark ? TRUE : FALSE;
  DwmSetWindowAttribute(hwnd, kDwmUseImmersiveDarkMode, &use_dark, sizeof use_dark);
  const int corner = kDwmCornerRound;
  DwmSetWindowAttribute(hwnd, kDwmWindowCornerPreference, &corner, sizeof corner);
  // Ask for Mica where it exists; on Windows 10 the call simply fails and the panel's
  // own gradient is what shows.
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
      mmi->ptMinTrackSize.x = 700;
      mmi->ptMinTrackSize.y = 520;
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
      SetWindowPos(hwnd, nullptr, suggested->left, suggested->top,
                   suggested->right - suggested->left, suggested->bottom - suggested->top,
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
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_LBUTTONUP:
      ReleaseCapture();
      g_input.mouse_down = false;
      g_input.mouse_released = true;
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_CHAR:
      if (wp >= 32) g_input.typed.push_back(static_cast<wchar_t>(wp));
      else if (wp == 8 || wp == 13) g_input.typed.push_back(static_cast<wchar_t>(wp));
      InvalidateRect(hwnd, nullptr, FALSE);
      return 0;

    case WM_KEYDOWN: {
      g_input.shift = (GetKeyState(VK_SHIFT) & 0x8000) != 0;
      g_input.control = (GetKeyState(VK_CONTROL) & 0x8000) != 0;
      g_input.keys.push_back(static_cast<unsigned>(wp));
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
  int w = cfg.window.valid() ? cfg.window.w : 800;
  int h = cfg.window.valid() ? cfg.window.h : 900;
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

  // Open at the height the panel was designed for, unless the screen cannot take it,
  // or the saved geometry already asked for more.
  {
    const int screen_h = GetSystemMetrics(SM_CYSCREEN);
    const int wanted = std::min(960, screen_h - 80);
    if (h < wanted) {
      RECT wr{};
      GetWindowRect(hwnd, &wr);
      h = wanted;
      if (y + h > screen_h - 40) y = std::max(0, screen_h - h - 40);
      SetWindowPos(hwnd, nullptr, x, y, w, h, SWP_NOZORDER | SWP_NOACTIVATE);
    }
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
