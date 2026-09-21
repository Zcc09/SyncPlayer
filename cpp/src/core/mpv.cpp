#include "core/mpv.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <sstream>

#ifdef _WIN32
#include <windows.h>
#endif

namespace sp {

// Generated from the Python source (see cpp/tools/embed_assets.py) so the beacon and
// the key bindings are the same ones the Python build drives mpv with.
extern const char* const kLuaBeaconSource;
extern const char* const kInputConfSource;

namespace {

#ifdef _WIN32
std::string win_error(DWORD code) {
  char* buf = nullptr;
  DWORD n = FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                               FORMAT_MESSAGE_IGNORE_INSERTS,
                           nullptr, code, 0, reinterpret_cast<char*>(&buf), 0, nullptr);
  std::string out = (n && buf) ? std::string(buf, n) : ("error " + std::to_string(code));
  if (buf) LocalFree(buf);
  while (!out.empty() && (out.back() == '\n' || out.back() == '\r')) out.pop_back();
  return out;
}

std::wstring widen(const std::string& s) {
  if (s.empty()) return {};
  int need = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()),
                                 nullptr, 0);
  std::wstring out(static_cast<size_t>(need), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), out.data(), need);
  return out;
}

// Windows command-line quoting, the same rules CreateProcess expects.
void append_arg(std::wstring& out, const std::wstring& arg) {
  if (!out.empty()) out.push_back(L' ');
  if (!arg.empty() && arg.find_first_of(L" \t\"") == std::wstring::npos) {
    out += arg;
    return;
  }
  out.push_back(L'"');
  for (size_t i = 0; i < arg.size(); ++i) {
    size_t backslashes = 0;
    while (i < arg.size() && arg[i] == L'\\') {
      ++backslashes;
      ++i;
    }
    if (i == arg.size()) {
      out.append(backslashes * 2, L'\\');
    } else if (arg[i] == L'"') {
      out.append(backslashes * 2 + 1, L'\\');
      out.push_back(L'"');
    } else {
      out.append(backslashes, L'\\');
      out.push_back(arg[i]);
    }
  }
  out.push_back(L'"');
}
#else
std::string win_error(int code) { return "error " + std::to_string(code); }
#endif

// "\\.\pipe\name" for the named-pipe client
std::string pipe_path(const std::string& name) {
  if (name.rfind("\\\\.\\pipe\\", 0) == 0) return name;
  return "\\\\.\\pipe\\" + name;
}

std::optional<double> to_double(const std::string& s) {
  try {
    return std::stod(s);
  } catch (...) {
    return std::nullopt;
  }
}

}  // namespace

// ---------------------------------------------------------------------------
// MpvIpc
// ---------------------------------------------------------------------------
struct MpvIpc::Impl {
#ifdef _WIN32
  HANDLE pipe = INVALID_HANDLE_VALUE;
  HANDLE io_event = nullptr;   // overlapped reads need their own event
  OVERLAPPED ov{};
#else
  int fd = -1;
#endif
  std::thread reader;
  std::atomic<bool> stop{false};
  std::mutex write_mu;
  std::string rx_buffer;
};

MpvIpc::MpvIpc() : impl_(new Impl) {}

MpvIpc::~MpvIpc() { close(); }

bool MpvIpc::connect(const std::string& pipe_name, int timeout_ms, std::string* error) {
#ifdef _WIN32
  const std::string path = pipe_path(pipe_name);
  const std::wstring wpath = widen(path);
  const DWORD deadline = GetTickCount() + static_cast<DWORD>(timeout_ms);
  while (GetTickCount() < deadline) {
    // FILE_FLAG_OVERLAPPED on purpose: the reader must be cancellable, and CancelIoEx
    // cannot cancel a synchronous ReadFile.
    HANDLE h = CreateFileW(wpath.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr,
                           OPEN_EXISTING, FILE_FLAG_OVERLAPPED, nullptr);
    if (h != INVALID_HANDLE_VALUE) {
      DWORD mode = PIPE_READMODE_BYTE;
      SetNamedPipeHandleState(h, &mode, nullptr, nullptr);
      impl_->pipe = h;
      impl_->io_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
      impl_->ov = OVERLAPPED{};
      impl_->ov.hEvent = impl_->io_event;
      impl_->stop = false;
      connected_ = true;
      impl_->reader = std::thread([this] { reader_loop(); });
      return true;
    }
    const DWORD err = GetLastError();
    if (err != ERROR_FILE_NOT_FOUND && err != ERROR_PIPE_BUSY) {
      if (error) *error = "opening " + path + ": " + win_error(err);
      return false;
    }
    Sleep(100);
  }
  if (error) *error = "timed out waiting for mpv's IPC pipe " + path;
  return false;
#else
  (void)pipe_name;
  if (error) *error = "not implemented on this platform yet";
  return false;
#endif
}

