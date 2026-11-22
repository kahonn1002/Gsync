"""Just enough of the Objective-C runtime, through ctypes, for Gsync's macOS features.

This avoids depending on PyObjC (which Homebrew's Python would need in a separate venv). Every call
goes through objc_msgSend with an explicit prototype, which arm64 requires.
"""
import contextlib
import ctypes
import functools
from ctypes import c_bool, c_char_p, c_double, c_long, c_size_t, c_ulong, c_void_p

_objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")  # GTK has loaded it already

for _name, _res, _args in (
    ("objc_getClass", c_void_p, [c_char_p]),
    ("sel_registerName", c_void_p, [c_char_p]),
    ("objc_allocateClassPair", c_void_p, [c_void_p, c_char_p, c_size_t]),
    ("class_addMethod", c_bool, [c_void_p, c_void_p, c_void_p, c_char_p]),
    ("objc_registerClassPair", None, [c_void_p]),
    ("objc_autoreleasePoolPush", c_void_p, []),
    ("objc_autoreleasePoolPop", None, [c_void_p]),
):
    getattr(_objc, _name).restype = _res
    getattr(_objc, _name).argtypes = _args

_prototypes = {}
_keep_alive = []  # Python callbacks handed to the runtime must never be garbage-collected


class NSSize(ctypes.Structure):
    _fields_ = [("width", c_double), ("height", c_double)]


@functools.lru_cache(maxsize=None)
def cls(name):
    return _objc.objc_getClass(name.encode())


@functools.lru_cache(maxsize=None)
def sel(name):
    return _objc.sel_registerName(name.encode())


def send(obj, selector, *args, restype=c_void_p, argtypes=None):
    """[obj selector args...]. Arguments default to object pointers unless argtypes says otherwise."""
    argtypes = tuple(argtypes) if argtypes is not None else (c_void_p,) * len(args)
    key = (restype, argtypes)
    fn = _prototypes.get(key)
    if fn is None:
        fn = _prototypes[key] = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(("objc_msgSend", _objc))
    return fn(obj, sel(selector), *args)


def new(class_name, init="init", *args, argtypes=None):
    """[[Class alloc] init...]; the caller owns the result."""
    return send(send(cls(class_name), "alloc"), init, *args, argtypes=argtypes)


def nsstring(text):
    """An owned NSString; release() it once handed to a setter (setters retain or copy)."""
    return new("NSString", "initWithUTF8String:", text.encode(), argtypes=(c_char_p,))


def release(obj):
    if obj:
        send(obj, "release", restype=None)


def set_string(obj, setter, text):
    s = nsstring(text)
    send(obj, setter, s, restype=None)
    release(s)


@contextlib.contextmanager
def autorelease_pool():
    """Cocoa calls made from GLib callbacks run outside any AppKit autorelease pool."""
    pool = _objc.objc_autoreleasePoolPush()
    try:
        yield
    finally:
        _objc.objc_autoreleasePoolPop(pool)


def define_class(name, superclass, methods):
    """Register an Objective-C class whose methods are Python functions.

    methods maps a selector to (type encoding, function); each function receives (self, _cmd, *args)
    as raw pointers. Returns the class, or the existing one if this process already defined it.
    """
    existing = _objc.objc_getClass(name.encode())
    if existing:
        return existing
    new_cls = _objc.objc_allocateClassPair(cls(superclass), name.encode(), 0)
    for selector, (encoding, fn) in methods.items():
        # encoding "v@:@" = returns void; takes self, _cmd, one object
        argc = len(encoding) - 3
        imp = ctypes.CFUNCTYPE(None, c_void_p, c_void_p, *([c_void_p] * argc))(fn)
        _keep_alive.append(imp)
        _objc.class_addMethod(new_cls, sel(selector), ctypes.cast(imp, c_void_p), encoding.encode())
    _objc.objc_registerClassPair(new_cls)
    return new_cls


def ns_app():
    return send(cls("NSApplication"), "sharedApplication")


def activate_app():
    """Bring this app in front of other apps (GTK's present() only raises the window)."""
    send(ns_app(), "activateIgnoringOtherApps:", True, restype=None, argtypes=(c_bool,))


def set_dock_icon_visible(visible):
    """Regular apps have a Dock icon; accessory apps live only in the menu bar."""
    policy = 0 if visible else 1  # NSApplicationActivationPolicyRegular / Accessory
    send(ns_app(), "setActivationPolicy:", policy, restype=c_bool, argtypes=(c_long,))
