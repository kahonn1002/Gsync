#!/usr/bin/python3
"""Gsync: a small GTK front-end for two-way Google Drive sync (rclone bisync)."""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

APP = "gsync"
CFG_DIR = os.path.join(GLib.get_user_config_dir(), APP)
CFG_FILE = os.path.join(CFG_DIR, "config.json")
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "gsync.desktop")
REMOTE = "gdrive"
MIN_RCLONE = (1, 66, 0)  # --conflict-resolve / --resilient need a recent bisync

DEFAULTS = {"interval_min": 10, "paused": False, "pairs": []}

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None


def load_config():
    try:
        with open(CFG_FILE) as f:
            return {**DEFAULTS, **json.load(f)}
    except (OSError, ValueError):
        return dict(DEFAULTS, pairs=[])


def save_config(cfg):
    os.makedirs(CFG_DIR, exist_ok=True)
    tmp = CFG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_FILE)


def rclone_version():
    """Return (major, minor, patch), or None when rclone is missing."""
    if not shutil.which("rclone"):
        return None
    out = subprocess.run(["rclone", "version"], capture_output=True, text=True).stdout
    m = re.search(r"rclone v(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def remote_configured():
    out = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout
    return f"{REMOTE}:" in out.split()


EXTERNAL_ROOTS = ("/media", "/mnt", "/run/media")


def local_path_problem(path):
    """Return why `path` must not be synced, or None if it looks safe."""
    home = os.path.realpath(os.path.expanduser("~"))
    path = os.path.realpath(path)
    if path == home or home.startswith(path.rstrip("/") + "/"):
        return "That is your home folder (or contains it). Use a dedicated folder such as ~/GDrive/<name>."
    if path.startswith(home + "/"):
        if os.path.relpath(path, home).split("/")[0].startswith("."):
            return "Hidden folders in your home hold app settings and can't be synced."
        return None
    if any(path.startswith(root + "/") for root in EXTERNAL_ROOTS):
        return None
    return "Choose a folder inside your home folder or on an external drive."


def overlaps(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def scan_local(path, max_files=50000, max_seconds=5):
    """Count files and bytes under path, stopping early on huge trees. Returns (files, bytes, complete)."""
    files = size = 0
    start = time.monotonic()
    for root, _dirs, names in os.walk(path):
        for n in names:
            files += 1
            try:
                size += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
        if files >= max_files or time.monotonic() - start > max_seconds:
            return files, size, False
    return files, size, True


def remote_size(path):
    """Return (files, bytes, exists) for a Drive folder, or None on failure."""
    try:
        r = subprocess.run(
            ["rclone", "size", "--json", f"{REMOTE}:{path}"], capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return (0, 0, False) if "directory not found" in r.stderr else None
    try:
        data = json.loads(r.stdout)
        return data["count"], data["bytes"], True
    except (ValueError, KeyError):
        return None


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class GsyncWindow(Gtk.ApplicationWindow):
    def __init__(self, app, background=False):
        super().__init__(application=app, title="Gsync")
        self.set_default_size(820, 560)
        self.cfg = load_config()
        self.running = {}  # pair id -> Popen
        self.lock = threading.Lock()
        self.indicator = None
        self.quitting = False

        self._build_ui()
        self._refresh_pairs()
        self._check_setup()
        self._build_tray(app)

        GLib.timeout_add_seconds(30, self._tick)
        self.connect("delete-event", self._on_close)
        if background:
            self.iconify()
        self.show_all()

    # ---------- UI ----------
    def _build_ui(self):
        header = Gtk.HeaderBar(show_close_button=True, title="Gsync", subtitle="Google Drive two-way sync")
        self.set_titlebar(header)

        self.btn_connect = Gtk.Button(label="Connect Google account")
        self.btn_connect.connect("clicked", self._on_connect)
        header.pack_start(self.btn_connect)

        self.btn_pause = Gtk.ToggleButton(label="Pause")
        self.btn_pause.set_active(self.cfg["paused"])
        self.btn_pause.connect("toggled", self._on_pause)
        header.pack_end(self.btn_pause)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
        self.add(box)

        self.banner = Gtk.Label(xalign=0)
        self.banner.set_line_wrap(True)
        box.pack_start(self.banner, False, False, 0)

        bar = Gtk.Box(spacing=6)
        for label, handler in (
            ("Add folder", self._on_add),
            ("Sync now", self._on_sync_selected),
            ("Repair (resync)", self._on_resync),
            ("Remove", self._on_remove),
        ):
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            bar.pack_start(b, False, False, 0)
        box.pack_start(bar, False, False, 0)

        self.store = Gtk.ListStore(str, str, str, str, str)  # id, drive, local, status, last
        self.tree = Gtk.TreeView(model=self.store)
        for i, title in ((1, "Drive folder"), (2, "Local folder"), (3, "Status"), (4, "Last sync")):
            col = Gtk.TreeViewColumn(title, Gtk.CellRendererText(ellipsize=3), text=i)
            col.set_resizable(True)
            col.set_expand(i in (1, 2))
            self.tree.append_column(col)
        sw = Gtk.ScrolledWindow(min_content_height=140)
        sw.add(self.tree)
        box.pack_start(sw, True, True, 0)

        settings = Gtk.Box(spacing=12)
        settings.pack_start(Gtk.Label(label="Sync every"), False, False, 0)
        self.spin = Gtk.SpinButton.new_with_range(1, 1440, 1)
        self.spin.set_value(self.cfg["interval_min"])
        self.spin.connect("value-changed", self._on_interval)
        settings.pack_start(self.spin, False, False, 0)
        settings.pack_start(Gtk.Label(label="minutes"), False, False, 0)
        self.sw_auto = Gtk.Switch(active=os.path.exists(AUTOSTART_FILE), valign=Gtk.Align.CENTER)
        self.sw_auto.connect("notify::active", self._on_autostart)
        settings.pack_end(self.sw_auto, False, False, 0)
        settings.pack_end(Gtk.Label(label="Start at login"), False, False, 0)
        box.pack_start(settings, False, False, 0)

        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        lsw = Gtk.ScrolledWindow(min_content_height=180)
        lsw.add(self.log)
        box.pack_start(lsw, True, True, 0)

    def _build_tray(self, app):
        if AppIndicator is None:
            return
        self.indicator = AppIndicator.Indicator.new(
            APP, "folder-remote", AppIndicator.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        for label, fn in (
            ("Open Gsync", lambda *_: self.present()),
            ("Sync all now", lambda *_: self._sync_all()),
            ("Quit", lambda *_: self._quit()),
        ):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", fn)
            menu.append(item)
        menu.show_all()
        self.indicator.set_menu(menu)

    def _on_close(self, *_):
        if self.indicator and not self.quitting:
            self.hide()  # keep syncing in the background
            return True
        self._quit()
        return True

    def _quit(self):
        self.quitting = True
        for p in list(self.running.values()):
            p.terminate()
        self.get_application().quit()

    def log_line(self, text):
        def add():
            buf = self.log.get_buffer()
            buf.insert(buf.get_end_iter(), text.rstrip("\n") + "\n")
            if buf.get_line_count() > 2000:
                buf.delete(buf.get_start_iter(), buf.get_iter_at_line(500))
            self.log.scroll_to_iter(buf.get_end_iter(), 0, False, 0, 0)
        GLib.idle_add(add)

    def _refresh_pairs(self):
        self.store.clear()
        for p in self.cfg["pairs"]:
            last = time.strftime("%Y-%m-%d %H:%M", time.localtime(p["last_sync"])) if p.get("last_sync") else "never"
            self.store.append([p["id"], p["remote_path"] or "(My Drive root)", p["local_path"], p["status"], last])

    def _selected(self):
        model, it = self.tree.get_selection().get_selected()
        if not it:
            return None
        pid = model[it][0]
        return next((p for p in self.cfg["pairs"] if p["id"] == pid), None)

    def _message(self, text, kind=Gtk.MessageType.INFO):
        d = Gtk.MessageDialog(transient_for=self, message_type=kind, buttons=Gtk.ButtonsType.OK, text=text)
        d.run()
        d.destroy()

    def _confirm(self, text, secondary="", kind=Gtk.MessageType.QUESTION):
        d = Gtk.MessageDialog(
            transient_for=self, message_type=kind,
            buttons=Gtk.ButtonsType.OK_CANCEL, text=text, secondary_text=secondary,
        )
        ok = d.run() == Gtk.ResponseType.OK
        d.destroy()
        return ok

    def _run_with_progress(self, text, fn):
        """Run fn() in a thread behind a modal 'please wait' dialog; None if the user closes it."""
        d = Gtk.MessageDialog(transient_for=self, modal=True, text=text)
        d.get_message_area().pack_start(Gtk.Spinner(active=True), False, False, 6)
        result, closed = {}, [False]

        def work():
            result["value"] = fn()
            GLib.idle_add(lambda: closed[0] or d.response(Gtk.ResponseType.OK))

        threading.Thread(target=work, daemon=True).start()
        d.show_all()
        d.run()
        closed[0] = True
        d.destroy()
        return result.get("value")

    # ---------- setup ----------
    def _check_setup(self):
        ver = rclone_version()
        if ver is None:
            self.banner.set_markup(
                "<b>rclone is not installed.</b> Install the latest release with:\n"
                "<tt>curl https://rclone.org/install.sh | sudo bash</tt>"
            )
            self.btn_connect.set_sensitive(False)
        elif ver < MIN_RCLONE:
            self.banner.set_markup(
                f"<b>rclone {'.'.join(map(str, ver))} is too old for reliable bisync</b> "
                f"(need {'.'.join(map(str, MIN_RCLONE))}+). Update with:\n"
                "<tt>curl https://rclone.org/install.sh | sudo bash</tt>"
            )
        elif not remote_configured():
            self.banner.set_text("Click “Connect Google account” to sign in to Google Drive.")
        else:
            self.banner.set_text("Connected to Google Drive.")
            self.btn_connect.set_label("Reconnect account")

    def _ask_client_id(self):
        """Return (client_id, client_secret), ("", "") to skip, or None if cancelled."""
        d = Gtk.Dialog(title="Google API client", transient_for=self, modal=True)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Continue", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)
        d.get_content_area().add(grid)
        info = Gtk.Label(xalign=0, wrap=True, max_width_chars=60)
        info.set_markup(
            "rclone's shared Google client is being retired. Paste your own client ID and secret "
            "(<a href='https://rclone.org/drive/#making-your-own-client-id'>how to make one</a>). "
            "Leave blank to keep using the shared one for now."
        )
        grid.attach(info, 0, 0, 2, 1)
        grid.attach(Gtk.Label(label="Client ID", xalign=1), 0, 1, 1, 1)
        e_id = Gtk.Entry(hexpand=True)
        grid.attach(e_id, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="Client secret", xalign=1), 0, 2, 1, 1)
        e_secret = Gtk.Entry(visibility=False, hexpand=True)
        grid.attach(e_secret, 1, 2, 1, 1)
        d.show_all()
        resp = d.run()
        result = (e_id.get_text().strip(), e_secret.get_text().strip())
        d.destroy()
        return result if resp == Gtk.ResponseType.OK else None

    def _on_connect(self, _btn):
        creds = self._ask_client_id()
        if creds is None:
            return
        client_id, client_secret = creds
        exists = remote_configured()
        if exists and not client_id:
            self._message("Already connected to Google Drive. Nothing to change.")
            return
        self.log_line("Opening browser for Google sign-in...")
        self.btn_connect.set_sensitive(False)

        def work():
            keys = [f"client_id={client_id}", f"client_secret={client_secret}"] if client_id else []
            if exists:
                # update re-runs the OAuth login so the token matches the new client
                cmd = ["rclone", "config", "update", REMOTE, *keys]
            else:
                cmd = ["rclone", "config", "create", REMOTE, "drive", "scope=drive", *keys]
            # Without our own client, rclone asks whether to keep the shared one; answer yes.
            stdin = "" if client_id else "y\n"
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
            self.log_line((r.stdout + r.stderr).strip() or f"rclone exited {r.returncode}")

            def done():
                self.btn_connect.set_sensitive(True)
                self._check_setup()
            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    # ---------- pairs ----------
    def _on_add(self, _btn):
        if not shutil.which("rclone") or not remote_configured():
            self._message("Connect your Google account first.", Gtk.MessageType.WARNING)
            return
        d = Gtk.Dialog(title="Add sync folder", transient_for=self, modal=True)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
        grid = Gtk.Grid(row_spacing=8, column_spacing=8, margin=12)
        d.get_content_area().add(grid)
        grid.attach(Gtk.Label(label="Drive folder", xalign=1), 0, 0, 1, 1)
        e_remote = Gtk.Entry(text="", placeholder_text="e.g. Documents/Work (blank = whole Drive)", hexpand=True)
        btn_browse = Gtk.Button(label="Browse…")
        e_local = Gtk.Entry(placeholder_text="e.g. ~/GDrive/Work", hexpand=True)
        btn_choose = Gtk.Button(label="Choose…")
        suggested = [""]

        def suggest_local(entry):
            # Follow the Drive folder name until the user types their own local path.
            if e_local.get_text() in ("", suggested[0]):
                name = entry.get_text().strip().strip("/").rsplit("/", 1)[-1] or "My Drive"
                suggested[0] = os.path.join("~", "GDrive", name)
                e_local.set_text(suggested[0])

        def browse(_b):
            path = self._pick_drive_folder(d)
            if path is not None:
                e_remote.set_text(path)

        def choose_local(_b):
            fc = Gtk.FileChooserDialog(
                title="Choose local folder", transient_for=d, action=Gtk.FileChooserAction.SELECT_FOLDER
            )
            fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
            fc.set_create_folders(True)
            start = os.path.abspath(os.path.expanduser(e_local.get_text().strip() or "~"))
            while not os.path.isdir(start):
                start = os.path.dirname(start)
            fc.set_current_folder(start)
            if fc.run() == Gtk.ResponseType.OK and fc.get_filename():
                e_local.set_text(fc.get_filename())
                if not e_remote.get_text().strip():  # blank would mean the whole Drive
                    e_remote.set_text(os.path.basename(fc.get_filename()))
            fc.destroy()

        e_remote.connect("changed", suggest_local)
        btn_browse.connect("clicked", browse)
        btn_choose.connect("clicked", choose_local)
        for row, (entry, btn) in enumerate(((e_remote, btn_browse), (e_local, btn_choose))):
            box = Gtk.Box(spacing=6)
            box.pack_start(entry, True, True, 0)
            box.pack_start(btn, False, False, 0)
            grid.attach(box, 1, row, 1, 1)
        grid.attach(Gtk.Label(label="Local folder", xalign=1), 0, 1, 1, 1)
        hint = Gtk.Label(
            label="Use a new or empty folder: everything inside it is copied to Drive.", xalign=0
        )
        hint.get_style_context().add_class("dim-label")
        grid.attach(hint, 1, 2, 1, 1)
        d.show_all()
        while True:
            if d.run() != Gtk.ResponseType.OK:
                d.destroy()
                return
            remote_path = e_remote.get_text().strip().strip("/")
            local_text = e_local.get_text().strip()
            local_path = os.path.abspath(os.path.expanduser(local_text)) if local_text else ""
            problem = "Choose a local folder." if not local_path else local_path_problem(local_path)
            if problem is None:
                problem = next(
                    (f"That overlaps a folder you already sync ({p['local_path']})."
                     for p in self.cfg["pairs"] if overlaps(p["local_path"], local_path)),
                    None,
                )
            if problem is None:
                break
            self._message(problem, Gtk.MessageType.WARNING)
        d.destroy()

        preview = self._run_with_progress(
            "Checking what the first sync will copy…",
            lambda: (scan_local(local_path), remote_size(remote_path)),
        )
        if preview is None:
            return
        (l_files, l_bytes, complete), remote = preview
        more = "" if complete else "+"
        up = f"{l_files:,}{more} files ({human(l_bytes)}{more})"
        if remote is None:
            down = "unknown (Drive didn't answer)"
        elif not remote[2]:
            down = "nothing (new Drive folder)"
        else:
            down = f"{remote[0]:,} files ({human(remote[1])})"
        secondary = (
            f"Upload to Drive: {up}\nDownload to this computer: {down}\n\n"
            "Nothing is deleted. If a file exists on both sides and differs, the Drive copy is kept."
        )
        merging = l_files and (remote is None or remote[0])
        if remote and not remote[2]:
            secondary = f"Drive folder “{remote_path}” doesn't exist yet and will be created.\n\n" + secondary
        elif merging:
            secondary = ("Both folders already have files, so they will be merged: "
                         "everything in the local folder is copied into Drive and vice versa.\n\n" + secondary)
        if not self._confirm(
            f"First sync: Drive “{remote_path or 'My Drive'}” ⇄ {local_path}", secondary,
            Gtk.MessageType.WARNING if merging else Gtk.MessageType.QUESTION,
        ):
            return
        pair = {
            "id": uuid.uuid4().hex[:8], "remote_path": remote_path, "local_path": local_path,
            "initialized": False, "last_sync": 0, "status": "Waiting",
        }
        self.cfg["pairs"].append(pair)
        save_config(self.cfg)
        self._refresh_pairs()
        self._start_sync(pair)

    def _pick_drive_folder(self, parent):
        """Browse Drive folders; return the chosen path ("" = whole Drive) or None."""
        d = Gtk.Dialog(title="Choose Drive folder", transient_for=parent, modal=True)
        d.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        d.set_default_size(460, 460)
        store = Gtk.TreeStore(str, str, bool, bool)  # name, path, children loaded, real folder
        tree = Gtk.TreeView(model=store, headers_visible=False)
        tree.append_column(Gtk.TreeViewColumn("Folder", Gtk.CellRendererText(), text=0))
        sw = Gtk.ScrolledWindow(vexpand=True)
        sw.add(tree)
        status = Gtk.Label(label="Loading folders…", xalign=0, margin=6)
        d.get_content_area().pack_start(sw, True, True, 0)
        d.get_content_area().pack_start(status, False, False, 0)
        closed = [False]

        def fill(parent_iter, path):
            def work():
                try:
                    r = subprocess.run(
                        ["rclone", "lsjson", "--dirs-only", f"{REMOTE}:{path}"],
                        capture_output=True, text=True, timeout=60,
                    )
                except subprocess.TimeoutExpired:
                    GLib.idle_add(done, [], "Google Drive did not respond; collapse and expand to retry.")
                    return
                try:
                    dirs = sorted((e["Name"] for e in json.loads(r.stdout or "[]")), key=str.lower)
                except ValueError:
                    dirs = []
                err = None
                if r.returncode != 0:
                    lines = r.stderr.strip().splitlines()
                    err = lines[-1] if lines else f"rclone exit {r.returncode}"
                GLib.idle_add(done, dirs, err)

            def done(dirs, err):
                if closed[0]:
                    return
                child = store.iter_children(parent_iter)
                while child is not None:  # drop the "Loading…" placeholder
                    store.remove(child)
                    child = store.iter_children(parent_iter)
                for name in dirs:
                    it = store.append(parent_iter, [name, f"{path}/{name}" if path else name, False, True])
                    store.append(it, ["Loading…", "", True, False])
                if err:  # keep the expander and allow a retry on next expand
                    store[parent_iter][2] = False
                    store.append(parent_iter, ["Loading…", "", True, False])
                if parent_iter == root and not err:
                    tree.expand_row(store.get_path(root), False)
                status.set_text(f"Error: {err}" if err else "Double-click a folder to choose it.")

            threading.Thread(target=work, daemon=True).start()

        def on_expand(_tree, it, _path):
            if not store[it][2]:
                store[it][2] = True
                fill(it, store[it][1])

        root = store.append(None, ["My Drive (everything)", "", True, True])
        store.append(root, ["Loading…", "", True, False])
        tree.connect("row-expanded", on_expand)
        tree.connect("row-activated", lambda *_: d.response(Gtk.ResponseType.OK))
        fill(root, "")
        d.show_all()
        resp = d.run()
        closed[0] = True
        model, it = tree.get_selection().get_selected()
        chosen = model[it][1] if it is not None and model[it][3] else None
        d.destroy()
        return chosen if resp == Gtk.ResponseType.OK else None

    def _on_remove(self, _btn):
        p = self._selected()
        if not p:
            return
        if not self._confirm("Stop syncing this folder?", "Files on Drive and on disk are left untouched."):
            return
        proc = self.running.get(p["id"])
        if proc:
            proc.terminate()
        self.cfg["pairs"] = [x for x in self.cfg["pairs"] if x["id"] != p["id"]]
        save_config(self.cfg)
        self._refresh_pairs()

    def _on_sync_selected(self, _btn):
        p = self._selected()
        if p:
            self._start_sync(p)

    def _on_resync(self, _btn):
        p = self._selected()
        if p and self._confirm(
            "Rebuild sync state?",
            "Runs a full resync. Files missing on one side are copied over; the Drive copy wins "
            "when both sides differ. Nothing is deleted.",
        ):
            self._start_sync(p, resync=True)

    # ---------- syncing ----------
    def _sync_all(self):
        for p in self.cfg["pairs"]:
            self._start_sync(p)

    def _tick(self):
        if self.cfg["paused"] or not shutil.which("rclone"):
            return True
        now = time.time()
        for p in self.cfg["pairs"]:
            if now - p.get("last_attempt", 0) >= self.cfg["interval_min"] * 60:
                self._start_sync(p)
        return True

    def _set_status(self, pair, status, ok=False):
        def apply():
            pair["status"] = status
            if ok:
                pair["last_sync"] = time.time()
            save_config(self.cfg)
            self._refresh_pairs()
        GLib.idle_add(apply)

    def _start_sync(self, pair, resync=False):
        with self.lock:
            if pair["id"] in self.running:
                return
            self.running[pair["id"]] = None
        pair["last_attempt"] = time.time()
        self._set_status(pair, "Syncing...")
        threading.Thread(target=self._sync_worker, args=(pair, resync), daemon=True).start()

    def _sync_worker(self, pair, resync):
        name = pair["remote_path"] or "My Drive"
        try:
            problem = local_path_problem(pair["local_path"])
            if problem:  # also guards pairs saved by older versions
                self.log_line(f"Refusing to sync {pair['local_path']}: {problem}")
                self._set_status(pair, "Blocked: unsafe local folder")
                return
            os.makedirs(pair["local_path"], exist_ok=True)
            if not pair["initialized"]:  # bisync needs both folders to exist
                subprocess.run(["rclone", "mkdir", f"{REMOTE}:{pair['remote_path']}"], capture_output=True)
            cmd = [
                "rclone", "bisync", f"{REMOTE}:{pair['remote_path']}", pair["local_path"],
                "--create-empty-src-dirs", "--drive-skip-gdocs", "--resilient",
                "--conflict-resolve", "newer", "--conflict-loser", "num", "-v",
            ]
            if resync or not pair["initialized"]:
                cmd += ["--resync"]  # default resync mode: Path1 (Drive) wins on differences
            self.log_line(f"== {name} ==")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.running[pair["id"]] = proc
            tail = []
            for line in proc.stdout:
                self.log_line(line)
                tail.append(line)
                tail = tail[-30:]
            code = proc.wait()
            if code == 0:
                pair["initialized"] = True
                self._set_status(pair, "Up to date", ok=True)
            elif any("--resync" in ln for ln in tail):
                self._set_status(pair, "Needs repair (use Repair)")
            else:
                self._set_status(pair, f"Error (rclone exit {code})")
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            self.log_line(f"Sync failed: {exc}")
            self._set_status(pair, "Error")
        finally:
            with self.lock:
                self.running.pop(pair["id"], None)

    # ---------- settings ----------
    def _on_pause(self, btn):
        self.cfg["paused"] = btn.get_active()
        save_config(self.cfg)
        self.log_line("Auto-sync paused." if self.cfg["paused"] else "Auto-sync resumed.")

    def _on_interval(self, spin):
        self.cfg["interval_min"] = spin.get_value_as_int()
        save_config(self.cfg)

    def _on_autostart(self, sw, _param):
        if sw.get_active():
            os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
            with open(AUTOSTART_FILE, "w") as f:
                f.write(
                    "[Desktop Entry]\nType=Application\nName=Gsync\n"
                    f"Exec=/usr/bin/python3 {os.path.abspath(__file__)} --background\n"
                    "X-GNOME-Autostart-enabled=true\n"
                )
        elif os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)


class GsyncApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="local.gsync")
        self.background = "--background" in sys.argv
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = GsyncWindow(self, background=self.background)
            self.background = False
        else:
            self.win.present()


if __name__ == "__main__":
    sys.exit(GsyncApp().run([sys.argv[0]]))
