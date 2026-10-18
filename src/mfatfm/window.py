"""The standalone app's window: a header bar around a :class:`FileManagerView`.

It is also the reference for embedding: everything here is what a host
application would do with the view in its own window.
"""

from __future__ import annotations

from gettext import gettext as _
from typing import Optional, Union

from gi.repository import Adw, Gio, Gtk

from .locations import Location
from .settings import get_settings
from .view import FileManagerView


class FileManagerWindow(Adw.ApplicationWindow):
    """Top-level window hosting one :class:`FileManagerView`."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        left: Union[Location, str, None] = None,
        right: Union[Location, str, None] = None,
        **view_options,
    ) -> None:
        super().__init__(application=application)
        settings = get_settings()
        width, height = settings.get("window_size", [1100, 680])
        self.set_default_size(int(width), int(height))
        self.set_size_request(600, 400)
        if settings.get("window_maximized", False):
            self.maximize()

        self.view = FileManagerView(left=left, right=right, **view_options)
        # The header bar's buttons and menu use the view's actions.
        self.insert_action_group("fm", self.view.actions)

        header = Adw.HeaderBar()
        self._title = Adw.WindowTitle(title=self.view.title, subtitle=self.view.subtitle)
        header.set_title_widget(self._title)
        self.view.connect("title-changed", self._on_title_changed)

        connect_button = Gtk.Button.new_from_icon_name("network-server-symbolic")
        connect_button.set_tooltip_text(_("Connect to Server (Ctrl+K)"))
        connect_button.set_action_name("fm.connect")
        header.pack_start(connect_button)

        menu = Gio.Menu()
        section = Gio.Menu()
        section.append(_("New Window"), "app.new-window")
        section.append(_("Two Panes"), "fm.dual-pane")
        section.append(_("Swap Panes"), "fm.swap-panes")
        section.append(_("Same Folder in Both Panes"), "fm.same-location")
        menu.append_section(None, section)
        section = Gio.Menu()
        section.append(_("Keyboard Shortcuts"), "fm.show-shortcuts")
        section.append(_("About mfatfm"), "app.about")
        menu.append_section(None, section)
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        menu_button.set_tooltip_text(_("Main Menu"))
        menu_button.set_primary(True)
        header.pack_end(menu_button)
        header.pack_end(self.view.operations_button)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(header)
        toolbar_view.set_content(self.view)
        self.set_content(toolbar_view)

        close = Gio.SimpleAction.new("close", None)
        close.connect("activate", lambda *_a: self.close())
        self.add_action(close)
        application.set_accels_for_action("win.close", ["<primary>w"])
        self.connect("close-request", self._on_close_request)
        self._closing = False

    def _on_title_changed(self, _view, title: str, subtitle: str) -> None:
        self._title.set_title(title)
        self._title.set_subtitle(subtitle)
        self.set_title(f"{title} — mfatfm")

    def _on_close_request(self, _window) -> bool:
        if self.view.has_running_operations() and not self._closing:
            dialog = Adw.AlertDialog(
                heading=_("Stop Running Operations?"),
                body=_("Closing the window cancels the copies and deletions still in progress."),
            )
            dialog.add_response("cancel", _("Keep Open"))
            dialog.add_response("close", _("Stop and Close"))
            dialog.set_response_appearance("close", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")

            def _on_response(_dialog, response: str) -> None:
                if response == "close":
                    self._closing = True
                    self.close()

            dialog.connect("response", _on_response)
            dialog.present(self)
            return True
        settings = get_settings()
        if not self.is_maximized():
            settings.set("window_size", [self.get_width(), self.get_height()])
        settings.set("window_maximized", self.is_maximized())
        self.view.shutdown()
        return False


def launch_file_manager_window(
    application: Adw.Application,
    left: Union[Location, str, None] = None,
    right: Union[Location, str, None] = None,
    *,
    transient_for: Optional[Gtk.Window] = None,
    **view_options,
) -> FileManagerWindow:
    """Open a file manager window from another application and return it."""
    window = FileManagerWindow(application, left=left, right=right, **view_options)
    if transient_for is not None:
        window.set_transient_for(transient_for)
    window.present()
    return window
