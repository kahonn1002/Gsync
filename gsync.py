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

MAC = sys.platform == "darwin"
if MAC:
    import mac_app  # noqa: E402
    import platform_mac  # noqa: E402

    platform_mac.fix_path()
    if platform_mac.app_executable():  # started as Gsync.app: no terminal to print to
        platform_mac.redirect_output()

APP = "gsync"
CFG_DIR = os.path.join(GLib.get_user_config_dir(), APP)
CFG_FILE = os.path.join(CFG_DIR, "config.json")
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "gsync.desktop")
REMOTE = "gdrive"
MIN_RCLONE = (1, 66, 0)  # --conflict-resolve / --resilient need a recent bisync

INSTALL_RCLONE = platform_mac.INSTALL_RCLONE if MAC else "curl https://rclone.org/install.sh | sudo bash"

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


def uses_shared_client():
    """True when the Drive remote has no client_id of its own and so rides rclone's shared Google client.

    That client's API quota is pooled across every rclone user, so Google answers with 403
    rateLimitExceeded and rclone sleeps through exponential back-offs: about 35 s of idle waiting
    before a sync even starts. Only a personal client ID removes that.
    """
    out = subprocess.run(["rclone", "config", "show", REMOTE], capture_output=True, text=True).stdout
    for line in out.splitlines():  # the output also holds the token; read client_id only, log nothing
        key, _, value = line.partition("=")
        if key.strip() == "client_id":
            return not value.strip()
    return True


EXTERNAL_ROOTS = platform_mac.EXTERNAL_ROOTS if MAC else ("/media", "/mnt", "/run/media")


def local_path_problem(path):
    """Return why `path` must not be synced, or None if it looks safe."""
    home = os.path.realpath(os.path.expanduser("~"))
    path = os.path.realpath(path)
    if MAC:  # case- and normalization-insensitive filesystem
        home, path = platform_mac.fold(home), platform_mac.fold(path)
    if path == home or home.startswith(path.rstrip("/") + "/"):
        return "That is your home folder (or contains it). Use a dedicated folder such as ~/GDrive/<name>."
    if path.startswith(home + "/"):
        rel = os.path.relpath(path, home)
        if rel.split("/")[0].startswith("."):
            return "Hidden folders in your home hold app settings and can't be synced."
        return platform_mac.home_path_problem(rel) if MAC else None
    if any(path.startswith(root + "/") for root in EXTERNAL_ROOTS):
        return None
    return "Choose a folder inside your home folder or on an external drive."


def overlaps(a, b):
    a, b = os.path.realpath(a), os.path.realpath(b)
    if MAC:
        a, b = platform_mac.fold(a), platform_mac.fold(b)
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


def ago(ts):
    if not ts:
        return "never"
    secs = time.time() - ts
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    return time.strftime("%b %-d, %H:%M", time.localtime(ts))


def pair_state(pair):
    """Map a pair's stored status to (css state, icon, text shown under the rail)."""
    s = pair.get("status", "")
    if s.startswith("Syncing"):
        return "busy", "emblem-synchronizing-symbolic", "Syncing…"
    if s.startswith("Needs repair"):
        return "warn", "dialog-warning-symbolic", "Needs repair"
    if s.startswith("Blocked"):
        return "err", "dialog-error-symbolic", "Blocked: unsafe folder"
    if s.startswith("Error"):
        return "err", "dialog-error-symbolic", s
    if not pair.get("initialized"):
        return "idle", "content-loading-symbolic", "Not synced yet"
    if s == "Up to date":
        return "ok", "emblem-ok-symbolic", f"Synced {ago(pair.get('last_sync'))}"
    return "idle", "content-loading-symbolic", s


STATES = ("ok", "busy", "warn", "err", "idle")
# Newer Adwaita (e.g. 51, as shipped by Homebrew) dropped these; use the closest icon that exists.
ICON_FALLBACKS = {"emblem-ok-symbolic": "object-select-symbolic",
                  "emblem-synchronizing-symbolic": "view-refresh-symbolic"}


def icon_name(name):
    if not Gtk.IconTheme.get_default().has_icon(name):
        return ICON_FALLBACKS.get(name, name)
    return name

