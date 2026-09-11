#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# SyncPlayer - Linux installer
#
#   ./install.sh [--prefix DIR] [--no-ytdlp] [--no-deps] [--yes]
#
# Installs the app to  ~/.local/share/syncplayer  (override with --prefix or
# $SYNCPLAYER_PREFIX), creates a launcher in ~/.local/bin and a desktop entry,
# and can fetch a private copy of yt-dlp so YouTube links work out of the box.
# ---------------------------------------------------------------------------
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${SYNCPLAYER_PREFIX:-$HOME/.local/share/syncplayer}"
BINDIR="$HOME/.local/bin"
APPSDIR="$HOME/.local/share/applications"
ICONDIR="$HOME/.local/share/icons/hicolor/256x256/apps"
WANT_YTDLP=1
CHECK_DEPS=1
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --prefix)     PREFIX="$2"; shift 2 ;;
    --no-ytdlp)   WANT_YTDLP=0; shift ;;
    --no-deps)    CHECK_DEPS=0; shift ;;
    --yes|-y)     ASSUME_YES=1; shift ;;
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

say "SyncPlayer installer (Linux)"
say "  install dir : $PREFIX"
say "  launcher    : $BINDIR/syncplayer"
say ""

# ---------------------------------------------------------------- deps -----
MISSING_HINTS=""
add_hint() { MISSING_HINTS="${MISSING_HINTS}    $1\n"; }

if [ "$CHECK_DEPS" = 1 ]; then
  say "checking dependencies..."

  if ! command -v python3 >/dev/null 2>&1; then
    add_hint "python3 is required (python3, python3-tk)"
    warn "python3 not found"
  else
    say "  ok python3 $(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)"
  fi

  if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    case "$( (. /etc/os-release 2>/dev/null; echo "${ID:-}") )" in
      debian|ubuntu|linuxmint|pop) add_hint "sudo apt install python3-tk" ;;
      arch|cachyos|manjaro|endeavouros) add_hint "sudo pacman -S tk" ;;
      fedora|rhel|centos) add_hint "sudo dnf install python3-tkinter" ;;
      opensuse*|sles) add_hint "sudo zypper install python3-tk" ;;
      *) add_hint "install your distro's python3 tkinter package (e.g. python3-tk)" ;;
    esac
    warn "python3 tkinter is MISSING (the app needs it for its window)"
  else
    say "  ok tkinter"
  fi

  if ! command -v mpv >/dev/null 2>&1 && [ ! -x "$PREFIX/mpv/mpv" ]; then
    case "$( (. /etc/os-release 2>/dev/null; echo "${ID:-}") )" in
      debian|ubuntu|linuxmint|pop) add_hint "sudo apt install mpv" ;;
      arch|cachyos|manjaro|endeavouros) add_hint "sudo pacman -S mpv" ;;
      fedora|rhel|centos) add_hint "sudo dnf install mpv" ;;
      opensuse*|sles) add_hint "sudo zypper install mpv" ;;
      *) add_hint "install mpv with your package manager" ;;
    esac
    warn "mpv is MISSING"
  else
    say "  ok mpv"
  fi

  if ! command -v yt-dlp >/dev/null 2>&1; then
    if [ "$WANT_YTDLP" = 1 ]; then
      say "  .. yt-dlp not installed - a private copy will be bundled"
    else
      warn "yt-dlp missing and --no-ytdlp was given: YouTube links will not play"
    fi
  else
    say "  ok yt-dlp"
  fi

  if ! ldconfig -p 2>/dev/null | grep -q 'libX11\.so'; then
    warn "libX11 not found - window arranging / PiP will be unavailable"
  else
    say "  ok libX11 (window control)"
  fi
  say ""
fi

if [ -n "$MISSING_HINTS" ]; then
  say "Missing dependencies - install them first:"
  printf "%b" "$MISSING_HINTS"
  say ""
  if [ "$ASSUME_YES" != 1 ]; then
    printf "Continue anyway? [y/N] "
    read -r ans
    case "$ans" in y|Y|yes|YES) ;; *) say "aborted."; exit 1 ;; esac
  fi
  say ""
fi

