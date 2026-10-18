"""The Connect to Server dialog."""

from __future__ import annotations

from gettext import gettext as _
from typing import Callable, List

from gi.repository import Adw, Gtk

from .format_utils import safe_display_text
from .locations import Location, parse_server_address, ssh_config_hosts
from .settings import get_settings


class ConnectDialog(Adw.AlertDialog):
    """Ask for a server: typed, a recent one, or a ``~/.ssh/config`` host."""

    def __init__(self, on_connect: Callable[[Location], None]) -> None:
        super().__init__(
            heading=_("Connect to Server"),
            body=_("Enter an address such as user@example.com, sftp://host:2222/path or a host from your SSH config."),
        )
        self._on_connect = on_connect
        self.add_response("cancel", _("Cancel"))
        self.add_response("connect", _("Connect"))
        self.set_response_appearance("connect", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("connect")
        self.set_close_response("cancel")
        self.set_response_enabled("connect", False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        entry_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entry_list.add_css_class("boxed-list")
        self._entry = Adw.EntryRow(title=_("Server Address"))
        self._entry.set_activates_default(True)
        self._entry.connect("changed", self._on_changed)
        entry_list.append(self._entry)
        box.append(entry_list)

        self._error = Gtk.Label(xalign=0)
        self._error.add_css_class("error")
        self._error.add_css_class("caption")
        self._error.set_wrap(True)
        self._error.set_visible(False)
        box.append(self._error)

        suggestions = self._suggestions()
        if suggestions:
            heading = Gtk.Label(label=_("Recent and Configured Servers"), xalign=0)
            heading.add_css_class("heading")
            heading.set_margin_top(6)
            box.append(heading)
            suggestion_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
            suggestion_list.add_css_class("boxed-list")
            for text, icon_name in suggestions:
                row = Adw.ActionRow(title=safe_display_text(text))
                row.set_activatable(True)
                row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
                row.connect("activated", self._on_suggestion_activated, text)
                suggestion_list.append(row)
            scrolled = Gtk.ScrolledWindow()
            scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled.set_propagate_natural_height(True)
            scrolled.set_max_content_height(240)
            scrolled.set_child(suggestion_list)
            box.append(scrolled)
        self.set_extra_child(box)
        self.connect("response", self._on_response)

    @staticmethod
    def _suggestions() -> List[tuple]:
        seen = set()
        items = []
        for uri in get_settings().recent_servers():
            if uri not in seen:
                seen.add(uri)
                items.append((uri, "document-open-recent-symbolic"))
        for alias in ssh_config_hosts():
            if alias not in seen:
                seen.add(alias)
                items.append((alias, "network-server-symbolic"))
        return items[:30]

    def _parse(self):
        try:
            return parse_server_address(self._entry.get_text()), None
        except ValueError as exc:
            return None, str(exc)

    def _on_changed(self, _entry) -> None:
        location, _error = self._parse()
        self.set_response_enabled("connect", location is not None)
        self._error.set_visible(False)

    def _on_suggestion_activated(self, _row, text: str) -> None:
        self._entry.set_text(text)
        location, _error = self._parse()
        if location is not None:
            self.close()
            self._on_connect(location)

    def _on_response(self, _dialog, response: str) -> None:
        if response != "connect":
            return
        location, error = self._parse()
        if location is not None:
            self._on_connect(location)

    def present_for(self, parent: Gtk.Widget) -> None:
        self.present(parent)
        self._entry.grab_focus()
