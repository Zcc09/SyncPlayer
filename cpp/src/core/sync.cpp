#include "core/sync.h"

#include <algorithm>
#include <cmath>
#include <cstdio>

namespace sp {

double drift(std::optional<double> react_pos, std::optional<double> movie_pos,
             double sync_off) {
  if (!react_pos || !movie_pos) return 0.0;
  return *react_pos - (*movie_pos + sync_off);
}

double reaction_target(double movie_pos, double sync_off) { return movie_pos + sync_off; }

bool needs_correction(std::optional<double> react_pos, std::optional<double> movie_pos,
                      double sync_off, bool playing, bool dragging, bool movie_at_end,
                      bool react_at_end, double threshold) {
  if (!playing || dragging || movie_at_end || react_at_end) return false;
  if (!react_pos || !movie_pos) return false;
  return std::fabs(drift(react_pos, movie_pos, sync_off)) > threshold;
}

double micro_rate(double drift_s, double deadband, double max_pct, double gain) {
  if (std::isnan(drift_s) || std::fabs(drift_s) <= deadband) return 1.0;
  double delta = std::max(-max_pct, std::min(max_pct, -drift_s * gain));
  return 1.0 + delta;
}

double scrub_target(double pos0, int dx_px, int lift_px, std::optional<double> duration) {
  double gain = (lift_px >= kScrubLiftPx) ? kScrubFineGain : kScrubBaseGain;
  double target = pos0 + (static_cast<double>(dx_px) * gain);
  if (duration) return std::max(0.0, std::min(target, *duration));
  return std::max(0.0, target);
}

// ---------------------------------------------------------------------------
std::string alignment_key(const std::string& movie, const std::string& reaction) {
  return movie + "\x1f" + reaction;
}

void AlignmentStore::remember(const std::string& movie, const std::string& reaction,
                              double offset) {
  const std::string key = alignment_key(movie, reaction);
  for (auto it = items_.begin(); it != items_.end(); ++it) {
    if (it->key == key) {
      it->offset = offset;
      Alignment moved = *it;
      items_.erase(it);
      items_.push_back(moved);  // most recent last
      return;
    }
  }
  items_.push_back({key, offset});
  while (items_.size() > static_cast<size_t>(kAlignCap)) {
    items_.erase(items_.begin());
  }
}

void AlignmentStore::forget(const std::string& movie, const std::string& reaction) {
  const std::string key = alignment_key(movie, reaction);
  items_.erase(std::remove_if(items_.begin(), items_.end(),
                              [&](const Alignment& a) { return a.key == key; }),
               items_.end());
}

std::optional<double> AlignmentStore::find(const std::string& movie,
                                           const std::string& reaction) const {
  const std::string key = alignment_key(movie, reaction);
  for (const auto& a : items_) {
    if (a.key == key) return a.offset;
  }
  return std::nullopt;
}

void AlignmentStore::load(const std::vector<Alignment>& items) {
  items_.clear();
  for (const auto& a : items) items_.push_back(a);
  while (items_.size() > static_cast<size_t>(kAlignCap)) items_.erase(items_.begin());
}

std::string SyncStatus::describe() const {
  char buf[160];
  std::snprintf(buf, sizeof buf, "%s  Off %+.2fs  d %+.1fs%s%s",
                locked ? "locked" : "unlocked", offset, delta,
                (std::fabs(rate - 1.0) > 1e-6) ? "  rate " : "",
                (std::fabs(rate - 1.0) > 1e-6) ? "" : "");
  std::string out(buf);
  if (std::fabs(rate - 1.0) > 1e-6) {
    char r[32];
    std::snprintf(r, sizeof r, "%.3fx", rate);
    out += r;
  }
  return out;
}

}  // namespace sp
