#!/usr/bin/env bash
# Uninstall SyncPlayer (Linux): remove the app, launcher, menu entry and icon.
set -u
PREFIX="${SYNCPLAYER_PREFIX:-$HOME/.local/share/syncplayer}"
rm -rf "$PREFIX"
rm -f  "$HOME/.local/bin/syncplayer"
rm -f  "$HOME/.local/share/applications/syncplayer.desktop"
rm -f  "$HOME/.local/share/icons/hicolor/256x256/apps/syncplayer.png"
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
echo "SyncPlayer removed (app dir: $PREFIX)"
echo "Your config in ~/.config/syncplayer and screenshots in ~/Pictures/syncplayer were left alone."
