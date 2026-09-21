// The panel: the application's own window, laid out as Fluent cards.
//
// Sections mirror the Python app so nothing is lost in translation: sources,
// transport, the three timelines, the typed offset, volume, windows and PiP, and the
// live status readout.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include "core/config.h"
#include "core/mpv.h"
#include "core/sync.h"
#include "ui/controls.h"
#include "ui/renderer.h"

namespace sp::app {

class Panel {
 public:
  bool init(HWND hwnd);
  void shutdown();

  // called from the window procedure
  void on_input(const ui::InputState& in) { in_ = in; }
  void on_resize(float w, float h);
  void on_dpi(float dpi);
  void draw();
  void tick();  // the sync loop, roughly every 33 ms

  bool quit_requested() const { return quit_; }

  // window geometry to persist on close
  void remember_window(int x, int y, int w, int h);

  // True while a text field has focus, so the window's shortcuts stay out of the way
  // of typing.
  bool editing_text() const;

  // ---- actions (public: the window procedure drives some of them) ----------
  void start_sources();
  void stop_players();
  void toggle_play();
  void seek_side(Side side, double seconds, bool exact);
  void seek_master(double seconds);
  void nudge_jump(int direction);

 private:
  void apply_offset_from_field();
  void apply_speed_from_field();
  void apply_volume();
  void arrange_windows();
  void toggle_floating_pip();
  void browse_for(Side side);
  void sync_tick();

  void set_message(const std::wstring& text);
  double jump_seconds() const;
  std::optional<double> parse_timecode(const std::wstring& text) const;
  std::wstring offset_label() const;

  // ---- drawing -------------------------------------------------------------
  void layout(float w, float h);
  void draw_title_bar();
  void draw_sources();
  void draw_transport();
  void draw_timelines();
  void draw_volume();
  void draw_windows_row();
  void draw_status();

  HWND hwnd_ = nullptr;
  ui::Renderer renderer_;
  ui::Controls ui_;
  ui::InputState in_;
  ui::RectF title_bar_, btn_min_, btn_close_;
  ui::RectF card_sources_, card_transport_, card_timelines_, card_volume_,
      card_windows_, status_strip_;
  ui::RectF src_a_field_, src_a_browse_, src_b_field_, src_b_browse_;
  ui::RectF btn_start_, btn_play_, btn_back_, btn_fwd_, jump_field_, speed_field_,
      btn_lock_, btn_arrange_;
  ui::RectF bar_movie_, bar_reaction_, bar_master_, lbl_movie_, lbl_reaction_,
      lbl_master_, goto_field_, offset_field_, lbl_offset_;
  ui::RectF sld_vol_a_, sld_vol_b_, sld_vol_m_;
  ui::RectF lbl_vol_a_, lbl_vol_b_, lbl_vol_m_;
  ui::RectF btn_pip_;

  Config cfg_;
  MpvProcess movie_;
  MpvProcess reaction_;
  std::optional<void*> hwnd_movie_;
  std::optional<void*> hwnd_reaction_;
  bool started_ = false;
  bool playing_ = false;
  bool locked_ = false;
  bool floating_pip_ = false;
  bool quit_ = false;

  double sync_off_ = 0.0;
  double applied_rate_ = 1.0;
  double trim_since_ = 0.0;
  double vol_a_ = 100.0, vol_b_ = 100.0, vol_m_ = 100.0;
  double last_scrub_pos_ = 0.0;
  Side scrub_side_ = Side::Movie;
  bool scrubbing_ = false;

  std::wstring src_a_text_, src_b_text_;
  std::wstring jump_text_, speed_text_, offset_text_, goto_text_;
  std::wstring status_text_, message_text_;
  double message_until_ = 0.0;

  float width_ = 760.0f, height_ = 820.0f;
  float dpi_ = 96.0f;
  float scale_ = 1.0f;      // design units -> pixels
  float design_h_ = 948.0f; // the height the layout naturally wants
};

}  // namespace sp::app
