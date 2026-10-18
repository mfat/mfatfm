"""Icons for files, from the icon theme's content types (as Nautilus does)."""

from __future__ import annotations

import os
from typing import Dict

from gi.repository import Gio

_FOLDER_ICON = Gio.ThemedIcon.new_with_default_fallbacks("folder")
_cache: Dict[str, Gio.Icon] = {}


def icon_for_name(name: str, is_dir: bool) -> Gio.Icon:
    """The themed icon for an entry, guessed from its name."""
    if is_dir:
        return _FOLDER_ICON
    _stem, ext = os.path.splitext(name.lower())
    key = ext or name.lower()
    icon = _cache.get(key)
    if icon is None:
        content_type, _uncertain = Gio.content_type_guess(name, None)
        icon = Gio.content_type_get_icon(content_type) or Gio.ThemedIcon.new("text-x-generic")
        _cache[key] = icon
    return icon


def image(icon_name: str, size: int = 0):
    """A ``Gtk.Image`` for a themed icon name."""
    from gi.repository import Gtk

    widget = Gtk.Image.new_from_icon_name(icon_name)
    if size:
        widget.set_pixel_size(size)
    return widget


def icon_button(icon_name: str, tooltip: str = ""):
    from gi.repository import Gtk

    button = Gtk.Button.new_from_icon_name(icon_name)
    if tooltip:
        button.set_tooltip_text(tooltip)
    return button
