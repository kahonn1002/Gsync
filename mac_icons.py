"""Gsync's artwork, drawn with cairo: the app icon and the menu-bar glyphs.

Run `python3 mac_icons.py <dir>.iconset` to write the PNG sizes iconutil needs (install_mac.sh does).
"""
import math
import os
import sys

import cairo


def _cloud(cr, s):
    """A cloud filling a 36-unit box, scaled by s/36."""
    k = s / 36
    for cx, cy, r in ((11, 21, 7), (18.5, 15, 9), (26, 21, 7)):
        cr.arc(cx * k, cy * k, r * k, 0, 2 * math.pi)
        cr.fill()
    cr.rectangle(11 * k, 14 * k, 15 * k, 14 * k)
    cr.fill()


def _sync_arrows(cr, cx, cy, r, width):
    """Two half-circle arrows chasing each other (the 'sync' symbol)."""
    cr.set_line_width(width)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    head = width * 1.6
    for start in (math.radians(200), math.radians(20)):
        end = start + math.radians(125)
        cr.arc(cx, cy, r, start, end)
        cr.stroke()
        ex, ey = cx + r * math.cos(end), cy + r * math.sin(end)
        tangent = end + math.pi / 2  # direction of travel at the arc's end
        cr.move_to(ex + head * math.cos(tangent), ey + head * math.sin(tangent))
        cr.line_to(ex + head * math.cos(tangent + 2.3), ey + head * math.sin(tangent + 2.3))
        cr.line_to(ex + head * math.cos(tangent - 2.3), ey + head * math.sin(tangent - 2.3))
        cr.close_path()
        cr.fill()


def _png(surface):
    import io

    buf = io.BytesIO()
    surface.write_to_png(buf)
    return buf.getvalue()


def menubar_glyph(state, size=36):
    """Black-on-transparent PNG for a template image: cloud, with sync arrows or pause bars cut out."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    cr.set_source_rgba(0, 0, 0, 1)
    _cloud(cr, size)
    k = size / 36
    cr.set_operator(cairo.OPERATOR_CLEAR)
    if state == "busy":
        _sync_arrows(cr, 18.5 * k, 20.5 * k, 4.6 * k, 1.9 * k)
    elif state == "paused":
        for x in (15.2, 19.8):
            cr.rectangle(x * k, 15.5 * k, 2.4 * k, 10 * k)
        cr.fill()
    return _png(surface)


def app_icon(size):
    """The Dock icon: a blue rounded square with a white cloud and blue sync arrows."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surface)
    margin, body = size * 100 / 1024, size * 824 / 1024  # Apple's icon grid: 824 of 1024
    radius = body * 0.225
    x0 = y0 = margin
    cr.new_sub_path()
    for cx, cy, a in ((x0 + body - radius, y0 + radius, -90), (x0 + body - radius, y0 + body - radius, 0),
                      (x0 + radius, y0 + body - radius, 90), (x0 + radius, y0 + radius, 180)):
        cr.arc(cx, cy, radius, math.radians(a), math.radians(a + 90))
    cr.close_path()
    grad = cairo.LinearGradient(0, y0, 0, y0 + body)
    grad.add_color_stop_rgb(0, 0.33, 0.67, 0.98)
    grad.add_color_stop_rgb(1, 0.10, 0.42, 0.86)
    cr.set_source(grad)
    cr.fill()

    cloud = body * 0.78
    cr.save()
    cr.translate(x0 + (body - cloud) / 2, y0 + (body - cloud) / 2 - body * 0.02)
    cr.set_source_rgb(1, 1, 1)
    _cloud(cr, cloud)
    k = cloud / 36
    cr.set_source_rgb(0.12, 0.45, 0.88)
    _sync_arrows(cr, 18.5 * k, 20.5 * k, 4.4 * k, 1.5 * k)
    cr.restore()
    return _png(surface)


def write_iconset(path):
    os.makedirs(path, exist_ok=True)
    for pt in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            name = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
            with open(os.path.join(path, name), "wb") as f:
                f.write(app_icon(pt * scale))


if __name__ == "__main__":
    write_iconset(sys.argv[1])
