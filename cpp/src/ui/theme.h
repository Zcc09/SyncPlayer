// Fluent/WinUI design tokens: colours, radii, metrics and type sizes.
//
// The palette follows the Fluent conventions rather than being invented: a layered
// background, cards with a 1px border, an accent that comes from the system, and
// Segoe UI Variable for type. Both themes are defined, and the one the system is
// already using is chosen at startup.
#pragma once

#include <d2d1.h>

#include <string>

namespace sp::ui {

struct Color {
  float r = 0, g = 0, b = 0, a = 1;
};

inline D2D1_COLOR_F to_d2d(Color c) { return D2D1::ColorF(c.r, c.g, c.b, c.a); }

// Fluent's radii: 4 for controls, 8 for cards and flyouts.
inline constexpr float kRadiusControl = 4.0f;
inline constexpr float kRadiusCard = 8.0f;

// Fluent's control metrics.
inline constexpr float kControlHeight = 32.0f;
inline constexpr float kControlHeightSmall = 24.0f;
inline constexpr float kGap = 8.0f;
inline constexpr float kPad = 16.0f;
inline constexpr float kRowGap = 6.0f;

struct Theme {
  bool dark = true;

  // surfaces
  Color window_bg;       // the window's own background (behind the Mica backdrop)
  Color layer;           // a section surface
  Color card;            // a card
  Color card_hover;
  Color border;          // 1px card border
  Color border_strong;   // focus rings, dividers with emphasis

  // text
  Color text;
  Color text_secondary;
  Color text_disabled;

  // accent
  Color accent;
  Color accent_hover;
  Color accent_pressed;
  Color on_accent;       // text on an accent fill

  // neutral controls (standard buttons)
  Color control;
  Color control_hover;
  Color control_pressed;
  Color control_disabled;

  // sliders and seek bars
  Color track;
  Color track_fill;
  Color track_hover;
  Color handle;

  Color danger;
  Color success;

  static Theme for_system(bool prefer_dark);
  static Theme dark_theme();
  static Theme light_theme();
};

// True when Windows is set to dark mode (HKCU ...\Themes\Personalize\AppsUseLightTheme).
bool system_prefers_dark();

// The user's accent colour, or Fluent's default blue.
Color system_accent();

// Reads a DWORD from HKCU; 0 when absent.
unsigned read_hkcu_dword(const wchar_t* subkey, const wchar_t* name, unsigned fallback);

}  // namespace sp::ui
