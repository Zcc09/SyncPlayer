// Window handling: finding mpv's windows, arranging them side by side, and the PiP
// styles. Ported from sp_plat's Win32Backend plus the placement helpers in
// syncplayer.py, keeping the rules that were learned the hard way: never steal focus,
// never reorder windows, and treat an async mpv style write as a race to be verified.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "core/config.h"

namespace sp {

struct Rect {
  int x = 0, y = 0, w = 0, h = 0;
  int right() const { return x + w; }
  int bottom() const { return y + h; }
};

struct ScreenInfo {
  Rect work;     // the primary monitor's work area (taskbar excluded)
  int width = 0; // full screen
  int height = 0;
  int dpi = 96;
};

ScreenInfo screen_info();

// Find a window belonging to pid whose title contains `title_sub`. An mpv window is
// found even when it is minimised or hidden, because the capture and crop paths need
// it exactly then. Returns nullopt when nothing matches within the timeout.
std::optional<void*> find_window_for_pid(int pid, const std::string& title_sub,
                                         int tries = 120, int delay_ms = 250);

// The first top-level window of the process, regardless of visibility.
std::optional<void*> any_window_for_pid(int pid);

Rect window_rect(void* hwnd);
bool window_minimised(void* hwnd);
bool window_visible(void* hwnd);
std::string window_title(void* hwnd);

// Place without stealing focus and without changing the z-order.
bool place_window(void* hwnd, const Rect& r, bool activate = false);
bool arrange_side_by_side(void* left, void* right, const Rect& work_area,
                          int gap = 8, bool activate = false);

// PiP styles
bool set_borderless(void* hwnd, bool borderless);
bool set_always_on_top(void* hwnd, bool on_top);
bool set_fullscreen(void* hwnd, bool fullscreen);
bool minimise(void* hwnd);
bool restore(void* hwnd);

// Embedding one mpv window inside another (integrated PiP)
bool embed_child(void* child, void* parent, const Rect& r);
bool undock_child(void* child);

// The window style bits, exposed because the Python build had to reason about mpv
// rewriting its own style asynchronously.
inline constexpr unsigned kStyleCaption = 0x00C00000;
inline constexpr unsigned kStyleThickFrame = 0x00040000;

}  // namespace sp
