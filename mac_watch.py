"""Sync a pair soon after its local folder changes, using macOS FSEvents (through ctypes).

Changes made in Drive are still picked up by the regular interval; this only shortens the wait
for changes made on this Mac.
"""
import ctypes
import os
import time
from ctypes import POINTER, Structure, c_bool, c_char_p, c_double, c_long, c_size_t, c_uint32, c_uint64, c_void_p

from gi.repository import GLib

QUIET_SECONDS = 20  # wait for this long without new changes (an app saving several files, a copy)
MAX_WAIT_SECONDS = 120  # but never longer than this after the first change
IGNORED = (".DS_Store",)  # never synced (see platform_mac.BISYNC_EXCLUDES), so never worth a sync

_cf = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")

_CALLBACK = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, c_size_t, POINTER(c_char_p), POINTER(c_uint32),
                             POINTER(c_uint64))


class _Context(Structure):
    _fields_ = [("version", c_long), ("info", c_void_p), ("retain", c_void_p),
                ("release", c_void_p), ("copyDescription", c_void_p)]


for _lib, _name, _res, _args in (
    (_cf, "CFStringCreateWithCString", c_void_p, [c_void_p, c_char_p, c_uint32]),
    (_cf, "CFArrayCreate", c_void_p, [c_void_p, POINTER(c_void_p), c_long, c_void_p]),
    (_cf, "CFRelease", None, [c_void_p]),
    (_cf, "CFRunLoopGetMain", c_void_p, []),
    (_cs, "FSEventStreamCreate", c_void_p,
     [c_void_p, _CALLBACK, POINTER(_Context), c_void_p, c_uint64, c_double, c_uint32]),
    (_cs, "FSEventStreamScheduleWithRunLoop", None, [c_void_p, c_void_p, c_void_p]),
    (_cs, "FSEventStreamStart", c_bool, [c_void_p]),
    (_cs, "FSEventStreamStop", None, [c_void_p]),
    (_cs, "FSEventStreamInvalidate", None, [c_void_p]),
    (_cs, "FSEventStreamRelease", None, [c_void_p]),
):
    getattr(_lib, _name).restype = _res
    getattr(_lib, _name).argtypes = _args

_UTF8 = 0x08000100
_SINCE_NOW = 0xFFFFFFFFFFFFFFFF
_FLAG_FILE_EVENTS = 0x10  # report the changed file, not just its folder
_COMMON_MODES = c_void_p.in_dll(_cf, "kCFRunLoopCommonModes").value  # also fires while a menu is open
_ARRAY_CALLBACKS = ctypes.c_byte.in_dll(_cf, "kCFTypeArrayCallBacks")


class FolderWatcher:
    """One FSEvents stream over several folders; calls on_change(path) on the main loop."""

    def __init__(self, on_change):
        self.on_change = on_change
        self._stream = None
        self._paths = ()
        self._callback = _CALLBACK(self._on_events)  # must stay referenced while the stream lives

    def watch(self, paths):
        paths = tuple(sorted(set(paths)))
        if paths == self._paths:
            return
        self.stop()
        self._paths = paths
        if not paths:
            return
        cf_paths = [_cf.CFStringCreateWithCString(None, p.encode(), _UTF8) for p in paths]
        array = _cf.CFArrayCreate(None, (c_void_p * len(cf_paths))(*cf_paths), len(cf_paths),
                                  ctypes.addressof(_ARRAY_CALLBACKS))
        for s in cf_paths:
            _cf.CFRelease(s)
        self._stream = _cs.FSEventStreamCreate(None, self._callback, None, array, _SINCE_NOW, 1.0,
                                               _FLAG_FILE_EVENTS)
        _cf.CFRelease(array)
        _cs.FSEventStreamScheduleWithRunLoop(self._stream, _cf.CFRunLoopGetMain(), _COMMON_MODES)
        _cs.FSEventStreamStart(self._stream)

    def stop(self):
        if self._stream:
            _cs.FSEventStreamStop(self._stream)
            _cs.FSEventStreamInvalidate(self._stream)
            _cs.FSEventStreamRelease(self._stream)
        self._stream = None
        self._paths = ()

    def _on_events(self, _stream, _info, count, paths, _flags, _ids):
        for i in range(count):
            self.on_change(os.fsdecode(paths[i]))


class ChangeSync:
    """Starts a pair's sync QUIET_SECONDS after the last change in its local folder.

    Files written by Gsync's own sync also produce events, so a sync that downloaded something is
    followed by one more sync; that one finds nothing to change locally, so it doesn't repeat.
    """

    def __init__(self, start_sync, is_running, is_paused):
        self.start_sync = start_sync  # start_sync(pair)
        self.is_running = is_running  # is_running(pair) -> bool
        self.is_paused = is_paused  # is_paused() -> bool
        self.watcher = FolderWatcher(self._on_change)
        self.pairs = {}  # canonical local path -> pair
        self.pending = {}  # pair id -> (first change time, GLib source id)

    def update(self, pairs):
        """Watch the local folders of pairs that have finished their first sync."""
        self.pairs = {}
        for p in pairs:
            if p.get("initialized") and os.path.isdir(p["local_path"]):
                self.pairs[os.path.realpath(p["local_path"])] = p  # FSEvents reports real paths
        self.watcher.watch(self.pairs)
        for pid in set(self.pending) - {p["id"] for p in self.pairs.values()}:
            GLib.source_remove(self.pending.pop(pid)[1])

    def _on_change(self, path):
        name = os.path.basename(path)
        if name in IGNORED or name.startswith("._"):
            return
        pair = next((p for root, p in self.pairs.items() if path == root or path.startswith(root + "/")), None)
        if pair is None or self.is_paused():
            return
        first, source = self.pending.pop(pair["id"], (time.monotonic(), None))
        if source:
            GLib.source_remove(source)
        delay = max(0.0, min(QUIET_SECONDS, first + MAX_WAIT_SECONDS - time.monotonic()))
        self.pending[pair["id"]] = (first, GLib.timeout_add(int(delay * 1000), self._fire, pair))

    def _fire(self, pair):
        first, _ = self.pending.pop(pair["id"], (time.monotonic(), None))
        if self.is_paused():
            return False
        if self.is_running(pair):  # try again once the running sync is done
            self.pending[pair["id"]] = (first, GLib.timeout_add_seconds(QUIET_SECONDS, self._fire, pair))
        else:
            self.start_sync(pair)
        return False

    def stop(self):
        self.watcher.stop()
        for _first, source in self.pending.values():
            GLib.source_remove(source)
        self.pending = {}