void MpvIpc::close() {
  impl_->stop = true;
#ifdef _WIN32
  if (impl_->pipe != INVALID_HANDLE_VALUE) {
    // cancels the pending overlapped read, which is what releases the reader thread
    CancelIoEx(impl_->pipe, nullptr);
    if (impl_->io_event) SetEvent(impl_->io_event);
  }
#endif
  if (impl_->reader.joinable()) impl_->reader.join();
#ifdef _WIN32
  if (impl_->pipe != INVALID_HANDLE_VALUE) {
    CloseHandle(impl_->pipe);
    impl_->pipe = INVALID_HANDLE_VALUE;
  }
  if (impl_->io_event) {
    CloseHandle(impl_->io_event);
    impl_->io_event = nullptr;
  }
#endif
  connected_ = false;
  std::lock_guard<std::mutex> lk(mu_);
  replies_.clear();
  done_.clear();
  cv_.notify_all();
}

void MpvIpc::reader_loop() {
#ifdef _WIN32
  char buf[4096];
  while (!impl_->stop) {
    DWORD got = 0;
    ResetEvent(impl_->io_event);
    if (!ReadFile(impl_->pipe, buf, sizeof buf, &got, &impl_->ov)) {
      const DWORD err = GetLastError();
      if (err == ERROR_IO_PENDING) {
        // wait in slices so a stop request is noticed promptly
        while (!impl_->stop) {
          const DWORD w = WaitForSingleObject(impl_->io_event, 200);
          if (w == WAIT_OBJECT_0) break;
        }
        if (impl_->stop) break;
        if (!GetOverlappedResult(impl_->pipe, &impl_->ov, &got, FALSE)) {
          const DWORD rerr = GetLastError();
          if (rerr == ERROR_OPERATION_ABORTED || rerr == ERROR_BROKEN_PIPE ||
              rerr == ERROR_INVALID_HANDLE) {
            break;
          }
          continue;
        }
      } else if (err == ERROR_BROKEN_PIPE || err == ERROR_OPERATION_ABORTED ||
                 err == ERROR_INVALID_HANDLE) {
        break;
      } else if (err == ERROR_NO_DATA) {
        Sleep(10);
        continue;
      } else {
        Sleep(20);
        continue;
      }
    }
    if (got == 0) break;
    impl_->rx_buffer.append(buf, got);
    size_t nl;
    while ((nl = impl_->rx_buffer.find('\n')) != std::string::npos) {
      std::string line = impl_->rx_buffer.substr(0, nl);
      impl_->rx_buffer.erase(0, nl + 1);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (line.empty()) continue;
      std::string err;
      Json msg = json_parse(line, &err);
      if (msg.is_null() && !err.empty()) continue;
      if (msg.has("request_id")) {
        const int64_t id = msg["request_id"].as_int();
        std::lock_guard<std::mutex> lk(mu_);
        replies_[id] = msg;
        done_.push_back(id);
        cv_.notify_all();
      } else if (on_event_) {
        on_event_(msg);
      }
    }
  }
  connected_ = false;
  std::lock_guard<std::mutex> lk(mu_);
  cv_.notify_all();
#endif
}

Json MpvIpc::request(const std::vector<Json>& command, int timeout_ms) {
  if (!connected_.load()) return Json();
  const int64_t id = next_id_++;
  Json req = Json::object();
  Json arr = Json::array();
  for (const auto& c : command) arr.push_back(c);
  req.set("command", arr);
  req.set("request_id", Json::integer(id));
  if (!send_raw(req.dump())) return Json();

  std::unique_lock<std::mutex> lk(mu_);
  if (!cv_.wait_for(lk, std::chrono::milliseconds(timeout_ms), [&] {
        return done_.end() != std::find(done_.begin(), done_.end(), id) ||
               !connected_.load();
      })) {
    return Json();
  }
  auto it = replies_.find(id);
  if (it == replies_.end()) return Json();
  Json out = it->second;
  replies_.erase(it);
  done_.erase(std::remove(done_.begin(), done_.end(), id), done_.end());
  return out;
}

