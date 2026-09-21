#include "core/paths.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <sstream>

#ifdef _WIN32
#include <windows.h>
#include <shlobj.h>
#endif

namespace sp {
namespace {

#ifdef _WIN32
std::optional<fs::path> known_folder(REFKNOWNFOLDERID id) {
  PWSTR raw = nullptr;
  if (SUCCEEDED(SHGetKnownFolderPath(id, 0, nullptr, &raw)) && raw) {
    fs::path p(raw);
    CoTaskMemFree(raw);
    return p;
  }
  if (raw) CoTaskMemFree(raw);
  return std::nullopt;
}

std::string env(const char* name) {
  char buf[32768];
  DWORD n = GetEnvironmentVariableA(name, buf, sizeof buf);
  if (n == 0 || n >= sizeof buf) return {};
  return std::string(buf, n);
}
#endif

bool is_file(const fs::path& p) {
  std::error_code ec;
  return fs::is_regular_file(p, ec);
}

std::optional<fs::path> first_existing(const std::vector<fs::path>& candidates) {
  for (const auto& c : candidates) {
    if (is_file(c)) return c;
  }
  return std::nullopt;
}

// PATH lookup, mirroring shutil.which
std::optional<fs::path> which(const std::string& name) {
#ifdef _WIN32
  std::string path = env("PATH");
  std::stringstream ss(path);
  std::string dir;
  while (std::getline(ss, dir, ';')) {
    if (dir.empty()) continue;
    fs::path cand = fs::path(dir) / name;
    if (is_file(cand)) return cand;
  }
#endif
  return std::nullopt;
}

}  // namespace

std::string to_utf8(const fs::path& p) {
#ifdef _WIN32
  const std::wstring& w = p.native();
  if (w.empty()) return {};
  int need = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()),
                                 nullptr, 0, nullptr, nullptr);
  std::string out(static_cast<size_t>(need), '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.c_str(), static_cast<int>(w.size()), out.data(),
                      need, nullptr, nullptr);
  return out;
#else
  return p.string();
#endif
}

fs::path from_utf8(const std::string& s) {
#ifdef _WIN32
  if (s.empty()) return {};
  int need = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                 nullptr, 0);
  std::wstring out(static_cast<size_t>(need), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(),
                      need);
  return fs::path(out);
#else
  return fs::path(s);
#endif
}

fs::path config_dir() {
#ifdef _WIN32
  if (auto p = known_folder(FOLDERID_RoamingAppData)) return *p / "SyncPlayer";
  if (auto v = env("APPDATA"); !v.empty()) return fs::path(v) / "SyncPlayer";
#endif
  return fs::current_path() / "SyncPlayer";
}

fs::path shot_dir() {
#ifdef _WIN32
  if (auto p = known_folder(FOLDERID_Pictures)) return *p / "SyncPlayer";
#endif
  return config_dir() / "screenshots";
}

fs::path downloads_dir() {
#ifdef _WIN32
  // FOLDERID_Downloads is not on every SDK target; the user profile is enough
  if (auto p = known_folder(FOLDERID_Profile)) return *p / "Downloads" / "SyncPlayer";
#endif
  return config_dir() / "downloads";
}

fs::path default_install_dir() {
#ifdef _WIN32
  if (auto p = known_folder(FOLDERID_LocalAppData)) return *p / "Programs" / "SyncPlayer";
#endif
  return config_dir();
}

fs::path legacy_install_dir() {
#ifdef _WIN32
  if (auto p = known_folder(FOLDERID_LocalAppData)) return *p / "SyncPlayer";
#endif
  return config_dir();
}

fs::path app_base_dir() {
#ifdef _WIN32
  wchar_t buf[MAX_PATH * 4];
  DWORD n = GetModuleFileNameW(nullptr, buf, static_cast<DWORD>(std::size(buf)));
  if (n > 0) return fs::path(std::wstring(buf, n)).parent_path();
#endif
  return fs::current_path();
}

