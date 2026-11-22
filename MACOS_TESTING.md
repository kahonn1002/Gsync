# Testing Gsync on macOS

Gsync on macOS is the same GTK 3 app as on Linux, run through Homebrew. Mac-only behaviour lives in
`platform_mac.py`; `gsync.py` calls it behind `sys.platform == "darwin"` checks.

## 1. Install

Needs [Homebrew](https://brew.sh). Then, from this folder:

```bash
./install_mac.sh
```

It installs any missing packages (`rclone gtk+3 pygobject3 adwaita-icon-theme`) and creates
`~/Applications/Gsync.app`. Open Gsync from Spotlight or Launchpad, or run
`/opt/homebrew/bin/python3 gsync.py`. Don't use `/usr/bin/python3`: it has no GTK bindings.
If you move this folder, run `./install_mac.sh` again.

Gsync shows in the Dock and menu bar as **Python** (with the Python rocket icon). That is expected
for now.

## 2. Connect Google Drive

Same as Linux: **Connect Google account** → paste your own client ID and secret (from your Google
Cloud project, not stored in this repo) → sign in in the browser. Each machine signs in separately;
don't copy `rclone.conf` from Linux.

## 3. First sync: do it safely

The rules from `TESTING.md` §4 apply: a throwaway Drive folder (for example `gsync-test`), an
empty new local folder (for example `~/GDrive/gsync-test`), and read the preview before confirming.

macOS asks once for permission when Gsync first touches Desktop, Documents, Downloads or an external
drive ("Python would like to access files in your Desktop folder"). Allow it, or sync stays at
"Error". You can change it later in System Settings → Privacy & Security → Files and Folders.

## 4. Test checklist

Items marked ✅ were checked automatically on the Mac mini on 2026-11-22 without a Google account
(GTK window, dialogs, threads, path rules, LaunchAgent file, local-to-local bisync excludes).
The rest need you and a Drive account.

| # | Test | Expected |
|---|------|----------|
| 1 | Add pair (Drive `gsync-test` ↔ `~/GDrive/gsync-test`) | Status becomes "Up to date"; Drive files appear locally |
| 2 | Create a file locally, then **Sync now** | File shows up in Drive (web) |
| 3 | Create a file in Drive web, then sync | File appears locally |
| 4 | Edit the same file here and on the Linux machine, sync both | Newer version wins; the older one is kept with a numbered suffix |
| 5 | Delete a file on one side, then sync | It is deleted on the other side |
| 6 | Open the pair folder in Finder so it gets a `.DS_Store`, then sync | No `.DS_Store` or `._*` files appear in Drive (✅ locally) |
| 7 | Names with accents and other scripts: `café.txt`, `naïve/`, `日本語.txt`, made on the Mac and in Drive web | One copy of each on both sides, no duplicates after two syncs |
| 8 | In Drive web make `Foo.txt` and `foo.txt` in the same folder, then sync | Sync reports the clash and doesn't lose either file; note what rclone does |
| 9 | Try `~`, `~/Library`, `~/library/x`, `~/.config`, `~/Pictures`, `/tmp/x` as local folder | Blocked with an explanation (✅) |
| 10 | Try `~/Desktop/GDrive`, `~/Pictures/Drive`, `/Volumes/<usb>/GDrive` | Allowed (✅) |
| 11 | Set the interval to 1 min and wait | Auto-sync runs; **Pause** stops it |
| 12 | Click the red close button | Window minimizes to the Dock and keeps syncing (✅) |
| 13 | Click the Gsync icon in the Dock / open Gsync.app again | The same window comes back; only one Gsync runs (`pgrep -fl gsync.py`) (✅) |
| 14 | Press ⌘Q, or use **Quit Gsync** in the ☰ menu | Gsync exits (✅ via GTK; check a real ⌘Q keypress) |
| 15 | Right-click the Dock icon → Quit | Gsync exits; afterwards `pgrep rclone` shows nothing left running |
| 16 | Turn on **Start at login**, log out and back in | Gsync starts minimized in the Dock and syncs. macOS may show "Background item added" |
| 17 | Turn it off again | `~/Library/LaunchAgents/local.gsync.plist` is gone; nothing starts at next login |
| 18 | **Repair** on a pair | Asks for confirmation, then runs a full resync without deleting anything |

## 5. Where things live on macOS

| What | Path |
|------|------|
| Gsync config (pairs, interval) | `~/.config/gsync/config.json` |
| Single-instance lock and socket | `~/.config/gsync/gsync.lock`, `gsync.sock` |
| Autostart entry | `~/Library/LaunchAgents/local.gsync.plist` |
| App launcher | `~/Applications/Gsync.app` |
| Log when started from the launcher or at login | `~/Library/Logs/gsync.log` |
| rclone remote and token | `~/.config/rclone/rclone.conf` |
| bisync state | `~/Library/Caches/rclone/bisync/` |

To uninstall, quit Gsync, then delete `~/Applications/Gsync.app`,
`~/Library/LaunchAgents/local.gsync.plist` and `~/.config/gsync`.

## macOS-specific behaviour

- **No tray icon.** Closing the window minimizes it and Gsync keeps syncing. Quit with ⌘Q or ☰ → Quit Gsync.
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

- On first start with no pairs, the "No folders synced yet" page stays blank until the first
  30-second refresh. This also happens on Linux (a `Gtk.Stack` ignores the switch before
  `show_all()`), so fix it on `main`.
- From `TESTING.md`: the duplicate `sponsorship` folders, and Google Docs/Sheets/Slides are skipped.
