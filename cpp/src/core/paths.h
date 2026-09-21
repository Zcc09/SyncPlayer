// Where things live, and where mpv/yt-dlp/ffmpeg are found. Ported from sp_plat.py and
// syncplayer.py's find_mpv/find_ytdl, including the discovery order and the rule that a
// missing mpv is reported as "not found" rather than as the bare string "mpv".
#pragma once

#include <filesystem>
#include <optional>
#include <string>
#include <vector>

namespace sp {

namespace fs = std::filesystem;

// ---------------------------------------------------------------------------
// directories
// ---------------------------------------------------------------------------

// %APPDATA%\SyncPlayer - configuration and remembered alignments
fs::path config_dir();
// %USERPROFILE%\Pictures\SyncPlayer - screenshots and mpv logs (keeps the Desktop clean)
fs::path shot_dir();
// %USERPROFILE%\Downloads\SyncPlayer
fs::path downloads_dir();
// %LOCALAPPDATA%\Programs\SyncPlayer - the default installation destination
fs::path default_install_dir();
// %LOCALAPPDATA%\SyncPlayer - the older per-user location, still honoured
fs::path legacy_install_dir();
// the directory the executable itself sits in
fs::path app_base_dir();

void ensure_dirs();

// ---------------------------------------------------------------------------
// tool discovery
// ---------------------------------------------------------------------------

// Search order, identical to the Python build:
//   bundled mpv\mpv.exe beside the exe  ->  %LOCALAPPDATA%\SyncPlayer\mpv\mpv.exe
//   ->  MPV_PATH  ->  PATH  ->  C:\Program Files\MPV Player\mpv.exe  ->  standard dirs
// Returns nullopt when mpv genuinely is not there.
std::optional<fs::path> find_mpv();

// bundled mpv\yt-dlp.exe -> %LOCALAPPDATA%\SyncPlayer\mpv\yt-dlp.exe -> PATH
std::optional<fs::path> find_ytdl();

// bundled mpv\ffmpeg.exe -> install dir -> PATH  (ffmpeg is what lets yt-dlp merge the
// separate video and audio streams that anything above ~720p is published as)
std::optional<fs::path> find_ffmpeg();

// the bundled mpv directory, which is prepended to PATH so yt-dlp's own subprocesses
// find ffmpeg the way the Python build did
std::optional<fs::path> bundled_mpv_dir();

// A path to pass to mpv --script-opts=ytdl_hook-ytdl_path=...
std::string ytdl_path_option();

// ---------------------------------------------------------------------------
// small helpers
// ---------------------------------------------------------------------------
std::string to_utf8(const fs::path& p);
fs::path from_utf8(const std::string& s);
std::string read_text_file(const fs::path& p, bool* ok = nullptr);
bool write_text_file(const fs::path& p, const std::string& text);
std::string format_hms(double seconds, bool force_hours = false);

}  // namespace sp
