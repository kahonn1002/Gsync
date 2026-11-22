"""macOS-specific pieces of Gsync. gsync.py imports this only when sys.platform == "darwin"."""
import ctypes
import fcntl
import os
import plistlib
import socket
import sys
import time
import unicodedata

from gi.repository import GLib

LABEL = "local.gsync"
HOME = os.path.expanduser("~")
LAUNCH_AGENT = os.path.join(HOME, "Library", "LaunchAgents", f"{LABEL}.plist")
LOG_FILE = os.path.join(HOME, "Library", "Logs", "gsync.log")
ICLOUD_DRIVE = os.path.join(HOME, "Library", "Mobile Documents", "com~apple~CloudDocs")
BREW_PREFIXES = ("/opt/homebrew", "/usr/local")  # Apple silicon, Intel

INSTALL_RCLONE = "brew install rclone"
EXTERNAL_ROOTS = ("/volumes",)  # already fold()ed: compared against fold(path)
# Finder litter. Excluded on both sides so it never reaches Drive or comes back down.
BISYNC_EXCLUDES = ["--exclude", ".DS_Store", "--exclude", "._*"]

# Top-level home folders that hold Photos / Music / TV libraries: live databases that two-way sync
# would corrupt and that are often huge. Subfolders the user made inside them are fine.
LIBRARY_HOMES = {"pictures": "Photos", "music": "Music", "movies": "TV"}
PACKAGE_SUFFIXES = (".photoslibrary", ".musiclibrary", ".tvlibrary", ".app")


def fix_path():
    """Put Homebrew's bin on PATH. Apps started by launchd or Finder get only /usr/bin:/bin:...,
    so shutil.which("rclone") would fail and Gsync would report rclone as missing."""
    parts = os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(":")
    for prefix in reversed(BREW_PREFIXES):
        bin_dir = os.path.join(prefix, "bin")
        if os.path.isdir(bin_dir) and bin_dir not in parts:
            parts.insert(0, bin_dir)
    os.environ["PATH"] = ":".join(parts)


def fold(path):
    """APFS is case-insensitive and normalization-insensitive by default, so compare paths that way.
    Folding only ever makes the safety checks stricter, never looser."""
    return unicodedata.normalize("NFC", path).casefold()


def home_path_problem(rel):
    """Extra macOS rules for a folder inside home. `rel` is fold()ed and relative to home."""
    parts = rel.split("/")
    if parts[0] == "library":
        return ("~/Library holds app data, iCloud Drive and other cloud-storage folders, "
                "and can't be synced.")
    if any(p.endswith(PACKAGE_SUFFIXES) for p in parts):
        return "That folder is inside an app or a Photos/Music library, which can't be synced."
    if len(parts) == 1 and parts[0] in LIBRARY_HOMES:
        return (f"~/{parts[0].capitalize()} holds the {LIBRARY_HOMES[parts[0]]} library. "
                f"Use a folder inside it, such as ~/{parts[0].capitalize()}/Drive.")
    if parts[0] in ("desktop", "documents") and os.path.isdir(os.path.join(ICLOUD_DRIVE, "Desktop")):
        return ("iCloud already syncs your Desktop and Documents folders. Syncing them with Drive "
                "too leads to conflicts; use a folder such as ~/GDrive/<name>.")
    return None


def activate_app():
    """Bring Gsync in front of other apps. GTK's present() raises the window inside Gsync but does
    not activate the app, so it stays behind whatever app is in front. AppKit is already loaded by
    GTK's Quartz backend, so a direct [NSApp activateIgnoringOtherApps:YES] is enough."""
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.restype = objc.sel_registerName.restype = ctypes.c_void_p
        send = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
        send_bool = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(("objc_msgSend", objc))
        app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
        if app:
            send_bool(app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
    except (OSError, AttributeError):
        pass  # cosmetic: the window is still raised, just maybe behind another app


_instance = {}  # keeps the lock file and listening socket alive for the life of the process
show_handlers = []  # called on the main loop when another launch asks this Gsync to show itself


def claim_single_instance(state_dir):
    """Return True if this is the only Gsync running. Otherwise ask the running one to show its
    window and return False. (GApplication does this through D-Bus on Linux; macOS has no D-Bus,
    so without this, opening Gsync while autostart already runs it would sync every pair twice.)"""
    os.makedirs(state_dir, exist_ok=True)
    sock_path = os.path.join(state_dir, "gsync.sock")
    lock = open(os.path.join(state_dir, "gsync.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # released by the OS when we exit
    except BlockingIOError:
        lock.close()
        for _ in range(30):  # the running one may still be starting up
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(sock_path)
                    s.sendall(b"show\n")
                return False
            except OSError:
                time.sleep(0.1)
        return False

    try:
        os.unlink(sock_path)  # left over from a crash
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)

    def on_request(_channel, _cond):
        conn, _ = server.accept()
        conn.close()
        for handler in show_handlers:
            handler()
        return True

    channel = GLib.IOChannel.unix_new(server.fileno())
    GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN, on_request)
    _instance.update(lock=lock, server=server, channel=channel)
    return True


def python_for_launch():
    """The interpreter autostart should run. Prefer Homebrew's unversioned python3: it moves with
    Homebrew upgrades together with pygobject3, while sys.executable names one Python version."""
    for prefix in BREW_PREFIXES:
        if sys.executable.startswith(prefix + "/"):
            stable = os.path.join(prefix, "bin", "python3")
            if os.path.exists(stable):
                return stable
    return sys.executable


def autostart_enabled():
    return os.path.exists(LAUNCH_AGENT)


def set_autostart(enabled, script):
    """Write or remove the LaunchAgent. launchd reads ~/Library/LaunchAgents at login.

    No launchctl calls on purpose: bootstrapping now would start a second Gsync next to this one
    (GApplication's single-instance check needs D-Bus, which macOS lacks), and booting out the job
    would kill this very process when it was itself started at login.
    """
    if not enabled:
        if os.path.exists(LAUNCH_AGENT):
            os.remove(LAUNCH_AGENT)
        return
    plist = {
        "Label": LABEL,
        "ProgramArguments": [python_for_launch(), os.path.abspath(script), "--background"],
        "RunAtLoad": True,
        "LimitLoadToSessionType": "Aqua",  # only in a logged-in GUI session
        "ProcessType": "Interactive",
        # fix_path() covers this too; set it anyway so rclone resolves the same way at every login.
        "EnvironmentVariables": {"PATH": ":".join(
            [os.path.join(p, "bin") for p in BREW_PREFIXES] + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])},
        "StandardOutPath": LOG_FILE,
        "StandardErrorPath": LOG_FILE,
    }
    os.makedirs(os.path.dirname(LAUNCH_AGENT), exist_ok=True)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    tmp = LAUNCH_AGENT + ".tmp"
    with open(tmp, "wb") as f:
        plistlib.dump(plist, f)
    os.replace(tmp, LAUNCH_AGENT)
