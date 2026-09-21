#include "core/config.h"

#include "core/json.h"
#include "core/paths.h"

namespace sp {

namespace {

// 1080p is the default because yt-dlp otherwise picks 2160p and stutters when the
// buffer runs dry; 137+140 needs ffmpeg to merge the separate streams.
const Quality kQualities[] = {
    {"Best available", "bestvideo+bestaudio/best", false},
    {"2160p", "bestvideo[height<=2160]+bestaudio/best[height<=2160]", true},
    {"1440p", "bestvideo[height<=1440]+bestaudio/best[height<=1440]", true},
    {"1080p", "bestvideo[height<=1080]+bestaudio/best[height<=1080]", true},
    {"720p", "bestvideo[height<=720]+bestaudio/best[height<=720]", true},
    {"480p", "bestvideo[height<=480]+bestaudio/best[height<=480]", true},
    {"360p", "bestvideo[height<=360]+bestaudio/best[height<=360]", true},
};

}  // namespace

std::vector<Quality> youtube_qualities() {
  return std::vector<Quality>(std::begin(kQualities), std::end(kQualities));
}

std::string Config::ytdl_format() const {
  for (const auto& q : youtube_qualities()) {
    if (youtube_quality == q.label) return q.expr;
  }
  return "bestvideo[height<=1080]+bestaudio/best[height<=1080]";
}

Config Config::load() {
  Config c;
  const fs::path path = config_dir() / "syncplayer_config.json";
  bool ok = false;
  const std::string text = read_text_file(path, &ok);
  if (!ok || text.empty()) return c;
  std::string err;
  Json j = json_parse(text, &err);
  if (!j.is_object()) return c;

  auto str = [&](const char* k, std::string& out) {
    if (j.has(k)) out = j[k].str_or(out);
  };
  auto num = [&](const char* k, double& out) {
    if (j.has(k)) out = j[k].numeric(out);
  };
  auto flag = [&](const char* k, bool& out) {
    if (j.has(k)) out = j[k].as_bool(out);
  };

  str("movie", c.movie);
  str("reaction", c.reaction);
  num("vol_a", c.vol_a);
  num("vol_b", c.vol_b);
  num("vol_m", c.vol_m);
  flag("vol_lbls", c.vol_lbls);
  num("speed", c.speed);
  num("jump_sec", c.jump_sec);
  str("youtube_quality", c.youtube_quality);
  if (j.has("download_connections")) {
    c.download_connections = static_cast<int>(j["download_connections"].as_int(8));
  }
  str("seek_mode", c.seek_mode);
  flag("show_readout", c.show_readout);

  if (j.has("window") && j["window"].is_object()) {
    const Json& w = j["window"];
    c.window.x = static_cast<int>(w["x"].as_int(0));
    c.window.y = static_cast<int>(w["y"].as_int(0));
    c.window.w = static_cast<int>(w["w"].as_int(0));
    c.window.h = static_cast<int>(w["h"].as_int(0));
  }

  // alignments: a map of "movie\x1freaction" -> offset
  if (j.has("alignments") && j["alignments"].is_object()) {
    std::vector<Alignment> items;
    for (const auto& key : j["alignments"].keys()) {
      items.push_back({key, j["alignments"][key].numeric(0.0)});
    }
    c.alignments.load(items);
  }
  return c;
}

bool Config::save() const {
  Json j = Json::object();
  j.set("movie", Json::str(movie));
  j.set("reaction", Json::str(reaction));
  j.set("vol_a", Json::number(vol_a));
  j.set("vol_b", Json::number(vol_b));
  j.set("vol_m", Json::number(vol_m));
  j.set("vol_lbls", Json::boolean(vol_lbls));
  j.set("speed", Json::number(speed));
  j.set("jump_sec", Json::number(jump_sec));
  j.set("youtube_quality", Json::str(youtube_quality));
  j.set("download_connections", Json::integer(download_connections));
  j.set("seek_mode", Json::str(seek_mode));
  j.set("show_readout", Json::boolean(show_readout));

  Json w = Json::object();
  w.set("x", Json::integer(window.x));
  w.set("y", Json::integer(window.y));
  w.set("w", Json::integer(window.w));
  w.set("h", Json::integer(window.h));
  j.set("window", w);

  Json aligns = Json::object();
  for (const auto& a : alignments.all()) {
    aligns.set(a.key, Json::number(a.offset));
  }
  j.set("alignments", aligns);

  return write_text_file(config_dir() / "syncplayer_config.json", j.dump(true));
}

}  // namespace sp
