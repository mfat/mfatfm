"""The embeddable file manager: two panes, each on this computer or a server.

``FileManagerView`` is a plain widget, so other GTK 4 / libadwaita
applications can put it in their own windows; ``FileManagerWindow`` is just
a header bar around one.

The view owns the filesystem work. Panes ask for things through signals
("path-changed", "request-operation"); the view runs them on worker threads
through the pane's backend and hands results back. Connections to servers
are shared between the panes (and the clipboard) and closed when nothing
uses them any more, except connections the host application handed in with
:meth:`FileManagerView.add_connection`, which stay the host's.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from gettext import gettext as _, ngettext
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .backends import Backend, LocalBackend
from .common import FileEntry, OperationCancelled
from .connect_dialog import ConnectDialog
from .format_utils import filename_extension_offset, safe_display_text
from .askpass import AskpassServer, default_server
from .locations import (
    Location,
    Place,
    SftpTarget,
    gtk_bookmarks,
    parse_location,
    parse_uri,
    ssh_config_hosts,
    standard_places,
)
from .operations import Operation, OperationsButton
from .pane import FilePane, present_error_alert
from .properties_dialog import PropertiesDialog
from .settings import get_settings
from .tasks import run_async, run_in_thread
from .transfer import (
    TransferItem,
    copy_name,
    existing_destinations,
    run_delete,
    run_transfer,
    same_filesystem,
)

logger = logging.getLogger(__name__)

# ``connection_factory(target)``: a (possibly not yet connected) backend for a
# server the user opens.
ConnectionFactory = Callable[[SftpTarget], Backend]
# ``prompt_handler(backend, prompt, kind)``: answer one of ssh's questions;
# called on a worker thread, may block. kind is "secret", "confirm" or "info".
PromptHandler = Callable[[Backend, str, str], Optional[str]]

_DEFAULT = object()

# How long a directory may take to list before the pane says it is loading.
_LOADING_TOAST_DELAY_MS = 500
# Batches of local changes are reloaded together.
_MONITOR_DEBOUNCE_MS = 300

_TERMINALS = (
    ("xdg-terminal-exec", []),
    ("ptyxis", ["--new-window", "--working-directory"]),
    ("kgx", ["--working-directory"]),
    ("gnome-terminal", ["--working-directory"]),
    ("konsole", ["--workdir"]),
    ("xfce4-terminal", ["--working-directory"]),
    ("tilix", ["--working-directory"]),
    ("alacritty", ["--working-directory"]),
    ("kitty", ["--directory"]),
    ("foot", ["--working-directory"]),
    ("xterm", []),
)


def _error_text(exc: BaseException) -> str:
    """A short, human description of an error."""
    if isinstance(exc, OSError):
        message = getattr(exc, "message", None) or exc.strerror
        if message:
            return str(message)
    text = str(exc)
    return text or exc.__class__.__name__


class _PaneState:
    """What the window tracks per pane."""

    def __init__(self) -> None:
        self.generation = 0  # bumped per listing request; stale results are dropped
        self.loading_toast: Optional[int] = None
        self.count_generation = 0
        self.monitor: Optional[Gio.FileMonitor] = None
        self.monitor_timeout: Optional[int] = None
        self.connecting_key: Optional[str] = None


class _Clipboard:
    """Items copied or cut in any mfatfm window (shared by all of them)."""

    def __init__(self) -> None:
        self.clear()

    def clear(self) -> None:
        self.backend: Optional[Backend] = None
        self.directory = ""
        self.entries: List[FileEntry] = []
        self.move = False
        # What we put on the system clipboard; while it is still there the
        # items above are what a paste means.
        self.provider: Optional[Gdk.ContentProvider] = None


_CLIPBOARD = _Clipboard()


class FileManagerView(Gtk.Box):
    """Two :class:`FilePane` side by side, with everything they need.

    Put it in any window. To give the host's header bar the usual controls,
    insert :attr:`actions` as an action group (``"fm"`` in the examples) and
    pack :attr:`operations_button`.

    :param left, right: what each pane opens first (default: home).
    :param connection_factory: builds the backend for a server the user
        opens, e.g. an :class:`~mfatfm.backends.sftp.SftpBackend` with the
        host's own ``connector``. Default: a plain ``SftpBackend``.
    :param prompt_handler: answers ssh's password and host-key questions
        (on a worker thread). Default: dialogs on top of this view.
    :param askpass: the :class:`~mfatfm.askpass.AskpassServer` that carries
        those questions; ``None`` leaves ssh without one (prompts fail).
    :param shortcuts: install the view's own keys (Ctrl+K, F3, F7, Ctrl+U).

    Signals: ``title-changed(title, subtitle)`` for the active pane's
    folder, ``location-changed(side, uri)`` whenever a pane shows a folder.
    """

    __gtype_name__ = "MfatfmFileManagerView"

    __gsignals__ = {
        "title-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        "location-changed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
    }

    def __init__(
        self,
        *,
        left: Union[Location, str, None] = None,
        right: Union[Location, str, None] = None,
        connection_factory: Optional[ConnectionFactory] = None,
        prompt_handler: Optional[PromptHandler] = None,
        askpass=_DEFAULT,
        shortcuts: bool = True,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._askpass: Optional[AskpassServer] = default_server() if askpass is _DEFAULT else askpass
        self._connection_factory = connection_factory
        self._prompt_handler = prompt_handler
        self._local = LocalBackend()
        self._connections: Dict[str, Backend] = {}
        # Backends the host handed in: never closed by the view.
        self._external: Set[int] = set()
        self.title = "mfatfm"
        self.subtitle = ""
        # Panes waiting for a connection that is being set up, by id(backend).
        self._connect_waiters: Dict[int, List[Callable[[Optional[BaseException]], None]]] = {}
        self._connect_cancelled: Set[int] = set()
        # ssh questions waiting for an answer; released if the window closes.
        self._pending_prompts: List[threading.Event] = []
        self._busy: Dict[str, int] = {}  # backend key -> running operations
        self._edit_monitors: List[Tuple[Gio.FileMonitor, str]] = []
        self._closing = False

        settings = get_settings()
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self.operations_button = OperationsButton()
        self._operations = self.operations_button

        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        panes.add_css_class("fm-panes")
        panes.set_wide_handle(True)
        panes.set_shrink_start_child(False)
        panes.set_shrink_end_child(False)
        panes.set_resize_start_child(True)
        panes.set_resize_end_child(True)
        self._paned = panes
        self._left = FilePane("left")
        self._right = FilePane("right")
        self._states: Dict[FilePane, _PaneState] = {}
        list_level, grid_level = FilePane.load_saved_icon_levels()
        for pane, partner in ((self._left, self._right), (self._right, self._left)):
            pane.set_view(self)
            pane.set_partner_pane(partner)
            pane.set_icon_levels(list_level, grid_level)
            pane.set_margin_top(0)
            pane.toolbar.location_button.set_create_popup_func(self._on_location_menu, pane)
            pane.connect("path-changed", self._on_path_changed)
            pane.connect("request-operation", self._on_request_operation)
            pane.connect("activated", self._on_pane_activated)
            pane.connect("directory-shown", self._on_directory_shown)
            self._states[pane] = _PaneState()
        panes.set_start_child(self._left)
        panes.set_end_child(self._right)
        # Double-clicking the divider evens the two panes out.
        self._last_handle_press = 0
        controllers = panes.observe_controllers()
        for index in range(controllers.get_n_items()):
            controller = controllers.get_item(index)
            if type(controller) is Gtk.GestureDrag:
                controller.connect("drag-begin", self._on_handle_drag_begin)
                break
        # The divider keeps its share of the width as the window resizes.
        self._split_ratio = 0.5
        self._setting_split = False
        panes.connect("notify::max-position", self._on_paned_allocated)
        panes.connect("notify::position", self._on_paned_moved)

        self._toast_overlay.set_child(panes)
        self.append(self._toast_overlay)

        self._active = self._left
        self._left.set_active(True)
        self.actions = Gio.SimpleActionGroup()
        self._install_actions(shortcuts)
        # Descendants (pane menus) reach the actions as "fm.*" too.
        self.insert_action_group("fm", self.actions)
        self._apply_dual_pane(bool(settings.get("dual_pane", True)))

        self._clipboard_handler = self.get_clipboard().connect("changed", self._on_system_clipboard_changed)
        self._update_paste_targets()
        self.connect("destroy", lambda *_a: self.shutdown())

        self._open_location(self._left, self._as_location(left))
        self._open_location(self._right, self._as_location(right))
        GLib.idle_add(lambda: self._left.focus_view() or False)

    # -- public API --------------------------------------------------------

    @property
    def left_pane(self) -> FilePane:
        return self._left

    @property
    def right_pane(self) -> FilePane:
        return self._right

    @property
    def active_pane(self) -> FilePane:
        return self._active

    @staticmethod
    def _as_location(location: Union[Location, str, None]) -> Location:
        if location is None:
            return Location(path="~")
        if isinstance(location, str):
            return parse_location(location)
        return location

    def open_location(self, location: Union[Location, str], pane: Optional[FilePane] = None) -> None:
        """Show *location* (a ``Location``, path or sftp:// URI) in *pane*.

        The active pane by default. A server is reached through a
        connection added with :meth:`add_connection` when one matches,
        else through ``connection_factory``.
        """
        self._open_location(pane or self._active, self._as_location(location))

    def add_connection(self, backend: Backend) -> None:
        """Use the host's own *backend* for its server.

        Panes opening that server (same ``backend.key``) use it instead of
        starting ssh. The view never closes it; the host owns it until
        :meth:`remove_connection`.
        """
        self._external.add(id(backend))
        self._connections[backend.key] = backend

    def remove_connection(self, backend: Backend) -> None:
        """Hand a connection back to the host; panes showing it go home."""
        self._external.discard(id(backend))
        if self._connections.get(backend.key) is backend:
            del self._connections[backend.key]
        for pane in (self._left, self._right):
            if pane.backend is backend:
                self._show_backend(pane, self._local, "~")

    def get_connection(self, target: Union[SftpTarget, str]) -> Optional[Backend]:
        """The connection the view uses for *target* (or its key), if any."""
        key = target.key if isinstance(target, SftpTarget) else target
        return self._connections.get(key)

    @property
    def connections(self) -> List[Backend]:
        return list(self._connections.values())

    def set_dual_pane(self, enabled: bool) -> None:
        action = self.actions.lookup_action("dual-pane")
        if action.get_state().get_boolean() != enabled:
            action.activate(None)

    def has_running_operations(self) -> bool:
        return bool(self._operations.running)

    def cancel_operations(self) -> None:
        for operation in self._operations.running:
            operation.cancel()

    def _on_directory_shown(self, pane: FilePane, path: str) -> None:
        if pane is self._active:
            self._update_title()
        self.emit("location-changed", pane.side, pane.backend.location_uri(path))

    # -- window actions ----------------------------------------------------

    def _install_actions(self, shortcuts: bool) -> None:
        controller = Gtk.ShortcutController()
        # Keys work wherever focus is inside the view, but not elsewhere in
        # a host window.
        controller.set_scope(Gtk.ShortcutScope.LOCAL)

        def add(name: str, callback, accel: str = "") -> None:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_a: callback())
            self.actions.add_action(action)
            if accel and shortcuts:
                controller.add_shortcut(Gtk.Shortcut.new(
                    Gtk.ShortcutTrigger.parse_string(accel),
                    Gtk.CallbackAction.new(lambda *_a, a=action: a.activate(None) or True),
                ))

        add("connect", self.show_connect_dialog, "<primary>k")
        add("swap-panes", self._swap_panes, "<primary>u")
        add("same-location", self._same_location, "<primary><shift>u")
        add("focus-other-pane", self._focus_other_pane, "F7")
        add("show-shortcuts", self._show_shortcuts)
        dual = Gio.SimpleAction.new_stateful(
            "dual-pane", None, GLib.Variant.new_boolean(bool(get_settings().get("dual_pane", True)))
        )
        dual.connect("activate", self._on_dual_pane_activated)
        self.actions.add_action(dual)
        if shortcuts:
            controller.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string("F3"),
                Gtk.CallbackAction.new(lambda *_a: dual.activate(None) or True),
            ))
            self.add_controller(controller)

    def _on_dual_pane_activated(self, action: Gio.SimpleAction, _param) -> None:
        enabled = not action.get_state().get_boolean()
        action.set_state(GLib.Variant.new_boolean(enabled))
        get_settings().set("dual_pane", enabled)
        self._apply_dual_pane(enabled)

    def _apply_dual_pane(self, enabled: bool) -> None:
        if not enabled and self._active is self._right:
            # Keep showing what the user was working on.
            self._swap_panes()
        self._right.set_visible(enabled)
        for pane in (self._left, self._right):
            pane.set_across_visible(enabled)
        if not enabled:
            self._set_active(self._left)

    def _swap_panes(self) -> None:
        """Exchange the two panes' locations."""
        left_backend, left_path = self._left.backend, self._left.current_path
        right_backend, right_path = self._right.backend, self._right.current_path
        self._show_backend(self._left, right_backend, right_path or "~")
        self._show_backend(self._right, left_backend, left_path or "~")

    def _same_location(self) -> None:
        """Show the active pane's folder in the other pane too."""
        other = self._other(self._active)
        if not other.get_visible():
            return
        self._show_backend(other, self._active.backend, self._active.current_path or "~")

    def _focus_other_pane(self) -> None:
        other = self._other(self._active)
        if other.get_visible():
            self._set_active(other)
            other.focus_view()

    def _show_shortcuts(self) -> None:
        rows = (
            (_("Connect to Server"), "Ctrl+K"),
            (_("Copy to Other Pane"), "F6"),
            (_("Move to Other Pane"), "Shift+F6"),
            (_("Switch Pane"), "F7"),
            (_("Toggle Two Panes"), "F3"),
            (_("Swap Panes"), "Ctrl+U"),
            (_("Same Folder in Both Panes"), "Ctrl+Shift+U"),
            (_("Location Bar"), "Ctrl+L"),
            (_("Back / Forward"), "Alt+Left / Alt+Right"),
            (_("Parent Folder"), "Alt+Up / Backspace"),
            (_("Home Folder"), "Alt+Home"),
            (_("Reload"), "F5 / Ctrl+R"),
            (_("Show Hidden Files"), "Ctrl+H"),
            (_("List / Grid View"), "Ctrl+1 / Ctrl+2"),
            (_("Zoom In / Out / Reset"), "Ctrl++ / Ctrl+- / Ctrl+0"),
            (_("New Folder"), "Ctrl+Shift+N"),
            (_("Rename"), "F2"),
            (_("Move to Trash"), "Delete"),
            (_("Delete Permanently"), "Shift+Delete"),
            (_("Copy / Cut / Paste"), "Ctrl+C / Ctrl+X / Ctrl+V"),
            (_("Select Items Matching"), "Ctrl+S"),
            (_("Invert Selection"), "Ctrl+Shift+I"),
            (_("Properties"), "Ctrl+I"),
            (_("New Window"), "Ctrl+N"),
            (_("Close Window"), "Ctrl+W"),
        )
        group = Adw.PreferencesGroup()
        for title, keys in rows:
            row = Adw.ActionRow(title=title)
            label = Gtk.Label(label=keys)
            label.add_css_class("dim-label")
            row.add_suffix(label)
            group.add(row)
        page = Adw.PreferencesPage()
        page.add(group)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(page)
        dialog = Adw.Dialog(title=_("Keyboard Shortcuts"), content_width=460, content_height=620)
        dialog.set_child(toolbar)
        dialog.present(self)

    # -- layout ------------------------------------------------------------

    def _on_paned_allocated(self, paned: Gtk.Paned, _pspec) -> None:
        self._apply_split()

    def _apply_split(self) -> None:
        # max-position is the width less the end pane's minimum, so split
        # the real width; GTK clamps to both panes' minimums.
        width = self._paned.get_width()
        if width > 0:
            self._setting_split = True
            self._paned.set_position(int(width * self._split_ratio))
            self._setting_split = False

    def _on_paned_moved(self, paned: Gtk.Paned, _pspec) -> None:
        width = paned.get_width()
        if not self._setting_split and width > 0 and self._right.get_visible():
            self._split_ratio = min(0.9, max(0.1, paned.get_position() / width))

    def _on_handle_drag_begin(self, gesture: Gtk.GestureDrag, _x: float, _y: float) -> None:
        now = GLib.get_monotonic_time()
        double_click_ms = Gtk.Settings.get_default().get_property("gtk-double-click-time")
        if now - self._last_handle_press < double_click_ms * 1000:
            gesture.set_state(Gtk.EventSequenceState.DENIED)
            self._split_ratio = 0.5
            self._apply_split()
            self._last_handle_press = 0
        else:
            self._last_handle_press = now

    def _other(self, pane: FilePane) -> FilePane:
        return self._right if pane is self._left else self._left

    def _on_pane_activated(self, pane: FilePane) -> None:
        self._set_active(pane)

    def _set_active(self, pane: FilePane) -> None:
        if pane is self._active:
            return
        self._active.set_active(False)
        self._active = pane
        pane.set_active(True)
        self._update_title()

    def _update_title(self) -> None:
        pane = self._active
        path = pane.current_path
        backend = pane.backend
        if not path:
            title, subtitle = "mfatfm", backend.display_name
        else:
            name = backend.basename(path) if path not in ("/", "") else "/"
            if not backend.is_remote and os.path.normpath(path) == os.path.expanduser("~"):
                name = _("Home")
            title = safe_display_text(name)
            subtitle = safe_display_text(backend.display_name if backend.is_remote else path)
        if (title, subtitle) != (self.title, self.subtitle):
            self.title, self.subtitle = title, subtitle
            self.emit("title-changed", title, subtitle)

    # -- locations and connections ----------------------------------------

    def _on_location_menu(self, button: Gtk.MenuButton, pane: FilePane) -> None:
        """Build the pane's location menu each time it opens."""
        popover = Gtk.Popover()
        popover.add_css_class("menu")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_margin_top(6)
        box.set_margin_bottom(6)

        def add_heading(text: str) -> None:
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("heading")
            label.add_css_class("dim-label")
            label.set_margin_start(12)
            label.set_margin_top(6)
            label.set_margin_bottom(3)
            box.append(label)

        def add_item(label: str, icon_name: str, callback) -> None:
            item = Gtk.Button()
            item.add_css_class("flat")
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            content.append(Gtk.Image.new_from_icon_name(icon_name))
            text = Gtk.Label(label=safe_display_text(label), xalign=0)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_max_width_chars(32)
            content.append(text)
            item.set_child(content)

            def _clicked(_button) -> None:
                popover.popdown()
                callback()

            item.connect("clicked", _clicked)
            box.append(item)

        add_heading(_("This Computer"))
        for place in standard_places():
            add_item(place.label, place.icon_name, self._place_opener(pane, place))
        bookmarks = gtk_bookmarks()
        local_marks = [p for p in bookmarks if isinstance(p.location, str)]
        remote_marks = [p for p in bookmarks if not isinstance(p.location, str)]
        if local_marks:
            add_heading(_("Bookmarks"))
            for place in local_marks:
                add_item(place.label, place.icon_name, self._place_opener(pane, place))

        servers: List[Place] = list(remote_marks)
        seen = {place.location.target.key for place in remote_marks}
        for uri in get_settings().recent_servers():
            try:
                location = parse_uri(uri)
            except ValueError:
                continue
            if location is not None and location.target.key not in seen:
                seen.add(location.target.key)
                servers.append(Place(location.target.display_name, location, "folder-remote-symbolic"))
        for key, backend in self._connections.items():
            target = getattr(backend, "target", None)
            if key not in seen and target is not None and (backend.connected or id(backend) in self._external):
                seen.add(key)
                servers.append(
                    Place(backend.display_name, Location(path="~", target=target), "folder-remote-symbolic")
                )
        add_heading(_("Servers"))
        for place in servers[:10]:
            add_item(place.label, place.icon_name, self._place_opener(pane, place))
        add_item(_("Connect to Server…"), "list-add-symbolic", lambda: self.show_connect_dialog(pane))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_propagate_natural_width(True)
        scrolled.set_max_content_height(560)
        scrolled.set_child(box)
        popover.set_child(scrolled)
        button.set_popover(popover)

    def _place_opener(self, pane: FilePane, place: Place):
        location = place.location
        if isinstance(location, str):
            location = Location(path=location)
        return lambda: self._open_location(pane, location)

    def show_connect_dialog(self, pane: Optional[FilePane] = None) -> None:
        target_pane = pane or self._active
        dialog = ConnectDialog(lambda location: self._open_location(target_pane, location))
        dialog.present_for(self)

    def _open_location(self, pane: FilePane, location: Location) -> None:
        """Show *location* in *pane*, connecting to its server if needed."""
        self._set_active(pane)
        if location.target is None:
            self._show_backend(pane, self._local, location.path)
            return
        key = location.target.key
        backend = self._connections.get(key)
        if backend is None:
            if self._connection_factory is not None:
                backend = self._connection_factory(location.target)
            else:
                from .backends.sftp import SftpBackend

                backend = SftpBackend(location.target)
            self._connections[key] = backend
        self._show_backend(pane, backend, location.path)

    def _show_backend(self, pane: FilePane, backend: Backend, path: str) -> None:
        old = pane.backend
        if backend is not old:
            pane.set_backend(backend)
            self._release_backend(old)
            if pane is self._active:
                self._update_title()
        self.load_directory(pane, path)

    def _backend_in_use(self, backend: Backend) -> bool:
        return (
            any(pane.backend is backend for pane in (self._left, self._right))
            or _CLIPBOARD.backend is backend
            or self._busy.get(backend.key, 0) > 0
        )

    def _release_backend(self, backend: Backend) -> None:
        """Close a server connection once nothing in this window uses it."""
        if not backend.is_remote or self._backend_in_use(backend) or id(backend) in self._external:
            return
        if self._connections.get(backend.key) is backend:
            del self._connections[backend.key]
        if id(backend) in self._connect_waiters:
            return  # closed by the connect callback once ssh has started
        run_in_thread(backend.close, name="mfatfm-close")

    def _askpass_env(self, backend: Backend) -> Dict[str, str]:
        return self._askpass.register(self._make_prompt_handler(backend)) if self._askpass else {}

    def connect_backend(self, backend: Backend) -> None:
        """Connect *backend* with this view's prompts; blocks (use a worker thread)."""
        self._connect_blocking(backend)

    def _connect_blocking(self, backend: Backend) -> None:
        """Connect *backend* on the calling (worker) thread."""
        env = self._askpass_env(backend)
        try:
            backend.connect(env)
        finally:
            if self._askpass:
                self._askpass.unregister(env)

    def _ensure_connected(self, pane: FilePane, then: Callable[[], None]) -> None:
        """Run *then* once the pane's server is connected (now, if it is)."""
        backend = pane.backend
        if not backend.is_remote or backend.connected:
            then()
            return
        wait_key = id(backend)
        state = self._states[pane]
        state.connecting_key = backend.key
        pane.show_connecting(_("Connecting to {host}…").format(host=backend.display_name))

        def _connected(error: Optional[BaseException]) -> None:
            if pane.backend is not backend or state.connecting_key != backend.key:
                return  # the pane moved on meanwhile
            state.connecting_key = None
            if error is None:
                then()
                return
            if isinstance(error, PermissionError):
                message = _("Could not log in to {host}. Check the user name and password.").format(
                    host=backend.display_name
                )
            else:
                message = _("Could not connect to {host}: {error}").format(
                    host=backend.display_name, error=_error_text(error)
                )
            pane.show_load_error(pane.current_path or "~", message)

        waiters = self._connect_waiters.get(wait_key)
        if waiters is not None:
            waiters.append(_connected)
            return
        self._connect_waiters[wait_key] = [_connected]

        def _done(_result, error) -> None:
            waiters = self._connect_waiters.pop(wait_key, [])
            cancelled = wait_key in self._connect_cancelled
            self._connect_cancelled.discard(wait_key)
            target = getattr(backend, "target", None)
            if error is None and not cancelled and target is not None:
                get_settings().add_recent_server(target.key)
            if not cancelled:
                for waiter in waiters:
                    waiter(error)
            if id(backend) in self._external:
                return
            if cancelled or not self._backend_in_use(backend):
                if self._connections.get(backend.key) is backend and not self._backend_in_use(backend):
                    del self._connections[backend.key]
                if not self._backend_in_use(backend):
                    run_in_thread(backend.close, name="mfatfm-close")

        run_in_thread(lambda: self._connect_blocking(backend), _done, name="mfatfm-connect")

    def _cancel_connect(self, pane: FilePane) -> None:
        """Stop waiting for a server; the attempt is closed when ssh returns."""
        backend = pane.backend
        if backend.is_remote and id(backend) in self._connect_waiters:
            self._connect_cancelled.add(id(backend))
        self._states[pane].connecting_key = None
        self._show_backend(pane, self._local, "~")

    def _make_prompt_handler(self, backend: Backend):
        """An askpass handler that shows ssh's question in this window."""

        if self._prompt_handler is not None:
            host_handler = self._prompt_handler
            return lambda prompt, kind: host_handler(backend, prompt, kind)

        def _handler(prompt: str, kind: str) -> Optional[str]:
            answer: List[Optional[str]] = [None]
            done = threading.Event()

            def _show() -> bool:
                if self._closing:
                    done.set()
                    return False
                self._pending_prompts.append(done)
                self._present_prompt(backend, prompt, kind, answer, done)
                return False

            GLib.idle_add(_show)
            done.wait()
            GLib.idle_add(lambda: done in self._pending_prompts and self._pending_prompts.remove(done) and False)
            return answer[0]

        return _handler

    def _present_prompt(self, backend, prompt: str, kind: str, answer: list, done: threading.Event) -> None:
        host = safe_display_text(backend.display_name)
        prompt_text = safe_display_text(prompt.strip())
        if kind == "info":
            self._toast_overlay.add_toast(Adw.Toast.new(prompt_text))
            answer[0] = ""
            done.set()
            return
        if kind == "confirm":
            dialog = Adw.AlertDialog(heading=_("Confirm Connection"), body=prompt_text)
            dialog.add_response("no", _("Cancel"))
            dialog.add_response("yes", _("Connect"))
            dialog.set_response_appearance("yes", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response("no")

            def _on_confirm(_dialog, response: str) -> None:
                answer[0] = "yes" if response == "yes" else "no"
                done.set()

            dialog.connect("response", _on_confirm)
            dialog.present(self)
            return

        dialog = Adw.AlertDialog(
            heading=_("Authentication Required"),
            body=_("{host} asks:\n{prompt}").format(host=host, prompt=prompt_text),
        )
        entry_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entry_list.add_css_class("boxed-list")
        entry = Adw.PasswordEntryRow(title=_("Password"))
        entry.set_activates_default(True)
        entry_list.append(entry)
        dialog.set_extra_child(entry_list)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", _("Log In"))
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def _on_response(_dialog, response: str) -> None:
            answer[0] = entry.get_text() if response == "ok" else None
            done.set()

        dialog.connect("response", _on_response)
        dialog.present(self)
        GLib.idle_add(lambda: entry.grab_focus() and False)

    # -- listing -----------------------------------------------------------

    def _on_path_changed(self, pane: FilePane, path: str) -> None:
        text = path.strip()
        try:
            location = parse_location(text, ssh_config_hosts())
        except ValueError as exc:
            pane.show_toast(_error_text(exc))
            return
        if location.target is not None:
            self._open_location(pane, location)
            return
        if text.startswith("file://") and pane.backend.is_remote:
            self._open_location(pane, location)
            return
        self.load_directory(pane, location.path)

    def load_directory(
        self,
        pane: FilePane,
        path: str,
        *,
        highlight: Optional[str] = None,
        select: Optional[Sequence[str]] = None,
        quiet: bool = False,
    ) -> None:
        """List *path* into *pane* (connecting first if needed)."""
        state = self._states[pane]
        state.generation += 1
        generation = state.generation
        backend = pane.backend
        push_history = not pane._suppress_history_push
        pane._suppress_history_push = False

        def _start() -> None:
            if state.generation != generation:
                return
            self._cancel_loading_toast(pane)
            if backend.is_remote and not quiet:
                state.loading_toast = GLib.timeout_add(
                    _LOADING_TOAST_DELAY_MS, self._show_loading_toast, pane, generation
                )

            def _list():
                resolved = backend.normalize(path)
                return resolved, backend.list_dir(resolved)

            run_async(_list, _done)

        def _done(result, error) -> None:
            if state.generation != generation or pane.backend is not backend:
                return
            self._cancel_loading_toast(pane)
            pane.dismiss_toasts()
            if error is not None:
                self._on_list_failed(pane, path, error, quiet=quiet)
                return
            resolved, entries = result
            pane.show_entries(resolved, entries)
            if push_history:
                pane.push_history(resolved)
            if highlight:
                pane.highlight_entry(highlight)
            if select:
                pane.select_names(select)
            self._watch_directory(pane, resolved)
            self._start_count_pass(pane, resolved, entries)
            if pane is self._active:
                self._update_title()

        self._ensure_connected(pane, _start)

    def reload(self, pane: FilePane, **kwargs) -> None:
        if pane.current_path:
            pane._suppress_history_push = True
            self.load_directory(pane, pane.current_path, **kwargs)

    def _show_loading_toast(self, pane: FilePane, generation: int) -> bool:
        state = self._states[pane]
        state.loading_toast = None
        if state.generation == generation:
            pane.show_toast(_("Loading folder…"), timeout=0)
        return False

    def _cancel_loading_toast(self, pane: FilePane) -> None:
        state = self._states[pane]
        if state.loading_toast is not None:
            GLib.source_remove(state.loading_toast)
            state.loading_toast = None

    def _on_list_failed(self, pane: FilePane, path: str, error: BaseException, *, quiet: bool) -> None:
        backend = pane.backend
        if isinstance(error, ConnectionError) or (backend.is_remote and backend.closed):
            message = _("The connection to {host} was lost.").format(host=backend.display_name)
            pane.show_load_error(path, message)
            return
        if isinstance(error, NotADirectoryError):
            message = _("“{path}” is not a folder").format(path=path)
        elif isinstance(error, FileNotFoundError):
            message = _("“{path}” does not exist").format(path=path)
        elif isinstance(error, PermissionError):
            message = _("You do not have permission to open “{path}”").format(path=path)
        else:
            message = _("Could not open “{path}”: {error}").format(path=path, error=_error_text(error))
        if quiet and pane.current_path:
            return
        if pane.current_path and pane._stack.get_visible_child_name() not in ("load-error", "connecting"):
            # Keep showing the folder we are in; just say why we did not move.
            pane.show_toast(message)
            pane._set_current_pathbar_text(pane.current_path)
            return
        pane.show_load_error(path, message)

    def _start_count_pass(self, pane: FilePane, path: str, entries: List[FileEntry]) -> None:
        """Count each folder's items off the main thread.

        Opening every subfolder while listing would make a folder-heavy
        directory slow to show; the counts fill in the Size column as they
        arrive. A newer listing supersedes the pass.
        """
        state = self._states[pane]
        state.count_generation += 1
        generation = state.count_generation
        folders = [entry.name for entry in entries if entry.is_dir]
        if not folders:
            return
        backend = pane.backend

        def _deliver(counts: Dict[str, int]) -> bool:
            if generation == state.count_generation:
                pane.update_item_counts(path, counts)
            return False

        def _count() -> None:
            for batch in backend.count_items(path, folders):
                if generation != state.count_generation:
                    return
                if batch:
                    GLib.idle_add(_deliver, batch)

        run_async(_count, lambda _result, error: logger.debug("Item count stopped: %s", error) if error else None)

    def _watch_directory(self, pane: FilePane, path: str) -> None:
        """Reload a local pane when its folder changes on disk."""
        state = self._states[pane]
        if state.monitor is not None:
            state.monitor.cancel()
            state.monitor = None
        if pane.backend.is_remote:
            return
        try:
            monitor = Gio.File.new_for_path(path).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error as exc:
            logger.debug("Cannot watch %s: %s", path, exc)
            return

        def _changed(*_args) -> None:
            if state.monitor_timeout is not None:
                return

            def _reload() -> bool:
                state.monitor_timeout = None
                if pane.current_path == path and not pane.backend.is_remote:
                    self.reload(pane, quiet=True)
                return False

            state.monitor_timeout = GLib.timeout_add(_MONITOR_DEBOUNCE_MS, _reload)

        monitor.connect("changed", _changed)
        state.monitor = monitor

    def _refresh_panes(self, backend: Backend, *directories: str, select: Optional[Sequence[str]] = None,
                       select_in: Optional[str] = None) -> None:
        """Reload every pane showing one of *directories* on *backend*'s filesystem."""
        wanted = {backend.paths.normpath(directory) for directory in directories if directory}
        for pane in (self._left, self._right):
            if pane.backend.key != backend.key or not pane.current_path:
                continue
            current = pane.backend.paths.normpath(pane.current_path)
            if current in wanted:
                names = select if select_in and current == backend.paths.normpath(select_in) else None
                self.reload(pane, quiet=True, select=names)

    # -- operations from panes --------------------------------------------

    def _on_request_operation(self, pane: FilePane, action: str, payload) -> None:
        payload = payload if isinstance(payload, dict) else {}
        entries: List[FileEntry] = list(payload.get("entries") or [])
        handlers = {
            "copy": lambda: self._set_clipboard(pane, entries, move=False),
            "cut": lambda: self._set_clipboard(pane, entries, move=True),
            "paste": lambda: self._paste(pane, force_move=bool(payload.get("force_move"))),
            "copy-across": lambda: self._across(pane, entries, move=False),
            "move-across": lambda: self._across(pane, entries, move=True),
            "mkdir": lambda: self._create_item(pane, folder=True),
            "newfile": lambda: self._create_item(pane, folder=False),
            "rename": lambda: self._rename(pane, entries[0]),
            "delete": lambda: self._delete(pane, entries, permanently=not pane.backend.can_trash),
            "delete-permanently": lambda: self._delete(pane, entries, permanently=True),
            "open": lambda: self._open_file(pane, entries[0]),
            "open-with": lambda: self._open_file(pane, entries[0], choose=True),
            "properties": lambda: self._show_properties(pane, entries),
            "open-terminal": lambda: self._open_terminal(pane, payload.get("path") or pane.current_path),
            "open-in-files": lambda: self._open_in_files(payload.get("path") or pane.current_path),
            "cancel-connect": lambda: self._cancel_connect(pane),
        }
        handler = handlers.get(action)
        if handler is None:
            logger.debug("Unknown pane action %r", action)
            return
        handler()

    # clipboard

    def _set_clipboard(self, pane: FilePane, entries: List[FileEntry], *, move: bool) -> None:
        clip = _CLIPBOARD
        old = clip.backend
        clip.backend = pane.backend
        clip.directory = pane.current_path
        clip.entries = list(entries)
        clip.move = move
        if old is not None and old is not pane.backend:
            self._release_backend(old)
        if len(entries) == 1:
            message = (_("Cut {name}") if move else _("Copied {name}")).format(name=entries[0].name)
        else:
            template = (
                ngettext("Cut {count} item", "Cut {count} items", len(entries))
                if move else ngettext("Copied {count} item", "Copied {count} items", len(entries))
            )
            message = template.format(count=len(entries))
        pane.show_toast(message)
        self._export_clipboard(pane, entries, move)
        self._update_paste_targets()

    def _export_clipboard(self, pane: FilePane, entries: List[FileEntry], move: bool) -> None:
        """Put the items on the system clipboard too.

        Local files go out in the formats Nautilus and other apps paste;
        remote items as their sftp:// locations. Either way the clipboard is
        then ours, which tells a later paste to use the items above.
        """
        if pane.backend.is_remote:
            text = "\n".join(pane.backend.location_uri(pane.path_for(entry)) for entry in entries)
            provider = Gdk.ContentProvider.new_for_value(text)
        else:
            files = [Gio.File.new_for_path(pane.path_for(entry)) for entry in entries]
            uris = [gfile.get_uri() for gfile in files]
            gnome = ("cut" if move else "copy") + "\n" + "\n".join(uris)
            provider = Gdk.ContentProvider.new_union([
                _bytes_provider("x-special/gnome-copied-files", gnome),
                _bytes_provider("text/uri-list", "\r\n".join(uris) + "\r\n"),
                Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list(files)),
            ])
        _CLIPBOARD.provider = provider
        self.get_clipboard().set_content(provider)

    def _on_system_clipboard_changed(self, clipboard: Gdk.Clipboard) -> None:
        self._update_paste_targets()

    def _clipboard_is_ours(self) -> bool:
        clip = _CLIPBOARD
        return bool(clip.entries) and clip.provider is not None and (
            self.get_clipboard().get_content() is clip.provider
        )

    def _update_paste_targets(self) -> None:
        formats = self.get_clipboard().get_formats()
        can_paste = self._clipboard_is_ours() or formats.contain_gtype(Gdk.FileList) or (
            formats.contain_mime_type("x-special/gnome-copied-files")
        )
        for pane in (self._left, self._right):
            pane.set_can_paste(can_paste)

    def _paste(self, pane: FilePane, *, force_move: bool) -> None:
        if not pane.current_path:
            return
        clip = _CLIPBOARD
        if not self._clipboard_is_ours():
            self._paste_from_system(pane, force_move=force_move)
            return
        move = clip.move or force_move
        source_backend = clip.backend
        entries = list(clip.entries)
        directory = clip.directory
        if move:
            clip.clear()
            self.get_clipboard().set_content(None)
        self._start_transfer(source_backend, directory, entries, pane.backend, pane.current_path, move=move)
        if move:
            self._release_backend(source_backend)

    def _paste_from_system(self, pane: FilePane, *, force_move: bool) -> None:
        """Paste files another application copied (or cut, from Nautilus)."""
        clipboard = self.get_clipboard()
        destination = pane.current_path

        def _paths_read(paths: List[str], cut: bool) -> None:
            if not paths:
                pane.show_toast(_("Nothing to paste"))
                return
            self.drop_local_files(paths, pane, destination, move=cut or force_move)

        def _got_file_list(clip, result) -> None:
            try:
                value = clip.read_value_finish(result)
            except GLib.Error:
                value = None
            paths = [gfile.get_path() for gfile in value.get_files()] if value is not None else []
            _paths_read([path for path in paths if path], False)

        def _got_gnome(clip, result) -> None:
            try:
                stream, _mime = clip.read_finish(result)
                data = stream.read_bytes(1 << 20, None).get_data().decode("utf-8", "replace")
                stream.close(None)
            except GLib.Error:
                clip.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, _got_file_list)
                return
            lines = [line for line in data.splitlines() if line]
            cut = bool(lines) and lines[0] == "cut"
            uris = lines[1:] if lines and lines[0] in ("cut", "copy") else lines
            paths = [Gio.File.new_for_uri(uri).get_path() for uri in uris]
            _paths_read([path for path in paths if path], cut)

        formats = clipboard.get_formats()
        if formats.contain_mime_type("x-special/gnome-copied-files"):
            clipboard.read_async(["x-special/gnome-copied-files"], GLib.PRIORITY_DEFAULT, None, _got_gnome)
        elif formats.contain_gtype(Gdk.FileList):
            clipboard.read_value_async(Gdk.FileList, GLib.PRIORITY_DEFAULT, None, _got_file_list)
        else:
            pane.show_toast(_("Nothing to paste"))

    def _across(self, pane: FilePane, entries: List[FileEntry], *, move: bool) -> None:
        other = self._other(pane)
        if not other.get_visible() or not other.current_path:
            pane.show_toast(_("The other pane is not showing a folder"))
            return
        self.transfer_entries(pane, entries, other, other.current_path, move=move)

    def transfer_entries(
        self, source: FilePane, entries: List[FileEntry], target: FilePane, destination: str, *, move: bool
    ) -> None:
        """Copy or move entries of *source* into *destination* on *target*'s filesystem."""
        self._start_transfer(source.backend, source.current_path, entries, target.backend, destination, move=move)

    def drop_local_files(self, paths: List[str], target: FilePane, destination: str, *, move: bool) -> None:
        """Files dropped from another app (always local)."""
        entries: List[FileEntry] = []
        directories = {os.path.dirname(os.path.normpath(path)) for path in paths}
        for path in paths:
            try:
                st = os.lstat(path)
            except OSError:
                continue
            entries.append(
                FileEntry(
                    name=os.path.basename(os.path.normpath(path)),
                    is_dir=os.path.isdir(path) and not os.path.islink(path),
                    size=st.st_size,
                    modified=st.st_mtime,
                )
            )
        if not entries:
            return
        if len(directories) != 1:
            # Items from several folders: one transfer per folder.
            for path in paths:
                self.drop_local_files([path], target, destination, move=move)
            return
        self._start_transfer(self._local, directories.pop(), entries, target.backend, destination, move=move)

    def _start_transfer(
        self,
        src: Backend,
        src_dir: str,
        entries: List[FileEntry],
        dst: Backend,
        dst_dir: str,
        *,
        move: bool,
    ) -> None:
        same_fs = same_filesystem(src, dst)
        same_dir = same_fs and src.paths.normpath(src_dir) == dst.paths.normpath(dst_dir)
        if same_dir and move:
            return  # moving a folder's items into the same folder does nothing
        items: List[TransferItem] = []
        taken = set()
        if same_dir:
            taken = {entry.name for pane in (self._left, self._right)
                     if pane.backend.key == src.key and pane.current_path == src_dir
                     for entry in pane._cached_entries}
        for entry in entries:
            name = entry.name
            if same_dir:
                # Pasting into the folder the items came from duplicates them.
                name = copy_name(entry.name, taken)
                taken.add(name)
            items.append(
                TransferItem(
                    name=entry.name,
                    src_path=src.join(src_dir, entry.name),
                    dst_path=dst.join(dst_dir, name),
                    is_dir=entry.is_dir and not entry.is_symlink,
                    size=0 if entry.is_dir else entry.size,
                )
            )

        def _checked(conflicts, error) -> None:
            if error is not None:
                present_error_alert(self, _("Could not prepare the copy: {error}").format(error=_error_text(error)))
                return
            if not conflicts:
                self._run_transfer(src, dst, src_dir, dst_dir, items, move=move)
                return
            self._ask_conflicts(conflicts, len(items), lambda replace: self._run_transfer(
                src, dst, src_dir, dst_dir,
                items if replace else [item for item in items if item not in conflicts],
                move=move,
            ))

        run_async(lambda: existing_destinations(dst, items), _checked)

    def _ask_conflicts(self, conflicts: List[TransferItem], total: int, proceed: Callable[[bool], None]) -> None:
        count = len(conflicts)
        if count == 1:
            body = _("“{name}” already exists in the destination folder.").format(
                name=safe_display_text(conflicts[0].name)
            )
            if conflicts[0].is_dir:
                body += " " + _("Replacing a folder merges the two.")
        else:
            body = ngettext(
                "{count} of {total} item already exists in the destination folder.",
                "{count} of {total} items already exist in the destination folder.",
                count,
            ).format(count=count, total=total)
        dialog = Adw.AlertDialog(
            heading=ngettext("Item Already Exists", "Items Already Exist", count), body=body
        )
        dialog.add_response("cancel", _("Cancel"))
        if count < total:
            dialog.add_response("skip", _("Skip Existing"))
        dialog.add_response("replace", _("Replace") if count == 1 else _("Replace All"))
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("skip" if count < total else "cancel")
        dialog.set_close_response("cancel")

        def _on_response(_dialog, response: str) -> None:
            if response in ("skip", "replace"):
                proceed(response == "replace")

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _transfer_title(self, src: Backend, dst: Backend, items: List[TransferItem], dst_dir: str, move: bool) -> Tuple[str, str]:
        count = len(items)
        what = (
            _("“{name}”").format(name=items[0].name)
            if count == 1
            else ngettext("{count} item", "{count} items", count).format(count=count)
        )
        where = dst.basename(dst_dir) or dst_dir
        if dst.is_remote and not src.is_remote:
            return _("Uploading {what} to {host}").format(what=what, host=dst.display_name), "document-send-symbolic"
        if src.is_remote and not dst.is_remote:
            return _("Downloading {what} to {folder}").format(what=what, folder=where), "folder-download-symbolic"
        if move:
            return _("Moving {what} to {folder}").format(what=what, folder=where), "edit-cut-symbolic"
        return _("Copying {what} to {folder}").format(what=what, folder=where), "edit-copy-symbolic"

    def _run_transfer(
        self, src: Backend, dst: Backend, src_dir: str, dst_dir: str, items: List[TransferItem], *, move: bool
    ) -> None:
        if not items:
            return
        title, icon = self._transfer_title(src, dst, items, dst_dir, move)
        operation = Operation(title, len(items), icon)
        self._operations.add(operation)
        self._hold(src, dst)

        def _work():
            return run_transfer(src, dst, items, move=move, progress=operation.progress)

        def _done(failures, error) -> None:
            self._unhold(src, dst)
            if isinstance(error, OperationCancelled):
                operation.finish(Operation.CANCELLED)
            elif error is not None:
                operation.finish(Operation.FAILED, _error_text(error))
            elif failures:
                name, exc = failures[0]
                message = _("“{name}”: {error}").format(name=name, error=_error_text(exc))
                if len(failures) > 1:
                    message += " " + ngettext(
                        "(and {count} more)", "(and {count} more)", len(failures) - 1
                    ).format(count=len(failures) - 1)
                operation.finish(Operation.FAILED, message)
            else:
                operation.finish(Operation.DONE)
            names = [dst.basename(item.dst_path) for item in items]
            self._refresh_panes(dst, dst_dir, select=names, select_in=dst_dir)
            if move:
                self._refresh_panes(src, src_dir)

        run_in_thread(_work, _done, name="mfatfm-transfer")

    def _hold(self, *backends: Backend) -> None:
        for backend in {b.key: b for b in backends}.values():
            self._busy[backend.key] = self._busy.get(backend.key, 0) + 1

    def _unhold(self, *backends: Backend) -> None:
        for backend in {b.key: b for b in backends}.values():
            self._busy[backend.key] = max(0, self._busy.get(backend.key, 0) - 1)
            self._release_backend(backend)

    # create, rename, delete

    def _name_dialog(
        self, heading: str, initial: str, confirm: str, on_name: Callable[[str], None], *, select_stem: bool
    ) -> None:
        dialog = Adw.AlertDialog(heading=heading)
        entry_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        entry_list.add_css_class("boxed-list")
        entry = Adw.EntryRow(title=_("Name"))
        entry.set_text(initial)
        entry.set_activates_default(True)
        entry_list.append(entry)
        dialog.set_extra_child(entry_list)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("ok", confirm)
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")

        def _valid(text: str) -> bool:
            return bool(text.strip()) and "/" not in text and text not in (".", "..")

        entry.connect("changed", lambda e: dialog.set_response_enabled("ok", _valid(e.get_text())))
        dialog.set_response_enabled("ok", _valid(initial))

        def _on_response(_dialog, response: str) -> None:
            text = entry.get_text().strip()
            if response == "ok" and _valid(text):
                on_name(text)

        dialog.connect("response", _on_response)
        dialog.present(self)

        def _focus() -> bool:
            entry.grab_focus()
            delegate = entry.get_delegate()
            if delegate is not None:
                end = filename_extension_offset(initial) if select_stem else len(initial)
                delegate.select_region(0, end)
            return False

        # After the entry row's own select-all on focus.
        GLib.timeout_add(80, _focus)

    def _create_item(self, pane: FilePane, *, folder: bool) -> None:
        if not pane.current_path:
            return
        backend = pane.backend
        directory = pane.current_path

        def _create(name: str) -> None:
            path = backend.join(directory, name)

            def _work() -> None:
                if backend.exists_many([path])[0]:
                    raise FileExistsError(path)
                if folder:
                    backend.mkdir(path)
                else:
                    backend.create_file(path)

            def _done(_result, error) -> None:
                if error is not None:
                    if isinstance(error, FileExistsError):
                        pane.show_toast(_("“{name}” already exists").format(name=name))
                    else:
                        present_error_alert(self, _("Could not create “{name}”: {error}").format(
                            name=name, error=_error_text(error)))
                    return
                self._refresh_panes(backend, directory)
                if pane.current_path == directory:
                    self.reload(pane, highlight=name)

            run_async(_work, _done)

        if folder:
            self._name_dialog(_("New Folder"), _("Untitled Folder"), _("Create"), _create, select_stem=False)
        else:
            self._name_dialog(_("New File"), _("Untitled.txt"), _("Create"), _create, select_stem=True)

    def _rename(self, pane: FilePane, entry: FileEntry) -> None:
        backend = pane.backend
        directory = pane.current_path

        def _do_rename(new_name: str) -> None:
            if new_name == entry.name:
                return
            old = backend.join(directory, entry.name)
            new = backend.join(directory, new_name)

            def _done(_result, error) -> None:
                if error is not None:
                    if isinstance(error, FileExistsError):
                        pane.show_toast(_("“{name}” already exists").format(name=new_name))
                    else:
                        present_error_alert(self, _("Could not rename “{name}”: {error}").format(
                            name=entry.name, error=_error_text(error)))
                    return
                self._refresh_panes(backend, directory)
                if pane.current_path == directory:
                    self.reload(pane, highlight=new_name)

            def _work():
                if backend.exists_many([new])[0] and new.lower() != old.lower():
                    raise FileExistsError(new)
                backend.rename(old, new)

            run_async(_work, _done)

        self._name_dialog(
            _("Rename Folder") if entry.is_dir else _("Rename File"),
            entry.name,
            _("Rename"),
            _do_rename,
            select_stem=not entry.is_dir,
        )

    def _delete(self, pane: FilePane, entries: List[FileEntry], *, permanently: bool) -> None:
        if not entries:
            return
        backend = pane.backend
        directory = pane.current_path
        paths = [backend.join(directory, entry.name) for entry in entries]
        names = [entry.name for entry in entries]

        def _run() -> None:
            count = len(entries)
            what = (
                _("“{name}”").format(name=names[0])
                if count == 1 else ngettext("{count} item", "{count} items", count).format(count=count)
            )
            trash = not permanently
            title = (_("Moving {what} to Trash") if trash else _("Deleting {what}")).format(what=what)
            operation = Operation(title, count, "user-trash-symbolic")
            self._operations.add(operation)
            pane.remove_cached_entries(names, hold=True)
            self._hold(backend)

            def _done(failures, error) -> None:
                self._unhold(backend)
                pane.release_removed_entries(names, directory)
                if isinstance(error, OperationCancelled):
                    operation.finish(Operation.CANCELLED)
                elif error is not None:
                    operation.finish(Operation.FAILED, _error_text(error))
                elif failures:
                    name, exc = failures[0]
                    operation.finish(Operation.FAILED, _("“{name}”: {error}").format(name=name, error=_error_text(exc)))
                    if trash and len(failures) == count:
                        self._offer_permanent_delete(pane, entries, exc)
                else:
                    operation.finish(Operation.DONE)
                    if trash:
                        pane.show_toast(
                            ngettext("{count} item moved to Trash", "{count} items moved to Trash", count).format(count=count)
                            if count > 1 else _("“{name}” moved to Trash").format(name=names[0])
                        )
                self._refresh_panes(backend, directory)

            run_in_thread(
                lambda: run_delete(backend, paths, trash=trash, progress=operation.progress),
                _done,
                name="mfatfm-delete",
            )

        if not permanently:
            _run()
            return
        count = len(entries)
        heading = (
            _("Permanently Delete “{name}”?").format(name=safe_display_text(names[0]))
            if count == 1
            else ngettext(
                "Permanently Delete {count} Item?", "Permanently Delete {count} Items?", count
            ).format(count=count)
        )
        dialog = Adw.AlertDialog(
            heading=heading,
            body=_("Permanently deleted items can not be restored.")
            if not backend.is_remote
            else _("Items deleted on {host} can not be restored.").format(host=backend.display_name),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _d, response: _run() if response == "delete" else None)
        dialog.present(self)

    def _offer_permanent_delete(self, pane: FilePane, entries: List[FileEntry], exc: BaseException) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Cannot Move to Trash"),
            body=_("{error}\n\nDelete permanently instead?").format(error=_error_text(exc)),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete Permanently"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, response: self._delete(pane, entries, permanently=True) if response == "delete" else None,
        )
        dialog.present(self)

    # opening files

    def _open_file(self, pane: FilePane, entry: FileEntry, *, choose: bool = False) -> None:
        backend = pane.backend
        path = pane.path_for(entry)
        if not backend.is_remote:
            self._launch(path, choose=choose)
            return
        self._open_remote_copy(backend, path, entry)

    def _launch(self, path: str, *, choose: bool = False, on_error: Optional[Callable[[], None]] = None) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
        launcher.set_always_ask(choose)

        def _finished(launcher, result) -> None:
            try:
                launcher.launch_finish(result)
            except GLib.Error as exc:
                if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.CANCELLED) or exc.matches(
                    Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED
                ):
                    return
                self._toast_overlay.add_toast(
                    Adw.Toast.new(_("Could not open “{name}”").format(name=safe_display_text(os.path.basename(path))))
                )

        launcher.launch(self.get_root(), None, _finished)

    def _open_remote_copy(self, backend: Backend, remote_path: str, entry: FileEntry) -> None:
        """Download to a private folder, open it, and upload it again on save."""
        cache_root = os.path.join(GLib.get_user_cache_dir(), "mfatfm", "open")
        os.makedirs(cache_root, exist_ok=True)
        local_dir = tempfile.mkdtemp(dir=cache_root)
        local_path = os.path.join(local_dir, entry.name)
        operation = Operation(_("Opening “{name}”").format(name=entry.name), 1, "document-open-symbolic")
        operation.progress.expect("item-0", entry.size)
        self._operations.add(operation)
        self._hold(backend)

        def _download():
            operation.progress.start_item("item-0", entry.name)
            backend.download(remote_path, local_path, False, operation.progress.callback_for("item-0"))
            operation.progress.finish_item("item-0")
            return os.stat(local_path).st_mtime

        def _done(mtime, error) -> None:
            self._unhold(backend)
            if isinstance(error, OperationCancelled):
                operation.finish(Operation.CANCELLED)
                shutil.rmtree(local_dir, ignore_errors=True)
                return
            if error is not None:
                operation.finish(Operation.FAILED, _error_text(error))
                shutil.rmtree(local_dir, ignore_errors=True)
                return
            operation.finish(Operation.DONE)
            self._watch_remote_copy(backend, remote_path, local_path, mtime)
            self._launch(local_path)

        run_in_thread(_download, _done, name="mfatfm-open")

    def _watch_remote_copy(self, backend: Backend, remote_path: str, local_path: str, mtime: float) -> None:
        """Upload the opened copy each time an application saves it."""
        folder = Gio.File.new_for_path(os.path.dirname(local_path))
        try:
            monitor = folder.monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        except GLib.Error:
            return
        state = {"mtime": mtime, "timeout": None, "uploading": False}
        name = os.path.basename(local_path)

        def _upload() -> bool:
            state["timeout"] = None
            try:
                current = os.stat(local_path).st_mtime
            except OSError:
                return False
            if current == state["mtime"] or state["uploading"]:
                return False
            state["mtime"] = current
            state["uploading"] = True
            self._hold(backend)

            def _work():
                if backend.closed:
                    self._connect_blocking(backend)
                backend.upload(local_path, remote_path, False, lambda _d, _t: None)

            def _done(_result, error) -> None:
                state["uploading"] = False
                self._unhold(backend)
                if error is not None:
                    present_error_alert(self, _("Could not save “{name}” to {host}: {error}").format(
                        name=name, host=backend.display_name, error=_error_text(error)))
                    return
                self._toast_overlay.add_toast(
                    Adw.Toast.new(_("Saved “{name}” to {host}").format(
                        name=safe_display_text(name), host=safe_display_text(backend.display_name)))
                )
                self._refresh_panes(backend, backend.dirname(remote_path))

            run_in_thread(_work, _done, name="mfatfm-save")
            return False

        def _changed(_monitor, gfile, _other, event) -> None:
            if gfile.get_basename() != name and (_other is None or _other.get_basename() != name):
                return
            if event not in (
                Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                Gio.FileMonitorEvent.RENAMED,
                Gio.FileMonitorEvent.MOVED_IN,
                Gio.FileMonitorEvent.CREATED,
            ):
                return
            if state["timeout"] is not None:
                GLib.source_remove(state["timeout"])
            state["timeout"] = GLib.timeout_add(500, _upload)

        monitor.connect("changed", _changed)
        self._edit_monitors.append((monitor, os.path.dirname(local_path)))

    # other

    def _show_properties(self, pane: FilePane, entries: List[FileEntry]) -> None:
        if not pane.current_path:
            return
        backend = pane.backend
        if entries:
            dialog = PropertiesDialog(entries, pane.current_path, backend)
        else:
            path = pane.current_path
            parent = backend.dirname(path)
            name = backend.basename(path) if path != "/" else "/"
            folder = FileEntry(name=name, is_dir=True, size=0, modified=0.0, item_count=len(pane._cached_entries))
            dialog = PropertiesDialog([folder], parent if path != "/" else "/", backend)
        dialog.present(self)

    def _open_terminal(self, pane: FilePane, path: str) -> None:
        if pane.backend.is_remote or not path:
            return
        for program, args in _TERMINALS:
            binary = GLib.find_program_in_path(program)
            if not binary:
                continue
            argv = [binary] + (args + [path] if args else [])
            try:
                subprocess.Popen(argv, cwd=path, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError as exc:
                logger.debug("Could not start %s: %s", program, exc)
        pane.show_toast(_("No terminal application found"))

    def _open_in_files(self, path: str) -> None:
        if path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self.get_root(), None, None)

    # -- shutting down ----------------------------------------------------

    def shutdown(self) -> None:
        """Stop watching folders and close the view's own connections.

        Running operations are cancelled. Connections added with
        :meth:`add_connection` are left open. Safe to call more than once;
        the view also calls it when destroyed.
        """
        if self._closing:
            return
        self._closing = True
        self.cancel_operations()
        for event in self._pending_prompts:
            event.set()
        for state in self._states.values():
            if state.monitor is not None:
                state.monitor.cancel()
        for monitor, _folder in self._edit_monitors:
            monitor.cancel()
        backends = [b for b in self._connections.values() if id(b) not in self._external]
        self._connections.clear()
        if _CLIPBOARD.backend is not None and any(_CLIPBOARD.backend is b for b in backends):
            _CLIPBOARD.clear()
        self.get_clipboard().disconnect(self._clipboard_handler)
        for backend in backends:
            run_in_thread(backend.close, name="mfatfm-close")


def _bytes_provider(mime_type: str, text: str) -> Gdk.ContentProvider:
    """A clipboard provider offering *text* as *mime_type*."""
    return Gdk.ContentProvider.new_for_bytes(mime_type, GLib.Bytes.new(text.encode("utf-8")))
