# Gsync macOS port: handoff

This file carries the context from the Linux session into a new Claude Code session on the Mac mini.
Start that session in this repo on the `macos` branch and tell Claude: "Read MACOS_HANDOFF.md and continue the plan."
Delete this file before merging `macos` into `main`.

## What Gsync is
- `gsync.py`: a GTK 3 front-end for two-way Google Drive sync using `rclone bisync`. The remote is named `gdrive:`.
- Config lives in `~/.config/gsync/config.json` (GLib returns `~/.config` on macOS too). Each device has its own rclone sign-in (`rclone config`) and its own pairs.
- Structure: platform-neutral helpers near the top (config, `rclone_version`, `uses_shared_client`, `local_path_problem`, `scan_local`, `remote_size`, `pair_state`), then `GsyncWindow` (GTK UI, sync worker, folder picker), then `GsyncApp`.

## Branch rules
- Linux development stays on `main`. All macOS work goes on `macos`.
- Put Mac-specific code in new files (for example `platform_mac.py`, `install_mac.sh`, `MACOS_TESTING.md`). Change `gsync.py` only where behaviour has to diverge, and keep those edits small and guarded by `sys.platform == "darwin"`.
- The user tests on the Mac mini before merging. Merging is the user's decision.

## Safety rules (from a real incident)
- 2026-10-27: an early version defaulted the local folder to `~`. Pairing Drive `3dprint` with the home folder uploaded home files into Drive. They were cleaned up. `local_path_problem()` now blocks home, ancestors of home, hidden dirs and system paths.
- Never run a first `--resync` without previewing what it will copy. Test only with a throwaway Drive folder and an empty local folder first.
- Do not copy `config.json` or bisync state between machines.
- Drive has two top-level folders both named `sponsorship`. Syncing them will break.

## Google client ID
- rclone's shared client ID is pooled across all users and gets throttled (403 rateLimitExceeded, about 35 s back-off per process). Gsync shows a banner while the shared client is in use (`uses_shared_client()`).
- The user created their own Desktop OAuth client in their own Google Cloud project. On the Mac, set up the remote with that client ID and secret (the app's "Use my own client ID" button does this). Ask the user for the values. They are not in this repo.
- `--fast-list` is already added to bisync. It cut Drive requests from about 474 to about 30 per run, with identical listings.

## Decision already made
- Phase 1 keeps GTK 3 through Homebrew, with no tray icon. If the gate below fails, stop and ask the user before switching to PySide6, which is a rewrite of about 750 lines of UI.

## Plan
1. **Gate.** On the Mac: `brew install rclone gtk+3 pygobject3 adwaita-icon-theme`. Run `python3 gsync.py` unchanged (Homebrew python, not `/usr/bin/python3`). Record what works and what breaks: window, dialogs, drive-folder picker, threading, icons.
2. **Platform layer.** Add behind a darwin check:
   - Open folder: replace `Gtk.show_uri_on_window` with `open <path>`.
   - Autostart: replace the `.desktop` file with a LaunchAgent plist in `~/Library/LaunchAgents` loaded with `launchctl bootstrap gui/$UID`. The Exec must use the Homebrew python and `--background`.
   - Path safety: extend `local_path_problem` and `EXTERNAL_ROOTS` for `/System`, `/Library`, `~/Library`, iCloud Drive (`~/Library/Mobile Documents`) and `/Volumes`. Keep the home block.
   - Tray: `AppIndicator` is unavailable, so `_build_tray` already returns early. Make sure closing the window behaves sensibly without a tray (quit, or keep running via the Dock).
   - Filters: exclude `.DS_Store` and `._*` from bisync.
3. **Safe first sync.** Throwaway Drive folder and empty local folder. Preview the first `--resync`. Then test edits on both sides, deletes, and a conflict.
4. **Packaging.** Write `install_mac.sh` (a launcher and optionally a `.app` wrapper) and `MACOS_TESTING.md` (checklist modelled on `TESTING.md`).
5. **Optional later.** A menu-bar item (`rumps`) or a proper `.app` bundle.

## Risks to test early (unverified, no Mac was available when this was written)
- GTK 3 on the Quartz backend: dialog, threading and `GLib.idle_add` behaviour, and the look of the window.
- Unicode normalization: macOS stores filenames as NFD, Drive as NFC. Test accented and non-English names.
- Case-insensitive APFS: `Foo` and `foo` can coexist on Drive but collide locally.
- Privacy prompts for Desktop, Documents and iCloud folders.
- The `rclone` version must be at least 1.66.0 (`MIN_RCLONE`). Homebrew's version should satisfy this.

## Status
- [x] `macos` branch created from `main` at the commit that added this file's parent.
- [ ] Phase 1 gate run on the Mac.
