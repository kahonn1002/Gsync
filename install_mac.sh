#!/bin/bash
# Installs Gsync on macOS: checks the Homebrew packages and adds ~/Applications/Gsync.app
# (a small launcher for gsync.py in this folder; rerun this script if you move the folder).
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

# The launcher is a tiny AppleScript applet: macOS 26 refuses to open a bundle whose executable is
# a shell script (LaunchServices error -10669), but an osacompile applet is a normal app. It starts
# gsync.py in the background and exits; Gsync then shows in the Dock as "Python".
as_str() { local s="${1//\\/\\\\}"; printf '"%s"' "${s//\"/\\\"}"; }  # AppleScript string literal
LOG="$HOME/Library/Logs/gsync.log"
APP="$HOME/Applications/Gsync.app"
mkdir -p "$HOME/Applications" "$(dirname "$LOG")"
rm -rf "$APP"
osacompile -o "$APP" \
  -e "set py to $(as_str "$PY")" \
  -e "set gsyncScript to $(as_str "$DIR/gsync.py")" \
  -e "set logFile to $(as_str "$LOG")" \
  -e "set binPath to $(as_str "$PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin")" \
  -e 'do shell script "export PATH=" & quoted form of binPath & "; " & quoted form of py & " " & quoted form of gsyncScript & " >> " & quoted form of logFile & " 2>&1 &"'
chmod +x "$DIR/gsync.py"

echo "Installed $APP"
echo "Open Gsync from Launchpad/Spotlight, or run:  open \"$APP\""
"$PREFIX/bin/rclone" version | head -1
