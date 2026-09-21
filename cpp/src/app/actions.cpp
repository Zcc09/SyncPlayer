// Everything the panel's controls actually do: launching the two players, the sync
// loop, seeking, the typed offset, volume, and the window operations.
#include <algorithm>
#include <cmath>
#include <cstdarg>
#include <cstdio>

#include <windows.h>
#include <commdlg.h>

#include "app/panel.h"
#include "core/paths.h"
#include "platform/win/window.h"

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

bool looks_like_url(const std::wstring& s) {
  return s.rfind(L"http://", 0) == 0 || s.rfind(L"https://", 0) == 0;
}

}  // namespace

void Panel::start_sources() {
  stop_players();
  if (src_a_text_.empty() || src_b_text_.empty()) {
    set_message(L"Both sources are needed: a movie and the reaction.");
    return;
  }

  const auto mpv = find_mpv();
  if (!mpv) {
    set_message(L"mpv was not found. Install SyncPlayer (it bundles mpv) or put mpv on "
                L"PATH.");
    return;
  }
  const MpvAssets assets = write_mpv_assets();

  MpvOptions base;
  base.lua_script = assets.lua_path;
  base.input_conf = assets.input_conf_path;
  base.ytdl_path = ytdl_path_option();
  base.ytdl_format = cfg_.ytdl_format();
  base.start_paused = true;

  MpvOptions oa = base;
  oa.ipc_pipe = "syncplayer-panel-A";
  oa.title = "SyncPlayer - Movie";
  oa.url_source = looks_like_url(src_a_text_);

  MpvOptions ob = base;
  ob.ipc_pipe = "syncplayer-panel-B";
  ob.title = "SyncPlayer - Reaction";
  ob.url_source = looks_like_url(src_b_text_);

  std::string err;
  if (!movie_.start(*mpv, oa, narrow(src_a_text_), &err)) {
    set_message(L"Could not start the movie: " + widen(err));
    return;
  }
  if (!reaction_.start(*mpv, ob, narrow(src_b_text_), &err)) {
    set_message(L"Could not start the reaction: " + widen(err));
    movie_.stop();
    return;
  }

  started_ = true;
  playing_ = false;

  // the remembered alignment for this exact pair, applied before anything plays
  if (auto remembered = cfg_.alignments.find(narrow(src_a_text_), narrow(src_b_text_))) {
    sync_off_ = *remembered;
    offset_text_ = fmt(L"%+.2f", sync_off_);
    set_message(fmt(L"Remembered alignment applied: %s.", offset_label().c_str()));
  } else {
    sync_off_ = 0.0;
    offset_text_ = L"0.00";
  }

  apply_volume();

  // find the two mpv windows and put them side by side
  hwnd_movie_ = find_window_for_pid(movie_.pid(), "Movie");
  hwnd_reaction_ = find_window_for_pid(reaction_.pid(), "Reaction");
  if (hwnd_movie_ && hwnd_reaction_) {
    const ScreenInfo si = screen_info();
    arrange_side_by_side(*hwnd_movie_, *hwnd_reaction_, si.work, 8, false);
  } else {
    set_message(L"The videos are playing, but their windows could not be found to "
                L"arrange them.");
  }

  // hold the reaction at its aligned spot so pressing Play starts them together
  if (std::fabs(sync_off_) > 0.005) {
    reaction_.seek_absolute(sync_off_ > 0 ? sync_off_ : 0.0);
  }
  status_text_ = L"Both videos loaded, paused at the start. Press Play.";
  set_message(L"Both videos loaded paused. Press Play when ready.");
}

void Panel::stop_players() {
  movie_.stop();
  reaction_.stop();
  started_ = false;
  playing_ = false;
  applied_rate_ = 1.0;
  hwnd_movie_.reset();
  hwnd_reaction_.reset();
}