bool MpvIpc::send_raw(const std::string& json_line) {
#ifdef _WIN32
  if (impl_->pipe == INVALID_HANDLE_VALUE) return false;
  std::string payload = json_line;
  payload.push_back('\n');
  std::lock_guard<std::mutex> lk(impl_->write_mu);
  size_t sent = 0;
  while (sent < payload.size()) {
    DWORD wrote = 0;
    if (!WriteFile(impl_->pipe, payload.data() + sent,
                   static_cast<DWORD>(payload.size() - sent), &wrote, nullptr)) {
      return false;
    }
    if (wrote == 0) return false;
    sent += wrote;
  }
  return true;
#else
  (void)json_line;
  return false;
#endif
}

void MpvIpc::set_event_handler(EventHandler h) { on_event_ = std::move(h); }

// ---------------------------------------------------------------------------
// MpvProcess
// ---------------------------------------------------------------------------
MpvProcess::MpvProcess() = default;

MpvProcess::~MpvProcess() { stop(); }

bool MpvProcess::start(const fs::path& mpv_exe, const MpvOptions& opts,
                       const std::string& source, std::string* error) {
  opts_ = opts;
  if (!fs::exists(mpv_exe)) {
    if (error) *error = "mpv not found at " + to_utf8(mpv_exe);
    return false;
  }

#ifdef _WIN32
  // stdout pipe so the Lua beacon and the status line can be read
  SECURITY_ATTRIBUTES sa{};
  sa.nLength = sizeof sa;
  sa.bInheritHandle = TRUE;
  HANDLE rd = nullptr, wr = nullptr;
  if (!CreatePipe(&rd, &wr, &sa, 1 << 20)) {
    if (error) *error = "CreatePipe: " + win_error(GetLastError());
    return false;
  }
  SetHandleInformation(rd, HANDLE_FLAG_INHERIT, 0);
  stdout_read_ = rd;
  stdout_write_ = wr;

  // the argument list, matching the Python build's
  std::wstring cmd;
  append_arg(cmd, mpv_exe.wstring());
  auto add = [&](const std::string& s) { append_arg(cmd, widen(s)); };
  add("--no-config");
  add("--input-ipc-server=" + opts_.ipc_pipe);
  if (!opts_.input_conf.empty()) add("--input-conf=" + opts_.input_conf);
  if (!opts_.lua_script.empty()) add("--script=" + opts_.lua_script);
  add("--input-terminal=no");
  add("--terminal=yes");
  add("--term-osd=force");
  add("--no-term-osd-bar");
  // The Lua beacon drives the bars; this status line stays as a fallback readout.
  add("--term-status-msg=SYNCSTATUS|${time-pos}|${duration}|${percent-pos}|${pause}|"
      "${volume}|${playback-time}|${eof-reached}");
  add("--osc=no");
  add("--keep-open=yes");
  add("--keepaspect=yes");
  add("--keepaspect-window=no");   // free-form window resize, no aspect snap
  add("--hwdec=auto-safe");        // mpv 0.41 dropped the old "safe"
  add("--fs=no");
  add("--ytdl=yes");
  if (!opts_.ytdl_format.empty()) add("--ytdl-format=" + opts_.ytdl_format);
  add("--volume-max=150");
  add("--audio-pitch-correction=yes");  // keep pitch while the drift loop trims rate
  if (!opts_.ytdl_path.empty()) add("--script-opts=ytdl_hook-ytdl_path=" + opts_.ytdl_path);
  if (opts_.url_source) {
    add("--ytdl-raw-options=extractor-args=youtube:player_client=android");
    add("--cache=yes");
    add("--demuxer-max-bytes=150M");
    add("--demuxer-readahead-secs=30");
    add("--network-timeout=30");
    add("--stream-buffer-size=2MiB");
  }
  add("--force-window=yes");
  add("--title=" + opts_.title);
  if (opts_.start_paused) add("--pause=yes");
  if (!source.empty()) add(source);

  STARTUPINFOW si{};
  si.cb = sizeof si;
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdOutput = wr;
  si.hStdError = wr;
  si.hStdInput = nullptr;
  PROCESS_INFORMATION pi{};
  std::wstring mutable_cmd = cmd;
  const DWORD flags = CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT;
  if (!CreateProcessW(nullptr, mutable_cmd.data(), nullptr, nullptr, TRUE, flags,
                      nullptr, nullptr, &si, &pi)) {
    if (error) *error = "starting mpv: " + win_error(GetLastError());
    CloseHandle(rd);
    CloseHandle(wr);
    stdout_read_ = stdout_write_ = nullptr;
    return false;
  }
  process_ = pi.hProcess;
  thread_ = pi.hThread;
  pid_ = static_cast<int>(pi.dwProcessId);
  CloseHandle(wr);  // our copy; mpv holds the write end
  stdout_write_ = nullptr;
  running_ = true;
  stop_ = false;
  reader_ = std::thread([this] { stdout_loop(); });

  // mpv creates the pipe a moment after starting
  std::string ipc_error;
  if (!ipc_.connect(opts_.ipc_pipe, 15000, &ipc_error)) {
    if (error) *error = ipc_error;
    // the process may still be alive; leave it to stop()
    return false;
  }
  return true;
#else
  (void)source;
  if (error) *error = "not implemented on this platform yet";
  return false;
#endif
}

