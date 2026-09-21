cmake_minimum_required(VERSION 3.24)
project(SyncPlayer VERSION 2.0.0 LANGUAGES CXX)

# C++20, MSVC + Ninja or Visual Studio. No third-party dependencies: the JSON layer,
# the IPC and the UI are all in-tree, so the app stays self-contained the way the
# Python build was (mpv, yt-dlp and ffmpeg ship alongside it as payload).
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

if(MSVC)
  add_compile_options(/W4 /permissive- /Zc:__cplusplus /utf-8 /EHsc /MP)
  add_compile_definitions(UNICODE _UNICODE NOMINMAX WIN32_LEAN_AND_MEAN)
endif()

option(SP_BUILD_TESTS "Build the smoke tests" ON)
option(SP_BUILD_APP "Build the Windows application (needs the UI layer)" ON)

# ---------------------------------------------------------------------------
# core: no UI. The Linux port reuses this unchanged; only platform/ and ui/ differ.
# ---------------------------------------------------------------------------
add_library(sp_core STATIC
  src/core/json.cpp
  src/core/paths.cpp
  src/core/config.cpp
  src/core/sync.cpp
  src/core/mpv.cpp
  src/core/mpv_assets.cpp
)
target_include_directories(sp_core PUBLIC src)

if(WIN32)
  target_sources(sp_core PRIVATE
    src/platform/win/window.cpp
  )
  target_link_libraries(sp_core PUBLIC
    user32 gdi32 dwmapi shlwapi ole32 shell32 advapi32 psapi
  )
endif()

# ---------------------------------------------------------------------------
# the application: a Win32 window drawn with Direct2D/DirectWrite, so controls are
# antialiased, rounded and themeable - the WinUI look, without the Windows App SDK
# runtime that the machine would otherwise have to carry.
# ---------------------------------------------------------------------------
if(WIN32 AND SP_BUILD_APP)
  add_executable(SyncPlayer WIN32
    src/main.cpp
    src/app/panel.cpp
    src/app/actions.cpp
    src/ui/renderer.cpp
    src/ui/theme.cpp
    src/ui/controls.cpp
  )
  target_link_libraries(SyncPlayer PRIVATE sp_core d2d1 dwrite)
  set_target_properties(SyncPlayer PROPERTIES
    OUTPUT_NAME "SyncPlayer"
    VS_DPI_AWARE "PerMonitor")
endif()

# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
if(SP_BUILD_TESTS)
  add_executable(sp_smoke tests/smoke.cpp)
  target_link_libraries(sp_smoke PRIVATE sp_core)
  enable_testing()
  add_test(NAME smoke COMMAND sp_smoke)
endif()