void Panel::toggle_play() {
  if (!started_) return;
  playing_ = !playing_;
  movie_.play_pause(!playing_);
  reaction_.play_pause(!playing_);
  if (!playing_) applied_rate_ = 1.0;
  status_text_ = playing_ ? L"Playing. Drift is corrected as it appears."
                          : L"Paused.";
}

void Panel::seek_side(Side side, double seconds, bool exact) {
  if (!started_) return;
  const double target = std::max(0.0, seconds);
  if (side == Side::Movie) movie_.seek_absolute(target);
  else reaction_.seek_absolute(target);
  (void)exact;
}

void Panel::seek_master(double seconds) {
  if (!started_) return;
  const double target = std::max(0.0, seconds);
  movie_.seek_absolute(target);
  reaction_.seek_absolute(reaction_target(target, sync_off_));
}

void Panel::nudge_jump(int direction) {
  if (!started_) return;
  const double step = jump_seconds() * (direction < 0 ? -1.0 : 1.0);
  const double base = movie_.position().value_or(0.0);
  seek_master(base + step);
}

void Panel::apply_offset_from_field() {
  const auto parsed = parse_timecode(offset_text_);
  if (!parsed) {
    set_message(L"That is not an offset. Try 12.5, 1:05 or -3.25.");
    offset_text_ = fmt(L"%+.2f", sync_off_);
    return;
  }
  sync_off_ = *parsed;
  offset_text_ = fmt(L"%+.2f", sync_off_);
  cfg_.alignments.remember(narrow(src_a_text_), narrow(src_b_text_), sync_off_);
  if (started_) {
    // move the reaction onto its aligned spot right away
    const double movie_pos = movie_.position().value_or(0.0);
    reaction_.seek_absolute(std::max(0.0, reaction_target(movie_pos, sync_off_)));
  }
  set_message(fmt(L"Offset %s applied%s.", offset_label().c_str(),
                  std::fabs(sync_off_) < 0.005 ? L" (alignment cleared)" : L""));
}

void Panel::apply_speed_from_field() {
  try {
    cfg_.speed = std::clamp(std::stod(speed_text_), 0.25, 4.0);
  } catch (...) {
    cfg_.speed = 1.0;
  }
  speed_text_ = fmt(L"%.2f", cfg_.speed);
  if (started_) {
    movie_.set_speed(cfg_.speed);
    reaction_.set_speed(cfg_.speed);
  }
  set_message(fmt(L"Speed set to %.2fx.", cfg_.speed));
}

void Panel::apply_volume() {
  const double master = std::clamp(vol_m_ / 100.0, 0.0, 1.0);
  const double va = std::clamp(vol_a_ * master, 0.0, kVolumeMax);
  const double vb = std::clamp(vol_b_ * master, 0.0, kVolumeMax);
  if (started_) {
    movie_.set_volume(va);
    reaction_.set_volume(vb);
  }
}

void Panel::arrange_windows() {
  if (!started_) return;
  if (!hwnd_movie_ || !hwnd_reaction_) {
    hwnd_movie_ = find_window_for_pid(movie_.pid(), "Movie");
    hwnd_reaction_ = find_window_for_pid(reaction_.pid(), "Reaction");
  }
  if (!hwnd_movie_ || !hwnd_reaction_) {
    set_message(L"Could not find both video windows to arrange them.");
    return;
  }
  const ScreenInfo si = screen_info();
  arrange_side_by_side(*hwnd_movie_, *hwnd_reaction_, si.work, 8, false);
  set_message(L"Videos arranged side by side.");
}

void Panel::toggle_floating_pip() {
  if (!started_ || !hwnd_reaction_) return;
  floating_pip_ = !floating_pip_;
  set_borderless(*hwnd_reaction_, floating_pip_);
  set_always_on_top(*hwnd_reaction_, floating_pip_);
  if (floating_pip_) {
    // a reaction cam in the corner, sized to a third of the screen height
    const ScreenInfo si = screen_info();
    const int h = static_cast<int>(si.work.h * 0.36);
    const int w = static_cast<int>(h * 16.0 / 9.0);
    place_window(*hwnd_reaction_,
                 Rect{si.work.x + si.work.w - w - 24, si.work.y + 24, w, h}, false);
    set_message(L"Floating PiP on: borderless and always on top.");
  } else {
    arrange_windows();
    set_message(L"Floating PiP off.");
  }
}

