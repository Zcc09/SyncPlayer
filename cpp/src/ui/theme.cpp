#include "ui/theme.h"

#include <windows.h>

namespace sp::ui {

unsigned read_hkcu_dword(const wchar_t* subkey, const wchar_t* name, unsigned fallback) {
  HKEY key = nullptr;
  if (RegOpenKeyExW(HKEY_CURRENT_USER, subkey, 0, KEY_READ, &key) != ERROR_SUCCESS) {
    return fallback;
  }
  DWORD value = 0, size = sizeof value, type = 0;
  const LSTATUS rc = RegQueryValueExW(key, name, nullptr, &type,
                                      reinterpret_cast<LPBYTE>(&value), &size);
  RegCloseKey(key);
  if (rc != ERROR_SUCCESS || type != REG_DWORD) return fallback;
  return value;
}

bool system_prefers_dark() {
  // 0 means "use light mode" for apps
  const unsigned light = read_hkcu_dword(
      L"Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize",
      L"AppsUseLightTheme", 1u);
  return light == 0;
}

Color system_accent() {
  // DWM's colorization colour is stored as 0x00BBGGRR
  HKEY key = nullptr;
  if (RegOpenKeyExW(HKEY_CURRENT_USER, L"Software\\Microsoft\\Windows\\DWM", 0,
                    KEY_READ, &key) == ERROR_SUCCESS) {
    DWORD value = 0, size = sizeof value, type = 0;
    const LSTATUS rc = RegQueryValueExW(key, L"AccentColor", nullptr, &type,
                                        reinterpret_cast<LPBYTE>(&value), &size);
    RegCloseKey(key);
    if (rc == ERROR_SUCCESS && type == REG_DWORD) {
      const float b = static_cast<float>((value >> 16) & 0xFF) / 255.0f;
      const float g = static_cast<float>((value >> 8) & 0xFF) / 255.0f;
      const float r = static_cast<float>(value & 0xFF) / 255.0f;
      if (r + g + b > 0.05f) return {r, g, b, 1.0f};
    }
  }
  return {0.0f, 0.47f, 0.83f, 1.0f};  // Fluent's default blue
}

Theme Theme::dark_theme() {
  Theme t;
  t.dark = true;
  t.window_bg = {0.086f, 0.086f, 0.086f, 1.0f};   // #161616
  t.layer = {0.118f, 0.118f, 0.118f, 1.0f};       // #1E1E1E
  t.card = {0.145f, 0.145f, 0.145f, 1.0f};        // #252525
  t.card_hover = {0.176f, 0.176f, 0.176f, 1.0f};
  t.border = {1.0f, 1.0f, 1.0f, 0.08f};
  t.border_strong = {1.0f, 1.0f, 1.0f, 0.16f};
  t.text = {1.0f, 1.0f, 1.0f, 0.96f};
  t.text_secondary = {1.0f, 1.0f, 1.0f, 0.64f};
  t.text_disabled = {1.0f, 1.0f, 1.0f, 0.36f};
  t.on_accent = {0.0f, 0.0f, 0.0f, 0.94f};
  t.control = {1.0f, 1.0f, 1.0f, 0.06f};
  t.control_hover = {1.0f, 1.0f, 1.0f, 0.10f};
  t.control_pressed = {1.0f, 1.0f, 1.0f, 0.04f};
  t.control_disabled = {1.0f, 1.0f, 1.0f, 0.03f};
  t.track = {1.0f, 1.0f, 1.0f, 0.14f};
  t.track_fill = {1.0f, 1.0f, 1.0f, 0.38f};
  t.track_hover = {1.0f, 1.0f, 1.0f, 0.20f};
  t.handle = {1.0f, 1.0f, 1.0f, 0.90f};
  t.danger = {0.91f, 0.30f, 0.24f, 1.0f};
  t.success = {0.37f, 0.78f, 0.44f, 1.0f};
  return t;
}

Theme Theme::light_theme() {
  Theme t;
  t.dark = false;
  t.window_bg = {0.953f, 0.953f, 0.953f, 1.0f};   // #F3F3F3
  t.layer = {0.988f, 0.988f, 0.988f, 1.0f};
  t.card = {1.0f, 1.0f, 1.0f, 1.0f};
  t.card_hover = {0.976f, 0.976f, 0.976f, 1.0f};
  t.border = {0.0f, 0.0f, 0.0f, 0.08f};
  t.border_strong = {0.0f, 0.0f, 0.0f, 0.16f};
  t.text = {0.0f, 0.0f, 0.0f, 0.90f};
  t.text_secondary = {0.0f, 0.0f, 0.0f, 0.61f};
  t.text_disabled = {0.0f, 0.0f, 0.0f, 0.36f};
  t.on_accent = {1.0f, 1.0f, 1.0f, 0.98f};
  t.control = {1.0f, 1.0f, 1.0f, 0.70f};
  t.control_hover = {0.97f, 0.97f, 0.97f, 1.0f};
  t.control_pressed = {0.93f, 0.93f, 0.93f, 1.0f};
  t.control_disabled = {0.0f, 0.0f, 0.0f, 0.04f};
  t.track = {0.0f, 0.0f, 0.0f, 0.20f};
  t.track_fill = {0.0f, 0.0f, 0.0f, 0.45f};
  t.track_hover = {0.0f, 0.0f, 0.0f, 0.26f};
  t.handle = {0.13f, 0.13f, 0.13f, 0.90f};
  t.danger = {0.77f, 0.16f, 0.11f, 1.0f};
  t.success = {0.06f, 0.49f, 0.19f, 1.0f};
  return t;
}

Theme Theme::for_system(bool prefer_dark) {
  Theme t = prefer_dark ? dark_theme() : light_theme();
  const Color accent = system_accent();
  t.accent = accent;
  if (prefer_dark) {
    t.accent_hover = {accent.r * 1.15f > 1 ? 1.0f : accent.r * 1.15f,
                      accent.g * 1.15f > 1 ? 1.0f : accent.g * 1.15f,
                      accent.b * 1.15f > 1 ? 1.0f : accent.b * 1.15f, 1.0f};
    t.accent_pressed = {accent.r * 0.85f, accent.g * 0.85f, accent.b * 0.85f, 1.0f};
  } else {
    t.accent_hover = {accent.r * 0.92f, accent.g * 0.92f, accent.b * 0.92f, 1.0f};
    t.accent_pressed = {accent.r * 0.80f, accent.g * 0.80f, accent.b * 0.80f, 1.0f};
  }
  return t;
}

}  // namespace sp::ui
