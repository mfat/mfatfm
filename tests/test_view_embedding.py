"""Embedding the view: host connections stay the host's.

Needs a display (e.g. ``xvfb-run``) and ``MFATFM_TEST_SFTP_HOST``.
"""

import os
import time

import pytest

SFTP_HOST = os.environ.get("MFATFM_TEST_SFTP_HOST")

pytestmark = pytest.mark.skipif(
    not SFTP_HOST or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="needs a display and MFATFM_TEST_SFTP_HOST",
)


def _spin(predicate, timeout=15.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_host_connection_survives_view(tmp_path):
    import mfatsftp
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk

    from mfatfm import FileManagerView, Location, Settings, SftpBackend, SftpTarget, use_settings

    Adw.init()
    use_settings(Settings(persist=False))
    target = SftpTarget(host=SFTP_HOST)
    client = mfatsftp.connect(SFTP_HOST, ssh_options=["BatchMode=yes"])
    lent = SftpBackend.from_client(target, client)
    made = []

    def factory(t):
        backend = SftpBackend(t, ssh_options=["BatchMode=yes"])
        made.append(backend)
        return backend

    window = Gtk.Window()
    view = FileManagerView(left=str(tmp_path), connection_factory=factory, askpass=None)
    window.set_child(view)
    window.present()
    view.add_connection(lent)
    try:
        view.open_location(Location(path="/", target=target), view.right_pane)
        assert _spin(lambda: view.right_pane.current_path == "/")
        assert view.right_pane.backend is lent and made == []

        # A server the host did not lend goes through the factory.
        view.open_location(Location(path="/", target=SftpTarget(host=SFTP_HOST, port=22)), view.left_pane)
        assert _spin(lambda: view.left_pane.current_path == "/")
        assert len(made) == 1 and view.left_pane.backend is made[0]

        # Moving away and shutting down never closes the host's client.
        view.open_location(str(tmp_path), view.right_pane)
        assert _spin(lambda: view.right_pane.current_path == str(tmp_path))
        view.shutdown()
        assert _spin(lambda: made[0].closed)
        assert not client.closed
    finally:
        window.destroy()
        client.close()
