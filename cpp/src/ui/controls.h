// The Fluent control set, drawn by hand on the renderer.
//
// The model is immediate: every frame the panel draws its controls and gets back what
// the user did. There is no widget tree to keep in sync with the state, which is what
// makes a hand-drawn Fluent surface practical.
//
// Controls take their value by reference and return what happened, so the panel owns
// the application state and the controls stay stateless.
#pragma once

#include <string>
#include <vector>

#include "ui/renderer.h"

namespace sp::ui {

enum class ButtonStyle { Standard, Accent, Subtle, Danger };

enum class FieldResult { None, Changed, Submitted };
enum class SeekResult { None, Clicked, Dragging, Released };

struct SeekDrag {
  SeekResult result = SeekResult::None;
  double clicked_seconds = 0.0;  // for Clicked
  int dx_px = 0;                 // pointer travel since the drag started
  int lift_px = 0;               // how far above the bar the pointer is
};

// Everything the controls need to know about input this frame.
struct InputState {
  float mouse_x = 0, mouse_y = 0;
  bool mouse_down = false;
  bool mouse_pressed = false;   // pressed this frame
  bool mouse_released = false;  // released this frame
  bool shift = false, control = false;
  std::wstring typed;           // WM_CHAR characters, in order
  std::vector<unsigned> keys;   // WM_KEYDOWN virtual keys, in order

  bool key_pressed(unsigned vk) const;
  bool take_key(unsigned vk);
  void clear_transient() {
    mouse_pressed = false;
    mouse_released = false;
    typed.clear();
    keys.clear();
  }
};

class Controls {
 public:
  void begin_frame(Renderer& r, const InputState& in);
  void end_frame();

  // ---- primitives -----------------------------------------------------------
  void label(const RectF& r, const std::wstring& s, Color c, float size = 14.0f,
             TextAlign align = TextAlign::Left, TextWeight weight = TextWeight::Regular);
  void card(const RectF& r, const std::wstring& title = {});
  void divider(float x0, float x1, float y, Color c);

  bool button(unsigned id, const RectF& r, const std::wstring& text,
              ButtonStyle style = ButtonStyle::Standard, bool enabled = true);
  bool icon_button(unsigned id, const RectF& r, const std::wstring& glyph,
                   Color tint);
  bool toggle(unsigned id, const RectF& r, bool& value, const std::wstring& text);

  // A text box. Returns Changed while typing and Submitted on Enter.
  FieldResult text_field(unsigned id, const RectF& r, std::wstring& value,
                         const std::wstring& placeholder, bool numeric = false);

  // A Fluent slider with a round handle; returns true while being dragged.
  bool slider(unsigned id, const RectF& r, double& value, double lo, double hi);

  // The seek bar. Dragging reports the pointer travel so the panel can apply the
  // precise-scrub gains (0.5 s per pixel on the bar, 0.02 s per pixel once lifted).
  SeekDrag seek_bar(unsigned id, const RectF& r, double position, double duration,
                    bool enabled = true);

  // ---- state the panel needs to know about ---------------------------------
  unsigned focused() const { return focus_; }
  void set_focus(unsigned id) { focus_ = id; }
  bool is_hovering(unsigned id) const { return hot_ == id; }
  bool wants_text_cursor() const { return text_cursor_; }

 private:
  bool hit(const RectF& r) const;
  bool pressed_in(const RectF& r) const;
  unsigned next_id_ = 1;

  Renderer* r_ = nullptr;
  InputState in_;
  unsigned hot_ = 0;
  unsigned active_ = 0;
  unsigned focus_ = 0;
  bool text_cursor_ = false;

  // text field editing state, per focused field
  int caret_ = 0;
  double blink_started_ = 0.0;
  double drag_start_x_ = 0.0;
  double drag_pos0_ = 0.0;
  bool dragging_seek_ = false;
};

}  // namespace sp::ui
