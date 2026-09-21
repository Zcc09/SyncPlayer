#include "app/debug.h"
#include "app/panel.h"

#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>

#include <windows.h>
#include <commdlg.h>
#include <dwmapi.h>
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

double now_seconds() { return static_cast<double>(GetTickCount64()) / 1000.0; }

// Fluent metrics, shared by every tab
constexpr float kTitleBarH = 40.0f;
constexpr float kTabStripH = 46.0f;
constexpr float kCardTitleH = 18.0f;
constexpr float kCardTop = 16.0f;
constexpr float kLabelH = 16.0f;
constexpr float kFieldH = 36.0f;
constexpr float kRowGap = 4.0f;    // a label to its field
constexpr float kBlockGap = 8.0f;  // one labelled row to the next
constexpr float kStripH = 44.0f;
// A labelled field is a caption with the input under it, the way WinUI does it.
constexpr float kFieldRowH = kLabelH + kRowGap + kFieldH;

enum Id {
  ID_TAB0 = 10, ID_TAB1, ID_TAB2, ID_TAB3,
  ID_SRC_A = 100, ID_SRC_A_BROWSE, ID_SRC_B, ID_SRC_B_BROWSE,
  ID_START, ID_PLAY,
  ID_SYNC_PLAY, ID_BACK, ID_FWD, ID_JUMP, ID_SPEED, ID_LOCK,
  ID_BAR_MOVIE, ID_BAR_REACTION, ID_BAR_MASTER, ID_GOTO, ID_OFFSET,
  ID_ARRANGE, ID_PIP,
  ID_VOL_A, ID_VOL_B, ID_VOL_M,
  ID_DARK, ID_READOUT,
  ID_MIN, ID_CLOSE,
};

const wchar_t* const kTabNames[kTabCount] = {L"Sources", L"Sync", L"Windows",
                                             L"Settings"};

// Places blocks down a column, either at a fixed height or filling what is left, so a
// tab always occupies exactly the window it was given.
struct Column {
  float x, w, y, gap;
  ui::RectF next(float h) {
    const ui::RectF r{x, y, w, h};
    y += h + gap;
    return r;
  }
  ui::RectF fill_to(float bottom) {
    const ui::RectF r{x, y, w, std::max(0.0f, bottom - y)};
    y = bottom + gap;
    return r;
  }
};

}  // namespace

// ---------------------------------------------------------------------------
bool Panel::init(HWND hwnd) {
  hwnd_ = hwnd;
  cfg_ = Config::load();
  dark_ = (cfg_.theme == "dark") || (cfg_.theme != "light" && ui::system_prefers_dark());
  renderer_.set_theme(dark_ ? ui::Theme::dark_theme() : ui::Theme::light_theme());
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
  message_text_ =
      L"Choose your movie and the reaction on the Sources tab, then press Start.";
  message_until_ = now_seconds() + 3600.0;
  set_dark(dark_);
  // reopen on the tab the user left, which is what a WinUI page does
  tab_ = static_cast<Tab>(std::clamp(cfg_.tab, 0, kTabCount - 1));
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
  cfg_.theme = dark_ ? "dark" : "light";
  cfg_.tab = static_cast<int>(tab_);
  cfg_.save();
  renderer_.shutdown();
}

void Panel::remember_window(int x, int y, int w, int h) {
  cfg_.window = WindowRect{x, y, w, h};
}

bool Panel::editing_text() const {
  const unsigned f = ui_.focused();
  return f == ID_SRC_A || f == ID_SRC_B || f == ID_JUMP || f == ID_SPEED ||
         f == ID_GOTO || f == ID_OFFSET;
}

void Panel::set_dark(bool dark) {
  dark_ = dark;
  renderer_.set_theme(dark_ ? ui::Theme::dark_theme() : ui::Theme::light_theme());
  const BOOL use_dark = dark_ ? TRUE : FALSE;
  constexpr DWORD kImmersiveDarkMode = 20;
  DwmSetWindowAttribute(hwnd_, kImmersiveDarkMode, &use_dark, sizeof use_dark);
  cfg_.theme = dark_ ? "dark" : "light";
}

