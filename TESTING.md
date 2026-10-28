# Testing Gsync on another device

Gsync is a small GTK app for two-way Google Drive sync, built on `rclone bisync`.
Use this checklist to set it up and test it on a second Linux machine.

## 1. Requirements

- Linux desktop with GTK 3 (tested on Ubuntu)
- System Python at `/usr/bin/python3` with GTK bindings. Anaconda/conda Python has no GTK, so don't use it.
- rclone **1.66.0 or newer**. Ubuntu's `apt` package is usually too old.

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1   # tray icon is optional
curl https://rclone.org/install.sh | sudo bash
rclone version   # should show v1.66.0 or newer
```

## 2. Get the code and install

```bash
git clone <repo-url> ~/Gsync      # or copy the folder over
cd ~/Gsync
./install.sh                      # adds "Gsync" to the app menu
```

Or run it directly: `/usr/bin/python3 gsync.py`

## 3. Connect Google Drive

1. Click **Connect Google account**.
2. Paste your own Google API client ID and secret, or leave them blank to keep using rclone's shared client for now. That shared client is being retired during 2026.
3. Sign in to Google in the browser window that opens.
4. The banner should change to "Connected to Google Drive."

rclone config is stored per machine in `~/.config/rclone/rclone.conf`, so each device has to sign in on its own.

## 4. First sync: do it safely

The first sync on a new device runs `--resync`. That step **copies anything missing on either side**, and when a file differs on both sides, Drive's copy wins. To avoid surprises:

- Pick a Drive folder you use for testing, such as a new `gsync-test` folder, not an important one.
- Pick a local folder that is **empty or new**, such as `~/GDrive/gsync-test`. Gsync won't let you choose your home folder, hidden folders, or system paths.
- Read the size preview in the Add dialog before you confirm.

## 5. Test checklist

| # | Test | Expected |
|---|------|----------|
| 1 | Add pair (Drive `gsync-test` ↔ `~/GDrive/gsync-test`) | Status becomes "Up to date"; Drive files appear locally |
| 2 | Create a file locally, then click **Sync selected** | File shows up in Drive (web) |
| 3 | Create a file in Drive web, then sync | File appears locally |
| 4 | Edit the same file on this device and on the other device, then sync both | Newer version wins; the older one is kept with a numbered suffix |
| 5 | Delete a file on one side, then sync | It is deleted on the other side |
| 6 | Try to choose `~` or `~/.config` as the local folder | Blocked with an explanation |
| 7 | Set the interval to 1 min and wait | Auto-sync runs; **Pause** stops it |
| 8 | Turn on autostart, then log out and back in | Gsync starts in the background (tray icon, if installed) |
| 9 | Close the window | App keeps running in the tray; Quit from the tray exits |
| 10 | **Repair** on a pair | Asks for confirmation, then runs a full resync without deleting anything |

## 6. Where things live (per device)

| What | Path |
|------|------|
| Gsync config (pairs, interval) | `~/.config/gsync/config.json` |
| Autostart entry | `~/.config/autostart/gsync.desktop` |
| rclone remote and token | `~/.config/rclone/rclone.conf` |
| bisync state | `~/.cache/rclone/bisync/` |

Don't copy `config.json` or the bisync state between machines. Each device should add its own pairs and run its own first sync.

## Known issues

- Drive has two top-level folders that are both named `sponsorship`. Syncing either one will fail until one is renamed.
- Google Docs/Sheets/Slides are skipped (`--drive-skip-gdocs`).
