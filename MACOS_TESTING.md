# Testing Gsync on macOS

Gsync on macOS is the same GTK 3 app as on Linux, run through Homebrew. `gsync.py` calls the
Mac-only code behind `sys.platform == "darwin"` checks:

| File | What it does |
|------|--------------|
| `platform_mac.py` | PATH fix, path-safety rules, autostart, single instance, log file |
| `mac_app.py` | Ties the pieces below to the Gsync window |
| `mac_menubar.py` | Menu-bar item and its menu |
| `mac_notify.py` | Notifications |
| `mac_watch.py` | Syncs a pair soon after its local folder changes |
| `mac_objc.py` | Small ctypes bridge to macOS's Objective-C APIs (no PyObjC needed) |
| `mac_icons.py` | Draws the app icon and the menu-bar glyphs |
| `mac_launcher.c` | `Gsync.app`'s executable, which runs `gsync.py` inside the app's own process |

## 1. Install

Needs [Homebrew](https://brew.sh) and Apple's command line tools (`xcode-select --install`, or Xcode).
Then, from this folder:

```bash
./install_mac.sh
```

It installs any missing packages (`rclone gtk+3 pygobject3 adwaita-icon-theme`) and builds
`~/Applications/Gsync.app`. Open Gsync from Spotlight or Launchpad. If you move this folder, run
`./install_mac.sh` again. Homebrew upgrading Python does not need a reinstall.

You can still run `/opt/homebrew/bin/python3 gsync.py` from Terminal to see its output directly.
Run that way, Gsync shows as **Python** in the Dock and notifications come from Script Editor.
Don't use `/usr/bin/python3`: it has no GTK bindings.

## 2. Connect Google Drive

Same as Linux: **Connect Google account** → paste your own client ID and secret (from your Google
Cloud project, not stored in this repo) → sign in in the browser. Each machine signs in separately;
don't copy `rclone.conf` from Linux.

## 3. First sync: do it safely

The rules from `TESTING.md` §4 apply: a throwaway Drive folder (for example `gsync-test`), an
empty new local folder (for example `~/GDrive/gsync-test`), and read the preview before confirming.

macOS asks for permission the first time Gsync touches Desktop, Documents, Downloads or an external
drive, and asks once whether Gsync may send notifications. Allow both. Permissions you gave
"Python" in earlier versions don't carry over to "Gsync". You can change them later in System
Settings → Privacy & Security → Files and Folders, and System Settings → Notifications → Gsync.

## 4. Test checklist

Items marked ✅ were checked automatically on the Mac mini on 2026-11-22. Those checks used scripted
GTK and AppKit calls and local folders, not real clicks or Google Drive. The rest need you.

| # | Test | Expected |
|---|------|----------|
| 1 | Add pair (Drive `gsync-test` ↔ `~/GDrive/gsync-test`) | Status becomes "Up to date"; Drive files appear locally |
| 2 | Create a file locally and wait about half a minute, without clicking anything | It syncs on its own; the file shows up in Drive (web) (✅ watcher with local folders) |
| 3 | Create a file in Drive web, then **Sync now** | File appears locally |
| 4 | Edit the same file here and on the Linux machine, sync both | Newer version wins; the older one is kept with a `.conflict1` suffix; a notification says so |
| 5 | Delete a file on one side, then sync | It is deleted on the other side |
| 6 | Open the pair folder in Finder so it gets a `.DS_Store` | No sync is triggered, and no `.DS_Store` or `._*` files reach Drive (✅ locally) |
| 7 | Names with accents and other scripts: `café.txt`, `naïve/`, `日本語.txt`, made on the Mac and in Drive web | One copy of each on both sides, no duplicates after two syncs |
| 8 | In Drive web make `Foo.txt` and `foo.txt` in the same folder, then sync | Sync reports the clash and doesn't lose either file; note what rclone does |
| 9 | Try `~`, `~/Library`, `~/library/x`, `~/.config`, `~/Pictures`, `/tmp/x` as local folder | Blocked with an explanation (✅) |
| 10 | Try `~/Desktop/GDrive`, `~/Pictures/Drive`, `/Volumes/<usb>/GDrive` | Allowed (✅) |
| 11 | Look at the menu bar | A cloud icon; it shows arrows while syncing, pause bars when paused, and a red/orange sign when a folder needs attention (✅) |
| 12 | Click the cloud | Menu: status line, one line per folder (click opens it in Finder), Open Gsync, Sync all now, Pause/Resume, Quit (✅ opened from code) |
| 13 | Click the red close button | Window closes, the Dock icon goes away, Gsync keeps syncing from the menu bar (✅) |
| 14 | Cloud → **Open Gsync**, or open Gsync.app again | The window comes back with its Dock icon; only one Gsync runs (`pgrep -fl Gsync`) (✅) |
| 15 | Make a sync fail (for example rename the Drive folder in Drive web, then sync) | One notification "… needs attention"; no repeat every interval. Fix it and sync: "… is syncing again" (✅ logic) |
| 16 | Press ⌘Q in the window, or cloud → **Quit Gsync** | Gsync exits; afterwards `pgrep rclone` shows nothing left running (✅ via code; check real keypress/click) |
| 17 | Turn on **Start at login**, log out and back in | Gsync starts with only the menu-bar cloud (no window, no Dock icon) and syncs. Login Items lists it as "Gsync" |
| 18 | Turn it off again | `~/Library/LaunchAgents/local.gsync.plist` is gone; nothing starts at next login |
| 19 | **Repair** on a pair | Asks for confirmation, then runs a full resync without deleting anything |
| 20 | Dock, ⌘Tab, menu bar and About show "Gsync" with the blue cloud icon | (✅) |

## 5. Where things live on macOS

| What | Path |
|------|------|
| Gsync config (pairs, interval) | `~/.config/gsync/config.json` |
| Single-instance lock and socket | `~/.config/gsync/gsync.lock`, `gsync.sock` |
| Autostart entry | `~/Library/LaunchAgents/local.gsync.plist` |
| The app | `~/Applications/Gsync.app` (runs `gsync.py` from this folder) |
| Log when started as the app or at login | `~/Library/Logs/gsync.log` |
| rclone remote and token | `~/.config/rclone/rclone.conf` |
| bisync state | `~/Library/Caches/rclone/bisync/` |

To uninstall, quit Gsync, then delete `~/Applications/Gsync.app`,
`~/Library/LaunchAgents/local.gsync.plist` and `~/.config/gsync`.

## macOS-specific behaviour

- **Menu-bar app.** Closing the window leaves Gsync running in the menu bar without a Dock icon.
  Quit from the cloud menu, ☰ → Quit Gsync, or ⌘Q.
- **Syncs soon after local changes.** About 20 seconds after the last change in a synced folder
  (at most 2 minutes while files keep changing), that folder syncs, unless auto-sync is paused.
  Changes made in Drive are still picked up on the regular interval. A sync that downloaded files
  is followed by one extra sync, because the download itself counts as a local change; that
  extra sync changes nothing.
- **Notifications** when a folder starts needing attention, when it recovers, and when bisync
  resolves a file changed on both sides.
- **One instance.** A second launch asks the running Gsync to show its window and exits. On Linux,
  GApplication does this over D-Bus, which macOS doesn't have.
- **Blocked local folders, in addition to the Linux rules:** `~/Library` (iCloud Drive, Google Drive
  for desktop, Dropbox and app data live there), `~/Pictures`, `~/Music` and `~/Movies` themselves
  (they hold the Photos/Music/TV libraries; folders inside them are fine), anything inside an `.app` or
  a library package, and Desktop/Documents when iCloud "Desktop & Documents Folders" is on.
  Comparisons ignore case and Unicode form because APFS does.
- **Excluded from sync:** `.DS_Store` and `._*` files, on both sides.
- The window keeps GNOME-style title-bar buttons. Dialogs get normal macOS buttons.

## Known issues

- From `TESTING.md`: the duplicate `sponsorship` folders, and Google Docs/Sheets/Slides are skipped.
