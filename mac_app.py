"""Mac-only behaviour of the Gsync window: menu-bar item, Dock icon, notifications and syncing
soon after local changes. gsync.py creates one MacExtras per window and calls it at a few points.
"""
import os
import traceback

from gi.repository import Gtk

import mac_objc
import platform_mac

PROBLEM_STATES = ("err", "warn")


class MacExtras:
    def __init__(self, win, pair_state):
        self.win = win
        self.pair_state = pair_state  # gsync.pair_state(pair) -> (state, icon, text)
        # Last finished (non-"busy") state per pair, so a failing auto-sync notifies once, not
        # every interval, and a recovery can be announced.
        self.settled = {p["id"]: pair_state(p)[0] for p in win.cfg["pairs"]}

        accel = Gtk.AccelGroup()  # ⌘Q while the window has focus; the menu-bar menu has its own
        key, mods = Gtk.accelerator_parse("<Primary>q")  # <Primary> is ⌘ on macOS
        accel.connect(key, mods, 0, lambda *_: win._quit() or True)
        win.add_accel_group(accel)
        platform_mac.show_handlers.append(self.show_window)  # a second launch asks us to show up

        self.menubar = self._try("menu-bar item", self._make_menubar)
        self.notifier = self._try("notifications", lambda: __import__("mac_notify").Notifier(win.log_line))
        self.change_sync = self._try("folder watching", self._make_change_sync)

    def _try(self, what, make):
        """Each extra is optional: if one fails, log why and keep the rest of Gsync working."""
        try:
            return make()
        except Exception:  # noqa: BLE001
            self.win.log_line(f"macOS {what} unavailable:\n{traceback.format_exc()}")
            return None

    def _make_menubar(self):
        import mac_menubar

        return mac_menubar.MenuBar(self._on_menu)

    def _make_change_sync(self):
        import mac_watch

        win = self.win
        return mac_watch.ChangeSync(start_sync=win._start_sync, is_running=lambda p: p["id"] in win.running,
                                    is_paused=lambda: win.cfg["paused"])

    # ---------- window and Dock ----------
    def start_hidden(self):
        """--background with a menu-bar item: run from the menu bar only, no window, no Dock icon."""
        for child in (self.win.get_titlebar(), self.win.get_child()):
            child.show_all()
        mac_objc.set_dock_icon_visible(False)

    def close_window(self):
        if self.menubar:
            self.win.hide()  # Gsync keeps running in the menu bar
            mac_objc.set_dock_icon_visible(False)
        else:
            self.win.iconify()  # no menu-bar item: park in the Dock instead

    def show_window(self):
        mac_objc.set_dock_icon_visible(True)
        self.win.deiconify()
        self.win.present()
        mac_objc.activate_app()

    # ---------- menu bar ----------
    def refresh(self, summary):
        """Called whenever the window refreshes its pair list."""
        pairs = self.win.cfg["pairs"]
        if self.change_sync:
            self.change_sync.update(pairs)
        if not self.menubar:
            return
        states = [self.pair_state(p)[0] for p in pairs]
        if "busy" in states:
            state = "busy"
        elif "err" in states:
            state = "err"
        elif "warn" in states:
            state = "warn"
        elif self.win.cfg["paused"]:
            state = "paused"
        else:
            state = "ok"
        entries = [(summary, None, False), None]
        for p in pairs:
            name = p["remote_path"].rsplit("/", 1)[-1] or "My Drive"
            entries.append((f"{name} — {self.pair_state(p)[2]}", f"open:{p['id']}", True))
        if pairs:
            entries.append(None)
        paused = self.win.cfg["paused"]
        entries += [("Open Gsync", "open", True), ("Sync all now", "sync", bool(pairs)),
                    ("Resume auto-sync" if paused else "Pause auto-sync", "pause", True),
                    None, ("Quit Gsync", "quit", True)]
        self.menubar.set_state(state, f"Gsync: {summary}")
        self.menubar.set_menu(entries)

    def _on_menu(self, action):
        win = self.win
        if action == "open":
            self.show_window()
        elif action == "sync":
            win._sync_all()
        elif action == "pause":
            win.btn_pause.set_active(not win.btn_pause.get_active())
        elif action == "quit":
            win._quit()
        elif action.startswith("open:"):  # a pair's line: show its local folder in Finder
            pair = next((p for p in win.cfg["pairs"] if p["id"] == action[5:]), None)
            if pair:
                win._on_open_local(pair)
        return False

    # ---------- notifications ----------
    def status_changed(self, pair):
        """Called after a pair's status changes. Notifies on entering or leaving a problem state."""
        state, _icon, text = self.pair_state(pair)
        if state == "busy":
            return
        before = self.settled.get(pair["id"])
        self.settled[pair["id"]] = state
        if not self.notifier or state == before:
            return
        name = pair["remote_path"].rsplit("/", 1)[-1] or "My Drive"
        if state in PROBLEM_STATES:
            self.notifier.notify(f"Gsync: {name} needs attention",
                                 f"{text}. Open Gsync and see Activity for details.")
        elif before in PROBLEM_STATES and state == "ok":
            self.notifier.notify(f"Gsync: {name} is syncing again", "The last sync finished without problems.")

    def conflicts(self, pair, names):
        """bisync found files changed on both sides; it kept the newer one and renamed the other."""
        if not self.notifier:
            return False
        shown = ", ".join(os.path.basename(n) for n in names[:3]) + (" …" if len(names) > 3 else "")
        noun = "file was" if len(names) == 1 else "files were"
        self.notifier.notify(f"Gsync: {len(names)} {noun} changed on both sides",
                             f"{shown}. The newer copy was kept; the other was saved with a .conflict suffix.")
        return False

    def stop(self):
        if self.change_sync:
            self.change_sync.stop()
        if self.menubar:
            self.menubar.remove()