void Panel::on_resize(float w, float h) {
  width_ = w;
  height_ = h;
  renderer_.resize(static_cast<UINT>(w), static_cast<UINT>(h), dpi_);
  // Each tab lays itself out to fill the window; the scale is only a safety net for a
  // window smaller than the minimum size.
  scale_ = std::min(1.0f, std::min(w / kMinWidth, h / kMinHeight));
  renderer_.set_scale(scale_);
  layout(w / scale_, h / scale_);
}

void Panel::on_dpi(float dpi) {
  dpi_ = dpi;
  renderer_.resize(static_cast<UINT>(width_), static_cast<UINT>(height_), dpi);
  scale_ = std::min(1.0f, std::min(width_ / kMinWidth, height_ / kMinHeight));
  renderer_.set_scale(scale_);
  layout(width_ / scale_, height_ / scale_);
}

void Panel::select_tab(int index) {
  if (index < 0 || index >= kTabCount) return;
  tab_ = static_cast<Tab>(index);
  layout(width_ / scale_, height_ / scale_);  // the new tab lays itself out
}

void Panel::set_message(const std::wstring& text) {
  message_text_ = text;
  message_until_ = now_seconds() + 8.0;
}

double Panel::jump_seconds() const {
  try {
    return std::clamp(std::stod(jump_text_), 0.5, 120.0);
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
  if (std::fabs(sync_off_) < 0.005) return L"Videos are aligned";
  return fmt(L"Reaction runs %+.2fs against the movie", sync_off_);
}

// ---------------------------------------------------------------------------
// layout
// ---------------------------------------------------------------------------
void Panel::layout(float w, float h) {
  title_bar_ = {0, 0, w, kTitleBarH};
  btn_close_ = {w - 46.0f, 0, 46.0f, kTitleBarH};
  btn_min_ = {w - 92.0f, 0, 46.0f, kTitleBarH};
  tab_strip_ = {0, kTitleBarH, w, kTabStripH};

  // the status strip is pinned to the bottom; the tab content fills what is above it
  status_strip_ = {ui::kPad, h - kStripH - 8.0f, w - 2 * ui::kPad, kStripH};
  const float content_top = kTitleBarH + kTabStripH + 12.0f;
  content_ = {ui::kPad, content_top, w - 2 * ui::kPad,
              std::max(160.0f, status_strip_.y - content_top - 12.0f)};

  // tab widths follow their labels, so a longer name never clips
  float tx = ui::kPad;
  for (int i = 0; i < kTabCount; ++i) {
    const float tw = renderer_.measure(kTabNames[i], 14.0f) + 32.0f;
    tab_rect_[i] = {tx, kTitleBarH + 7.0f, tw, 32.0f};
    tx += tw + 6.0f;
  }

  switch (tab_) {
    case Tab::Sources: layout_sources(content_); break;
    case Tab::Sync: layout_sync(content_); break;
    case Tab::Windows: layout_windows(content_); break;
    case Tab::Settings: layout_settings(content_); break;
    default: break;
  }
}

void Panel::layout_sources(const ui::RectF& c) {
  const float actions_h = kCardTop + kCardTitleH + 8.0f + kFieldH + 8.0f + 18.0f + 12.0f;
  const float videos_h = std::max(210.0f, c.h - actions_h - ui::kGap);

  Column col{c.x, c.w, c.y, ui::kGap};
  card_videos_ = col.next(videos_h);
  card_actions_ = col.next(actions_h);

  const float label_w = 74.0f;
  const float browse_w = 92.0f;
  float ry = card_videos_.y + kCardTop + kCardTitleH + 8.0f;
  for (int i = 0; i < 2; ++i) {
    const float fx = card_videos_.x + ui::kPad + label_w;
    const float fw = card_videos_.w - 2 * ui::kPad - label_w - browse_w - ui::kGap;
    src_row_[i].label = {card_videos_.x + ui::kPad, ry, label_w, kLabelH};
    src_row_[i].field = {fx, ry + kLabelH + kRowGap, fw, kFieldH};
    src_row_[i].browse = {fx + fw + ui::kGap, ry + kLabelH + kRowGap, browse_w, kFieldH};
    ry += kFieldRowH + kBlockGap;
  }
  src_hint_ = {card_videos_.x + ui::kPad, card_videos_.y + card_videos_.h - 30.0f,
               card_videos_.w - 2 * ui::kPad, 18.0f};

  const float inner = card_actions_.y + kCardTop + kCardTitleH + 8.0f;
  btn_start_ = {card_actions_.x + ui::kPad, inner, 190.0f, kFieldH};
  btn_play_ = {btn_start_.x + btn_start_.w + ui::kGap, inner, 150.0f, kFieldH};
}

void Panel::layout_sync(const ui::RectF& c) {
  const float playback_h =
      kCardTop + kCardTitleH + 8.0f + kFieldH + 8.0f + kFieldRowH + 12.0f;
  const float align_h = kCardTop + kCardTitleH + 8.0f + kFieldRowH + 8.0f + 18.0f + 12.0f;
  const float timelines_h = std::max(230.0f, c.h - playback_h - align_h - 2 * ui::kGap);

  Column col{c.x, c.w, c.y, ui::kGap};
  card_playback_ = col.next(playback_h);
  card_timelines_ = col.next(timelines_h);
  card_align_ = col.next(align_h);

  float ry = card_playback_.y + kCardTop + kCardTitleH + 8.0f;
  btn_sync_play_ = {card_playback_.x + ui::kPad, ry, 150.0f, kFieldH};
  btn_back_ = {btn_sync_play_.x + btn_sync_play_.w + ui::kGap, ry, 96.0f, kFieldH};
  btn_fwd_ = {btn_back_.x + btn_back_.w + ui::kGap, ry, 96.0f, kFieldH};
  ry += kFieldH + 8.0f;
  const float fw = 120.0f;
  field_jump_ = {card_playback_.x + ui::kPad, ry + kLabelH + kRowGap, fw, kFieldH};
  field_speed_ = {field_jump_.x + fw + ui::kGap, field_jump_.y, fw, kFieldH};
  const float lock_x = field_speed_.x + fw + ui::kGap;
  toggle_lock_ = {lock_x, ry, card_playback_.x + card_playback_.w - ui::kPad - lock_x,
                  kFieldRowH};

  ry = card_timelines_.y + kCardTop + kCardTitleH + 8.0f;
  const float lbl_w = 62.0f;
  const float time_w = 96.0f;
  for (int i = 0; i < 3; ++i) {
    tl_[i].label = {card_timelines_.x + ui::kPad, ry, lbl_w, kLabelH};
    tl_[i].bar = {card_timelines_.x + ui::kPad + lbl_w, ry + 2.0f,
                  card_timelines_.w - 2 * ui::kPad - lbl_w - time_w - ui::kGap, 20.0f};
    tl_[i].time = {tl_[i].bar.x + tl_[i].bar.w + ui::kGap, ry - 1.0f, time_w, kLabelH + 2};
    ry += 40.0f;
  }
  ry += 4.0f;
  field_goto_ = {card_timelines_.x + ui::kPad, ry + kLabelH + kRowGap, 220.0f, kFieldH};

  const float ay = card_align_.y + kCardTop + kCardTitleH + 8.0f;
  field_offset_ = {card_align_.x + ui::kPad, ay + kLabelH + kRowGap, 200.0f, kFieldH};
  lbl_offset_ = {field_offset_.x + 200.0f + ui::kGap, ay + kLabelH + kRowGap + 9.0f,
                 card_align_.w - 2 * ui::kPad - 200.0f - ui::kGap, kLabelH};
}

void Panel::layout_windows(const ui::RectF& c) {
  const float windows_h = kCardTop + kCardTitleH + 8.0f + kFieldH + 8.0f + 18.0f + 12.0f;
  const float shortcuts_h = kCardTop + kCardTitleH + 8.0f + 4 * 20.0f + 12.0f;
  const float volume_h = std::max(190.0f, c.h - windows_h - shortcuts_h - 2 * ui::kGap);

  Column col{c.x, c.w, c.y, ui::kGap};
  card_windows_ = col.next(windows_h);
  card_volume_ = col.next(volume_h);
  card_shortcuts_ = col.next(shortcuts_h);

  const float wy = card_windows_.y + kCardTop + kCardTitleH + 8.0f;
  const float half = (card_windows_.w - 2 * ui::kPad - ui::kGap) / 2.0f;
  btn_arrange_ = {card_windows_.x + ui::kPad, wy, half, kFieldH};
  btn_pip_ = {btn_arrange_.x + half + ui::kGap, wy, half, kFieldH};

  float vy = card_volume_.y + kCardTop + kCardTitleH + 8.0f;
  const float vlbl_w = 96.0f;
  for (int i = 0; i < 3; ++i) {
    vol_label_[i] = {card_volume_.x + ui::kPad, vy, vlbl_w, kLabelH};
    vol_slider_[i] = {card_volume_.x + ui::kPad + vlbl_w, vy + 4.0f,
                      card_volume_.w - 2 * ui::kPad - vlbl_w - ui::kGap, 20.0f};
    vy += 40.0f;
  }
}

void Panel::layout_settings(const ui::RectF& c) {
  const float toggle_h = kCardTop + kCardTitleH + 8.0f + 32.0f + 12.0f;
  const float about_h = std::max(200.0f, c.h - 2 * toggle_h - 2 * ui::kGap);

  Column col{c.x, c.w, c.y, ui::kGap};
  card_appearance_ = col.next(toggle_h);
  card_status_ = col.next(toggle_h);
  card_about_ = col.next(about_h);

  const float ay = card_appearance_.y + kCardTop + kCardTitleH + 8.0f;
  toggle_dark_ = {card_appearance_.x + ui::kPad, ay, card_appearance_.w - 2 * ui::kPad,
                  32.0f};
  const float sy = card_status_.y + kCardTop + kCardTitleH + 8.0f;
  toggle_readout_ = {card_status_.x + ui::kPad, sy, card_status_.w - 2 * ui::kPad, 32.0f};
}

// ---------------------------------------------------------------------------
// drawing
// ---------------------------------------------------------------------------
void Panel::draw() {
  // Trust the window over our own record of it: a late WM_DPICHANGED can resize the
  // window after the initial clamp, and a layout left at the old size would push the
  // last card off the bottom.
  {
    RECT rc{};
    if (GetClientRect(hwnd_, &rc)) {
      const float cw = static_cast<float>(rc.right - rc.left);
      const float ch = static_cast<float>(rc.bottom - rc.top);
      if (cw > 1.0f && ch > 1.0f &&
          (std::fabs(cw - width_) > 0.5f || std::fabs(ch - height_) > 0.5f)) {
        on_resize(cw, ch);
      }
    }
  }

  // input arrives in pixels; the layout works in design units
  ui::InputState mapped = in_;
  if (scale_ > 0.0f) {
    mapped.mouse_x = in_.mouse_x / scale_;
    mapped.mouse_y = in_.mouse_y / scale_;
  }
  ui_.begin_frame(renderer_, mapped);

  const ui::Theme& t = renderer_.theme();
  const float dw = width_ / scale_, dh = height_ / scale_;
  renderer_.begin();
  renderer_.fill_gradient_v({0, 0, dw, dh},
                            dark_ ? ui::Color{0.11f, 0.11f, 0.12f, 1.0f}
                                  : ui::Color{0.97f, 0.97f, 0.98f, 1.0f},
                            t.window_bg);

  draw_title_bar();
  draw_tabs();
  switch (tab_) {
    case Tab::Sources: draw_sources_tab(); break;
    case Tab::Sync: draw_sync_tab(); break;
    case Tab::Windows: draw_windows_tab(); break;
    case Tab::Settings: draw_settings_tab(); break;
    default: break;
  }
  draw_status();

  renderer_.end();
  ui_.end_frame();
}

void Panel::draw_title_bar() {
  const ui::Theme& t = renderer_.theme();
  renderer_.fill_rect(title_bar_,
                      dark_ ? ui::Color{0, 0, 0, 0.18f} : ui::Color{0, 0, 0, 0.03f});
  renderer_.text(L"SyncPlayer", {ui::kPad, 0, 200, kTitleBarH}, t.text, 14.0f,
                 ui::TextAlign::Left, ui::TextWeight::SemiBold);
  const std::wstring chip = L"2.0.0";
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

void Panel::draw_tabs() {
  const ui::Theme& t = renderer_.theme();
  renderer_.line(tab_strip_.x, tab_strip_.y + tab_strip_.h - 1.0f,
                 tab_strip_.x + tab_strip_.w, tab_strip_.y + tab_strip_.h - 1.0f,
                 t.border, 1.0f);

  const unsigned ids[kTabCount] = {ID_TAB0, ID_TAB1, ID_TAB2, ID_TAB3};
  const float mx = in_.mouse_x / scale_, my = in_.mouse_y / scale_;
  for (int i = 0; i < kTabCount; ++i) {
    const bool selected = (static_cast<int>(tab_) == i);
    const bool hovered = tab_rect_[i].contains(mx, my);
    // the control gives us the click; the selected look is drawn on top of it
    if (ui_.button(ids[i], tab_rect_[i], kTabNames[i], ui::ButtonStyle::Subtle)) {
      tab_ = static_cast<Tab>(i);
      layout(width_ / scale_, height_ / scale_);  // the new tab lays itself out
      message_until_ = 0.0;
    }
    if (selected) {
      renderer_.fill_rounded(tab_rect_[i], ui::kRadiusControl, t.control);
      renderer_.text(kTabNames[i], tab_rect_[i], t.text, 14.0f, ui::TextAlign::Center,
                     ui::TextWeight::SemiBold);
      renderer_.fill_rounded({tab_rect_[i].x + 8.0f, tab_strip_.y + tab_strip_.h - 3.0f,
                              tab_rect_[i].w - 16.0f, 2.0f},
                             1.0f, t.accent);
    } else {
      if (hovered) {
        renderer_.fill_rounded(tab_rect_[i], ui::kRadiusControl, t.control_hover);
      }
      renderer_.text(kTabNames[i], tab_rect_[i], hovered ? t.text : t.text_secondary,
                     14.0f, ui::TextAlign::Center);
    }
  }
}

void Panel::draw_sources_tab() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_videos_, L"VIDEOS");

  const wchar_t* names[2] = {L"Movie", L"Reaction"};
  const unsigned field_ids[2] = {ID_SRC_A, ID_SRC_B};
  const unsigned browse_ids[2] = {ID_SRC_A_BROWSE, ID_SRC_B_BROWSE};
  for (int i = 0; i < 2; ++i) {
    renderer_.text(names[i], src_row_[i].label, t.text_secondary, 12.0f);
    std::wstring& value = (i == 0) ? src_a_text_ : src_b_text_;
    const ui::FieldResult res =
        ui_.text_field(field_ids[i], src_row_[i].field, value,
                       i == 0 ? L"movie.mp4 or a link" : L"reaction.mp4 or a link");
    if (res != ui::FieldResult::None) {
      if (i == 0) cfg_.movie = narrow(src_a_text_);
      else cfg_.reaction = narrow(src_b_text_);
    }
    if (ui_.button(browse_ids[i], src_row_[i].browse, L"Browse\u2026")) {
      browse_for(i == 0 ? Side::Movie : Side::Reaction);
    }
  }
  renderer_.text(L"Paste a link, or drop a file onto the window.", src_hint_,
                 t.text_secondary, 12.0f);

  ui_.card(card_actions_, L"GET STARTED");
  if (ui_.button(ID_START, btn_start_, started_ ? L"Reload both" : L"Start",
                 ui::ButtonStyle::Accent)) {
    start_sources();
  }
  if (ui_.button(ID_PLAY, btn_play_, playing_ ? L"Pause" : L"Play",
                 playing_ ? ui::ButtonStyle::Standard : ui::ButtonStyle::Accent,
                 started_)) {
    toggle_play();
  }
}