void MpvProcess::stop() {
  stop_ = true;
  ipc_.close();
#ifdef _WIN32
  // Terminate mpv BEFORE joining the stdout reader: its end of the pipe closing is
  // what makes the reader's ReadFile return, and a synchronous read cannot be
  // cancelled. Joining first deadlocks.
  if (running_ && process_) {
    TerminateProcess(static_cast<HANDLE>(process_), 0);
    WaitForSingleObject(static_cast<HANDLE>(process_), 3000);
  }
#endif
  if (reader_.joinable()) reader_.join();
#ifdef _WIN32
  if (process_) {
    CloseHandle(static_cast<HANDLE>(process_));
    process_ = nullptr;
  }
  if (thread_) {
    CloseHandle(static_cast<HANDLE>(thread_));
    thread_ = nullptr;
  }
  if (stdout_read_) {
    CloseHandle(static_cast<HANDLE>(stdout_read_));
    stdout_read_ = nullptr;
  }
#endif
  running_ = false;
}

bool MpvProcess::running() const { return running_.load(); }

void MpvProcess::stdout_loop() {
#ifdef _WIN32
  char buf[4096];
  std::string acc;
  while (!stop_) {
    DWORD got = 0;
    if (!ReadFile(static_cast<HANDLE>(stdout_read_), buf, sizeof buf, &got, nullptr)) {
      break;
    }
    if (got == 0) break;
    acc.append(buf, got);
    size_t nl;
    while ((nl = acc.find('\n')) != std::string::npos) {
      std::string line = acc.substr(0, nl);
      acc.erase(0, nl + 1);
      if (!line.empty() && line.back() == '\r') line.pop_back();
      if (!line.empty()) handle_line(line);
    }
    if (acc.size() > (1u << 20)) acc.clear();
  }
#endif
}

void MpvProcess::handle_line(const std::string& line) {
  // The Lua beacon prints SYNCPOS|1234.567, SYNCEOF|true, SYNCPAUSE|false and
  // SYNCPIPDRAG|..., but mpv prefixes Lua output with the script's name, so the marker
  // is looked for anywhere in the line rather than at the start:
  //     [syncplayer_events] SYNCPOS|0.000
  auto after = [&](const char* marker) -> std::optional<std::string> {
    const size_t at = line.find(marker);
    if (at == std::string::npos) return std::nullopt;
    return line.substr(at + std::strlen(marker));
  };

  if (auto v = after("SYNCPOS|")) {
    if (auto d = to_double(*v)) {
      std::lock_guard<std::mutex> lk(state_mu_);
      pos_ = d;
    }
    return;
  }
  if (auto v = after("SYNCEOF|")) {
    std::lock_guard<std::mutex> lk(state_mu_);
    eof_ = v->find("true") != std::string::npos;
    return;
  }
  if (auto v = after("SYNCPAUSE|")) {
    std::lock_guard<std::mutex> lk(state_mu_);
    // SYNCPAUSE|eof also counts as paused
    paused_ = v->find("false") == std::string::npos;
    return;
  }
  if (auto drag = after("SYNCPIPDRAG|")) {
    const std::string what = *drag;
    std::lock_guard<std::mutex> lk(state_mu_);
    if (what.rfind("start", 0) == 0) pip_dragging_ = true;
    else if (what.rfind("end", 0) == 0 || what.rfind("undock", 0) == 0) {
      pip_dragging_ = false;
    }
    return;
  }
  // the status line: mpv does not emit it when stdout is a pipe, so it is a bonus
  if (auto status = after("SYNCSTATUS|")) {
    std::vector<std::string> parts;
    std::stringstream ss(*status);
    std::string item;
    while (std::getline(ss, item, '|')) parts.push_back(item);
    std::lock_guard<std::mutex> lk(state_mu_);
    last_status_ = line;
    if (parts.size() >= 6) {
      if (auto v = to_double(parts[0])) pos_ = v;
      if (auto v = to_double(parts[1])) dur_ = v;
      if (auto v = to_double(parts[4])) vol_ = v;
      paused_ = parts[3] == "yes" || parts[3] == "true";
    }
    return;
  }
  std::lock_guard<std::mutex> lk(state_mu_);
  log_.push_back(line);
  if (log_.size() > 200) log_.erase(log_.begin(), log_.begin() + 100);
}

