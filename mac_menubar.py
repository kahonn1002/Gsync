"""Gsync's menu-bar item on macOS: a native NSStatusItem with an NSMenu.

macOS has no tray for AppIndicator, and Gtk.StatusIcon can neither show a native menu nor use
template images (icons that follow the light/dark menu bar), so this talks to AppKit directly.
"""
from gi.repository import Gdk, GLib, Gtk

import mac_icons
import mac_objc as objc
from mac_objc import NSSize, c_bool, c_char_p, c_double, c_long, c_ulong

# Problems get a coloured icon so they stand out; every other state is a template cloud glyph
# (mac_icons.menubar_glyph) that macOS tints to match the menu bar.
ALERT_ICONS = {"warn": ("dialog-warning-symbolic", (0.73, 0.49, 0.06)),
               "err": ("dialog-error-symbolic", (0.77, 0.28, 0.23))}

_handler = [None]  # the MenuBar whose callback receives clicks


def _on_menu_action(_self, _cmd, sender):
    tag = objc.send(sender, "tag", restype=c_long)
    bar = _handler[0]
    if bar is not None and tag in bar.actions:
        # Run after the menu has closed and outside AppKit's menu-tracking loop.
        GLib.idle_add(bar.callback, bar.actions[tag])


def _alert_png(name, rgb):
    """Render a coloured symbolic icon at 2x as PNG bytes."""
    info = Gtk.IconTheme.get_default().lookup_icon(name, 36, Gtk.IconLookupFlags.FORCE_SIZE)
    if info is None:
        return None
    pixbuf, _ = info.load_symbolic(Gdk.RGBA(*rgb, 1), None, None, None)
    ok, data = pixbuf.save_to_bufferv("png", [], [])
    return bytes(data) if ok else None


class MenuBar:
    def __init__(self, callback):
        """callback(key) runs on the GTK main loop when the user picks a menu entry."""
        self.callback = callback
        self.actions = {}
        self._images = {}
        self._state = None
        self._menu_key = None
        target_cls = objc.define_class("GsyncMenuTarget", "NSObject", {"gsyncAction:": ("v@:@", _on_menu_action)})
        with objc.autorelease_pool():
            self._target = objc.send(objc.send(target_cls, "alloc"), "init")
            bar = objc.send(objc.cls("NSStatusBar"), "systemStatusBar")
            self._item = objc.send(bar, "statusItemWithLength:", -1.0, argtypes=(c_double,))  # variable width
            objc.send(self._item, "retain")
            self._button = objc.send(self._item, "button")
            self._menu = objc.new("NSMenu")
            objc.send(self._menu, "setAutoenablesItems:", False, restype=None, argtypes=(c_bool,))
            objc.send(self._item, "setMenu:", self._menu, restype=None)
        _handler[0] = self

    def set_state(self, state, tooltip):
        with objc.autorelease_pool():
            if state != self._state:
                image = self._image(state)
                if image:
                    objc.send(self._button, "setImage:", image, restype=None)
                else:  # icon theme missing: fall back to text
                    objc.set_string(self._button, "setTitle:", "Gsync")
                self._state = state
            objc.set_string(self._button, "setToolTip:", tooltip)

    def set_menu(self, entries):
        """entries: list of (label, key, enabled) tuples, or None for a separator.
        Keys are passed to the callback; a key of None makes an informational line."""
        key = repr(entries)
        if key == self._menu_key:  # rebuilt every refresh otherwise; skip when nothing changed
            return
        self._menu_key = key
        self.actions = {}
        with objc.autorelease_pool():
            objc.send(self._menu, "removeAllItems", restype=None)
            for tag, entry in enumerate(entries, start=1):
                if entry is None:
                    item = objc.send(objc.cls("NSMenuItem"), "separatorItem")
                    objc.send(self._menu, "addItem:", item, restype=None)
                    continue
                label, action, enabled = entry
                shortcut = "q" if action == "quit" else ""
                title, key_eq = objc.nsstring(label), objc.nsstring(shortcut)
                item = objc.new("NSMenuItem", "initWithTitle:action:keyEquivalent:",
                                title, objc.sel("gsyncAction:") if action else None, key_eq)
                objc.release(title)
                objc.release(key_eq)
                if action:
                    objc.send(item, "setTarget:", self._target, restype=None)
                    objc.send(item, "setTag:", tag, restype=None, argtypes=(c_long,))
                    self.actions[tag] = action
                objc.send(item, "setEnabled:", bool(enabled and action), restype=None, argtypes=(c_bool,))
                objc.send(self._menu, "addItem:", item, restype=None)
                objc.release(item)

    def _image(self, state):
        if state in self._images:
            return self._images[state]
        template = state not in ALERT_ICONS
        png = mac_icons.menubar_glyph(state) if template else _alert_png(*ALERT_ICONS[state])
        image = None
        if png:
            data = objc.send(objc.cls("NSData"), "dataWithBytes:length:", png, len(png),
                             argtypes=(c_char_p, c_ulong))
            image = objc.new("NSImage", "initWithData:", data)
            objc.send(image, "setSize:", NSSize(18, 18), restype=None, argtypes=(NSSize,))
            objc.send(image, "setTemplate:", template, restype=None, argtypes=(c_bool,))
        self._images[state] = image  # kept for the life of the app
        return image

    def remove(self):
        with objc.autorelease_pool():
            bar = objc.send(objc.cls("NSStatusBar"), "systemStatusBar")
            objc.send(bar, "removeStatusItem:", self._item, restype=None)
        _handler[0] = None
