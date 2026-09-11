#!/usr/bin/env bash
# Build the Linux release tarball: dist/SyncPlayer-<version>-linux.tar.gz
#
# The tarball is self-installing: unpack it and run ./install.sh
# (no PyInstaller needed - on Linux the app runs from source with python3,
# which is how distro packages ship Python apps anyway).
set -eu

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

VERSION="$(grep -m1 '^APP_VERSION' syncplayer.py | sed 's/.*"\(.*\)".*/\1/')"
OUT="dist"
NAME="SyncPlayer-${VERSION}-linux"
STAGE="$(mktemp -d)"
DIR="$STAGE/$NAME"

echo "building $NAME ..."
mkdir -p "$DIR" "$OUT"

for f in syncplayer.py sp_plat.py install.sh uninstall.sh README.md LICENSE icon.png; do
  [ -f "$f" ] && install -m 644 "$f" "$DIR/$f"
done
chmod 755 "$DIR/install.sh" "$DIR/uninstall.sh"
# NOTE: the test suites (selftest*.py, install_test_linux.py) and testmedia/
# are intentionally NOT shipped: this tarball is for people who want to run the
# app, and the 6 MB of test clips would dominate it. Run the tests from a git
# checkout instead.

tar -C "$STAGE" -czf "$OUT/$NAME.tar.gz" "$NAME"
rm -rf "$STAGE"
echo "wrote $OUT/$NAME.tar.gz ($(du -h "$OUT/$NAME.tar.gz" | cut -f1))"
echo
echo "users:  tar xzf $NAME.tar.gz && cd $NAME && ./install.sh"
