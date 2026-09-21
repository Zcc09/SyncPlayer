#include "app/panel.h"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>
#include <ctime>

#include <windows.h>
#include <commdlg.h>
#include <shlobj.h>

#include "core/paths.h"

namespace sp::app {
namespace {

std::wstring widen(const std::string& s) {
  if (s.empty()) return {};
  const int need = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                       nullptr, 0);
  std::wstring out(static_cast<size_t>(need), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(), need);
  return out;
}

std::string narrow(const std::wstring& s) {
  if (s.empty()) return {};
  const int need = WideCharToMultiByte(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                       nullptr, 0, nullptr, nullptr);
  std::string out(static_cast<size_t>(need), '\0');
  WideCharToMultiByte(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(), need,
                      nullptr, nullptr);
  return out;
}

std::wstring fmt(const wchar_t* pattern, ...) {
  wchar_t buf[512];
  va_list args;
  va_start(args, pattern);
  _vsnwprintf_s(buf, _TRUNCATE, pattern, args);
  va_end(args);
  return buf;
}

double now_seconds() {
  return static_cast<double>(GetTickCount64()) / 1000.0;
}

// control ids: stable numbers, one per interactive control
enum Id {
  ID_SRC_A = 100, ID_SRC_A_BROWSE, ID_SRC_B, ID_SRC_B_BROWSE,
  ID_START, ID_PLAY, ID_BACK, ID_FWD, ID_JUMP, ID_SPEED, ID_LOCK, ID_ARRANGE,
  ID_BAR_MOVIE, ID_BAR_REACTION, ID_BAR_MASTER, ID_GOTO, ID_OFFSET,
  ID_VOL_A, ID_VOL_B, ID_VOL_M, ID_PIP,
  ID_MIN, ID_CLOSE,
};

}  // namespace

// ---------------------------------------------------------------------------
bool Panel::init(HWND hwnd) {
  hwnd_ = hwnd;
  cfg_ = Config::load();
  renderer_.set_theme(ui::Theme::for_system(ui::system_prefers_dark()));
  if (!renderer_.init(hwnd)) {
    MessageBoxW(hwnd, L"Direct2D could not be initialised.", L"SyncPlayer",
                MB_OK | MB_ICONERROR);
    return false;
  }
  ensure_dirs();
  src_a_text_ = widen(cfg_.movie);
  src_b_text_ = widen(cfg_.reaction);
  jump_text_ = fmt(L"%.1f", cfg_.jump_sec);
  speed_text_ = fmt(L"%.2f", cfg_.speed);
  offset_text_ = L"0.00";
  vol_a_ = cfg_.vol_a;
  vol_b_ = cfg_.vol_b;
  vol_m_ = cfg_.vol_m;
  message_text_ = L"Choose your movie and the reaction, then press Start.";
  message_until_ = now_seconds() + 3600.0;
  return true;
}

void Panel::shutdown() {
  stop_players();
  cfg_.movie = narrow(src_a_text_);
  cfg_.reaction = narrow(src_b_text_);
  cfg_.vol_a = vol_a_;
  cfg_.vol_b = vol_b_;
  cfg_.vol_m = vol_m_;
  cfg_.jump_sec = jump_seconds();
  cfg_.save();
  renderer_.shutdown();
}

void Panel::remember_window(int x, int y, int w, int h) {
  cfg_.window = WindowRect{x, y, w, h};
}

void Panel::on_resize(float w, float h) {
  width_ = w;
  height_ = h;
  renderer_.resize(static_cast<UINT>(w), static_cast<UINT>(h), dpi_);
  // Scale to fit: at full size the panel is drawn 1:1, and a shorter window scales the
  // whole thing down instead of hiding the last cards behind the status strip.
  scale_ = std::min(1.0f, h / design_h_);
  renderer_.set_scale(scale_);
  layout(w / scale_, h / scale_);
}

void Panel::on_dpi(float dpi) {
  dpi_ = dpi;
  renderer_.resize(static_cast<UINT>(width_), static_cast<UINT>(height_), dpi);
  scale_ = std::min(1.0f, height_ / design_h_);
  renderer_.set_scale(scale_);
  layout(width_ / scale_, height_ / scale_);
}

