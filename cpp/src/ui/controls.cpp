#include "ui/controls.h"

#include <algorithm>
#include <cmath>

namespace sp::ui {
namespace {

bool is_digit(wchar_t c) { return c >= L'0' && c <= L'9'; }
bool is_numeric_ok(const std::wstring& s) {
  bool dot = false;
  for (wchar_t c : s) {
    if (is_digit(c)) continue;
    if ((c == L'.' || c == L',') && !dot) {
      dot = true;
      continue;
    }
    if (c == L'-' || c == L'+') continue;
    return false;
  }
  return true;
}

double to_seconds(const std::wstring& s) {
  std::wstring cleaned;
  for (wchar_t c : s) cleaned.push_back(c == L',' ? L'.' : c);
  try {
    return std::stod(cleaned);
  } catch (...) {
    return 0.0;
  }
}

}  // namespace

bool InputState::key_pressed(unsigned vk) const {
  return std::find(keys.begin(), keys.end(), vk) != keys.end();
}

bool InputState::take_key(unsigned vk) {
  auto it = std::find(keys.begin(), keys.end(), vk);
  if (it == keys.end()) return false;
  keys.erase(it);
  return true;
}

void Controls::begin_frame(Renderer& r, const InputState& in) {
  r_ = &r;
  in_ = in;
  text_cursor_ = false;
  next_id_ = 1;
}

void Controls::end_frame() {
  if (!in_.mouse_down) active_ = 0;
}

bool Controls::hit(const RectF& r) const {
  return r.contains(in_.mouse_x, in_.mouse_y);
}

bool Controls::pressed_in(const RectF& r) const {
  return in_.mouse_pressed && hit(r);
}

// ---------------------------------------------------------------------------
// primitives
// ---------------------------------------------------------------------------
void Controls::label(const RectF& r, const std::wstring& s, Color c, float size,
                     TextAlign align, TextWeight weight) {
  r_->text(s, r, c, size, align, weight);
}

void Controls::card(const RectF& r, const std::wstring& title) {
  const Theme& t = r_->theme();
  r_->fill_rounded(r, kRadiusCard, t.card);
  r_->stroke_rounded(r, kRadiusCard, t.border, 1.0f);
  if (!title.empty()) {
    const RectF head{r.x + kPad, r.y + 10.0f, r.w - 2 * kPad, 18.0f};
    r_->text(title, head, t.text_secondary, 12.0f, TextAlign::Left,
             TextWeight::SemiBold);
  }
}

void Controls::divider(float x0, float x1, float y, Color c) {
  r_->line(x0, y, x1, y, c, 1.0f);
}

// ---------------------------------------------------------------------------
// button
// ---------------------------------------------------------------------------
bool Controls::button(unsigned id, const RectF& r, const std::wstring& text,
                      ButtonStyle style, bool enabled) {
  const Theme& t = r_->theme();
  const bool hovered = enabled && hit(r);
  const bool held = enabled && active_ == id && in_.mouse_down;
  bool clicked = false;
  if (enabled) {
    if (hovered) hot_ = id;
    if (pressed_in(r)) active_ = id;
    if (active_ == id && in_.mouse_released && hovered) clicked = true;
  }

  Color bg, fg;
  switch (style) {
    case ButtonStyle::Accent:
      bg = !enabled ? t.control_disabled
                    : (held ? t.accent_pressed : (hovered ? t.accent_hover : t.accent));
      fg = t.on_accent;
      break;
    case ButtonStyle::Subtle:
      bg = !enabled ? t.control_disabled
                    : (held ? t.control_pressed : (hovered ? t.control_hover : Color{0, 0, 0, 0}));
      fg = enabled ? t.text : t.text_disabled;
      break;
    case ButtonStyle::Danger:
      bg = !enabled ? t.control_disabled : (hovered ? t.danger : t.control);
      fg = hovered ? Color{1, 1, 1, 1} : t.text;
      break;
    default:
      bg = !enabled ? t.control_disabled
                    : (held ? t.control_pressed : (hovered ? t.control_hover : t.control));
      fg = enabled ? t.text : t.text_disabled;
      break;
  }
  r_->fill_rounded(r, kRadiusControl, bg);
  if (style == ButtonStyle::Standard || style == ButtonStyle::Danger) {
    r_->stroke_rounded(r, kRadiusControl, t.border_strong, 1.0f);
  }
  r_->text(text, r, fg, 14.0f, TextAlign::Center, TextWeight::Regular);
  if (focus_ == id && enabled) r_->focus_ring(r, kRadiusControl, t.text_secondary);
  return clicked;
}

bool Controls::icon_button(unsigned id, const RectF& r, const std::wstring& glyph,
                           Color tint) {
  const Theme& t = r_->theme();
  const bool hovered = hit(r);
  const bool held = active_ == id && in_.mouse_down;
  bool clicked = false;
  if (hovered) hot_ = id;
  if (pressed_in(r)) active_ = id;
  if (active_ == id && in_.mouse_released && hovered) clicked = true;
  if (held || hovered) {
    r_->fill_rounded(r, kRadiusControl, held ? t.control_pressed : t.control_hover);
  }
  r_->text(glyph, r, tint, 12.0f, TextAlign::Center);
  return clicked;
}

// ---------------------------------------------------------------------------
// toggle (a Fluent switch with a label)
// ---------------------------------------------------------------------------
bool Controls::toggle(unsigned id, const RectF& r, bool& value, const std::wstring& text) {
  const Theme& t = r_->theme();
  const float track_w = 40.0f, track_h = 20.0f;
  const RectF track{r.x + r.w - track_w, r.y + (r.h - track_h) / 2.0f, track_w, track_h};
  const bool hovered = hit(r);
  bool changed = false;
  if (hovered) hot_ = id;
  if (pressed_in(r)) {
    value = !value;
    changed = true;
    active_ = id;
  }
  r_->text(text, RectF{r.x, r.y, r.w - track_w - 8.0f, r.h},
           value ? t.text : t.text_secondary, 14.0f);
  const Color on = value ? t.accent : (hovered ? t.track_hover : t.track);
  r_->fill_rounded(track, track_h / 2.0f, on);
  const float knob_r = track_h / 2.0f - 3.0f;
  const float knob_x = value ? track.x + track_w - track_h / 2.0f : track.x + track_h / 2.0f;
  r_->fill_circle(knob_x, track.y + track_h / 2.0f, knob_r,
                  value ? t.on_accent : t.handle);
  return changed;
}

// ---------------------------------------------------------------------------
// text field
// ---------------------------------------------------------------------------
FieldResult Controls::text_field(unsigned id, const RectF& r, std::wstring& value,
                                 const std::wstring& placeholder, bool numeric) {
  const Theme& t = r_->theme();
  const bool hovered = hit(r);
  const bool focused = focus_ == id;
  FieldResult result = FieldResult::None;

  if (hovered) {
    hot_ = id;
    text_cursor_ = true;
  }
  if (pressed_in(r)) {
    set_focus(id);
    caret_ = static_cast<int>(value.size());
  }

  if (focused) {
    // typing
    std::wstring edited = value;
    bool changed = false;
    for (wchar_t c : in_.typed) {
      if (c == L'\r' || c == L'\n') {
        result = FieldResult::Submitted;
        continue;
      }
      if (c < 32) continue;
      if (numeric && !(is_digit(c) || c == L'.' || c == L',' || c == L'-')) continue;
      edited.push_back(c);
      changed = true;
    }
    if (in_.take_key(VK_BACK) && !edited.empty()) {
      edited.pop_back();
      changed = true;
    }
    if (in_.take_key(VK_RETURN)) result = FieldResult::Submitted;
    if (changed) {
      if (numeric && !is_numeric_ok(edited)) {
        // refuse junk rather than storing it, the way the Python field does
      } else {
        value = edited;
        caret_ = static_cast<int>(value.size());
        if (result != FieldResult::Submitted) result = FieldResult::Changed;
      }
    }
  }

  const Color bg = focused ? (t.dark ? Color{0, 0, 0, 0.25f} : Color{1, 1, 1, 1})
                           : (hovered ? t.control_hover : t.control);
  r_->fill_rounded(r, kRadiusControl, bg);
  r_->stroke_rounded(r, kRadiusControl,
                     focused ? t.accent : t.border_strong, focused ? 1.5f : 1.0f);
  const RectF text_box{r.x + 10.0f, r.y, r.w - 20.0f, r.h};
  if (value.empty() && !focused) {
    r_->text(placeholder, text_box, t.text_disabled, 14.0f);
  } else {
    r_->text(value, text_box, t.text, 14.0f);
    if (focused) {
      // a caret, blinking at Fluent's rate
      const float caret_x =
          text_box.x + std::min(r_->measure(value, 14.0f), text_box.w - 2.0f);
      r_->line(caret_x, r.y + 7.0f, caret_x, r.y + r.h - 7.0f, t.text, 1.5f);
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// slider (volume)
// ---------------------------------------------------------------------------
bool Controls::slider(unsigned id, const RectF& r, double& value, double lo, double hi) {
  const Theme& t = r_->theme();
  const float track_h = 4.0f;
  const RectF track{r.x, r.y + (r.h - track_h) / 2.0f, r.w, track_h};
  const bool hovered = hit(r);
  bool active_now = false;

  if (hovered) hot_ = id;
  if (pressed_in(r)) active_ = id;
  if (active_ == id && in_.mouse_down) {
    active_now = true;
    const float frac = std::clamp((in_.mouse_x - r.x) / std::max(r.w, 1.0f), 0.0f, 1.0f);
    value = lo + frac * (hi - lo);
  }
  if (active_ == id && in_.mouse_released) active_now = false;

  const float frac = static_cast<float>((value - lo) / std::max(hi - lo, 1e-6));
  r_->fill_rounded(track, track_h / 2.0f, t.track);
  r_->fill_rounded(RectF{track.x, track.y, track.w * frac, track.h}, track_h / 2.0f,
                   t.accent);
  const float knob_x = track.x + track.w * frac;
  const float knob_r = (hovered || active_now) ? 10.0f : 8.0f;
  r_->fill_circle(knob_x, track.y + track_h / 2.0f, knob_r, t.accent);
  r_->fill_circle(knob_x, track.y + track_h / 2.0f, knob_r - 4.0f, t.card);
  return active_now;
}

// ---------------------------------------------------------------------------
// seek bar
// ---------------------------------------------------------------------------
SeekDrag Controls::seek_bar(unsigned id, const RectF& r, double position, double duration,
                            bool enabled) {
  const Theme& t = r_->theme();
  SeekDrag out;
  const float track_h = 4.0f;
  const RectF track{r.x, r.y + (r.h - track_h) / 2.0f, r.w, track_h};
  const bool hovered = enabled && hit(r);

  if (hovered) hot_ = id;
  if (enabled && pressed_in(r)) {
    active_ = id;
    dragging_seek_ = true;
    drag_start_x_ = in_.mouse_x;
    drag_pos0_ = position;
    const float frac = std::clamp((in_.mouse_x - r.x) / std::max(r.w, 1.0f), 0.0f, 1.0f);
    out.result = SeekResult::Clicked;
    out.clicked_seconds = frac * std::max(duration, 0.0);
  }
  if (dragging_seek_ && active_ == id) {
    const int dx = static_cast<int>(std::lround(in_.mouse_x - drag_start_x_));
    const int lift = static_cast<int>(std::lround(track.y - in_.mouse_y));
    if (std::abs(dx) > 2) {
      out.result = SeekResult::Dragging;
      out.dx_px = dx;
      out.lift_px = lift;
    }
    if (in_.mouse_released) {
      out.result = SeekResult::Released;
      out.dx_px = dx;
      out.lift_px = lift;
      dragging_seek_ = false;
    }
  }

  const float frac =
      duration > 0 ? static_cast<float>(std::clamp(position / duration, 0.0, 1.0)) : 0.0f;
  const Color base = enabled ? (hovered ? t.track_hover : t.track) : t.control_disabled;
  r_->fill_rounded(track, track_h / 2.0f, base);
  r_->fill_rounded(RectF{track.x, track.y, track.w * frac, track.h}, track_h / 2.0f,
                   t.accent);
  if (enabled) {
    const float knob_x = track.x + track.w * frac;
    const float knob_r = (hovered || dragging_seek_) ? 9.0f : 6.0f;
    r_->fill_circle(knob_x, track.y + track_h / 2.0f, knob_r, t.accent);
  }
  return out;
}

}  // namespace sp::ui