void Panel::draw_sync_tab() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_playback_, L"PLAYBACK");

  if (ui_.button(ID_SYNC_PLAY, btn_sync_play_, playing_ ? L"Pause" : L"Play",
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
  renderer_.text(L"Jump (s)",
                 {field_jump_.x, field_jump_.y - kLabelH - kRowGap, field_jump_.w, kLabelH},
                 t.text_secondary, 12.0f);
  renderer_.text(L"Speed",
                 {field_speed_.x, field_speed_.y - kLabelH - kRowGap, field_speed_.w,
                  kLabelH},
                 t.text_secondary, 12.0f);
  if (ui_.text_field(ID_JUMP, field_jump_, jump_text_, L"5", true) ==
      ui::FieldResult::Submitted) {
    cfg_.jump_sec = jump_seconds();
  }
  if (ui_.text_field(ID_SPEED, field_speed_, speed_text_, L"1.00", true) ==
      ui::FieldResult::Submitted) {
    apply_speed_from_field();
  }
  if (ui_.toggle(ID_LOCK, toggle_lock_, locked_, L"Lock sync to the master bar")) {
    set_message(locked_ ? L"Sync locked: the Master bar drives both videos."
                        : L"Sync unlocked: align with the per-video bars.");
  }

  ui_.card(card_timelines_, L"TIMELINES");
  const std::optional<double> pos_a = movie_.position();
  const std::optional<double> dur_a = movie_.duration();
  const std::optional<double> pos_b = reaction_.position();
  const std::optional<double> dur_b = reaction_.duration();
  const bool have = started_;

  auto row = [&](int i, unsigned id, const wchar_t* name, const std::optional<double>& pos,
                 const std::optional<double>& dur, Side side) {
    renderer_.text(name, tl_[i].label, have ? t.text : t.text_disabled, 13.0f,
                   ui::TextAlign::Left, ui::TextWeight::SemiBold);
    const ui::SeekDrag drag = ui_.seek_bar(id, tl_[i].bar, pos.value_or(0),
                                           dur.value_or(0), have && dur.value_or(0) > 0);
    renderer_.text(fmt(L"%s / %s", widen(format_hms(pos.value_or(0))).c_str(),
                       widen(format_hms(dur.value_or(0))).c_str()),
                   tl_[i].time, t.text_secondary, 12.0f, ui::TextAlign::Right);
    scrubbing_ = drag.result == ui::SeekResult::Dragging;
    if (drag.result == ui::SeekResult::Clicked) {
      last_scrub_pos_ = drag.clicked_seconds;
      seek_side(side, drag.clicked_seconds);
    } else if (drag.result == ui::SeekResult::Dragging ||
               drag.result == ui::SeekResult::Released) {
      // precise scrubbing: sideways travel at a fixed gain, finer once lifted
      seek_side(side, scrub_target(last_scrub_pos_, drag.dx_px, drag.lift_px, dur));
    }
  };

  row(0, ID_BAR_MOVIE, L"Movie", pos_a, dur_a, Side::Movie);
  row(1, ID_BAR_REACTION, L"Reaction", pos_b, dur_b, Side::Reaction);

  // the master bar drives both, keeping the offset
  renderer_.text(L"Master", tl_[2].label, have ? t.text : t.text_disabled, 13.0f,
                 ui::TextAlign::Left, ui::TextWeight::SemiBold);
  const ui::SeekDrag master = ui_.seek_bar(ID_BAR_MASTER, tl_[2].bar, pos_a.value_or(0),
                                           dur_a.value_or(0), have && locked_);
  renderer_.text(fmt(L"%s / %s", widen(format_hms(pos_a.value_or(0))).c_str(),
                     widen(format_hms(dur_a.value_or(0))).c_str()),
                 tl_[2].time, t.text_secondary, 12.0f, ui::TextAlign::Right);
  if (master.result == ui::SeekResult::Clicked) {
    last_scrub_pos_ = master.clicked_seconds;
    seek_master(master.clicked_seconds);
  } else if (master.result == ui::SeekResult::Dragging ||
             master.result == ui::SeekResult::Released) {
    seek_master(scrub_target(last_scrub_pos_, master.dx_px, master.lift_px, dur_a));
  }
  if (!locked_) {
    renderer_.text(L"master bar (lock sync to use it)", tl_[2].label, t.text_disabled,
                   12.0f);
  }

  renderer_.text(L"Go to",
                 {field_goto_.x, field_goto_.y - kLabelH - kRowGap, field_goto_.w, kLabelH},
                 t.text_secondary, 12.0f);
  if (ui_.text_field(ID_GOTO, field_goto_, goto_text_, L"90 or 1:30:00") ==
      ui::FieldResult::Submitted) {
    if (auto secs = parse_timecode(goto_text_)) {
      seek_master(*secs);
      set_message(fmt(L"Moved both videos to %s.", widen(format_hms(*secs)).c_str()));
    } else {
      set_message(L"That is not a time. Try 90, 1:30 or 1:30:00.");
    }
    goto_text_.clear();
  }

  ui_.card(card_align_, L"ALIGNMENT");
  renderer_.text(L"Offset",
                 {field_offset_.x, field_offset_.y - kLabelH - kRowGap, field_offset_.w,
                  kLabelH},
                 t.text_secondary, 12.0f);
  if (ui_.text_field(ID_OFFSET, field_offset_, offset_text_, L"12.5 / 1:05 / -3.25",
                     true) == ui::FieldResult::Submitted) {
    apply_offset_from_field();
  }
  renderer_.text(offset_label(), lbl_offset_, t.text_secondary, 12.0f);
}

void Panel::draw_windows_tab() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_windows_, L"WINDOWS");
  if (ui_.button(ID_ARRANGE, btn_arrange_, L"Arrange side by side",
                 ui::ButtonStyle::Standard, started_)) {
    arrange_windows();
  }
  if (ui_.button(ID_PIP, btn_pip_, floating_pip_ ? L"Exit floating PiP" : L"Floating PiP",
                 ui::ButtonStyle::Standard, started_)) {
    toggle_floating_pip();
  }
  renderer_.text(L"Floating PiP makes the reaction borderless and always on top.",
                 {card_windows_.x + ui::kPad, card_windows_.y + card_windows_.h - 30.0f,
                  card_windows_.w - 2 * ui::kPad, 18.0f},
                 t.text_secondary, 12.0f);

  ui_.card(card_volume_, L"VOLUME");
  const wchar_t* vnames[3] = {L"Movie", L"Reaction", L"Master"};
  double* values[3] = {&vol_a_, &vol_b_, &vol_m_};
  const unsigned ids[3] = {ID_VOL_A, ID_VOL_B, ID_VOL_M};
  bool changed = false;
  for (int i = 0; i < 3; ++i) {
    renderer_.text(fmt(L"%s  %.0f%%", vnames[i], *values[i]), vol_label_[i],
                   t.text_secondary, 13.0f);
    changed |= ui_.slider(ids[i], vol_slider_[i], *values[i], 0, kVolumeMax);
  }
  if (changed) apply_volume();

  ui_.card(card_shortcuts_, L"SHORTCUTS");
  float sy = card_shortcuts_.y + kCardTop + kCardTitleH + 8.0f;
  const wchar_t* keys[4][2] = {{L"Space", L"Play or pause both videos"},
                               {L"Left", L"Jump back by the jump amount"},
                               {L"Right", L"Jump forward by the jump amount"},
                               {L"Ctrl+1..4", L"Switch tabs (Ctrl+Tab cycles)"}};
  for (int i = 0; i < 4; ++i) {
    renderer_.text(keys[i][0], {card_shortcuts_.x + ui::kPad, sy, 80.0f, 18.0f}, t.text,
                   13.0f, ui::TextAlign::Left, ui::TextWeight::SemiBold);
    renderer_.text(keys[i][1],
                   {card_shortcuts_.x + ui::kPad + 88.0f, sy,
                    card_shortcuts_.w - 2 * ui::kPad - 88.0f, 18.0f},
                   t.text_secondary, 13.0f);
    sy += 20.0f;
  }
}