bool Panel::editing_text() const {
  const unsigned f = ui_.focused();
  return f == ID_SRC_A || f == ID_SRC_B || f == ID_JUMP || f == ID_SPEED ||
         f == ID_GOTO || f == ID_OFFSET;
}

void Panel::set_message(const std::wstring& text) {
  message_text_ = text;
  message_until_ = now_seconds() + 8.0;
}

double Panel::jump_seconds() const {
  try {
    const double v = std::stod(jump_text_);
    return std::clamp(v, 0.5, 120.0);
  } catch (...) {
    return 5.0;
  }
}

std::optional<double> Panel::parse_timecode(const std::wstring& text) const {
  std::wstring cleaned;
  for (wchar_t c : text) {
    if (c == L',') cleaned.push_back(L'.');
    else if (c != L' ') cleaned.push_back(c);
  }
  if (cleaned.empty()) return std::nullopt;
  double sign = 1.0;
  if (cleaned.front() == L'-') {
    sign = -1.0;
    cleaned.erase(cleaned.begin());
  } else if (cleaned.front() == L'+') {
    cleaned.erase(cleaned.begin());
  }
  if (cleaned.empty()) return std::nullopt;  // a lone sign is not a value
  try {
    // hh:mm:ss, mm:ss or plain seconds
    std::vector<std::wstring> parts;
    std::wstring part;
    for (wchar_t c : cleaned) {
      if (c == L':') {
        parts.push_back(part);
        part.clear();
      } else {
        part.push_back(c);
      }
    }
    parts.push_back(part);
    double total = 0.0;
    for (const auto& p : parts) total = total * 60.0 + std::stod(p);
    return sign * total;
  } catch (...) {
    return std::nullopt;
  }
}

std::wstring Panel::offset_label() const {
  if (std::fabs(sync_off_) < 0.005) return L"Off 0.00s";
  return fmt(L"Off %+.2fs", sync_off_);
}

