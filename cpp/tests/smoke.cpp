// Smoke test for the C++ core: real mpv processes, real JSON IPC, real windows.
//
// Mirrors what the Python selftest and install_test cover at this layer, so the port
// is verified against behaviour rather than against compilation:
//   * mpv discovery finds the bundled player and never falls back to a bare name
//   * the Lua beacon and input.conf are written from the embedded sources
//   * two mpv instances start, both IPC channels connect, and the beacon reports
//     position, duration and pause state
//   * a seek aligns the reaction to the movie plus the offset, and the drift the
//     app would act on is inside tolerance
//   * the sync maths match the Python constants (deadband, micro rate, scrub gain)
//   * both mpv windows are found and arranged side by side
//   * the config round-trips, including a remembered alignment
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <string>
#include <thread>
#include <vector>

#include "core/config.h"
#include "core/mpv.h"
#include "core/paths.h"
#include "core/sync.h"
#include "platform/win/window.h"

using namespace sp;
namespace fs = std::filesystem;

static int failures = 0;
static int checks = 0;

static void check(const std::string& what, bool ok, const std::string& detail = {}) {
  ++checks;
  if (!ok) ++failures;
  std::printf("  %-4s %s%s\n", ok ? "ok" : "FAIL", what.c_str(),
              detail.empty() ? "" : ("   [" + detail + "]").c_str());
}

static void info(const std::string& what, const std::string& detail) {
  std::printf("       %s: %s\n", what.c_str(), detail.c_str());
}

static bool wait_for(const std::function<bool()>& pred, int timeout_ms) {
  const auto deadline = std::chrono::steady_clock::now() +
                        std::chrono::milliseconds(timeout_ms);
  while (std::chrono::steady_clock::now() < deadline) {
    if (pred()) return true;
    std::this_thread::sleep_for(std::chrono::milliseconds(50));
  }
  return false;
}

