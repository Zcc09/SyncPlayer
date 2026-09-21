#include "ui/renderer.h"

#include <algorithm>
#include <cmath>
#include <cstdio>

#pragma comment(lib, "d2d1.lib")
#pragma comment(lib, "dwrite.lib")

namespace sp::ui {
namespace {

uint32_t color_key(Color c) {
  auto q = [](float v) {
    return static_cast<uint32_t>(std::lround(std::clamp(v, 0.0f, 1.0f) * 255.0f));
  };
  return (q(c.a) << 24) | (q(c.r) << 16) | (q(c.g) << 8) | q(c.b);
}

uint64_t format_key(float size, TextAlign align, TextWeight weight) {
  return (static_cast<uint64_t>(std::lround(size * 4)) << 16) |
         (static_cast<uint64_t>(align) << 8) | static_cast<uint64_t>(weight);
}

}  // namespace

bool Renderer::init(HWND hwnd) {
  hwnd_ = hwnd;
  if (FAILED(D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, factory_.GetAddressOf()))) {
    return false;
  }
  if (FAILED(DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED, __uuidof(IDWriteFactory),
                                 reinterpret_cast<IUnknown**>(dwrite_.GetAddressOf())))) {
    return false;
  }
  RECT rc{};
  GetClientRect(hwnd, &rc);
  const D2D1_SIZE_U size = D2D1::SizeU(std::max<LONG>(1, rc.right - rc.left),
                                       std::max<LONG>(1, rc.bottom - rc.top));
  const D2D1_RENDER_TARGET_PROPERTIES props = D2D1::RenderTargetProperties(
      D2D1_RENDER_TARGET_TYPE_DEFAULT,
      D2D1::PixelFormat(DXGI_FORMAT_B8G8R8A8_UNORM, D2D1_ALPHA_MODE_IGNORE), 0, 0,
      D2D1_RENDER_TARGET_USAGE_NONE, D2D1_FEATURE_LEVEL_DEFAULT);
  const D2D1_HWND_RENDER_TARGET_PROPERTIES hwnd_props =
      D2D1::HwndRenderTargetProperties(hwnd, size, D2D1_PRESENT_OPTIONS_IMMEDIATELY);
  return SUCCEEDED(factory_->CreateHwndRenderTarget(props, hwnd_props,
                                                    target_.GetAddressOf()));
}

void Renderer::shutdown() {
  release_device_objects();
  target_.Reset();
  dwrite_.Reset();
  factory_.Reset();
  hwnd_ = nullptr;
}

void Renderer::release_device_objects() {
  brushes_.clear();
  formats_.clear();
}

void Renderer::resize(UINT width, UINT height, float dpi) {
  dpi_ = dpi > 0 ? dpi : 96.0f;
  if (!target_) return;
  target_->Resize(D2D1::SizeU(std::max<UINT>(1, width), std::max<UINT>(1, height)));
  float current_x = 96.0f, current_y = 96.0f;
  target_->GetDpi(&current_x, &current_y);
  if (std::fabs(dpi_ - current_x) > 0.5f) {
    target_->SetDpi(dpi_, dpi_);
    // text formats embed a size in DIPs, so a DPI change invalidates them
    formats_.clear();
  }
}

void Renderer::set_scale(float scale) {
  scale_ = (scale > 0.05f) ? scale : 0.05f;
  if (target_) {
    target_->SetTransform(D2D1::Matrix3x2F::Scale(scale_, scale_));
  }
}