void ensure_dirs() {
  std::error_code ec;
  fs::create_directories(config_dir(), ec);
  fs::create_directories(shot_dir(), ec);
}

std::optional<fs::path> bundled_mpv_dir() {
  const fs::path base = app_base_dir();
  for (const auto& cand : {base / "mpv", legacy_install_dir() / "mpv",
                           default_install_dir() / "mpv"}) {
    if (is_file(cand / "mpv.exe")) return cand;
  }
  return std::nullopt;
}

std::optional<fs::path> find_mpv() {
  std::vector<fs::path> candidates;
  const fs::path base = app_base_dir();
  // bundled first: the installer ships mpv, and that copy is the tested one
  candidates.push_back(base / "mpv" / "mpv.exe");
  candidates.push_back(legacy_install_dir() / "mpv" / "mpv.exe");
  candidates.push_back(default_install_dir() / "mpv" / "mpv.exe");
  if (std::string mpv_env = env("MPV_PATH"); !mpv_env.empty()) {
    candidates.push_back(from_utf8(mpv_env));
  }
  if (auto on_path = which("mpv.exe")) candidates.push_back(*on_path);
#ifdef _WIN32
  candidates.push_back(fs::path("C:\\Program Files\\MPV Player\\mpv.exe"));
  candidates.push_back(fs::path("C:\\Program Files (x86)\\MPV Player\\mpv.exe"));
  if (auto pf = known_folder(FOLDERID_ProgramFiles)) {
    candidates.push_back(*pf / "MPV Player" / "mpv.exe");
  }
#endif
  return first_existing(candidates);
}

std::optional<fs::path> find_ytdl() {
  std::vector<fs::path> candidates;
  if (auto d = bundled_mpv_dir()) candidates.push_back(*d / "yt-dlp.exe");
  candidates.push_back(app_base_dir() / "yt-dlp.exe");
  candidates.push_back(legacy_install_dir() / "yt-dlp.exe");
  if (auto on_path = which("yt-dlp.exe")) candidates.push_back(*on_path);
  return first_existing(candidates);
}

std::optional<fs::path> find_ffmpeg() {
  std::vector<fs::path> candidates;
  // the bundled copy first, so "ffmpeg found" matches what the installer shipped
  if (auto d = bundled_mpv_dir()) candidates.push_back(*d / "ffmpeg.exe");
  candidates.push_back(app_base_dir() / "ffmpeg.exe");
  candidates.push_back(legacy_install_dir() / "ffmpeg.exe");
  if (auto on_path = which("ffmpeg.exe")) candidates.push_back(*on_path);
  return first_existing(candidates);
}

std::string ytdl_path_option() {
  if (auto y = find_ytdl()) return to_utf8(*y);
  return {};
}

std::string read_text_file(const fs::path& p, bool* ok) {
  std::ifstream in(p, std::ios::binary);
  if (!in) {
    if (ok) *ok = false;
    return {};
  }
  std::ostringstream ss;
  ss << in.rdbuf();
  if (ok) *ok = true;
  return ss.str();
}

bool write_text_file(const fs::path& p, const std::string& text) {
  std::error_code ec;
  fs::create_directories(p.parent_path(), ec);
  std::ofstream out(p, std::ios::binary | std::ios::trunc);
  if (!out) return false;
  out.write(text.data(), static_cast<std::streamsize>(text.size()));
  return static_cast<bool>(out);
}

std::string format_hms(double seconds, bool force_hours) {
  if (!(seconds > 0)) seconds = 0;
  long total = static_cast<long>(seconds);
  long h = total / 3600, m = (total % 3600) / 60, s = total % 60;
  char buf[32];
  if (h > 0 || force_hours) {
    std::snprintf(buf, sizeof buf, "%02ld:%02ld:%02ld", h, m, s);
  } else {
    std::snprintf(buf, sizeof buf, "%02ld:%02ld", m, s);
  }
  return buf;
}

}  // namespace sp