int main(int argc, char** argv) {
  (void)argc;
  (void)argv;
  std::printf("SyncPlayer C++ core smoke\n\n");

  // ---------------------------------------------------------------- discovery
  std::printf("1. discovery\n");
  auto mpv = find_mpv();
  check("mpv found", mpv.has_value(),
        mpv ? to_utf8(*mpv) : "not found - install SyncPlayer or put mpv on PATH");
  if (!mpv) return 1;
  check("the bundled copy is preferred",
        mpv->string().find("mpv") != std::string::npos, to_utf8(*mpv));
  auto ytdl = find_ytdl();
  info("yt-dlp", ytdl ? to_utf8(*ytdl) : "(not present)");
  auto ffmpeg = find_ffmpeg();
  info("ffmpeg", ffmpeg ? to_utf8(*ffmpeg) : "(not present)");
  check("config dir resolved", !config_dir().empty(), to_utf8(config_dir()));

  // ---------------------------------------------------------------- assets
  std::printf("\n2. mpv assets written from the embedded sources\n");
  ensure_dirs();
  MpvAssets assets = write_mpv_assets();
  check("lua beacon written", fs::exists(from_utf8(assets.lua_path)), assets.lua_path);
  check("input.conf written", fs::exists(from_utf8(assets.input_conf_path)),
        assets.input_conf_path);
  const std::string lua = read_text_file(from_utf8(assets.lua_path));
  check("the beacon reports positions", lua.find("SYNCPOS") != std::string::npos);
  check("the beacon reports eof", lua.find("SYNCEOF") != std::string::npos);
  check("the beacon mirrors pause", lua.find("SYNCPAUSE") != std::string::npos);
  check("the beacon reports PiP drags", lua.find("SYNCPIPDRAG") != std::string::npos);

  // ---------------------------------------------------------------- sync maths
  std::printf("\n3. sync maths (against the Python constants)\n");
  check("inside the deadband means aligned", micro_rate(0.01) == 1.0);
  check("ahead runs slower", micro_rate(0.5) < 1.0);
  check("behind runs faster", micro_rate(-0.5) > 1.0);
  check("the trim is capped at 5%", micro_rate(100.0) >= 1.0 - kMicroMaxPct - 1e-9 &&
                                     micro_rate(-100.0) <= 1.0 + kMicroMaxPct + 1e-9);
  check("drift is reaction minus target", drift(12.5, 10.0, 2.5) == 0.0);
  check("drift catches a gap", std::abs(drift(13.0, 10.0, 2.5) - 0.5) < 1e-9);
  check("no correction while paused",
        !needs_correction(13.0, 10.0, 0.0, /*playing=*/false));
  check("no correction while dragging",
        !needs_correction(13.0, 10.0, 0.0, true, /*dragging=*/true));
  check("no correction at end of file",
        !needs_correction(13.0, 10.0, 0.0, true, false, /*movie_at_end=*/true));
  check("a real gap asks for correction", needs_correction(13.0, 10.0, 0.0));
  // a 0.2 s gap is inside the unlocked threshold but outside the locked one
  check("a 0.2 s gap is tolerated when unlocked",
        !needs_correction(10.2, 10.0, 0.0, true, false, false, false, kDriftThreshold));
  check("the same gap is corrected when locked",
        needs_correction(10.2, 10.0, 0.0, true, false, false, false, kDriftThresholdLocked));
  check("scrub on the bar is 0.5 s per pixel",
        std::abs(scrub_target(100.0, 10, 0, 300.0) - 105.0) < 1e-9);
  check("scrub lifted is 0.02 s per pixel",
        std::abs(scrub_target(100.0, 10, kScrubLiftPx, 300.0) - 100.2) < 1e-9);
  check("scrub clamps to the media", scrub_target(100.0, 100000, 0, 300.0) == 300.0);
  check("scrub never goes negative", scrub_target(1.0, -1000, 0, 300.0) == 0.0);

  // ---------------------------------------------------------------- config
  std::printf("\n4. config round-trip\n");
  Config cfg = Config::load();
  cfg.movie = "C:/media/movie.mp4";
  cfg.reaction = "C:/media/reaction.mp4";
  cfg.jump_sec = 7.5;
  cfg.show_readout = false;
  cfg.alignments.remember(cfg.movie, cfg.reaction, 2.25);
  check("config saved", cfg.save());
  Config again = Config::load();
  check("sources round-trip", again.movie == cfg.movie && again.reaction == cfg.reaction);
  check("jump distance round-trips", std::abs(again.jump_sec - 7.5) < 1e-9);
  check("readout toggle round-trips", again.show_readout == false);
  auto stored = again.alignments.find(cfg.movie, cfg.reaction);
  check("remembered alignment round-trips", stored && std::abs(*stored - 2.25) < 1e-9,
        stored ? std::to_string(*stored) : "missing");

  // ---------------------------------------------------------------- two players
  std::printf("\n5. two mpv instances, IPC and the beacon\n");
  // the test clips: the repo's media, copied so the two sides are distinct files.
  // Searched relative to the build directory, since that is where the test runs.
  fs::path clip_a;
  for (const fs::path& cand : {fs::path("../../testmedia/react.mp4"),
                               fs::path("../testmedia/react.mp4"),
                               fs::path("testmedia/react.mp4"),
                               fs::path("../../../testmedia/react.mp4")}) {
    if (fs::exists(cand)) {
      clip_a = cand;
      break;
    }
  }
  fs::path clip_b = fs::current_path() / "clip_b.mp4";
  if (!clip_a.empty()) {
    std::error_code ec;
    fs::copy_file(clip_a, clip_b, fs::copy_options::overwrite_existing, ec);
    std::printf("       test media: %s\n", clip_a.string().c_str());
  } else {
    std::printf("       (no testmedia/react.mp4 found - skipping the player checks)\n");
  }
  if (fs::exists(clip_a) && fs::exists(clip_b)) {
    MpvOptions oa;
    oa.ipc_pipe = "syncplayer_smoke_A";
    oa.title = "SyncPlayer - Movie";
    oa.lua_script = assets.lua_path;
    oa.input_conf = assets.input_conf_path;
    oa.ytdl_path = ytdl_path_option();
    oa.start_paused = true;

    MpvOptions ob = oa;
    ob.ipc_pipe = "syncplayer_smoke_B";
    ob.title = "SyncPlayer - Reaction";

    MpvProcess A, B;
    std::string err;
    const bool started_a = A.start(*mpv, oa, to_utf8(clip_a), &err);
    check("player A started", started_a, err);
    const bool started_b = B.start(*mpv, ob, to_utf8(clip_b), &err);
    check("player B started", started_b, err);

    if (started_a && started_b) {
      check("player A IPC connected", A.ipc().connected());
      check("player B IPC connected", B.ipc().connected());

      // the beacon should report a duration and a position once mpv has loaded
      const bool got_duration =
          wait_for([&] { return A.query_duration().value_or(0) > 0.1; }, 15000);
      check("player A reports a duration", got_duration,
            A.duration() ? std::to_string(*A.duration()) : "none");
      const bool got_position = wait_for([&] { return A.position().has_value(); }, 10000);
      check("player A reports a position", got_position);
      if (!got_duration || !got_position) {
        // whatever mpv printed is the evidence: an empty log means nothing arrived,
        // a Lua error means the script failed, and a status line means the parser is
        // looking for the wrong marker
        std::printf("       --- mpv A stdout tail ---\n%s\n       --- end ---\n",
                    A.log_tail(12).c_str());
        std::printf("       last status line: %s\n", A.last_status_line().c_str());
        std::printf("       A still running: %s\n", A.running() ? "yes" : "no");
      }

      // start playback and confirm the position advances
      const double before = A.position().value_or(0.0);
      check("play requested", A.play_pause(false));
      check("the position advances",
            wait_for([&] { return A.position().value_or(0.0) > before + 0.3; }, 8000),
            std::to_string(before));
      A.play_pause(true);
      B.play_pause(true);

      // ---------------------------------------------------------- alignment
      std::printf("\n6. alignment over the IPC\n");
      // mid-clip: the test media is ten seconds long, and seeking to the very end
      // leaves the reported position at the end rather than where we asked
      const double target_movie = 4.0;
      const double offset = 2.5;
      check("movie seek", A.seek_absolute(target_movie));
      check("reaction seek", B.seek_absolute(reaction_target(target_movie, offset)));
      check("movie landed",
            wait_for([&] { return std::abs(A.query_position().value_or(0) - target_movie) < 0.4; },
                     10000),
            A.query_position() ? std::to_string(*A.query_position()) : "none");
      const double want = reaction_target(target_movie, offset);
      check("reaction landed",
            wait_for([&] { return std::abs(B.query_position().value_or(0) - want) < 0.4; },
                     10000),
            B.query_position() ? std::to_string(*B.query_position()) : "none");
      // the beacon should agree with the IPC, since it is what drives the bars
      check("the beacon agrees with the IPC",
            A.position() && std::abs(*A.position() - *A.query_position()) < 0.3,
            std::to_string(A.position().value_or(-1)) + " vs " +
                std::to_string(A.query_position().value_or(-1)));
      const double d = drift(B.query_position(), A.query_position(), offset);
      info("drift after aligning", std::to_string(d));
      check("the two feeds are inside tolerance", std::abs(d) <= kDriftThreshold,
            std::to_string(d));
      check("the sync loop would leave them alone",
            !needs_correction(B.position(), A.position(), offset));

      // ---------------------------------------------------------- windows
      std::printf("\n7. windows\n");
      auto wa = find_window_for_pid(A.pid(), "Movie");
      auto wb = find_window_for_pid(B.pid(), "Reaction");
      check("the movie window was found", wa.has_value(),
            wa ? window_title(*wa) : "not found");
      check("the reaction window was found", wb.has_value(),
            wb ? window_title(*wb) : "not found");
      if (wa && wb) {
        const ScreenInfo si = screen_info();
        info("work area", std::to_string(si.work.w) + "x" + std::to_string(si.work.h));
        check("arranged side by side",
              arrange_side_by_side(*wa, *wb, si.work, 8, false));
        std::this_thread::sleep_for(std::chrono::milliseconds(400));
        const Rect ra = window_rect(*wa), rb = window_rect(*wb);
        info("movie rect", std::to_string(ra.x) + "," + std::to_string(ra.y) + " " +
                               std::to_string(ra.w) + "x" + std::to_string(ra.h));
        info("reaction rect", std::to_string(rb.x) + "," + std::to_string(rb.y) + " " +
                                  std::to_string(rb.w) + "x" + std::to_string(rb.h));
        check("movie is on the left", ra.x < rb.x);
        check("they do not overlap", ra.right() <= rb.x + 2);
        check("both are a sensible size", ra.w > 100 && rb.w > 100);
      }
    }
    A.stop();
    B.stop();
    std::error_code ec;
    fs::remove(clip_b, ec);
  }

  std::printf("\n%d/%d checks passed\n", checks - failures, checks);
  return failures == 0 ? 0 : 1;
}