// ---------------------------------------------------------------------------
// layout: one column of Fluent cards, top to bottom
// ---------------------------------------------------------------------------
void Panel::layout(float w, float h) {
  const float pad = ui::kPad;
  const float gap = ui::kGap;
  const float x = pad;
  const float cw = w - 2 * pad;

  title_bar_ = {0, 0, w, 40.0f};
  btn_close_ = {w - 46.0f, 0, 46.0f, 40.0f};
  btn_min_ = {w - 92.0f, 0, 46.0f, 40.0f};

  float y = 40.0f + gap;

  // ---- sources
  const float src_h = 16 + 18 + 8 + 36 + 6 + 36 + 6 + 18 + 12;
  card_sources_ = {x, y, cw, src_h};
  {
    float ry = card_sources_.y + 16 + 18 + 8;
    const float label_w = 84.0f;
    const float browse_w = 92.0f;
    src_a_field_ = {x + pad + label_w, ry, cw - 2 * pad - label_w - browse_w - gap, 36};
    src_a_browse_ = {src_a_field_.x + src_a_field_.w + gap, ry, browse_w, 36};
    ry += 36 + 6;
    src_b_field_ = {x + pad + label_w, ry, src_a_field_.w, 36};
    src_b_browse_ = {src_b_field_.x + src_b_field_.w + gap, ry, browse_w, 36};
  }
  y += src_h + gap;

  // ---- transport
  const float tr_h = 16 + 18 + 8 + 36 + 8 + 36 + 12;
  card_transport_ = {x, y, cw, tr_h};
  {
    float ry = card_transport_.y + 16 + 18 + 8;
    const float btn_w = (cw - 2 * pad - 3 * gap) / 4.0f;
    btn_start_ = {x + pad, ry, btn_w, 36};
    btn_play_ = {btn_start_.x + btn_w + gap, ry, btn_w, 36};
    btn_back_ = {btn_play_.x + btn_w + gap, ry, btn_w, 36};
    btn_fwd_ = {btn_back_.x + btn_w + gap, ry, btn_w, 36};
    ry += 36 + 8;
    const float field_w = (cw - 2 * pad - 3 * gap) / 4.0f;
    jump_field_ = {x + pad, ry, field_w, 36};
    speed_field_ = {jump_field_.x + field_w + gap, ry, field_w, 36};
    btn_lock_ = {speed_field_.x + field_w + gap, ry, field_w * 2 + gap, 36};
    ry += 36;
    btn_arrange_ = {x + pad, ry, field_w * 2 + gap, 0};  // placed in the windows card
  }
  y += tr_h + gap;

  // ---- timelines
  const float tl_h = 16 + 18 + 8 + (16 + 24) * 3 + 10 + 36 + 8 + 36 + 12;
  card_timelines_ = {x, y, cw, tl_h};
  {
    float ry = card_timelines_.y + 16 + 18 + 8;
    const float lbl_w = 66.0f;
    const float time_w = 74.0f;
    auto row = [&](ui::RectF& bar, ui::RectF& lbl) {
      lbl = {x + pad, ry + 3.0f, lbl_w, 18.0f};
      bar = {x + pad + lbl_w, ry + 6.0f, cw - 2 * pad - lbl_w - time_w - gap, 20.0f};
      ry += 16 + 24;
    };
    row(bar_movie_, lbl_movie_);
    row(bar_reaction_, lbl_reaction_);
    row(bar_master_, lbl_master_);
    ry += 10;
    const float half = (cw - 2 * pad - gap) / 2.0f;
    goto_field_ = {x + pad, ry, half, 36};
    offset_field_ = {goto_field_.x + half + gap, ry, half, 36};
    ry += 36 + 8;
    lbl_offset_ = {x + pad, ry, cw - 2 * pad, 18.0f};
  }
  y += tl_h + gap;

  // ---- volume
  const float vol_h = 16 + 18 + 8 + 3 * (20 + 18) + 12;
  card_volume_ = {x, y, cw, vol_h};
  {
    float ry = card_volume_.y + 16 + 18 + 8;
    const float lbl_w = 92.0f;
    auto row = [&](ui::RectF& slider, ui::RectF& lbl) {
      lbl = {x + pad, ry - 2.0f, lbl_w, 20.0f};
      slider = {x + pad + lbl_w, ry, cw - 2 * pad - lbl_w - gap, 20.0f};
      ry += 20 + 18;
    };
    row(sld_vol_a_, lbl_vol_a_);
    row(sld_vol_b_, lbl_vol_b_);
    row(sld_vol_m_, lbl_vol_m_);
  }
  y += vol_h + gap;

  // ---- windows and PiP
  const float win_h = 16 + 18 + 8 + 36 + 12;
  card_windows_ = {x, y, cw, win_h};
  {
    const float ry = card_windows_.y + 16 + 18 + 8;
    const float half = (cw - 2 * pad - gap) / 2.0f;
    btn_arrange_ = {x + pad, ry, half, 36};
    btn_pip_ = {btn_arrange_.x + half + gap, ry, half, 36};
  }
  y += win_h + gap;

  // ---- status strip, pinned to the bottom of the window
  status_strip_ = {x, std::max(y, h - 56.0f), cw, 44.0f};
}

// ---------------------------------------------------------------------------
// drawing
// ---------------------------------------------------------------------------
void Panel::draw() {
  // the input arrived in pixels; the layout works in design units
  ui::InputState mapped = in_;
  if (scale_ > 0.0f) {
    mapped.mouse_x = in_.mouse_x / scale_;
    mapped.mouse_y = in_.mouse_y / scale_;
  }
  ui_.begin_frame(renderer_, mapped);

  const ui::Theme& t = renderer_.theme();
  renderer_.begin();

  // A Fluent surface: a vertical gradient over the window colour. Real Mica needs a
  // composition surface, so this is the closest equivalent that still composites with
  // a bundled mpv's child windows.
  const float dw = width_ / scale_, dh = height_ / scale_;
  renderer_.fill_gradient_v({0, 0, dw, dh},
                            t.dark ? ui::Color{0.11f, 0.11f, 0.12f, 1.0f}
                                   : ui::Color{0.97f, 0.97f, 0.98f, 1.0f},
                            t.window_bg);

  draw_title_bar();
  draw_sources();
  draw_transport();
  draw_timelines();
  draw_volume();
  draw_windows_row();
  draw_status();

  renderer_.end();
  ui_.end_frame();
}