void Panel::draw_settings_tab() {
  const ui::Theme& t = renderer_.theme();
  ui_.card(card_appearance_, L"APPEARANCE");
  if (ui_.toggle(ID_DARK, toggle_dark_, dark_, L"Dark theme")) {
    set_dark(dark_);
    set_message(dark_ ? L"Dark theme." : L"Light theme.");
  }

  ui_.card(card_status_, L"STATUS BAR");
  if (ui_.toggle(ID_READOUT, toggle_readout_, cfg_.show_readout,
                 L"Show the live position readout")) {
    set_message(cfg_.show_readout ? L"Readout shown." : L"Readout hidden.");
  }

  ui_.card(card_about_, L"ABOUT");
  float ay = card_about_.y + kCardTop + kCardTitleH + 8.0f;
  const auto mpv = find_mpv();
  const struct {
    const wchar_t* label;
    std::wstring value;
  } lines[] = {
      {L"Version", L"2.0.0 (C++ build)"},
      {L"mpv", mpv ? widen(to_utf8(*mpv)) : std::wstring(L"not found")},
      {L"Config", widen(to_utf8(config_dir() / "syncplayer_config.json"))},
  };
  for (const auto& line : lines) {
    renderer_.text(line.label, {card_about_.x + ui::kPad, ay, 80.0f, 20.0f},
                   t.text_secondary, 13.0f);
    renderer_.text(line.value,
                   {card_about_.x + ui::kPad + 88.0f, ay,
                    card_about_.w - 2 * ui::kPad - 88.0f, 20.0f},
                   t.text, 13.0f);
    ay += 24.0f;
  }
  renderer_.text(L"This build reads the same settings file as the Python version, so "
                 L"your sources and remembered alignments carry over.",
                 {card_about_.x + ui::kPad, card_about_.y + card_about_.h - 52.0f,
                  card_about_.w - 2 * ui::kPad, 40.0f},
                 t.text_secondary, 12.0f);
}

void Panel::draw_status() {
  const ui::Theme& t = renderer_.theme();
  renderer_.fill_rounded(status_strip_, ui::kRadiusCard, t.layer);
  renderer_.stroke_rounded(status_strip_, ui::kRadiusCard, t.border, 1.0f);

  const std::wstring readout =
      cfg_.show_readout ? (status_text_.empty() ? L"Idle. Press Start on the Sources tab."
                                                : status_text_)
                        : L"Readout hidden (Settings).";
  renderer_.text(readout,
                 {status_strip_.x + 12.0f, status_strip_.y + 2.0f, status_strip_.w - 24.0f,
                  20.0f},
                 t.text, 13.0f);
  const bool show_message = now_seconds() < message_until_ && !message_text_.empty();
  renderer_.text(show_message ? message_text_ : L"",
                 {status_strip_.x + 12.0f, status_strip_.y + 21.0f, status_strip_.w - 24.0f,
                  18.0f},
                 t.text_secondary, 12.0f);
}

}  // namespace sp::app
