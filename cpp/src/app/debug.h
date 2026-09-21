// A gated debug log: active only when SP_UI_DEBUG is set in the environment, so a
// shipped build never writes anything. Used to trace the input path.
#pragma once

#include <windows.h>

#include <cstdio>

namespace sp::app {

inline bool debug_on() {
  static const bool on = [] {
    wchar_t buf[8]{};
    return GetEnvironmentVariableW(L"SP_UI_DEBUG", buf, 8) > 0;
  }();
  return on;
}

inline void dbg(const char* what, long a = 0, long b = 0) {
  if (!debug_on()) return;
  wchar_t path[MAX_PATH]{};
  GetTempPathW(MAX_PATH, path);
  wcscat_s(path, L"sp_ui_debug.log");
  FILE* f = nullptr;
  if (_wfopen_s(&f, path, L"a") == 0 && f) {
    std::fprintf(f, "%s %ld %ld\n", what, a, b);
    std::fclose(f);
  }
}

}  // namespace sp::app
