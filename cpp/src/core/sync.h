// The sync policy, ported from the Python implementation rather than re-derived.
// cpp/PORTING-REFERENCE.md carries the original source for every function here.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace sp {

// ---------------------------------------------------------------------------
// tunables (identical to the Python constants)
// ---------------------------------------------------------------------------
inline constexpr double kMicroDeadband = 0.06;   // s: aligned inside this
inline constexpr double kMicroMaxDrift = 0.8;    // s: above this, seek instead
inline constexpr double kMicroMaxPct = 0.05;     // cap the rate trim at +-5%
inline constexpr double kMicroGain = 0.10;       // rate delta per second of drift
inline constexpr double kMicroHold = 8.0;        // s: hold a trim before re-evaluating
inline constexpr double kDriftThreshold = 0.45;  // s: unlocked correction threshold
inline constexpr double kDriftThresholdLocked = 0.15;
inline constexpr double kScrubBaseGain = 0.5;    // s per pixel on the bar
inline constexpr double kScrubFineGain = 0.02;   // s per pixel once lifted
inline constexpr int kScrubLiftPx = 24;          // px above the bar to go fine
inline constexpr int kAlignCap = 60;             // remembered alignments kept
inline constexpr double kVolumeMax = 150.0;

enum class Side { Movie = 0, Reaction = 1 };

inline const char* side_name(Side s) {
  return s == Side::Movie ? "Movie" : "Reaction";
}

// ---------------------------------------------------------------------------
// the maths, pure functions on purpose: testable without a player
// ---------------------------------------------------------------------------

// How far the reaction has wandered from its aligned spot (seconds).
// Positive means it is ahead of where it should be.
double drift(std::optional<double> react_pos, std::optional<double> movie_pos,
             double sync_off);

// Where the reaction should be, given the movie's position and the offset.
double reaction_target(double movie_pos, double sync_off);

// True when the reaction should be pulled back into alignment. At end of file, while
// dragging, or while paused, nothing is corrected - the Python app does the same.
bool needs_correction(std::optional<double> react_pos, std::optional<double> movie_pos,
                      double sync_off, bool playing = true, bool dragging = false,
                      bool movie_at_end = false, bool react_at_end = false,
                      double threshold = kDriftThreshold);

// Playback-rate factor that absorbs drift without a seek. 1.0 inside the deadband,
// otherwise within [1-max_pct, 1+max_pct].
double micro_rate(double drift_s, double deadband = kMicroDeadband,
                  double max_pct = kMicroMaxPct, double gain = kMicroGain);

// Where a precise drag points, given how far the pointer moved.
double scrub_target(double pos0, int dx_px, int lift_px, std::optional<double> duration);

// ---------------------------------------------------------------------------
// per-pair alignment memory: keyed by BOTH sources, capped, like the Python dict
// ---------------------------------------------------------------------------
struct Alignment {
  std::string key;
  double offset = 0.0;
};

std::string alignment_key(const std::string& movie, const std::string& reaction);

class AlignmentStore {
 public:
  void remember(const std::string& movie, const std::string& reaction, double offset);
  void forget(const std::string& movie, const std::string& reaction);
  std::optional<double> find(const std::string& movie, const std::string& reaction) const;
  const std::vector<Alignment>& all() const { return items_; }
  void load(const std::vector<Alignment>& items);
  size_t size() const { return items_.size(); }

 private:
  std::vector<Alignment> items_;  // most recent last, trimmed to kAlignCap
};

// ---------------------------------------------------------------------------
// the live sync state of one reaction feed
// ---------------------------------------------------------------------------
struct SyncStatus {
  double offset = 0.0;       // the alignment in use
  double delta = 0.0;        // current drift
  double rate = 1.0;         // trim currently applied to the reaction
  bool locked = false;
  bool correcting = false;
  std::string describe() const;
};

}  // namespace sp