void Renderer::begin() {
  if (target_) {
    target_->SetTransform(D2D1::Matrix3x2F::Scale(scale_, scale_));
  }
  if (!target_) return;
  if (target_->CheckWindowState() & D2D1_WINDOW_STATE_OCCLUDED) {
    // still draw: the window may be partially covered, and a stale frame is worse
  }
  target_->BeginDraw();
  target_->SetTransform(D2D1::Matrix3x2F::Identity());
  target_->SetAntialiasMode(D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
  target_->SetTextAntialiasMode(D2D1_TEXT_ANTIALIAS_MODE_CLEARTYPE);
}

void Renderer::end() {
  if (!target_) return;
  while (!clip_stack_.empty()) clip_pop();
  const HRESULT hr = target_->EndDraw();
  if (hr == D2DERR_RECREATE_TARGET) {
    release_device_objects();
    target_.Reset();
    init(hwnd_);
  }
}

Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> Renderer::brush_for(Color c) {
  const uint32_t key = color_key(c);
  auto it = brushes_.find(key);
  if (it != brushes_.end()) return it->second;
  Microsoft::WRL::ComPtr<ID2D1SolidColorBrush> brush;
  if (target_) target_->CreateSolidColorBrush(to_d2d(c), brush.GetAddressOf());
  brushes_[key] = brush;
  return brush;
}

void Renderer::fill_rect(const RectF& r, Color c) {
  if (!target_ || r.w <= 0 || r.h <= 0) return;
  target_->FillRectangle(r.d2d(), brush_for(c).Get());
}

void Renderer::fill_rounded(const RectF& r, float radius, Color c) {
  if (!target_ || r.w <= 0 || r.h <= 0) return;
  const float rad = std::min(radius, std::min(r.w, r.h) / 2.0f);
  target_->FillRoundedRectangle(D2D1::RoundedRect(r.d2d(), rad, rad),
                                brush_for(c).Get());
}

void Renderer::stroke_rounded(const RectF& r, float radius, Color c, float width) {
  if (!target_ || r.w <= 0 || r.h <= 0) return;
  const float rad = std::min(radius, std::min(r.w, r.h) / 2.0f);
  // a hairline centred on the edge would blur; inset by half the stroke
  const RectF inset{r.x + width / 2, r.y + width / 2, r.w - width, r.h - width};
  target_->DrawRoundedRectangle(D2D1::RoundedRect(inset.d2d(), rad, rad),
                                brush_for(c).Get(), width);
}

void Renderer::fill_gradient_v(const RectF& r, Color top, Color bottom) {
  if (!target_ || r.w <= 0 || r.h <= 0) return;
  D2D1_GRADIENT_STOP stops[2] = {};
  stops[0].position = 0.0f;
  stops[0].color = to_d2d(top);
  stops[1].position = 1.0f;
  stops[1].color = to_d2d(bottom);
  Microsoft::WRL::ComPtr<ID2D1GradientStopCollection> collection;
  if (FAILED(target_->CreateGradientStopCollection(stops, 2, collection.GetAddressOf()))) {
    fill_rect(r, top);
    return;
  }
  Microsoft::WRL::ComPtr<ID2D1LinearGradientBrush> brush;
  const D2D1_LINEAR_GRADIENT_BRUSH_PROPERTIES props =
      D2D1::LinearGradientBrushProperties(D2D1::Point2F(r.x, r.y),
                                          D2D1::Point2F(r.x, r.y + r.h));
  if (SUCCEEDED(target_->CreateLinearGradientBrush(props, collection.Get(),
                                                   brush.GetAddressOf()))) {
    target_->FillRectangle(r.d2d(), brush.Get());
  }
}

void Renderer::fill_circle(float cx, float cy, float radius, Color c) {
  if (!target_ || radius <= 0) return;
  target_->FillEllipse(D2D1::Ellipse(D2D1::Point2F(cx, cy), radius, radius),
                       brush_for(c).Get());
}

void Renderer::line(float x0, float y0, float x1, float y1, Color c, float width) {
  if (!target_) return;
  target_->DrawLine(D2D1::Point2F(x0, y0), D2D1::Point2F(x1, y1), brush_for(c).Get(),
                    width);
}

IDWriteTextFormat* Renderer::format_for(float size, TextAlign align, TextWeight weight) {
  const uint64_t key = format_key(size, align, weight);
  auto it = formats_.find(key);
  if (it != formats_.end()) return it->second.Get();
  if (!dwrite_) return nullptr;

  // Segoe UI Variable is what WinUI uses; fall back to Segoe UI on older builds.
  const wchar_t* family = L"Segoe UI Variable Text";
  auto create = [&](const wchar_t* name, IDWriteTextFormat** out) {
    return dwrite_->CreateTextFormat(
        name, nullptr,
        weight == TextWeight::Regular ? DWRITE_FONT_WEIGHT_NORMAL
                                      : (weight == TextWeight::SemiBold
                                             ? DWRITE_FONT_WEIGHT_SEMI_BOLD
                                             : DWRITE_FONT_WEIGHT_BOLD),
        DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_STRETCH_NORMAL, size, L"en-us", out);
  };
  Microsoft::WRL::ComPtr<IDWriteTextFormat> format;
  if (FAILED(create(family, format.GetAddressOf()))) {
    if (FAILED(create(L"Segoe UI", format.GetAddressOf()))) return nullptr;
  }
  switch (align) {
    case TextAlign::Center:
      format->SetTextAlignment(DWRITE_TEXT_ALIGNMENT_CENTER);
      break;
    case TextAlign::Right:
      format->SetTextAlignment(DWRITE_TEXT_ALIGNMENT_TRAILING);
      break;
    default:
      format->SetTextAlignment(DWRITE_TEXT_ALIGNMENT_LEADING);
      break;
  }
  format->SetParagraphAlignment(DWRITE_PARAGRAPH_ALIGNMENT_CENTER);
  format->SetWordWrapping(DWRITE_WORD_WRAPPING_NO_WRAP);
  formats_[key] = format;
  return formats_[key].Get();
}

void Renderer::text(const std::wstring& s, const RectF& r, Color c, float size,
                    TextAlign align, TextWeight weight, bool ellipsis) {
  if (!target_ || s.empty()) return;
  IDWriteTextFormat* format = format_for(size, align, weight);
  if (!format) return;
  // leave room for the glyphs to sit inside the box rather than clip
  const RectF box{r.x, r.y, std::max(r.w, 1.0f), std::max(r.h, 1.0f)};
  D2D1_DRAW_TEXT_OPTIONS opts = D2D1_DRAW_TEXT_OPTIONS_CLIP;
  if (!ellipsis) opts = D2D1_DRAW_TEXT_OPTIONS_NONE;
  target_->DrawTextW(s.c_str(), static_cast<UINT32>(s.size()), format, box.d2d(),
                     brush_for(c).Get(), opts, DWRITE_MEASURING_MODE_NATURAL);
}

float Renderer::measure(const std::wstring& s, float size, TextWeight weight) {
  if (!dwrite_ || s.empty()) return 0.0f;
  Microsoft::WRL::ComPtr<IDWriteTextLayout> layout;
  IDWriteTextFormat* format = format_for(size, TextAlign::Left, weight);
  if (!format) return 0.0f;
  if (FAILED(dwrite_->CreateTextLayout(s.c_str(), static_cast<UINT32>(s.size()), format,
                                       4096.0f, 100.0f, layout.GetAddressOf()))) {
    return 0.0f;
  }
  DWRITE_TEXT_METRICS metrics{};
  layout->GetMetrics(&metrics);
  return metrics.width;
}

float Renderer::line_height(float size) const { return std::ceil(size * 1.45f); }

void Renderer::clip_push(const RectF& r) {
  if (!target_) return;
  clip_stack_.push_back(r.d2d());
  target_->PushAxisAlignedClip(r.d2d(), D2D1_ANTIALIAS_MODE_PER_PRIMITIVE);
}

void Renderer::clip_pop() {
  if (!target_ || clip_stack_.empty()) return;
  target_->PopAxisAlignedClip();
  clip_stack_.pop_back();
}

void Renderer::focus_ring(const RectF& r, float radius, Color c) {
  // Fluent draws the ring as two strokes: a dark inner and a light outer line
  stroke_rounded(RectF{r.x - 1, r.y - 1, r.w + 2, r.h + 2}, radius + 1, c, 2.0f);
}

}  // namespace sp::ui
