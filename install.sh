#!/bin/bash
# Adds Gsync to the desktop app menu.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$DIR/gsync.py"
mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/gsync.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Gsync
Comment=Two-way Google Drive sync
Exec=/usr/bin/python3 $DIR/gsync.py
Icon=folder-remote
Categories=Utility;Network;
EOF
echo "Installed. Launch 'Gsync' from your app menu."
if ! command -v rclone >/dev/null; then
  echo "Next: install rclone with:  curl https://rclone.org/install.sh | sudo bash"
fi