# -------------------------------------------------------------- files ------
say "installing files -> $PREFIX"
mkdir -p "$PREFIX/mpv" "$BINDIR" "$APPSDIR" "$ICONDIR"

install -m 644 "$SCRIPT_DIR/syncplayer.py" "$PREFIX/syncplayer.py"
install -m 644 "$SCRIPT_DIR/sp_plat.py"   "$PREFIX/sp_plat.py"
for extra in README.md LICENSE; do
  [ -f "$SCRIPT_DIR/$extra" ] && install -m 644 "$SCRIPT_DIR/$extra" "$PREFIX/$extra"
done
if [ -f "$SCRIPT_DIR/icon.png" ]; then
  install -m 644 "$SCRIPT_DIR/icon.png" "$ICONDIR/syncplayer.png"
  install -m 644 "$SCRIPT_DIR/icon.png" "$PREFIX/icon.png"
fi
say "  ok app files"

# optional: bundle yt-dlp beside the app (mirrors the Windows installer, which
# ships yt-dlp next to mpv so a fresh machine can play YouTube links)
if [ "$WANT_YTDLP" = 1 ] && ! command -v yt-dlp >/dev/null 2>&1 \
   && [ ! -x "$PREFIX/mpv/yt-dlp" ]; then
  say "fetching yt-dlp (self-contained YouTube support)..."
  if command -v curl >/dev/null 2>&1; then
    if curl -fsSL -o "$PREFIX/mpv/yt-dlp" \
        "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"; then
      chmod 755 "$PREFIX/mpv/yt-dlp"
      say "  ok yt-dlp -> $PREFIX/mpv/yt-dlp"
    else
      warn "could not download yt-dlp (install it from your distro instead)"
    fi
  else
    warn "curl not found - skipping the yt-dlp download"
  fi
fi

# ------------------------------------------------------------ launcher -----
cat > "$BINDIR/syncplayer" <<EOF
#!/usr/bin/env bash
# SyncPlayer launcher (generated by install.sh)
exec python3 "$PREFIX/syncplayer.py" "\$@"
EOF
chmod 755 "$BINDIR/syncplayer"
say "  ok launcher $BINDIR/syncplayer"

# --------------------------------------------------------- desktop file ----
DESKTOP_EXEC="$BINDIR/syncplayer"
cat > "$APPSDIR/syncplayer.desktop" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=SyncPlayer
GenericName=Video Sync Player
Comment=Watch a movie together with its reaction video in perfect sync
Exec=$DESKTOP_EXEC %F
Icon=syncplayer
Terminal=false
Categories=AudioVideo;Video;Player;
Keywords=video;sync;reaction;mpv;youtube;side-by-side;
MimeType=video/mp4;video/x-matroska;video/webm;video/x-msvideo;video/quicktime;
StartupNotify=true
EOF
chmod 644 "$APPSDIR/syncplayer.desktop"
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPSDIR" >/dev/null 2>&1 || true
say "  ok desktop entry (app menu / 'Open with')"

# ----------------------------------------------------------- metadata ------
MPVVER="$(mpv --version 2>/dev/null | head -1 | awk '{print $2}' || echo unknown)"
PYVER="$(python3 -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])' 2>/dev/null || echo unknown)"
cat > "$PREFIX/install.json" <<EOF
{
  "app_version": "1.5.0",
  "platform": "linux",
  "install_dir": "$PREFIX",
  "python": "$PYVER",
  "mpv": "$MPVVER",
  "installed_at": "$(date -Iseconds 2>/dev/null || date)"
}
EOF
say "  ok metadata"

# ------------------------------------------------------------- summary -----
say ""
say "SyncPlayer installed."
say "  run:      syncplayer            (or: python3 $PREFIX/syncplayer.py)"
case ":$PATH:" in
  *":$BINDIR:"*) ;;
  *) say "  note:     $BINDIR is not on your PATH - add it, or use the full path" ;;
esac
say "  check:    syncplayer --check-env"
say "  uninstall: $SCRIPT_DIR/uninstall.sh   (or delete $PREFIX, $BINDIR/syncplayer,"
say "             $APPSDIR/syncplayer.desktop)"
