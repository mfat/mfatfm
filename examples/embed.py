"""Embedding mfatfm in another GTK 4 / libadwaita application.

Run:  python examples/embed.py [user@]host

The host window has its own header bar. It connects to *host* itself (as an
SSH client app would, with its own ssh options) and lends that connection to
the file manager. Any other server the user opens goes through the host's
``connection_factory``.
"""

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import mfatsftp  # noqa: E402
from gi.repository import Adw, Gtk  # noqa: E402

from mfatfm import FileManagerView, Location, SftpBackend, SftpTarget, parse_location  # noqa: E402

HOST_SSH_OPTIONS = ["ConnectTimeout=10", "ServerAliveInterval=30"]


def host_connector(target: SftpTarget):
    """How the host starts ssh; *env* carries mfatfm's password prompts."""

    def connect(env):
        argv = mfatsftp.build_ssh_argv(
            target.ssh_destination, port=target.port, ssh_options=HOST_SSH_OPTIONS
        )
        return mfatsftp.from_command(argv, env=env)

    return connect


def connection_factory(target: SftpTarget) -> SftpBackend:
    return SftpBackend(target, connector=host_connector(target))


class HostWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, server: str) -> None:
        super().__init__(application=app, title="Host app", default_width=1100, default_height=680)
        target = parse_location(server + ":").target or SftpTarget(host=server)

        # The host's own session, made before the file manager exists.
        client = mfatsftp.connect(target.ssh_destination, port=target.port, ssh_options=HOST_SSH_OPTIONS)
        self.host_backend = SftpBackend.from_client(target, client)

        self.view = FileManagerView(left="~", connection_factory=connection_factory)
        self.view.add_connection(self.host_backend)
        self.view.open_location(Location(path="~", target=target), self.view.right_pane)

        self.insert_action_group("fm", self.view.actions)
        header = Adw.HeaderBar()
        title = Adw.WindowTitle(title="Host app")
        self.view.connect("title-changed", lambda _v, t, s: (title.set_title(t), title.set_subtitle(s)))
        header.set_title_widget(title)
        connect_button = Gtk.Button.new_from_icon_name("network-server-symbolic")
        connect_button.set_action_name("fm.connect")
        header.pack_start(connect_button)
        header.pack_end(self.view.operations_button)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.view)
        self.set_content(toolbar)
        self.connect("close-request", self._on_close)

    def _on_close(self, _window) -> bool:
        self.view.shutdown()  # closes the view's own connections only
        # The lent connection is still ours; the host closes it when done.
        self.host_backend.client.close()
        return False


def main() -> None:
    server = sys.argv[1] if len(sys.argv) > 1 else "localhost"
    app = Adw.Application(application_id="org.example.MfatfmHost")
    app.connect("activate", lambda a: HostWindow(a, server).present())
    app.run([sys.argv[0]])


if __name__ == "__main__":
    main()