std::optional<double> MpvProcess::position() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return pos_;
}
std::optional<double> MpvProcess::duration() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return dur_;
}

std::optional<double> MpvProcess::query_position() {
  Json r = ipc_.request({Json::str("get_property"), Json::str("time-pos")});
  if (r.is_null() || !r["data"].is_number()) return std::nullopt;
  return r["data"].as_double();
}

std::optional<double> MpvProcess::query_duration() {
  Json r = ipc_.request({Json::str("get_property"), Json::str("duration")});
  if (r.is_null()) return std::nullopt;
  const Json& data = r["data"];
  if (!data.is_number()) return std::nullopt;
  const double d = data.as_double();
  if (!(d > 0.0)) return std::nullopt;
  {
    std::lock_guard<std::mutex> lk(state_mu_);
    dur_ = d;
  }
  return d;
}
bool MpvProcess::paused() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return paused_;
}
bool MpvProcess::eof_reached() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return eof_;
}
std::optional<double> MpvProcess::volume() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return vol_;
}
std::string MpvProcess::last_status_line() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return last_status_;
}
bool MpvProcess::pip_dragging() const {
  std::lock_guard<std::mutex> lk(state_mu_);
  return pip_dragging_;
}

std::string MpvProcess::log_tail(int lines) const {
  std::lock_guard<std::mutex> lk(state_mu_);
  std::string out;
  const int start = std::max(0, static_cast<int>(log_.size()) - lines);
  for (int i = start; i < static_cast<int>(log_.size()); ++i) {
    if (!out.empty()) out += " | ";
    out += log_[static_cast<size_t>(i)];
  }
  return out;
}

bool MpvProcess::play_pause(bool pause) {
  Json r = ipc_.request({Json::str("set_property"), Json::str("pause"),
                         Json::boolean(pause)});
  return !r.is_null();
}

bool MpvProcess::set_speed(double speed) {
  Json r = ipc_.request({Json::str("set_property"), Json::str("speed"),
                         Json::number(speed)});
  return !r.is_null();
}

bool MpvProcess::set_volume(double vol) {
  Json r = ipc_.request({Json::str("set_property"), Json::str("volume"),
                         Json::number(vol)});
  return !r.is_null();
}

bool MpvProcess::seek_absolute(double seconds) {
  Json r = ipc_.request({Json::str("seek"), Json::number(seconds),
                         Json::str("absolute+exact")});
  return !r.is_null();
}

bool MpvProcess::step_frames(int frames) {
  Json r = ipc_.request({Json::str("frame-step"), Json::integer(frames)});
  return !r.is_null();
}

bool MpvProcess::loadfile(const std::string& source, bool paused) {
  Json r = ipc_.request({Json::str("loadfile"), Json::str(source),
                         Json::str(paused ? "replace" : "replace")});
  return !r.is_null();
}

// ---------------------------------------------------------------------------
MpvAssets write_mpv_assets() {
  ensure_dirs();
  MpvAssets out;
  const fs::path lua = shot_dir() / "syncplayer_events.lua";
  const fs::path conf = shot_dir() / "input.conf";
  write_text_file(lua, kLuaBeaconSource);
  write_text_file(conf, kInputConfSource);
  out.lua_path = to_utf8(lua);
  out.input_conf_path = to_utf8(conf);
  return out;
}

}  // namespace sp
