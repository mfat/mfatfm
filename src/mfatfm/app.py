"""The mfatfm application: command line, windows and app-wide actions."""

from __future__ import annotations

import logging
import os
import sys
from gettext import gettext as _
from typing import List, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from . import __version__  # noqa: E402
from .locations import Location, parse_location  # noqa: E402

APP_ID = "io.github.mfat.mfatfm"

logger = logging.getLogger(__name__)

_MIN_GTK = (4, 12)
_MIN_ADW = (1, 5)


class MfatfmApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.add_main_option(
            "new-window", ord("n"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            _("Open a new window (the default)"), None,
        )
        self.add_main_option(
            "verbose", ord("v"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
            _("Log debugging information"), None,
        )
        self.set_option_context_parameter_string(_("[LOCATION] [LOCATION]"))
        self.set_option_context_summary(
            _("Open up to two locations, one per pane: a local path, "
              "sftp://[user@]host[:port]/path or user@host:path.")
        )

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        if (Gtk.get_major_version(), Gtk.get_minor_version()) < _MIN_GTK or (
            Adw.get_major_version(), Adw.get_minor_version()
        ) < _MIN_ADW:
            logger.warning("mfatfm needs GTK %s.%s and libadwaita %s.%s or newer", *_MIN_GTK, *_MIN_ADW)
        for name, callback, accels in (
            ("new-window", lambda *_a: self.new_window(), ["<primary>n"]),
            ("about", lambda *_a: self._show_about(), []),
            ("quit", lambda *_a: self._quit(), ["<primary>q"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        options = command_line.get_options_dict().end().unpack()
        if options.get("verbose"):
            logging.getLogger().setLevel(logging.DEBUG)
        args: List[str] = list(command_line.get_arguments()[1:])
        cwd = command_line.get_cwd() or os.getcwd()
        locations: List[Location] = []
        for arg in args[:2]:
            try:
                location = parse_location(arg)
            except ValueError as exc:
                command_line.printerr(f"mfatfm: {exc}\n")
                return 1
            if location.target is None and not os.path.isabs(os.path.expanduser(location.path)):
                location = Location(path=os.path.join(cwd, location.path))
            locations.append(location)
        if len(args) > 2:
            command_line.printerr(_("mfatfm: only two locations can be opened; ignoring the rest\n"))
        left = locations[0] if locations else None
        right = locations[1] if len(locations) > 1 else None
        self.new_window(left, right)
        return 0

    def new_window(self, left: Optional[Location] = None, right: Optional[Location] = None) -> None:
        from .window import FileManagerWindow

        window = FileManagerWindow(self, left=left, right=right)
        window.present()

    def _quit(self) -> None:
        for window in list(self.get_windows()):
            window.close()

    def _show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="mfatfm",
            application_icon="system-file-manager",
            version=__version__,
            developer_name="Mehdi",
            license_type=Gtk.License.GPL_3_0,
            comments=_("A two-pane file manager that can also browse SFTP servers."),
            website="https://github.com/mfat/mfatfm",
            issue_url="https://github.com/mfat/mfatfm/issues",
        )
        about.present(self.get_active_window())


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO if os.environ.get("MFATFM_DEBUG") is None else logging.DEBUG,
        format="%(levelname)s %(name)s: %(message)s",
    )
    app = MfatfmApplication()
    return app.run(sys.argv if argv is None else argv)


if __name__ == "__main__":
    sys.exit(main())