void Panel::browse_for(Side side) {
  wchar_t file[MAX_PATH * 4] = {};
  OPENFILENAMEW ofn{};
  ofn.lStructSize = sizeof ofn;
  ofn.hwndOwner = hwnd_;
  ofn.lpstrFilter =
      L"Video files\0*.mp4;*.mkv;*.mov;*.webm;*.avi;*.ts;*.m4v\0All files\0*.*\0";
  ofn.lpstrFile = file;
  ofn.nMaxFile = static_cast<DWORD>(std::size(file));
  ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_EXPLORER;
  if (!GetOpenFileNameW(&ofn)) return;
  if (side == Side::Movie) {
    src_a_text_ = file;
    cfg_.movie = narrow(src_a_text_);
  } else {
    src_b_text_ = file;
    cfg_.reaction = narrow(src_b_text_);
  }
}

// ---------------------------------------------------------------------------
// the sync loop: 33 ms, the same cadence as the Python app
// ---------------------------------------------------------------------------
void Panel::tick() { sync_tick(); }

void Panel::sync_tick() {
  if (!started_) return;

  const auto pos_a = movie_.position();
  const auto pos_b = reaction_.position();
  const auto dur_a = movie_.duration();
  const auto dur_b = reaction_.duration();

  const bool dragging = scrubbing_;
  const bool movie_end = movie_.eof_reached();
  const bool react_end = reaction_.eof_reached();

  const double d = drift(pos_b, pos_a, sync_off_);

  if (playing_) {
    if (needs_correction(pos_b, pos_a, sync_off_, true, dragging, movie_end, react_end)) {
      if (std::fabs(d) > kMicroMaxDrift) {
        // a real gap: seek, and drop any trim that was in flight
        reaction_.seek_absolute(std::max(0.0, reaction_target(pos_a.value_or(0.0), sync_off_)));
        applied_rate_ = 1.0;
        reaction_.set_speed(cfg_.speed);
        status_text_ = fmt(L"Resynced with a seek (was %.1fs out).", d);
      } else {
        // small drift is absorbed by trimming the rate, which is inaudible
        const double rate = micro_rate(d);
        reaction_.set_speed(cfg_.speed * rate);
        applied_rate_ = rate;
        trim_since_ = static_cast<double>(GetTickCount64()) / 1000.0;
      }
    } else if (std::fabs(applied_rate_ - 1.0) > 1e-6) {
      const double now = static_cast<double>(GetTickCount64()) / 1000.0;
      if (now - trim_since_ > kMicroHold) {
        reaction_.set_speed(cfg_.speed);
        applied_rate_ = 1.0;
      }
    }
  }

  const std::wstring movie_txt = widen(format_hms(pos_a.value_or(0)));
  const std::wstring react_txt = widen(format_hms(pos_b.value_or(0)));
  const std::wstring dur_txt = widen(format_hms(dur_a.value_or(0)));
  const double shown_delta = (playing_ && pos_a && pos_b) ? d : 0.0;
  status_text_ = fmt(L"Movie %s / %s   \u00b7   Reaction %s / %s   \u00b7   \u0394 %.1fs   \u00b7   %s",
                     movie_txt.c_str(), dur_txt.c_str(), react_txt.c_str(),
                     widen(format_hms(dur_b.value_or(0))).c_str(), shown_delta,
                     offset_label().c_str());
  if (std::fabs(applied_rate_ - 1.0) > 1e-6) {
    status_text_ += fmt(L"   \u00b7   trim %.3fx", applied_rate_);
  }
}

}  // namespace sp::app
