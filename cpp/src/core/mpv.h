// One mpv instance: the process, its stdout beacon and the JSON IPC channel.
//
// Ported from MpvDriver + sp_plat.Ipc. Two details matter and are preserved:
//   * the mpv JSON-IPC transport is OS-specific (named pipe here, unix socket on
//     Linux), and
//   * on Windows the pipe is used with OVERLAPPED I/O, because the CRT poisons a pipe
//     handle that has been read and a synchronous write blocks forever once mpv's
//     reply buffer (a few KB) is full - which a drag-spammed volume or seek burst
//     fills in a second. Reads are drained by a reader thread and writes are
//     serialised, so the UI thread can never block on the pipe.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "core/json.h"
#include "core/paths.h"

namespace sp {

// ---------------------------------------------------------------------------
// the JSON IPC channel
// ---------------------------------------------------------------------------
class MpvIpc {
 public:
  MpvIpc();
  ~MpvIpc();

  // Waits for the pipe to appear, since mpv creates it a moment after starting.
  bool connect(const std::string& pipe_name, int timeout_ms = 10000,
               std::string* error = nullptr);
  void close();
  bool connected() const { return connected_.load(); }

  // Sends a request and waits for its reply. Returns null when it times out or the
  // channel died; never throws.
  Json request(const std::vector<Json>& command, int timeout_ms = 4000);

  // Fire and forget (used for high-rate updates where a reply is irrelevant).
  bool send_raw(const std::string& json_line);

  // Anything mpv pushes that is not a reply to us (property-change events) lands here.
  using EventHandler = std::function<void(const Json&)>;
  void set_event_handler(EventHandler h);

 private:
  void reader_loop();
  struct Impl;
  std::unique_ptr<Impl> impl_;
  std::atomic<bool> connected_{false};
  std::atomic<int64_t> next_id_{1};
  std::mutex mu_;
  std::condition_variable cv_;
  std::map<int64_t, Json> replies_;
  std::vector<int64_t> done_;
  EventHandler on_event_;
};

// ---------------------------------------------------------------------------
// launch options, mirroring the Python argument list
// ---------------------------------------------------------------------------
struct MpvOptions {
  std::string ipc_pipe;      // \\.\pipe\syncplayer_A
  std::string title;
  std::string lua_script;    // path to syncplayer_events.lua
  std::string input_conf;    // path to the generated input.conf
  std::string ytdl_path;     // --script-opts=ytdl_hook-ytdl_path=
  std::string ytdl_format;   // --ytdl-format=
  bool url_source = false;   // adds the YouTube-friendly buffering flags
  bool start_paused = true;  // Start loads paused, like the Python build
};

class MpvProcess {
 public:
  MpvProcess();
  ~MpvProcess();

  bool start(const fs::path& mpv_exe, const MpvOptions& opts, const std::string& source,
             std::string* error);
  void stop();
  bool running() const;

  // stdout beacon: the Lua script prints SYNCPOS|<pos> about ten times a second
  // because mpv's ${time-pos} status line is cached and too stale to drive the bars.
  std::optional<double> position() const;
  std::optional<double> duration() const;
  // mpv does not print the status line when stdout is a pipe, so duration is asked
  // for over the IPC. Returns nullopt while the file is still being parsed.
  std::optional<double> query_duration();
  // The position mpv itself reports, for checks that must not depend on the beacon.
  std::optional<double> query_position();
  bool paused() const;
  bool eof_reached() const;
  std::optional<double> volume() const;
  std::string last_status_line() const;
  std::string log_tail(int lines = 3) const;
  // state reported by the Lua beacon rather than by a property read
  bool pip_dragging() const;

  int pid() const { return pid_; }
  const std::string& ipc_pipe() const { return opts_.ipc_pipe; }

  MpvIpc& ipc() { return ipc_; }

  // convenience wrappers over the IPC, used by the panel
  bool play_pause(bool pause);
  bool set_speed(double speed);
  bool set_volume(double vol);
  bool seek_absolute(double seconds);
  bool step_frames(int frames);
  bool loadfile(const std::string& source, bool paused);

 private:
  void stdout_loop();
  void handle_line(const std::string& line);

  MpvOptions opts_;
  MpvIpc ipc_;
  void* process_ = nullptr;   // HANDLE
  void* thread_ = nullptr;    // HANDLE
  void* stdout_read_ = nullptr;
  void* stdout_write_ = nullptr;
  int pid_ = 0;
  std::thread reader_;
  std::atomic<bool> stop_{false};
  std::atomic<bool> running_{false};

  mutable std::mutex state_mu_;
  std::optional<double> pos_;
  std::optional<double> dur_;
  std::optional<double> vol_;
  bool paused_ = true;
  bool eof_ = false;
  bool pip_dragging_ = false;
  std::string last_status_;
  std::vector<std::string> log_;
};

// Writes the Lua beacon and the input.conf the app drives mpv with, and returns their
// paths. Content comes from cpp/assets, which was extracted from the Python source so
// the behaviour is identical.
struct MpvAssets {
  std::string lua_path;
  std::string input_conf_path;
};
MpvAssets write_mpv_assets();

}  // namespace sp