void Panel::draw_title_bar() {
  const ui::Theme& t = renderer_.theme();
  renderer_.fill_rect(title_bar_, t.dark ? ui::Color{0, 0, 0, 0.18f}
                                         : ui::Color{0, 0, 0, 0.03f});
  renderer_.text(L"SyncPlayer", {ui::kPad, 0, 200, 40.0f}, t.text, 14.0f,
                 ui::TextAlign::Left, ui::TextWeight::SemiBold);
  const std::wstring chip = L"v1.6.13";
  const float chip_w = renderer_.measure(chip, 12.0f) + 20.0f;
  const ui::RectF chip_rect{ui::kPad + 96.0f, 12.0f, chip_w, 16.0f};
  renderer_.fill_rounded(chip_rect, 8.0f, t.control);
  renderer_.text(chip, chip_rect, t.text_secondary, 12.0f, ui::TextAlign::Center);

  if (ui_.icon_button(ID_MIN, btn_min_, L"\u2500", t.text)) {
    ShowWindow(hwnd_, SW_MINIMIZE);
  }
  if (ui_.icon_button(ID_CLOSE, btn_close_, L"\u2715", t.text)) {
    PostMessageW(hwnd_, WM_CLOSE, 0, 0);
  }
}

void Panel::draw_sources() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_sources_, L"SOURCES");
  const float label_w = 84.0f;
  renderer_.text(L"Movie", {card_sources_.x + ui::kPad, src_a_field_.y, label_w, 36.0f},
                 t.text_secondary, 14.0f);
  renderer_.text(L"Reaction", {card_sources_.x + ui::kPad, src_b_field_.y, label_w, 36.0f},
                 t.text_secondary, 14.0f);

  if (ui_.text_field(ID_SRC_A, src_a_field_, src_a_text_, L"movie.mp4 or a link") !=
      ui::FieldResult::None) {
    cfg_.movie = narrow(src_a_text_);
  }
  if (ui_.button(ID_SRC_A_BROWSE, src_a_browse_, L"Browse\u2026")) browse_for(Side::Movie);
  if (ui_.text_field(ID_SRC_B, src_b_field_, src_b_text_, L"reaction.mp4 or a link") !=
      ui::FieldResult::None) {
    cfg_.reaction = narrow(src_b_text_);
  }
  if (ui_.button(ID_SRC_B_BROWSE, src_b_browse_, L"Browse\u2026")) browse_for(Side::Reaction);

  const float hint_y = src_b_field_.y + 36.0f + 6.0f;
  renderer_.text(L"Paste a link, or drop a file onto the window. Start loads both paused.",
                 {card_sources_.x + ui::kPad, hint_y, card_sources_.w - 2 * ui::kPad, 18.0f},
                 t.text_secondary, 12.0f);
}

