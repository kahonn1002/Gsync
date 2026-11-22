#!/bin/bash
# Installs Gsync on macOS: checks the Homebrew packages and adds ~/Applications/Gsync.app
# (an app that runs gsync.py from this folder; rerun this script if you move the folder).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

BREW="$(command -v brew || true)"
for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -z "$BREW" ] && [ -x "$b" ] && BREW="$b"
done
if [ -z "$BREW" ]; then
  echo "Homebrew is required. Install it from https://brew.sh and run this script again." >&2
  exit 1
fi
PREFIX="$("$BREW" --prefix)"

missing=()
for f in rclone gtk+3 pygobject3 adwaita-icon-theme; do
  "$BREW" list --versions "$f" >/dev/null 2>&1 || missing+=("$f")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Installing with Homebrew: ${missing[*]}"
  "$BREW" install "${missing[@]}"
fi

# Homebrew's python3, never /usr/bin/python3: only Homebrew's has the GTK bindings.
PY="$PREFIX/bin/python3"
if ! "$PY" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "$PY cannot load GTK 3. Try: $BREW reinstall pygobject3 gtk+3" >&2
  exit 1
fi

# Gsync.app runs gsync.py inside its own process (see mac_launcher.c), so macOS shows it as Gsync
# with its own icon, notifications and privacy prompts, rather than as "Python".
case "$DIR" in *\"*|*\\*)
  echo "Move this folder to a path without \" or \\ in it, then run this script again." >&2; exit 1;;
esac
if ! xcrun --find clang >/dev/null 2>&1; then
  echo "The app launcher needs Apple's command line tools. Run:  xcode-select --install" >&2
  exit 1
fi
APP="$HOME/Applications/Gsync.app"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
mkdir -p "$BUILD/Gsync.app/Contents/MacOS" "$BUILD/Gsync.app/Contents/Resources"
xcrun clang -O2 -Wall -Werror -framework CoreFoundation -Wl,-rpath,"$PREFIX/lib" \
  -DPYTHON_LIB="\"$PREFIX/opt/python3/Frameworks/Python.framework/Versions/Current/Python\"" \
  -DGSYNC_SCRIPT="\"$DIR/gsync.py\"" \
  "$DIR/mac_launcher.c" -o "$BUILD/Gsync.app/Contents/MacOS/Gsync"
"$PY" "$DIR/mac_icons.py" "$BUILD/Gsync.iconset"
iconutil -c icns "$BUILD/Gsync.iconset" -o "$BUILD/Gsync.app/Contents/Resources/Gsync.icns"
cat > "$BUILD/Gsync.app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Gsync</string>
  <key>CFBundleDisplayName</key><string>Gsync</string>
  <key>CFBundleIdentifier</key><string>local.gsync</string>
  <key>CFBundleExecutable</key><string>Gsync</string>
  <key>CFBundleIconFile</key><string>Gsync</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign - "$BUILD/Gsync.app"  # ad-hoc: Apple silicon only runs signed code
mkdir -p "$HOME/Applications"
rm -rf "$APP"
mv "$BUILD/Gsync.app" "$APP"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
chmod +x "$DIR/gsync.py"

echo "Installed $APP"
echo "Open Gsync from Launchpad/Spotlight, or run:  open \"$APP\""
"$PREFIX/bin/rclone" version | head -1
