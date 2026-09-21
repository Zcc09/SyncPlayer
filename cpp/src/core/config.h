// Persistent settings and the remembered alignments, with the same keys the Python
// build writes so an existing configuration keeps working.
#pragma once

#include <string>
#include <vector>

#include "core/sync.h"

namespace sp {

struct WindowRect {
  int x = 0, y = 0, w = 0, h = 0;
  bool valid() const { return w > 0 && h > 0; }
};

struct Config {
  // sources
  std::string movie;
  std::string reaction;

  // volume
  double vol_a = 100.0;
  double vol_b = 100.0;
  double vol_m = 100.0;
  bool vol_lbls = true;

  // transport
  double speed = 1.0;
  double jump_sec = 5.0;

  // settings dialog
  std::string youtube_quality = "1080p";
  int download_connections = 8;
  std::string seek_mode = "precise";   // precise | direct
  bool show_readout = true;
  std::string theme = "system";       // system | dark | light
  int tab = 0;                        // the tab the panel reopens on

  // window geometry
  WindowRect window;

  // remembered alignments, keyed by both sources
  AlignmentStore alignments;

  // the last window size, so the panel reopens where it was
  static Config load();
  bool save() const;

  // quality name -> yt-dlp format expression, mirroring ytdl_format_expr()
  std::string ytdl_format() const;
};

// The quality list offered by the Settings dialog and the Download window.
struct Quality {
  const char* label;
  const char* expr;   // "" means "best"
  bool needs_ffmpeg;
};
std::vector<Quality> youtube_qualities();

}  // namespace sp
