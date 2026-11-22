"""macOS notifications for Gsync.

Inside Gsync.app this uses Apple's UserNotifications framework, so notifications come from Gsync
(macOS asks once whether to allow them). Run any other way (python3 gsync.py), the process has no
bundle of its own and UserNotifications would abort it, so it falls back to osascript, whose
notifications macOS attributes to Script Editor.
"""
import ctypes
import subprocess
import uuid
from ctypes import POINTER, Structure, c_bool, c_char_p, c_int, c_ulong, c_void_p

import mac_objc as objc

BUNDLE_ID = "local.gsync"
_OPTIONS = 4 | 2  # UNAuthorizationOptionAlert | UNAuthorizationOptionSound


class _BlockDescriptor(Structure):
    _fields_ = [("reserved", c_ulong), ("size", c_ulong)]


class _Block(Structure):
    _fields_ = [("isa", c_void_p), ("flags", c_int), ("reserved", c_int),
                ("invoke", c_void_p), ("descriptor", POINTER(_BlockDescriptor))]


_GLOBAL_BLOCK = ctypes.addressof(c_void_p.in_dll(ctypes.CDLL("/usr/lib/libSystem.B.dylib"), "_NSConcreteGlobalBlock"))
_keep_alive = []


def _block(fn, *argtypes):
    """An Objective-C block with no captured variables that calls fn(*args)."""
    invoke = ctypes.CFUNCTYPE(None, c_void_p, *argtypes)(lambda _blk, *args: fn(*args))
    desc = _BlockDescriptor(0, ctypes.sizeof(_Block))
    block = _Block(_GLOBAL_BLOCK, 1 << 28, 0, ctypes.cast(invoke, c_void_p), ctypes.pointer(desc))  # BLOCK_IS_GLOBAL
    _keep_alive.extend((invoke, desc, block))
    return ctypes.addressof(block)


def running_as_app():
    """True when this process is Gsync.app (the compiled launcher), not plain python3."""
    with objc.autorelease_pool():
        bundle = objc.send(objc.cls("NSBundle"), "mainBundle")
        ident = objc.send(bundle, "bundleIdentifier")
        return bool(ident) and objc.send(ident, "UTF8String", restype=c_char_p) == BUNDLE_ID.encode()


def _quote(text):  # AppleScript string literal
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


class Notifier:
    def __init__(self, log=print):
        self.log = log
        self.center = None
        if not running_as_app():
            return
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/UserNotifications.framework/UserNotifications")
        with objc.autorelease_pool():
            self.center = objc.send(objc.cls("UNUserNotificationCenter"), "currentNotificationCenter")
            objc.send(self.center, "retain")
            # Shows "Allow Gsync to send notifications?" the first time; later calls just answer.
            done = _block(self._authorized, c_bool, c_void_p)
            objc.send(self.center, "requestAuthorizationWithOptions:completionHandler:", _OPTIONS, done,
                      restype=None, argtypes=(c_ulong, c_void_p))

    def _authorized(self, granted, _error):
        if not granted:  # runs on a background queue; only log
            self.log("Notifications are off for Gsync (System Settings → Notifications → Gsync).")

    def notify(self, title, body):
        if self.center is None:
            script = f"display notification {_quote(body)} with title {_quote(title)}"
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        with objc.autorelease_pool():
            content = objc.new("UNMutableNotificationContent")
            objc.set_string(content, "setTitle:", title)
            objc.set_string(content, "setBody:", body)
            ident = objc.nsstring(uuid.uuid4().hex)
            request = objc.send(objc.cls("UNNotificationRequest"), "requestWithIdentifier:content:trigger:",
                                ident, content, None)
            objc.send(self.center, "addNotificationRequest:withCompletionHandler:", request, None, restype=None)
            objc.release(ident)
            objc.release(content)