CSS = b"""
@define-color gs_ok #2f8a6c;
@define-color gs_busy #4a78c2;
@define-color gs_warn #b97c10;
@define-color gs_err #c4473a;

.pair-row { padding: 14px 10px 14px 18px; }
.pair-title { font-size: 1.1em; font-weight: 600; }
.pair-caption, .rail-text { font-size: 0.88em; }
.rail-line { min-height: 2px; background-color: currentColor; opacity: 0.45; }
.state-idle .rail-line { min-height: 0; background-color: transparent; border-top: 2px dashed currentColor; }
.state-ok { color: @gs_ok; }
.state-busy { color: @gs_busy; }
.state-busy .rail-line { animation: gs-pulse 1.6s ease-in-out infinite; }
.state-warn { color: @gs_warn; }
.state-err { color: @gs_err; }
.state-idle { color: alpha(@theme_fg_color, 0.5); }
@keyframes gs-pulse { 0% { opacity: 0.85; } 50% { opacity: 0.15; } 100% { opacity: 0.85; } }
.empty-title { font-size: 1.35em; font-weight: 600; }
.activity { padding: 4px 6px; }
"""


class GsyncWindow(Gtk.ApplicationWindow):
    def __init__(self, app, background=False):
        super().__init__(application=app, title="Gsync")
        self.set_default_size(780, 520)
        self.cfg = load_config()
        self.running = {}  # pair id -> Popen
        self.lock = threading.Lock()
        self.indicator = None
        self.quitting = False
        self.mac = None

        self._build_ui()
        self._refresh_pairs()
        self._check_setup()
        self._build_tray(app)
        if MAC:  # menu-bar item, Dock icon, notifications, syncing on local changes, ⌘Q
            self.mac = mac_app.MacExtras(self, pair_state)
            self._update_summary()

        GLib.timeout_add_seconds(30, self._tick)
        self.connect("delete-event", self._on_close)
        if background and self.mac and self.mac.menubar:
            self.mac.start_hidden()  # run from the menu bar; the window opens from there
        else:
            if background:
                self.iconify()
            self.show_all()

    # ---------- UI ----------
    def _build_ui(self):
        css = Gtk.CssProvider()
        css.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            self.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self.header = Gtk.HeaderBar(show_close_button=True, title="Gsync")
        self.set_titlebar(self.header)

        add = Gtk.Button(tooltip_text="Sync a Drive folder with a folder on this computer")
        add_box = Gtk.Box(spacing=6)
        add_box.pack_start(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        add_box.pack_start(Gtk.Label(label="Add folder"), False, False, 0)
        add.add(add_box)
        add.get_style_context().add_class("suggested-action")
        add.connect("clicked", self._on_add)
        self.header.pack_start(add)

        menu_btn = Gtk.MenuButton(tooltip_text="Settings")
        menu_btn.add(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        menu_btn.set_popover(self._build_settings())
        self.header.pack_end(menu_btn)

        self.btn_pause = Gtk.ToggleButton()
        self.pause_icon = Gtk.Image()
        self.btn_pause.add(self.pause_icon)
        self.btn_pause.set_active(self.cfg["paused"])
        self.btn_pause.connect("toggled", self._on_pause)
        self.header.pack_end(self.btn_pause)
        self._update_pause_button()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        self.infobar = Gtk.InfoBar(revealed=False)
        self.banner = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.infobar.get_content_area().add(self.banner)
        self.btn_connect = self.infobar.add_button("Connect Google account", 1)
        self.btn_connect.set_no_show_all(True)
        self.infobar.connect("response", lambda *_: self._on_connect(None))
        box.pack_start(self.infobar, False, False, 0)

        self.stack = Gtk.Stack(vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        box.pack_start(self.stack, True, True, 0)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.listbox.set_header_func(
            lambda row, before: row.set_header(Gtk.Separator() if before else None)
        )
        frame = Gtk.Frame(margin=18, valign=Gtk.Align.START)
        frame.add(self.listbox)
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.add(frame)
        self.stack.add_named(sw, "pairs")
        self.stack.add_named(self._build_empty(), "empty")
        # Gtk.Stack ignores set_visible_child_name() for pages that aren't visible yet; show them
        # now so _refresh_pairs() can pick the right page before the window first appears.
        for page in self.stack.get_children():
            page.show_all()
        self.rows = {}  # pair id -> widgets of its row

        self.activity = Gtk.Expander(label="Activity", margin_start=18, margin_end=18, margin_bottom=12)
        self.log = Gtk.TextView(editable=False, monospace=True, cursor_visible=False, top_margin=6,
                                bottom_margin=6, left_margin=8, right_margin=8)
        self.log.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        lsw = Gtk.ScrolledWindow(min_content_height=170, margin_top=6, shadow_type=Gtk.ShadowType.IN)
        lsw.add(self.log)
        self.activity.add(lsw)
        box.pack_start(self.activity, False, False, 0)

    def _build_settings(self):
        pop = Gtk.Popover()
        grid = Gtk.Grid(row_spacing=10, column_spacing=12, margin=14)
        pop.add(grid)
        grid.attach(Gtk.Label(label="Sync every", xalign=0), 0, 0, 1, 1)
        every = Gtk.Box(spacing=6)
        self.spin = Gtk.SpinButton.new_with_range(1, 1440, 1)
        self.spin.set_value(self.cfg["interval_min"])
        self.spin.connect("value-changed", self._on_interval)
        every.pack_start(self.spin, False, False, 0)
        every.pack_start(Gtk.Label(label="min"), False, False, 0)
        grid.attach(every, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="Start at login", xalign=0), 0, 1, 1, 1)
        autostart = platform_mac.autostart_enabled() if MAC else os.path.exists(AUTOSTART_FILE)
        self.sw_auto = Gtk.Switch(active=autostart, halign=Gtk.Align.END)
        self.sw_auto.connect("notify::active", self._on_autostart)
        grid.attach(self.sw_auto, 1, 1, 1, 1)
        grid.attach(Gtk.Separator(), 0, 2, 2, 1)
        items = [("Sync all now", lambda *_: self._sync_all()),
                 ("Reconnect Google account", self._on_connect)]
        if MAC:
            items.append(("Quit Gsync (⌘Q)", lambda *_: self._quit()))
        for row, (label, fn) in enumerate(items, start=3):
            b = Gtk.ModelButton(text=label)
            b.get_child().set_halign(Gtk.Align.START)
            b.connect("clicked", fn)
            grid.attach(b, 0, row, 2, 1)
        grid.show_all()
        return pop

    def _build_empty(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name("folder-remote-symbolic", Gtk.IconSize.DIALOG)
        icon.set_pixel_size(64)
        icon.get_style_context().add_class("dim-label")
        box.pack_start(icon, False, False, 0)
        title = Gtk.Label(label="No folders synced yet")
        title.get_style_context().add_class("empty-title")
        box.pack_start(title, False, False, 0)
        text = Gtk.Label(label="Pick a Drive folder and a folder on this computer,\nand Gsync keeps them the same.",
                         justify=Gtk.Justification.CENTER)
        text.get_style_context().add_class("dim-label")
        box.pack_start(text, False, False, 0)
        btn = Gtk.Button(label="Add folder", halign=Gtk.Align.CENTER, margin_top=8)
        btn.get_style_context().add_class("suggested-action")
        btn.connect("clicked", self._on_add)
        box.pack_start(btn, False, False, 0)
        return box

    def _endpoint(self, caption, align):
        """One side of a pair: a folder name over a small caption."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        title = Gtk.Label(xalign=align, ellipsize=3, width_chars=8, max_width_chars=8)
        title.get_style_context().add_class("pair-title")
        cap = Gtk.Label(label=caption, xalign=align, ellipsize=2, width_chars=8, max_width_chars=8)
        cap.get_style_context().add_class("pair-caption")
        cap.get_style_context().add_class("dim-label")
        box.pack_start(title, False, False, 0)
        box.pack_start(cap, False, False, 0)
        return box, title, cap

    def _make_row(self, pair):
        row = Gtk.ListBoxRow(activatable=False, selectable=False)
        grid = Gtk.Grid(column_spacing=18)
        grid.get_style_context().add_class("pair-row")
        row.add(grid)

        drive, d_title, d_cap = self._endpoint("Google Drive", 0)
        local, l_title, l_cap = self._endpoint("", 1)

        rail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        rail.set_size_request(150, -1)
        line = Gtk.Box(spacing=8)
        for w in (Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER), None, Gtk.Box(hexpand=True, valign=Gtk.Align.CENTER)):
            if w is None:
                icon = Gtk.Image()
                line.pack_start(icon, False, False, 0)
            else:
                w.get_style_context().add_class("rail-line")
                line.pack_start(w, True, True, 0)
        text = Gtk.Label(ellipsize=3, max_width_chars=22)
        text.get_style_context().add_class("rail-text")
        rail.pack_start(line, False, False, 0)
        rail.pack_start(text, False, False, 0)

        actions = Gtk.Box(spacing=2, valign=Gtk.Align.CENTER)
        sync = Gtk.Button(tooltip_text="Sync now", relief=Gtk.ReliefStyle.NONE)
        sync.add(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        sync.connect("clicked", lambda *_: self._start_sync(pair))
        more = Gtk.MenuButton(tooltip_text="More actions", relief=Gtk.ReliefStyle.NONE)
        more.add(Gtk.Image.new_from_icon_name("view-more-symbolic", Gtk.IconSize.BUTTON))
        pop = Gtk.Popover()
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin=6)
        for label, fn in (("Open folder on this computer", self._on_open_local),
                          ("Repair…", self._on_resync),
                          ("Stop syncing…", self._on_remove)):
            b = Gtk.ModelButton(text=label)
            b.get_child().set_halign(Gtk.Align.START)
            b.connect("clicked", lambda _b, fn=fn: fn(pair))
            pbox.pack_start(b, False, False, 0)
        pbox.show_all()
        pop.add(pbox)
        more.set_popover(pop)
        actions.pack_start(sync, False, False, 0)
        actions.pack_start(more, False, False, 0)

        for col, w in enumerate((drive, rail, local, actions)):
            grid.attach(w, col, 0, 1, 1)
        row.show_all()
        return {"row": row, "d_title": d_title, "d_cap": d_cap, "l_title": l_title,
                "l_cap": l_cap, "rail": rail, "icon": icon, "text": text, "sync": sync}

    def _update_row(self, w, pair):
        remote = pair["remote_path"]
        parent, _, name = remote.rpartition("/")
        w["d_title"].set_text(name or "My Drive")
        w["d_cap"].set_text("Google Drive" + (f" / {parent}" if parent else ""))
        w["d_title"].set_tooltip_text(f"Drive: {remote or 'My Drive (everything)'}")
        home = os.path.expanduser("~")
        local = pair["local_path"]
        folder, name = os.path.split(local)
        w["l_title"].set_text(name)
        w["l_cap"].set_text("~" + folder[len(home):] if folder == home or folder.startswith(home + "/") else folder)
        w["l_title"].set_tooltip_text(local)
        state, icon, text = pair_state(pair)
        ctx = w["rail"].get_style_context()
        for s in STATES:
            ctx.remove_class(f"state-{s}")
        ctx.add_class(f"state-{state}")
        w["icon"].set_from_icon_name(icon_name(icon), Gtk.IconSize.BUTTON)
        w["text"].set_text(text)
        last = f"Last successful sync: {ago(pair.get('last_sync'))}"
        w["rail"].set_tooltip_text(f"{pair['status']}\n{last}")
        w["sync"].set_sensitive(state != "busy")

    def _update_pause_button(self):
        paused = self.btn_pause.get_active()
        self.pause_icon.set_from_icon_name(
            "media-playback-start-symbolic" if paused else "media-playback-pause-symbolic", Gtk.IconSize.BUTTON
        )
        self.btn_pause.set_tooltip_text("Resume auto-sync" if paused else "Pause auto-sync")

    def _update_summary(self):
        pairs = self.cfg["pairs"]
        states = [pair_state(p)[0] for p in pairs]
        trouble = states.count("err") + states.count("warn")
        if not pairs:
            text = "Google Drive two-way sync"
        elif states.count("busy"):
            n = states.count("busy")
            text = f"Syncing {n} folder{'s' if n > 1 else ''}"
        elif trouble:
            text = f"{trouble} folder{'s need' if trouble > 1 else ' needs'} attention"
        elif self.cfg["paused"]:
            text = "Auto-sync paused"
        elif all(s == "ok" for s in states):
            text = "Everything is up to date"
        else:
            text = f"{len(pairs)} folder{'s' if len(pairs) > 1 else ''}"
        self.header.set_subtitle(text)
        if self.mac:
            self.mac.refresh(text)

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
        if self.mac and not self.quitting:
            self.mac.close_window()  # keep syncing from the menu bar (or the Dock)
            return True
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
        ids = [p["id"] for p in self.cfg["pairs"]]
        if ids != list(self.rows):
            for w in self.rows.values():
                self.listbox.remove(w["row"])
            self.rows = {}
            for p in self.cfg["pairs"]:
                self.rows[p["id"]] = self._make_row(p)
                self.listbox.add(self.rows[p["id"]]["row"])
        for p in self.cfg["pairs"]:
            self._update_row(self.rows[p["id"]], p)
        self.stack.set_visible_child_name("pairs" if ids else "empty")
        self._update_summary()

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
        self.btn_connect.set_label("Connect Google account")
        self.btn_connect.set_visible(False)
        if ver is None:
            self.infobar.set_message_type(Gtk.MessageType.ERROR)
            self.banner.set_markup(
                "<b>rclone is not installed.</b> Install the latest release with:\n"
                f"<tt>{INSTALL_RCLONE}</tt>"
            )
        elif ver < MIN_RCLONE:
            self.infobar.set_message_type(Gtk.MessageType.WARNING)
            self.banner.set_markup(
                f"<b>rclone {'.'.join(map(str, ver))} is too old for reliable sync</b> "
                f"(need {'.'.join(map(str, MIN_RCLONE))} or newer). Update with:\n"
                f"<tt>{INSTALL_RCLONE}</tt>"
            )
        elif not remote_configured():
            self.infobar.set_message_type(Gtk.MessageType.INFO)
            self.banner.set_text("Sign in to Google Drive to start syncing.")
            self.btn_connect.set_visible(True)
        elif uses_shared_client():
            self.infobar.set_message_type(Gtk.MessageType.WARNING)
            self.banner.set_text(
                "Syncing is slow because Gsync is using rclone's shared Google client. Google throttles "
                "it heavily (every sync waits about 35 s on rate limits) and it is being retired during "
                "2026. Create your own client ID to fix both."
            )
            self.btn_connect.set_label("Use my own client ID")
            self.btn_connect.set_visible(True)
        else:
            self.infobar.set_revealed(False)
            return
        self.infobar.set_revealed(True)

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

    def _on_open_local(self, pair):
        path = pair["local_path"]
        if not os.path.isdir(path):
            self._message(f"{path} doesn't exist yet. It is created on the first sync.")
            return
        Gtk.show_uri_on_window(self, GLib.filename_to_uri(path, None), Gtk.get_current_event_time())

    def _on_remove(self, p):
        if not self._confirm("Stop syncing this folder?", "Files on Drive and on disk are left untouched."):
            return
        proc = self.running.get(p["id"])
        if proc:
            proc.terminate()
        self.cfg["pairs"] = [x for x in self.cfg["pairs"] if x["id"] != p["id"]]
        save_config(self.cfg)
        self._refresh_pairs()

    def _on_resync(self, p):
        if self._confirm(
            "Repair this folder?",
            "Runs a full resync. Files missing on one side are copied over; the Drive copy wins "
            "when both sides differ. Nothing is deleted.",
        ):
            self._start_sync(p, resync=True)

    # ---------- syncing ----------
    def _sync_all(self):
        for p in self.cfg["pairs"]:
            self._start_sync(p)

    def _tick(self):
        self._refresh_pairs()  # keeps "Synced 5 min ago" current
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
            if pair_state(pair)[0] in ("err", "warn"):
                self.activity.set_expanded(True)  # show the rclone output that explains it
            if self.mac:
                self.mac.status_changed(pair)
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
                "--color", "NEVER",  # bisync colours its log even into a pipe; keep Activity readable
                # bisync lists Drive one folder at a time; on a 400-folder tree that was 474 API
                # requests per sync vs 30 with --fast-list (same listing, far fewer rate-limit stalls).
                "--fast-list",
            ]
            if MAC:
                cmd += platform_mac.BISYNC_EXCLUDES
            if resync or not pair["initialized"]:
                cmd += ["--resync"]  # default resync mode: Path1 (Drive) wins on differences
            self.log_line(f"== {name} ==")
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.running[pair["id"]] = proc
            tail, conflicts = [], []
            for line in proc.stdout:
                self.log_line(line)
                tail.append(line)
                tail = tail[-30:]
                if "New or changed in both paths" in line:  # bisync keeps the newer copy
                    conflicts.append(line.rsplit(" - ", 1)[-1].strip())
            code = proc.wait()
            if conflicts and self.mac:
                GLib.idle_add(self.mac.conflicts, pair, conflicts)
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
        self._update_pause_button()
        self._update_summary()
        self.log_line("Auto-sync paused." if self.cfg["paused"] else "Auto-sync resumed.")

    def _on_interval(self, spin):
        self.cfg["interval_min"] = spin.get_value_as_int()
        save_config(self.cfg)

    def _on_autostart(self, sw, _param):
        if MAC:
            platform_mac.set_autostart(sw.get_active(), __file__)
            return
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
    if MAC and not platform_mac.claim_single_instance(CFG_DIR):
        sys.exit(0)  # the running Gsync was asked to show its window
    sys.exit(GsyncApp().run([sys.argv[0]]))