void Panel::draw_transport() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_transport_, L"TRANSPORT");

  if (ui_.button(ID_START, btn_start_, started_ ? L"Restart" : L"Start",
                 ui::ButtonStyle::Accent)) {
    start_sources();
  }
  const std::wstring play_label = playing_ ? L"Pause" : L"Play";
  if (ui_.button(ID_PLAY, btn_play_, play_label,
                 playing_ ? ui::ButtonStyle::Standard : ui::ButtonStyle::Accent,
                 started_)) {
    toggle_play();
  }
  if (ui_.button(ID_BACK, btn_back_, fmt(L"\u2190 %gs", jump_seconds()),
                 ui::ButtonStyle::Standard, started_)) {
    nudge_jump(-1);
  }
  if (ui_.button(ID_FWD, btn_fwd_, fmt(L"%gs \u2192", jump_seconds()),
                 ui::ButtonStyle::Standard, started_)) {
    nudge_jump(+1);
  }

  if (ui_.text_field(ID_JUMP, jump_field_, jump_text_, L"5", true) ==
      ui::FieldResult::Submitted) {
    cfg_.jump_sec = jump_seconds();
  }
  if (ui_.text_field(ID_SPEED, speed_field_, speed_text_, L"1.00", true) ==
      ui::FieldResult::Submitted) {
    apply_speed_from_field();
  }
  if (ui_.toggle(ID_LOCK, btn_lock_, locked_, L"Lock sync")) {
    set_message(locked_ ? L"Sync locked: the Master bar now drives both videos."
                        : L"Sync unlocked: align with the per-video bars.");
  }
  renderer_.text(L"Jump (s)", {jump_field_.x, jump_field_.y - 18.0f, jump_field_.w, 16.0f},
                 t.text_secondary, 11.0f);
  renderer_.text(L"Speed", {speed_field_.x, speed_field_.y - 18.0f, speed_field_.w, 16.0f},
                 t.text_secondary, 11.0f);
}

void Panel::draw_timelines() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_timelines_, L"TIMELINES");

  const std::optional<double> pos_a = movie_.position();
  const std::optional<double> dur_a = movie_.duration();
  const std::optional<double> pos_b = reaction_.position();
  const std::optional<double> dur_b = reaction_.duration();
  const bool have = started_;

  auto times = [&](const std::optional<double>& pos, const std::optional<double>& dur) {
    return fmt(L"%s / %s", widen(format_hms(pos.value_or(0))).c_str(),
               widen(format_hms(dur.value_or(0))).c_str());
  };
  auto draw_row = [&](unsigned id, const ui::RectF& bar, const ui::RectF& lbl,
                      const wchar_t* name, const std::optional<double>& pos,
                      const std::optional<double>& dur, Side side) {
    renderer_.text(name, lbl, have ? t.text : t.text_disabled, 13.0f, ui::TextAlign::Left,
                   ui::TextWeight::SemiBold);
    const ui::SeekDrag drag =
        ui_.seek_bar(id, bar, pos.value_or(0), dur.value_or(0), have && dur.value_or(0) > 0);
    renderer_.text(times(pos, dur),
                   {bar.x + bar.w + 8.0f, bar.y - 4.0f, 74.0f, 24.0f}, t.text_secondary,
                   12.0f, ui::TextAlign::Right);
    scrubbing_ = drag.result == ui::SeekResult::Dragging;
    if (drag.result == ui::SeekResult::Clicked) {
      scrub_side_ = side;
      last_scrub_pos_ = drag.clicked_seconds;
      if (side == Side::Movie) seek_side(Side::Movie, drag.clicked_seconds, true);
      else seek_side(Side::Reaction, drag.clicked_seconds, true);
    } else if (drag.result == ui::SeekResult::Dragging ||
               drag.result == ui::SeekResult::Released) {
      // precise scrubbing: sideways travel at a fixed gain, finer once lifted
      const double target = scrub_target(last_scrub_pos_, drag.dx_px, drag.lift_px, dur);
      scrub_side_ = side;
      if (side == Side::Movie) seek_side(Side::Movie, target, drag.result == ui::SeekResult::Released);
      else seek_side(Side::Reaction, target, drag.result == ui::SeekResult::Released);
    }
  };

  draw_row(ID_BAR_MOVIE, bar_movie_, lbl_movie_, L"Movie", pos_a, dur_a, Side::Movie);
  draw_row(ID_BAR_REACTION, bar_reaction_, lbl_reaction_, L"Reaction", pos_b, dur_b,
           Side::Reaction);

  // the master bar drives both, keeping the offset
  renderer_.text(L"Master", lbl_master_, have ? t.text : t.text_disabled, 13.0f,
                 ui::TextAlign::Left, ui::TextWeight::SemiBold);
  const ui::SeekDrag master = ui_.seek_bar(ID_BAR_MASTER, bar_master_, pos_a.value_or(0),
                                           dur_a.value_or(0), have && locked_);
  renderer_.text(times(pos_a, dur_a),
                 {bar_master_.x + bar_master_.w + 8.0f, bar_master_.y - 4.0f, 74.0f, 24.0f},
                 t.text_secondary, 12.0f, ui::TextAlign::Right);
  scrubbing_ = master.result == ui::SeekResult::Dragging;
  if (master.result == ui::SeekResult::Clicked) {
    last_scrub_pos_ = master.clicked_seconds;
    seek_master(master.clicked_seconds);
  } else if (master.result == ui::SeekResult::Dragging ||
             master.result == ui::SeekResult::Released) {
    const double target =
        scrub_target(last_scrub_pos_, master.dx_px, master.lift_px, dur_a);
    seek_master(target);
  }

  if (ui_.text_field(ID_GOTO, goto_field_, goto_text_, L"Go to  90 / 1:30:00") ==
      ui::FieldResult::Submitted) {
    if (auto secs = parse_timecode(goto_text_)) {
      seek_master(*secs);
      set_message(fmt(L"Moved both videos to %s.", widen(format_hms(*secs)).c_str()));
    } else {
      set_message(L"That is not a time. Try 90, 1:30 or 1:30:00.");
    }
    goto_text_.clear();
  }
  if (ui_.text_field(ID_OFFSET, offset_field_, offset_text_,
                     L"Offset  12.5 / 1:05 / -3.25", true) ==
      ui::FieldResult::Submitted) {
    apply_offset_from_field();
  }
  renderer_.text(offset_label(), lbl_offset_, t.text_secondary, 12.0f);
}

