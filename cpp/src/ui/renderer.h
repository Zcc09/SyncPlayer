// A small Direct2D + DirectWrite wrapper: everything the Fluent look needs, and
// nothing more. Controls are drawn by hand on top of this, which is what lets the
// window look like a WinUI surface without carrying the Windows App SDK runtime.
#pragma once

#include <d2d1.h>
#include <dwrite.h>
#include <windows.h>
#include <wrl/client.h>

#include <map>
#include <string>
#include <vector>

#include "ui/theme.h"

namespace sp::ui {

enum class TextAlign { Left, Center, Right };
enum class TextWeight { Regular, SemiBold, Bold };

struct RectF {
  float x = 0, y = 0, w = 0, h = 0;
  D2D1_RECT_F d2d() const { return D2D1::RectF(x, y, x + w, y + h); }
  bool contains(float px, float py) const {
    return px >= x && px <= x + w && py >= y && py <= y + h;
  }
};

class Renderer {
 public:
  bool init(HWND hwnd);
  void shutdown();
  void resize(UINT width, UINT height, float dpi);

  // A frame: everything drawn between these two calls lands on screen.
  void begin();
  void end();

  void fill_rect(const RectF& r, Color c);
  void fill_rounded(const RectF& r, float radius, Color c);
  void stroke_rounded(const RectF& r, float radius, Color c, float width = 1.0f);
  void fill_gradient_v(const RectF& r, Color top, Color bottom);
  void fill_circle(float cx, float cy, float radius, Color c);
  void line(float x0, float y0, float x1, float y1, Color c, float width = 1.0f);

  // Text with Fluent's type sizes. `size` is a point size; the family is Segoe UI
  // Variable with a Segoe UI fallback, which is what WinUI uses.
  void text(const std::wstring& s, const RectF& r, Color c, float size,
            TextAlign align = TextAlign::Left, TextWeight weight = TextWeight::Regular,
            bool ellipsis = true);
  float measure(const std::wstring& s, float size,
                TextWeight weight = TextWeight::Regular);
  float line_height(float size) const;

  void clip_push(const RectF& r);
  void clip_pop();

  // The focus ring Fluent draws around a focused control.
  void focus_ring(const RectF& r, float radius, Color c);

  const Theme& theme() const { return theme_; }
  void set_theme(const Theme& t) { theme_ = t; }
  float dpi() const { return dpi_; }
  bool ready() const { return target_ != nullptr; }

  // A uniform scale for the whole surface. The panel lays out in design units and
  // the scale makes that fit whatever height the window has, so a short window shows
  // a smaller panel instead of a clipped one.
  void set_scale(float scale);
  float scale() const { return scale_; }

 private:
  IDWriteTextFormat* format_for(float size, TextAlign align, TextWeight weight);
  Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> brush_for(Color c);
  void release_device_objects();

  HWND hwnd_ = nullptr;
  float dpi_ = 96.0f;
  float scale_ = 1.0f;
  Theme theme_ = Theme::dark_theme();

  Microsoft::WRL::ComPtr<ID2D1Factory> factory_;
  Microsoft::WRL::ComPtr<IDWriteFactory> dwrite_;
  Microsoft::WRL::ComPtr<ID2D1HwndRenderTarget> target_;

  // brushes are cached per colour; a frame uses a couple of dozen distinct ones
  std::map<uint32_t, Microsoft::WRL::ComPtr<ID2D1SolidColorBrush>> brushes_;
  std::map<uint64_t, Microsoft::WRL::ComPtr<IDWriteTextFormat>> formats_;
  std::vector<D2D1_RECT_F> clip_stack_;
};

}  // namespace sp::ui