void Panel::draw_volume() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_volume_, L"VOLUME");
  renderer_.text(fmt(L"Movie  %.0f%%", vol_a_), lbl_vol_a_, t.text_secondary, 13.0f);
  renderer_.text(fmt(L"Reaction  %.0f%%", vol_b_), lbl_vol_b_, t.text_secondary, 13.0f);
  renderer_.text(fmt(L"Master  %.0f%%", vol_m_), lbl_vol_m_, t.text_secondary, 13.0f);
  bool changed = false;
  changed |= ui_.slider(ID_VOL_A, sld_vol_a_, vol_a_, 0, kVolumeMax);
  changed |= ui_.slider(ID_VOL_B, sld_vol_b_, vol_b_, 0, kVolumeMax);
  changed |= ui_.slider(ID_VOL_M, sld_vol_m_, vol_m_, 0, kVolumeMax);
  if (changed) apply_volume();
}

void Panel::draw_windows_row() {
  ui_.card(card_windows_, L"WINDOWS AND PIP");
  if (ui_.button(ID_ARRANGE, btn_arrange_, L"Arrange side by side",
                 ui::ButtonStyle::Standard, started_)) {
    arrange_windows();
  }
  if (ui_.button(ID_PIP, btn_pip_, floating_pip_ ? L"Exit floating PiP" : L"Floating PiP",
                 ui::ButtonStyle::Standard, started_)) {
    toggle_floating_pip();
  }
}

void Panel::draw_status() {
  const ui::Theme& t = renderer_.theme();
  renderer_.fill_rounded(status_strip_, ui::kRadiusCard, t.layer);
  renderer_.stroke_rounded(status_strip_, ui::kRadiusCard, t.border, 1.0f);

  const std::wstring readout =
      cfg_.show_readout
          ? (status_text_.empty() ? L"Idle. Press Start to load both videos."
                                  : status_text_)
          : L"Status readout hidden in Settings.";
  renderer_.text(readout,
                 {status_strip_.x + 12.0f, status_strip_.y, status_strip_.w - 24.0f, 22.0f},
                 t.text, 13.0f);
  const bool show_message = now_seconds() < message_until_;
  if (show_message) {
    renderer_.text(message_text_,
                   {status_strip_.x + 12.0f, status_strip_.y + 20.0f,
                    status_strip_.w - 24.0f, 20.0f},
                   t.text_secondary, 12.0f);
  }
}

}  // namespace sp::app
