"""Single file-manager pane.

Houses ``FilePane`` — the browser widget shown on each side of
``FileManagerView``. A pane shows one folder of whatever filesystem its
backend is (this computer or an SFTP server); it emits signals back to the
view, which does the actual filesystem work.
"""

from __future__ import annotations

import json
import logging
import os
import re
import weakref
from gettext import gettext as _, ngettext
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Set, Tuple
from urllib.parse import unquote, urlsplit

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from .backends import Backend, LocalBackend
from .common import FileEntry
from .format_utils import _human_time, _item_count_text, format_size, safe_display_text
from .icon_levels import (
    _DEFAULT_ICON_LEVELS,
    _GRID_ICON_SIZES,
    _LIST_ICON_SIZES,
    _MAX_ICON_LEVELS,
    clamp_icon_level,
)
from .icons import icon_for_name, image
from .pane_controls import PaneToolbar
from .settings import get_settings

if TYPE_CHECKING:
    from .view import FileManagerView

logger = logging.getLogger(__name__)

_TOAST_TEXT_LIMIT = 60
_DRAG_FORMAT = "mfatfm_drag"

# Every live pane by id(), so a drop can find the pane a drag came from,
# also when it came from another window.
_PANES: "weakref.WeakValueDictionary[int, FilePane]" = weakref.WeakValueDictionary()


def _is_macos() -> bool:
    return GLib.get_os_info("ID") == "macos"


def toast_overflows(text: str) -> bool:
    """True when *text* is too long or multi-line to read in a toast."""
    text = text or ""
    return len(text) > _TOAST_TEXT_LIMIT or "\n" in text


def present_error_alert(anchor: Gtk.Widget, text: str, heading: Optional[str] = None) -> None:
    """Show *text* in an alert dialog on *anchor*'s root window."""
    # Error text often embeds a path, which may not be valid UTF-8.
    text = safe_display_text(text)
    try:
        dialog = Adw.AlertDialog(heading=heading or _("Error"), body=text)
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(anchor)
    except Exception:
        logger.error("Failed to present error alert: %s", text)


# Installed once per process; scoped to .fm-browser-card so it only affects
# the file-manager browser cards. The Adwaita `card` class supplies the
# rounded corners/background; this just keeps the list/grid views from
# painting their own view background over the card (visible in dark mode).
_browser_card_css_installed = False

# CSS variables need GTK 4.16; older GTK would warn about every line.
_NEUTRAL_ACCENT_CSS = b"""
columnview.fm-list-view > listview,
gridview.fm-grid-view {
    --accent-bg-color: #959595;
    --accent-color: oklab(from var(--accent-bg-color) var(--standalone-color-oklab));
}
"""


def _ensure_browser_card_css() -> None:
    global _browser_card_css_installed
    if _browser_card_css_installed:
        return
    try:
        provider = Gtk.CssProvider()
        css = (b"""
.fm-browser-card columnview,
.fm-browser-card gridview {
    background: transparent;
}
/* Nautilus's list and grid spacing (nautilus src/resources/style.css).
   Horizontal list padding sits on the columnview so column widths account
   for it; the listview's negative margins let rubberbanding reach into it. */
columnview.fm-list-view {
    padding-left: 24px;
    padding-right: 24px;
}
columnview.fm-list-view > listview {
    padding-top: 16px;
    padding-bottom: 24px;
    border-spacing: 8px;
    margin-left: -24px;
    margin-right: -24px;
}
columnview.fm-list-view > listview > row {
    margin-left: 24px;
    margin-right: 24px;
    border-radius: 9px;
}
columnview.fm-list-view > listview > row > cell {
    padding: 0px;
}
.fm-list-view .fm-view-cell {
    padding: 6px;
}
columnview.fm-list-view.compact > listview {
    border-spacing: 4px;
}
.fm-list-view.compact .fm-view-cell {
    padding-top: 3px;
    padding-bottom: 3px;
}
gridview.fm-grid-view {
    padding: 18px;
    border-spacing: 6px;
}
gridview.fm-grid-view > child {
    padding: 0px;
    border-radius: 12px;
}
/* Grid cells are flat buttons; the child row draws hover and selection. */
gridview.fm-grid-view button.fm-view-cell,
gridview.fm-grid-view button.fm-view-cell:hover,
gridview.fm-grid-view button.fm-view-cell:active {
    padding: 6px;
    border-radius: 12px;
    min-height: 0px;
    min-width: 0px;
    background: none;
    box-shadow: none;
}
.fm-browser-card image.fm-view-icon {
    filter: drop-shadow(0px 1px 1px rgba(0,0,0,0.3));
}
.fm-browser-card .fm-hidden-file {
    opacity: 0.55;
}
.fm-browser-card rubberband {
    border-radius: 6px;
}
/* Nautilus highlights selection in neutral grey rather than the accent
   colour. Spelled out with libadwaita 1.5's alphas; GTK 4.16+ also gets
   Nautilus's own variable override below. */
columnview.fm-list-view > listview > row:selected,
gridview.fm-grid-view > child:selected {
    background-color: alpha(#959595, 0.25);
}
columnview.fm-list-view > listview > row.activatable:selected:hover {
    background-color: alpha(#959595, 0.32);
}
columnview.fm-list-view > listview > row.activatable:selected:active {
    background-color: alpha(#959595, 0.39);
}
gridview.fm-grid-view > child:focus:focus-visible {
    outline-color: alpha(#959595, 0.5);
}
/* ActionBar paints its background/border on an inner box, which would
   square-fill the rounded card; let the card class show through instead. */
actionbar.fm-bottom-card > revealer > box {
    background: transparent;
    border-top: none;
    /* libadwaita draws the actionbar's top hairline as an inset box-shadow */
    box-shadow: none;
}
/* The framed cards already separate the panes; hide the paned hairline.
   Only the paint is removed - the separator keeps its size and hit area,
   so drag-resizing still works. */
paned.fm-panes > separator {
    background: none;
    /* the theme draws the paned hairline as an inset box-shadow */
    box-shadow: none;
}
/* The pane that keyboard actions apply to. */
.fm-pane-active .fm-browser-card {
    border-color: alpha(@accent_bg_color, 0.6);
}
.fm-pane.drop-target-active .fm-browser-card {
    background-color: alpha(@accent_bg_color, 0.08);
    border: 2px dashed @accent_bg_color;
}
""" + (_NEUTRAL_ACCENT_CSS if (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 16) else b""))
        if hasattr(provider, "load_from_string"):  # GTK 4.12+
            provider.load_from_string(css.decode())
        else:
            provider.load_from_data(css, -1)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _browser_card_css_installed = True
    except Exception:  # pragma: no cover - headless/test doubles
        logger.debug("Browser card CSS install failed", exc_info=True)


def _normalize_dir(path: Optional[str]) -> str:
    if not path:
        return ""
    stripped = path.rstrip("/")
    return stripped or "/"


def _same_path(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and _normalize_dir(a) == _normalize_dir(b)


def _child_toward(ancestor: Optional[str], descendant: Optional[str]) -> Optional[str]:
    """Name of ``ancestor``'s child on the way down to ``descendant``.

    ``_child_toward("/a", "/a/b/c")`` is ``"b"``; ``None`` when ``ancestor`` is
    not a strict ancestor of ``descendant``.
    """
    parent = _normalize_dir(ancestor)
    child = _normalize_dir(descendant)
    if not parent or not child or parent == child:
        return None
    prefix = parent if parent.endswith("/") else parent + "/"
    if not child.startswith(prefix):
        return None
    name = child[len(prefix):].split("/", 1)[0]
    return name or None


def _set_hidden_file_style(widget: Gtk.Widget, name: str) -> None:
    """Dim dotfiles, as Nautilus does when hidden files are shown."""
    if name.startswith("."):
        widget.add_css_class("fm-hidden-file")
    else:
        widget.remove_css_class("fm-hidden-file")


_DIGIT_RUNS = re.compile(r"(\d+)")


def _natural_name_key(name: str) -> Tuple:
    """Sort key that orders names like Nautilus: "file2" before "file10".

    Digit runs compare by value and everything else case-insensitively,
    the way g_utf8_collate_key_for_filename() orders Nautilus's names.
    """
    parts = _DIGIT_RUNS.split(name.casefold())
    chunks = tuple(
        (0, int(part), "") if index % 2 else (1, 0, part)
        for index, part in enumerate(parts)
    )
    # Names equal by value ("a01" / "a1") still need a stable order.
    return chunks, name


def _type_sort_key(name: str) -> str:
    """The file type Nautilus sorts by: its content type's description."""
    try:
        content_type, _uncertain = Gio.content_type_guess(name, None)
        description = Gio.content_type_get_description(content_type)
        if description:
            return description.casefold()
    except Exception:
        pass
    _stem, ext = os.path.splitext(name)
    return ext.casefold()


def _pattern_matcher(pattern: str) -> Callable[[str], bool]:
    """Match names as g_pattern_spec does in Nautilus's Select Pattern:
    ``*`` and ``?`` are wildcards, everything else is literal and
    case-sensitive."""
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    compiled = re.compile(regex, re.DOTALL)
    return lambda name: compiled.fullmatch(name) is not None


def _local_paths_from_uri_list(text: str) -> List[str]:
    """Local paths from a text/uri-list (or plain path list) drop."""
    paths: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("file://"):
            paths.append(unquote(urlsplit(line).path))
        elif line.startswith("/"):
            paths.append(line)
        else:
            return []
    return paths


class FilePane(Gtk.Box):
    """Represents a single pane in the manager."""

    _TYPEAHEAD_TIMEOUT = 1.0

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # A different folder finished loading (not a reload of this one).
        "directory-shown": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
        # Keyboard focus entered this pane's file view.
        "activated": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, side: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _PANES[id(self)] = self
        self.add_css_class("fm-pane")
        self.side = side
        self.backend: Backend = LocalBackend()
        self.toolbar = PaneToolbar()
        self.append(self.toolbar)

        self._view: Optional["FileManagerView"] = None
        # Icon zoom level (index into _LIST_ICON_SIZES / _GRID_ICON_SIZES).
        # Set silently here so __init__ paths that build factory widgets get a
        # sane initial size; the window may overwrite this with the user's
        # persisted value before the first directory load.
        self._icon_levels: Dict[str, int] = dict(_DEFAULT_ICON_LEVELS)
        # Track currently bound icon widgets so zoom updates them in place
        # (O(visible)) instead of forcing a full list-store rebuild. Use plain
        # sets (not WeakSet): PyGObject can drop the Python wrapper for a live
        # GTK widget between bind/unbind cycles, which would make WeakSet
        # entries vanish unpredictably. The factory's bind/unbind pair makes
        # explicit add/discard reliable.
        self._bound_list_icons: set = set()
        # Currently-bound Size-column labels, so deferred folder item-counts
        # can be written into visible rows in place (no list-store rebuild).
        self._bound_size_labels: set = set()
        self._bound_grid_images: set = set()

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._list_store = Gio.ListStore(item_type=Gtk.StringObject)
        self._selection_model = Gtk.MultiSelection.new(self._list_store)
        self._selection_anchor: Optional[int] = None

        self._suppress_next_context_menu: bool = False
        self._syncing_column_sort: bool = False
        self._list_columns: Dict[str, Any] = {}
        self._entries: List[FileEntry] = []

        list_view = self._create_column_view()
        self._list_view = list_view
        self._update_list_density()
        list_view.connect("activate", self._on_list_activate)

        # Wrap list view in a scrolled window for proper scrolling
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_child(list_view)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_factory.connect("unbind", self._on_grid_unbind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=20,
        )
        grid_view.add_css_class("fm-grid-view")
        grid_view.set_enable_rubberband(True)
        grid_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        self._grid_view = grid_view
        # Navigate on grid item activation (double click / Enter)
        grid_view.connect("activate", self._on_grid_activate)

        # Wrap grid view in a scrolled window for proper scrolling
        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scrolled.set_child(grid_view)

        # Ctrl/Cmd + wheel = zoom icons. Attach to each scrolled view so the
        # modifier+scroll combination is captured before the regular scroll
        # reaches the ScrolledWindow.
        for scrolled in (list_scrolled, grid_scrolled):
            scroll_ctrl = Gtk.EventControllerScroll.new(
                Gtk.EventControllerScrollFlags.VERTICAL
            )
            scroll_ctrl.connect("scroll", self._on_pane_scroll)
            scrolled.add_controller(scroll_ctrl)

        self._stack.add_named(list_scrolled, "list")
        self._stack.add_named(grid_scrolled, "grid")
        # A view keeps building and binding cells for every listing even on a
        # hidden stack page, so only the active one holds the model.
        self._active_view_name = "list"
        self._grid_view.set_model(None)
        self._stack.connect("notify::visible-child-name", self._on_stack_page_changed)

        # Error state shown when a directory fails to load, so it can't be
        # mistaken for an empty directory.
        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        error_box.set_halign(Gtk.Align.CENTER)
        error_box.set_valign(Gtk.Align.CENTER)
        error_box.set_margin_start(24)
        error_box.set_margin_end(24)
        error_icon = image("dialog-warning-symbolic", 48)
        error_icon.add_css_class("error")
        error_box.append(error_icon)
        self._load_error_label = Gtk.Label()
        self._load_error_label.add_css_class("error")
        self._load_error_label.set_wrap(True)
        self._load_error_label.set_max_width_chars(48)
        self._load_error_label.set_justify(Gtk.Justification.CENTER)
        error_box.append(self._load_error_label)
        error_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        error_buttons.set_halign(Gtk.Align.CENTER)
        retry_btn = Gtk.Button(label=_("Retry"))
        retry_btn.add_css_class("pill")
        retry_btn.connect("clicked", self._on_retry_load_clicked)
        error_buttons.append(retry_btn)
        self._error_home_button = Gtk.Button(label=_("Go Home"))
        self._error_home_button.add_css_class("pill")
        self._error_home_button.connect("clicked", self._on_error_home_clicked)
        error_buttons.append(self._error_home_button)
        error_box.append(error_buttons)
        self._stack.add_named(error_box, "load-error")
        self._load_error_path: Optional[str] = None
        self._pre_error_view: Optional[str] = None

        # Connecting state: spinner + live status while an SFTP connection is
        # set up.
        conn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        conn_box.set_halign(Gtk.Align.CENTER)
        conn_box.set_valign(Gtk.Align.CENTER)
        self._connecting_spinner = Adw.Spinner() if hasattr(Adw, "Spinner") else Gtk.Spinner()
        self._connecting_spinner.set_size_request(32, 32)
        self._connecting_spinner.set_halign(Gtk.Align.CENTER)
        if isinstance(self._connecting_spinner, Gtk.Spinner):
            self._connecting_spinner.set_spinning(True)
        conn_box.append(self._connecting_spinner)
        self._connecting_label = Gtk.Label()
        self._connecting_label.add_css_class("dim-label")
        self._connecting_label.set_wrap(True)
        self._connecting_label.set_justify(Gtk.Justification.CENTER)
        conn_box.append(self._connecting_label)
        self._cancel_connect_button = Gtk.Button(label=_("Cancel"))
        self._cancel_connect_button.add_css_class("pill")
        self._cancel_connect_button.set_halign(Gtk.Align.CENTER)
        self._cancel_connect_button.connect(
            "clicked", lambda *_a: self.emit("request-operation", "cancel-connect", None)
        )
        conn_box.append(self._cancel_connect_button)
        self._stack.add_named(conn_box, "connecting")

        # Empty folder placeholder, so an empty listing doesn't look broken.
        empty = Adw.StatusPage()
        empty.set_icon_name("folder-symbolic")
        empty.set_title(_("Folder is Empty"))
        empty.add_css_class("compact")
        self._empty_page = empty
        self._empty_overlay = Gtk.Overlay()

        overlay = Adw.ToastOverlay()
        self._overlay = overlay
        self._current_toast = None  # Keep reference to current toast for dismissal

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self._stack)
        empty.set_can_target(False)
        empty.set_visible(False)
        content_overlay.add_overlay(empty)

        # Rounded-corner card framing the browser area: `card` supplies the
        # radius/shadow, `view` the content-surface colors (its rule follows
        # .card in the Adwaita stylesheet, so the view background wins).
        # overflow=HIDDEN clips the scrolled content to the corners.
        _ensure_browser_card_css()
        content_overlay.add_css_class("card")
        content_overlay.add_css_class("view")
        # `frame` draws a 1px border following the card's rounded corners.
        content_overlay.add_css_class("frame")
        content_overlay.add_css_class("fm-browser-card")
        content_overlay.set_overflow(Gtk.Overflow.HIDDEN)
        content_overlay.set_margin_top(2)
        content_overlay.set_margin_bottom(2)
        content_overlay.set_margin_start(8)
        content_overlay.set_margin_end(8)

        overlay.set_child(content_overlay)
        self.append(overlay)

        # Drops: our own drags carry a JSON string; other apps send a file
        # list (or a text/uri-list string).
        drop_target = Gtk.DropTarget.new(type=GObject.TYPE_NONE, actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop_target.set_gtypes([GObject.TYPE_STRING, Gdk.FileList])
        drop_target.connect("drop", self._on_drop)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("motion", self._on_drop_motion)
        drop_target.connect("leave", self._on_drop_leave)
        self.add_controller(drop_target)

        self._partner_pane: Optional["FilePane"] = None

        self._action_buttons: Dict[str, Gtk.Button] = {}
        action_bar = Gtk.ActionBar()
        action_bar.add_css_class("inline-toolbar")
        # Same rounded-card framing as the toolbar and browser area above.
        action_bar.add_css_class("card")
        action_bar.add_css_class("fm-bottom-card")
        action_bar.set_margin_bottom(8)
        action_bar.set_margin_start(8)
        action_bar.set_margin_end(8)

        def _create_action_button(
            name: str,
            icon_name: str,
            label: str,
            callback: Callable[[Gtk.Button], None],
            *,
            with_label: bool = False,
        ) -> Gtk.Button:
            button = Gtk.Button()
            if with_label:
                content = Adw.ButtonContent(icon_name=icon_name, label=label)
                button.set_child(content)
            else:
                button.set_icon_name(icon_name)
                button.set_tooltip_text(label)
            button.set_valign(Gtk.Align.CENTER)
            button.set_has_frame(False)
            button.add_css_class("flat")
            button.connect("clicked", callback)
            self._action_buttons[name] = button
            return button

        # Copy/move to the other pane: the arrow points at it.
        toward = "go-next-symbolic" if side == "left" else "go-previous-symbolic"
        copy_across = _create_action_button(
            "copy_across",
            toward,
            _("Copy"),
            lambda _button: self._emit_entry_operation("copy-across"),
            with_label=True,
        )
        copy_across.set_tooltip_text(_("Copy to the other pane"))
        move_across = _create_action_button(
            "move_across",
            toward,
            _("Move"),
            lambda _button: self._emit_entry_operation("move-across"),
            with_label=True,
        )
        move_across.set_tooltip_text(_("Move to the other pane"))
        copy_button = _create_action_button(
            "copy",
            "edit-copy-symbolic",
            _("Copy"),
            lambda _button: self._emit_entry_operation("copy"),
        )
        cut_button = _create_action_button(
            "cut",
            "edit-cut-symbolic",
            _("Cut"),
            lambda _button: self._emit_entry_operation("cut"),
        )
        paste_button = _create_action_button(
            "paste",
            "edit-paste-symbolic",
            _("Paste"),
            lambda _button: self._emit_paste_operation(),
        )
        rename_button = _create_action_button(
            "rename",
            "document-edit-symbolic",
            _("Rename"),
            lambda _button: self._emit_entry_operation("rename"),
        )
        delete_button = _create_action_button(
            "delete",
            "user-trash-symbolic",
            _("Delete"),
            lambda _button: self._emit_entry_operation("delete"),
        )
        self._across_buttons = (copy_across, move_across)

        if side == "left":
            action_bar.pack_end(move_across)
            action_bar.pack_end(copy_across)
        else:
            action_bar.pack_start(copy_across)
            action_bar.pack_start(move_across)
        selection_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        for button in (paste_button, copy_button, cut_button, rename_button, delete_button):
            selection_box.append(button)
        if side == "left":
            action_bar.pack_start(selection_box)
        else:
            action_bar.pack_end(selection_box)

        self._status_label = Gtk.Label()
        self._status_label.add_css_class("dim-label")
        self._status_label.add_css_class("caption")
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
        action_bar.set_center_widget(self._status_label)

        self._action_bar = action_bar
        self.append(action_bar)

        self._can_paste: bool = False

        # Connect to view-changed signal from toolbar
        self.toolbar.connect("view-changed", self._on_view_toggle)
        self.toolbar.connect("show-hidden-toggled", self._on_toolbar_show_hidden_toggled)
        self.toolbar.connect("zoom-changed", self._on_toolbar_zoom_changed)
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        # Wire navigation buttons
        self.toolbar.controls.up_button.connect("clicked", self._on_up_clicked)
        self.toolbar.controls.back_button.connect("clicked", self._on_back_clicked)
        self.toolbar.controls.forward_button.connect("clicked", self._on_forward_clicked)
        self.toolbar.controls.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )

        self._history: List[str] = []
        # Locations left with Back, most recent last.
        self._forward_history: List[str] = []
        self._current_path = ""
        self._cached_entries: List[FileEntry] = []
        # Names hidden per directory while a delete of them is in flight.
        self._held_removals: Dict[str, Set[str]] = {}
        self._raw_entries: List[FileEntry] = []
        self._show_hidden = bool(get_settings().get("show_hidden", False))
        self.toolbar.set_show_hidden_state(self._show_hidden)
        self._sort_key = "name"  # Default sort by name
        self._sort_descending = False  # Default ascending order

        self._suppress_history_push: bool = False
        self._update_history_buttons()
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        self._menu_actions: Dict[str, Gio.SimpleAction] = {}
        self._menu_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._menu_action_group)
        self._create_menu_actions()
        # The open context menu, rebuilt on every right-click.
        self._menu_popover: Optional[Gtk.PopoverMenu] = None
        # True while the context menu was opened on the view background: like
        # Nautilus, it then offers folder actions but leaves the selection alone.
        self._menu_for_background: bool = False
        self._add_context_controller(list_view)
        self._add_context_controller(grid_view)

        # A plain click on an already-selected grid item among several narrows
        # the selection to it on release (so a press can still drag the group).
        self._pending_grid_collapse: Optional[int] = None
        # Keyboard navigation moves GTK's own anchor, not ``_selection_anchor``;
        # when set, the grid's next Shift+click extends from the focused item.
        self._anchor_follows_focus: bool = False
        nav_controller = Gtk.EventControllerKey.new()
        nav_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        nav_controller.connect("key-pressed", self._on_grid_nav_key_pressed)
        grid_view.add_controller(nav_controller)

        for view in (list_view, grid_view):
            controller = Gtk.EventControllerKey.new()
            controller.connect("key-pressed", self._on_typeahead_key_pressed)
            view.add_controller(controller)
            self._attach_shortcuts(view)
            focus = Gtk.EventControllerFocus.new()
            focus.connect("enter", lambda *_a: self.emit("activated"))
            view.add_controller(focus)
        # A click anywhere in the pane (toolbar included) makes it active.
        click = Gtk.GestureClick()
        click.set_button(0)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", lambda *_a: self.emit("activated"))
        self.add_controller(click)

        self._update_menu_state()
        # Set up sorting actions for the split button
        self._setup_sorting_actions()

        # Initialize view button icon and direction states
        self._update_view_button_icon()
        self._update_sort_direction_states()
        self._sync_column_sort_indicator()

        self._typeahead_buffer: str = ""
        self._typeahead_last_time: float = 0.0

        saved_view = get_settings().get("view", "list")
        if saved_view == "grid":
            self.toolbar.set_view("grid")

    # -- backend -------------------------------------------------------

    @property
    def _is_remote(self) -> bool:
        return self.backend.is_remote

    def set_backend(self, backend: Backend) -> None:
        """Show another filesystem: the history of the old one is dropped."""
        if backend is self.backend:
            return
        self.backend = backend
        self.clear_history()
        self._current_path = ""
        self._cached_entries = []
        self._apply_entry_filter(preserve_selection=False)
        self.toolbar.location_button.set_location(
            safe_display_text(backend.display_name), remote=backend.is_remote
        )
        self.toolbar.path_entry.set_text("")
        self._error_home_button.set_label(
            _("Back to This Computer") if backend.is_remote else _("Go Home")
        )
        self._update_menu_state()

    def path_for(self, entry: FileEntry) -> str:
        return self.backend.join(self._current_path or "/", entry.name)

    @property
    def current_path(self) -> str:
        return self._current_path

    # -- drop zone & drag support -------------------------------------

    def set_partner_pane(self, partner: Optional["FilePane"]) -> None:
        self._partner_pane = partner

    def set_across_visible(self, visible: bool) -> None:
        """Show the copy/move-to-other-pane buttons (dual-pane mode only)."""
        for button in self._across_buttons:
            button.set_visible(visible)
        self._update_menu_state()

    def set_active(self, active: bool) -> None:
        if active:
            self.add_css_class("fm-pane-active")
        else:
            self.remove_css_class("fm-pane-active")

    def focus_view(self) -> None:
        view = self._visible_view()
        if view is not None:
            view.grab_focus()

    # -- callbacks ------------------------------------------------------

    def _attach_shortcuts(self, view: Gtk.Widget) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)

        def add_shortcut(trigger: Gtk.ShortcutTrigger, handler: Callable[[], bool]) -> None:
            if trigger is None:
                return
            action = Gtk.CallbackAction.new(lambda _widget, _args: handler())
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))

        def add_trigger_string(trigger_str: str, handler: Callable[[], bool]) -> None:
            if not trigger_str:
                return
            trigger = Gtk.ShortcutTrigger.parse_string(trigger_str)
            add_shortcut(trigger, handler)

        add_trigger_string("<primary>l", self._shortcut_focus_path_entry)
        add_trigger_string("<primary>r", self._shortcut_refresh)
        add_shortcut(Gtk.KeyvalTrigger.new(Gdk.KEY_F5, Gdk.ModifierType(0)), self._shortcut_refresh)
        add_trigger_string("<primary>c", lambda: self._shortcut_operation("copy"))
        add_trigger_string("<primary>x", lambda: self._shortcut_operation("cut"))
        add_trigger_string("<primary>v", lambda: self._shortcut_operation("paste"))
        add_trigger_string(
            "<shift><primary>v",
            lambda: self._shortcut_operation("paste", force_move=True),
        )

        # Delete moves to the trash where there is one; Shift+Delete deletes
        # permanently, as in Nautilus.
        for keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete):
            add_shortcut(
                Gtk.KeyvalTrigger.new(keyval, Gdk.ModifierType(0)), self._shortcut_delete
            )
            add_shortcut(
                Gtk.KeyvalTrigger.new(keyval, Gdk.ModifierType.SHIFT_MASK),
                self._shortcut_delete_permanently,
            )

        # Ctrl+A / Ctrl+Shift+A come from GtkListBase; Nautilus adds invert.
        add_trigger_string("<shift><primary>i", self._shortcut_invert_selection)

        # Nautilus's view keys (nautilus-files-view.c, nautilus-window-slot.c).
        # <primary> is Command on macOS, where the keys that clash with
        # system shortcuts (Cmd+H hides the app) use Finder's instead.
        add_shortcut(
            Gtk.KeyvalTrigger.new(Gdk.KEY_F2, Gdk.ModifierType(0)),
            lambda: self._shortcut_operation("rename"),
        )
        add_trigger_string("Menu|<shift>F10", self._shortcut_popup_menu)
        add_trigger_string("<primary>i|<alt>Return", self._shortcut_properties)
        add_trigger_string("<shift><primary>n", self._shortcut_new_folder)
        add_trigger_string("<primary>s", self._shortcut_select_pattern)
        add_trigger_string(
            "<primary>equal|<primary>plus|<primary>KP_Add|ZoomIn",
            lambda: self._shortcut_zoom(1),
        )
        add_trigger_string(
            "<primary>minus|<primary>KP_Subtract|ZoomOut",
            lambda: self._shortcut_zoom(-1),
        )
        add_trigger_string("<primary>0|<primary>KP_0", lambda: self._shortcut_zoom(0))
        add_trigger_string("<primary>1", lambda: self._shortcut_view("list"))
        add_trigger_string("<primary>2", lambda: self._shortcut_view("grid"))
        add_trigger_string("F6", lambda: self._shortcut_operation("copy-across"))
        add_trigger_string("<shift>F6", lambda: self._shortcut_operation("move-across"))

        if _is_macos():
            hidden = "<shift><primary>period|<shift><primary>greater"
            back, forward = "<primary>bracketleft|Back", "<primary>bracketright|Forward"
            up, down = "<primary>Up", "<primary>Down"
            # Mac keyboards label Backspace "delete"; Finder deletes with Cmd+it.
            add_trigger_string("<primary>BackSpace", self._shortcut_delete)
        else:
            hidden = "<primary>h"
            back, forward = "<alt>Left|Back", "<alt>Right|Forward"
            if Gtk.Widget.get_default_direction() == Gtk.TextDirection.RTL:
                back, forward = "<alt>Right|Back", "<alt>Left|Forward"
            up, down = "<alt>Up", "<alt>Down"
            add_trigger_string("BackSpace", lambda: self._shortcut_navigate("up"))
        add_trigger_string(hidden, self._shortcut_toggle_hidden)
        add_trigger_string(back, lambda: self._shortcut_navigate("back"))
        add_trigger_string(forward, lambda: self._shortcut_navigate("forward"))
        add_trigger_string(up, lambda: self._shortcut_navigate("up"))
        add_trigger_string(down, lambda: self._shortcut_navigate("down"))
        add_trigger_string("<alt>Home", lambda: self._shortcut_navigate("home"))

        view.add_controller(controller)

    def _shortcut_focus_path_entry(self) -> bool:
        entry = getattr(self.toolbar, "path_entry", None)
        if isinstance(entry, Gtk.Entry):
            try:
                entry.grab_focus()
                entry.select_region(0, -1)
            except Exception:
                pass
        return True

    def _shortcut_refresh(self) -> bool:
        self._on_refresh_clicked(None)
        return True

    def _shortcut_delete(self) -> bool:
        self._emit_entry_operation("delete")
        return True

    def _shortcut_delete_permanently(self) -> bool:
        self._emit_entry_operation("delete-permanently")
        return True

    def _shortcut_invert_selection(self) -> bool:
        selected = set(self._get_selected_indices())
        for index in range(len(self._entries)):
            if index in selected:
                self._selection_model.unselect_item(index)
            else:
                self._selection_model.select_item(index, False)
        self._selection_anchor = None
        return True

    def _shortcut_properties(self) -> bool:
        self._menu_for_background = False
        self._on_menu_properties()
        return True

    def _shortcut_new_folder(self) -> bool:
        self.emit("request-operation", "mkdir", None)
        return True

    def _shortcut_toggle_hidden(self) -> bool:
        self.set_show_hidden(not self._show_hidden)
        return True

    def _shortcut_zoom(self, direction: int) -> bool:
        """Zoom in (+1), out (-1), or back to the default size (0)."""
        if direction == 0:
            view = self._zoom_view()
            self.set_icon_level(view, _DEFAULT_ICON_LEVELS[view])
        else:
            self._request_zoom(direction)
        return True

    def _shortcut_view(self, view: str) -> bool:
        self.toolbar.set_view(view)
        return True

    def _shortcut_navigate(self, where: str) -> bool:
        if where == "back":
            self._on_back_clicked(None)
        elif where == "forward":
            self._on_forward_clicked(None)
        elif where == "up":
            self._on_up_clicked(None)
        elif where == "home":
            self.emit("path-changed", "~")
        elif where == "down":
            # Nautilus's Alt+Down opens the selected folder.
            selected = self._get_selected_indices()
            if len(selected) == 1 and self._entries[selected[0]].is_dir:
                self._navigate_to_entry(selected[0])
        return True

    def select_pattern(self, pattern: str) -> int:
        """Select the items matching *pattern*; return how many matched."""
        matches = _pattern_matcher(pattern)
        self._selection_model.unselect_all()
        self._selection_anchor = None
        count = 0
        for index, entry in enumerate(self._entries):
            if matches(entry.name):
                self._selection_model.select_item(index, False)
                if count == 0:
                    self._scroll_to_position(index)
                self._selection_anchor = index
                count += 1
        return count

    def _shortcut_select_pattern(self) -> bool:
        dialog = Adw.AlertDialog.new(_("Select Items Matching"), None)
        pattern_entry = Gtk.Entry()
        pattern_entry.set_placeholder_text(_("Pattern, e.g. *.txt"))
        pattern_entry.set_activates_default(True)
        dialog.set_extra_child(pattern_entry)
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("select", _("Select"))
        dialog.set_response_appearance("select", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("select")
        dialog.set_close_response("cancel")

        def _on_response(_dialog, response: str) -> None:
            pattern = pattern_entry.get_text()
            if response == "select" and pattern:
                if not self.select_pattern(pattern):
                    self.show_toast(_("No items match"))

        dialog.connect("response", _on_response)
        dialog.present(self)

        def _focus_entry() -> bool:
            pattern_entry.grab_focus()
            return False

        GLib.idle_add(_focus_entry)
        return True

    def _visible_view(self) -> Optional[Gtk.Widget]:
        visible = self._stack.get_visible_child_name()
        if visible == "list":
            return self._list_view
        if visible == "grid":
            return self._grid_view
        return None

    def _shortcut_popup_menu(self) -> bool:
        """Open the context menu from the keyboard (Menu / Shift+F10).

        Like Nautilus it offers the selection menu when something is
        selected and the folder menu otherwise, pointing at the focused item
        when there is one.
        """
        view = self._visible_view()
        if view is None:
            return False
        x, y = view.get_width() / 2, view.get_height() / 2
        root = view.get_root()
        focus = root.get_focus() if root is not None else None
        if focus is not None and focus is not view and focus.is_ancestor(view):
            ok, bounds = focus.compute_bounds(view)
            if ok:
                x = bounds.get_x() + bounds.get_width() / 2
                y = bounds.get_y() + bounds.get_height()
        background = not self._get_selected_indices()
        self._show_context_menu(view, x, y, background=background)
        return True

    def _on_stack_page_changed(self, stack: Gtk.Stack, _pspec) -> None:
        # The error and connecting pages leave the last file view attached,
        # so returning from them does not rebuild its rows.
        name = stack.get_visible_child_name()
        self._update_empty_state()
        if name not in ("list", "grid") or name == self._active_view_name:
            return
        self._active_view_name = name
        if name == "grid":
            self._list_view.set_model(None)
            self._grid_view.set_model(self._selection_model)
        else:
            self._grid_view.set_model(None)
            self._list_view.set_model(self._selection_model)

    def _on_view_toggle(self, toolbar, view_name: str) -> None:
        self._stack.set_visible_child_name(view_name)
        get_settings().set("view", view_name)
        # Update the split button icon to reflect current view
        self._update_view_button_icon()
        # Each view has its own zoom; show this one's on the slider.
        self._sync_zoom_slider()

    def _on_toolbar_show_hidden_toggled(self, _toolbar, show_hidden: bool) -> None:
        self.set_show_hidden(show_hidden)

    def _on_toolbar_zoom_changed(self, _toolbar, level: int) -> None:
        self.set_icon_level(self._zoom_view(), level)

    def _on_path_entry(self, entry: Gtk.Entry) -> None:
        self.emit("path-changed", entry.get_text() or "/")
        self.focus_view()

    def _create_column_view(self) -> Gtk.ColumnView:
        """Build the Nautilus-style details list (Name / Size / Modified).

        Header clicks are wired through per-column ``Gtk.CustomSorter``
        objects; the actual order still comes from ``_sort_entries`` so
        directories stay grouped first.
        """
        column_view = Gtk.ColumnView(model=self._selection_model)
        column_view.add_css_class("fm-list-view")
        column_view.set_can_focus(True)
        column_view.set_hexpand(True)
        column_view.set_single_click_activate(False)
        column_view.set_enable_rubberband(True)

        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect("setup", self._on_name_setup)
        name_factory.connect("bind", self._on_name_bind)
        name_factory.connect("unbind", self._on_name_unbind)

        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect("setup", self._on_size_setup)
        size_factory.connect("bind", self._on_size_bind)
        size_factory.connect("unbind", self._on_size_unbind)

        modified_factory = Gtk.SignalListItemFactory()
        modified_factory.connect("setup", self._on_modified_setup)
        modified_factory.connect("bind", self._on_modified_bind)

        name_column = self._make_list_column("name", _("Name"), name_factory, expand=True)
        size_column = self._make_list_column(
            "size", _("Size"), size_factory, fixed_width=96
        )
        modified_column = self._make_list_column(
            "modified", _("Modified"), modified_factory, fixed_width=148
        )
        column_view.append_column(name_column)
        column_view.append_column(size_column)
        column_view.append_column(modified_column)
        self._list_columns = {
            "name": name_column,
            "size": size_column,
            "modified": modified_column,
        }

        sorter = column_view.get_sorter()
        if sorter is not None:
            sorter.connect("changed", self._on_column_sorter_changed)
        return column_view

    def _make_list_column(
        self,
        column_id: str,
        title: str,
        factory: Gtk.SignalListItemFactory,
        *,
        expand: bool = False,
        fixed_width: Optional[int] = None,
    ) -> Gtk.ColumnViewColumn:
        column = Gtk.ColumnViewColumn(title=title, factory=factory)
        column.set_expand(expand)
        column.set_id(column_id)
        column.set_resizable(True)
        if fixed_width is not None:
            column.set_fixed_width(fixed_width)
        # A sorter is required for the header to be clickable; comparison is a
        # no-op because ``_sort_entries`` owns the real order.
        column.set_sorter(Gtk.CustomSorter.new(None))
        return column

    def _set_view_rubberband_enabled(self, view, enabled: bool) -> None:
        """Toggle rubberband on a list/grid view.

        GTK #5670: with rubberband left on, a press-drag on an item often
        starts a selection rectangle instead of DnD. Nautilus disables
        rubberband for the duration of an item press (see
        ``rubberband_set_state`` in nautilus-list-base.c).
        """
        if view is not None:
            view.set_enable_rubberband(enabled)

    def _set_list_rubberband_enabled(self, enabled: bool) -> None:
        self._set_view_rubberband_enabled(getattr(self, "_list_view", None), enabled)

    def _set_grid_rubberband_enabled(self, enabled: bool) -> None:
        self._set_view_rubberband_enabled(getattr(self, "_grid_view", None), enabled)

    def _on_list_item_pressed(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        self._set_list_rubberband_enabled(False)

    def _on_list_item_released(
        self, _gesture: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        self._set_list_rubberband_enabled(True)

    def _on_list_item_stopped(self, _gesture: Gtk.GestureClick) -> None:
        self._set_list_rubberband_enabled(True)

    def _attach_list_cell_controllers(self, widget: Gtk.Widget, cell) -> None:
        # Fill the column cell so blank row space (e.g. left of a right-aligned
        # Size/Modified label) still hits this drag source, matching Nautilus
        # view cells that use BinLayout.
        widget.set_hexpand(True)
        widget.set_halign(Gtk.Align.FILL)
        widget.set_valign(Gtk.Align.FILL)

        # Disable rubberband on press so DnD can win (GTK #5670 / Nautilus).
        # Do not claim the sequence — GTK's default list selection stays intact.
        item_click = Gtk.GestureClick()
        item_click.set_button(Gdk.BUTTON_PRIMARY)
        item_click.connect("pressed", self._on_list_item_pressed)
        item_click.connect("released", self._on_list_item_released)
        item_click.connect("stopped", self._on_list_item_stopped)
        widget.add_controller(item_click)

        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        widget.add_controller(drag_source)

        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_list_item_right_click, cell)
        widget.add_controller(right_click_gesture)

    def _entry_from_cell(self, cell) -> Tuple[Optional[int], Optional[FileEntry]]:
        position = cell.get_position()
        entry = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
        return position, entry

    @staticmethod
    def _size_column_text(entry: FileEntry) -> str:
        if entry.is_dir:
            if entry.item_count is not None:
                return _item_count_text(entry.item_count)
            return "—"
        return format_size(entry.size)

    @staticmethod
    def _modified_column_text(entry: FileEntry) -> str:
        if not entry.modified:
            return "—"
        return _human_time(entry.modified)

    def _on_name_setup(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.add_css_class("fm-view-cell")
        icon = Gtk.Image()
        icon.set_pixel_size(self._list_icon_px())
        icon.add_css_class("fm-view-icon")
        icon.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(xalign=0)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name_label.set_hexpand(True)
        box.append(icon)
        box.append(name_label)
        box.set_hexpand(True)
        box.icon = icon
        box.name_label = name_label
        self._attach_list_cell_controllers(box, cell)
        cell.set_child(box)

    def _on_name_bind(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        box = cell.get_child()
        icon: Gtk.Image = box.icon
        name_label: Gtk.Label = box.name_label

        # Cells are pooled; reset pixel size every bind so zoom applies
        # without rebuilding the store.
        icon.set_pixel_size(self._list_icon_px())
        self._bound_list_icons.add(icon)

        position, entry = self._entry_from_cell(cell)
        box.drag_position = position

        if entry is None:
            value = cell.get_item().get_string()
            name_label.set_text(value)
            name_label.set_tooltip_text(value)
            is_dir = value.endswith('/')
            icon.set_from_gicon(icon_for_name(value.rstrip("/"), is_dir))
            return

        display_name = safe_display_text(entry.name)
        name_label.set_text(display_name)
        name_label.set_tooltip_text(display_name)
        icon.set_from_gicon(icon_for_name(entry.name, entry.is_dir))
        _set_hidden_file_style(box, entry.name)
        if entry.is_symlink:
            name_label.add_css_class("fm-link")
            name_label.set_attributes(_ITALIC)
        else:
            name_label.remove_css_class("fm-link")
            name_label.set_attributes(None)
        box._pane_entry = entry
        box._pane_index = position

    def _on_name_unbind(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        box = cell.get_child()
        if box is None:
            return
        icon = getattr(box, "icon", None)
        if icon is not None:
            self._bound_list_icons.discard(icon)

    def _on_size_setup(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        # FILL + xalign=1: label covers the whole cell (for DnD hit testing)
        # while the text stays right-aligned.
        label = Gtk.Label(xalign=1)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("dim-label")
        label.add_css_class("numeric")
        label.add_css_class("fm-view-cell")
        self._attach_list_cell_controllers(label, cell)
        cell.set_child(label)

    def _on_size_bind(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        label = cell.get_child()
        position, entry = self._entry_from_cell(cell)
        label.drag_position = position
        if entry is None:
            label.set_text("—")
            label.set_tooltip_text(None)
            return
        text = self._size_column_text(entry)
        label.set_text(text)
        label.set_tooltip_text(text)
        label._pane_entry = entry
        self._bound_size_labels.add(label)

    def _on_size_unbind(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        label = cell.get_child()
        if label is not None:
            self._bound_size_labels.discard(label)

    def _on_modified_setup(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        # FILL + xalign=1: same full-cell hit target as the Size column.
        label = Gtk.Label(xalign=1)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.add_css_class("dim-label")
        label.add_css_class("numeric")
        label.add_css_class("fm-view-cell")
        self._attach_list_cell_controllers(label, cell)
        cell.set_child(label)

    def _on_modified_bind(self, factory: Gtk.SignalListItemFactory, cell) -> None:
        label = cell.get_child()
        position, entry = self._entry_from_cell(cell)
        label.drag_position = position
        if entry is None:
            label.set_text("—")
            label.set_tooltip_text(None)
            return
        text = self._modified_column_text(entry)
        label.set_text(text)
        label.set_tooltip_text(text)
        label._pane_entry = entry

    def update_item_counts(self, path: str, counts) -> None:
        """Apply background-computed folder item-counts to the current listing.

        Mutates the shared ``FileEntry`` objects (so rows bound later read the
        count) and refreshes currently visible Size cells in place — no
        list-store rebuild, so scroll position and selection are preserved.
        Ignored if the user has navigated away.
        """
        if not counts or path != self._current_path:
            return
        for entry in self._cached_entries:
            if entry.is_dir and entry.name in counts:
                entry.item_count = counts[entry.name]
        for label in list(self._bound_size_labels):
            entry = getattr(label, "_pane_entry", None)
            if entry is None or not entry.is_dir or entry.name not in counts:
                continue
            count_text = _item_count_text(entry.item_count)
            label.set_text(count_text)
            label.set_tooltip_text(count_text)

    def _list_icon_px(self) -> int:
        return _LIST_ICON_SIZES[self._icon_levels["list"]]

    def _grid_icon_px(self) -> int:
        return _GRID_ICON_SIZES[self._icon_levels["grid"]]

    def _zoom_view(self) -> str:
        """The view zoom acts on: the one on screen, as in Nautilus."""
        stack = getattr(self, "_stack", None)
        visible = stack.get_visible_child_name() if stack is not None else None
        if visible in ("list", "grid"):
            return visible
        return getattr(getattr(self, "toolbar", None), "_current_view", "list")

    def set_icon_levels(self, list_level: int, grid_level: int) -> None:
        """Seed both views' zoom (e.g. from settings) without persisting."""
        self._icon_levels = {
            "list": clamp_icon_level("list", list_level),
            "grid": clamp_icon_level("grid", grid_level),
        }
        self._update_list_density()
        self._sync_zoom_slider()

    def set_icon_level(self, view: str, level: int) -> None:
        """Set one view's icon zoom level and resize its visible items.

        List and grid zoom separately, like Nautilus.
        """
        clamped = clamp_icon_level(view, level)
        if clamped == self._icon_levels[view]:
            return
        self._icon_levels[view] = clamped
        # Resize the currently bound icon widgets in place. This is O(visible)
        # — far cheaper than rebuilding the list store. New rows that get
        # bound while scrolling will pick up the size from the bind callback
        # (which reads self._icon_levels directly). queue_resize() forces
        # GtkGridView to re-measure cell sizes; set_pixel_size alone updates
        # the image's request but the grid caches its cell extents.
        if view == "list":
            self._update_list_density()
            images, px = self._bound_list_icons, self._list_icon_px()
            widget = getattr(self, "_list_view", None)
        else:
            images, px = self._bound_grid_images, self._grid_icon_px()
            widget = getattr(self, "_grid_view", None)
        for icon in list(images):
            try:
                icon.set_pixel_size(px)
                icon.queue_resize()
            except Exception:
                pass
        if widget is not None:
            widget.queue_resize()
        # Keep the toolbar's slider in sync — e.g. when the level was changed
        # via Ctrl+wheel rather than by the user dragging the slider itself.
        self._sync_zoom_slider()
        # Persist whichever pane was zoomed last as the new default for any
        # newly opened windows.
        get_settings().set(f"{view}_icon_level", self._icon_levels[view])

    def _sync_zoom_slider(self) -> None:
        toolbar = getattr(self, "toolbar", None)
        if toolbar is None:
            return
        view = self._zoom_view()
        toolbar.set_zoom_level(self._icon_levels[view], _MAX_ICON_LEVELS[view])

    def _update_list_density(self) -> None:
        """Tighter rows at the smallest list size, like Nautilus's compact."""
        view = getattr(self, "_list_view", None)
        if view is None:
            return
        if self._list_icon_px() <= 16:
            view.add_css_class("compact")
        else:
            view.remove_css_class("compact")

    def _request_zoom(self, direction: int) -> None:
        """Zoom the visible view by *direction* (+1 / -1)."""
        view = self._zoom_view()
        self.set_icon_level(view, self._icon_levels[view] + direction)

    @staticmethod
    def load_saved_icon_levels() -> Tuple[int, int]:
        """Return the persisted (list, grid) zoom levels for new panes."""
        settings = get_settings()
        try:
            return (
                clamp_icon_level("list", settings.get("list_icon_level", _DEFAULT_ICON_LEVELS["list"])),
                clamp_icon_level("grid", settings.get("grid_icon_level", _DEFAULT_ICON_LEVELS["grid"])),
            )
        except (TypeError, ValueError):
            return _DEFAULT_ICON_LEVELS["list"], _DEFAULT_ICON_LEVELS["grid"]

    def _on_pane_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        """Intercept Ctrl/Cmd + wheel to zoom icons; otherwise let it scroll."""
        try:
            event = controller.get_current_event()
            state = event.get_modifier_state() if event is not None else Gdk.ModifierType(0)
        except Exception:
            state = Gdk.ModifierType(0)

        if _is_macos():
            primary = bool(state & Gdk.ModifierType.META_MASK)
        else:
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if not primary:
            return False  # propagate; ScrolledWindow handles normal scrolling

        if dy > 0:
            direction = -1
        elif dy < 0:
            direction = +1
        else:
            return False

        self._request_zoom(direction)
        return True  # consume; don't also scroll the view

    def _on_grid_setup(self, factory: Gtk.SignalListItemFactory, item):
        button = Gtk.Button()
        button.set_has_frame(False)
        button.add_css_class("fm-view-cell")
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image()
        icon.set_pixel_size(self._grid_icon_px())
        icon.add_css_class("fm-view-icon")
        icon.set_halign(Gtk.Align.CENTER)
        content.append(icon)

        # Nautilus grid names: up to three wrapped lines, cut in the middle.
        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(3)
        # Force normal weight: Gtk.Button styling makes its label bold by
        # default, which looks wrong for filenames in a grid cell.
        normal_weight_attrs = Pango.AttrList()
        normal_weight_attrs.insert(Pango.attr_weight_new(Pango.Weight.NORMAL))
        label.set_attributes(normal_weight_attrs)
        content.append(label)

        button.set_child(content)

        # GestureClick allows inspecting modifier state and click counts before
        # the button consumes the event. Use this to keep selection behaviour in
        # sync with Gtk.GridView expectations.
        click_gesture = Gtk.GestureClick()
        click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        click_gesture.set_exclusive(True)
        click_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click_gesture.connect("pressed", self._on_grid_cell_pressed, button)
        click_gesture.connect("released", self._on_grid_cell_released, button)
        click_gesture.connect("stopped", self._on_grid_cell_stopped)
        button.add_controller(click_gesture)

        # Add right-click gesture to select item and show context menu
        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_grid_item_right_click, item)
        button.add_controller(right_click_gesture)

        # Add drag source for file operations
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        button.add_controller(drag_source)

        item.set_child(button)

    def _on_grid_bind(self, factory: Gtk.SignalListItemFactory, item):
        button = item.get_child()
        content = button.get_child()
        icon = content.get_first_child()
        label = content.get_last_child()

        # Grid cells are pooled by GtkGridView; reset pixel size every bind so
        # the current zoom level wins after re-binding.
        icon.set_pixel_size(self._grid_icon_px())
        # Track this image so a zoom event can update it in place.
        self._bound_grid_images.add(icon)

        value = item.get_item().get_string()
        display_text = value[:-1] if value.endswith('/') else value

        label.set_text(display_text)
        label.set_tooltip_text(display_text)
        button.set_tooltip_text(display_text)

        entry: Optional[FileEntry] = None
        position = item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]

        # Use the resolved FileEntry when available so the icon reflects the
        # real filename rather than the display string with its trailing '/'.
        if entry is not None:
            icon.set_from_gicon(icon_for_name(entry.name, entry.is_dir))
            _set_hidden_file_style(button, entry.name)
        else:
            is_dir = value.endswith('/')
            icon.set_from_gicon(icon_for_name(display_text, is_dir))

        # Store position in the button for drag operations
        button.drag_position = position

    def _on_grid_unbind(self, factory: Gtk.SignalListItemFactory, item):
        button = item.get_child()
        if button is None:
            return
        content = button.get_child()
        if content is not None:
            icon = content.get_first_child()
            if icon is not None:
                self._bound_grid_images.discard(icon)

    def _on_grid_cell_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
        button: Gtk.Button,
    ) -> None:
        # Same GTK #5670 workaround as list mode / Nautilus: rubberband must
        # be off while the item gesture is active so DnD can claim the drag.
        self._set_grid_rubberband_enabled(False)
        position = getattr(button, "drag_position", None)
        if position is None or not (0 <= position < len(self._entries)):
            return

        if n_press == 1:
            self._update_grid_selection_for_press(position, gesture)
            return

        self._pending_grid_collapse = None
        if n_press >= 2:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            self._suppress_next_context_menu = True
            self._navigate_to_entry(position)

    def _update_grid_selection_for_press(
        self, position: int, gesture: Gtk.GestureClick
    ) -> None:
        state = gesture.get_current_event_state()
        if state is None:
            state = Gdk.ModifierType(0)

        primary_mask = Gdk.ModifierType.CONTROL_MASK
        if _is_macos():
            primary_mask |= Gdk.ModifierType.META_MASK | Gdk.ModifierType.SUPER_MASK

        has_primary = bool(state & primary_mask)
        has_shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
        self._pending_grid_collapse = None

        if has_shift and self._anchor_follows_focus:
            focused = self._grid_focus_position()
            if focused is not None:
                self._selection_anchor = focused
        self._anchor_follows_focus = False

        if has_shift and self._selection_anchor is not None:
            start = min(self._selection_anchor, position)
            end = max(self._selection_anchor, position)
            self._selection_model.unselect_all()
            for index in range(start, end + 1):
                self._selection_model.select_item(index, False)
            self._selection_anchor = position
        elif has_shift:
            self._selection_model.select_item(position, True)
            self._selection_anchor = position
        elif has_primary:
            if self._selection_model.is_selected(position):
                self._selection_model.unselect_item(position)
            else:
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        else:
            if not self._selection_model.is_selected(position):
                self._selection_model.unselect_all()
                self._selection_model.select_item(position, False)
            else:
                # Keep the group for a possible drag; narrow on release.
                self._pending_grid_collapse = position
            self._selection_anchor = position

    def _on_grid_cell_released(
        self,
        _gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
        button: Gtk.Button,
    ) -> None:
        self._set_grid_rubberband_enabled(True)
        pending = self._pending_grid_collapse
        self._pending_grid_collapse = None
        position = getattr(button, "drag_position", None)
        if n_press != 1 or pending is None or pending != position:
            return
        if not (0 <= position < len(self._entries)):
            return
        # Matches GtkListBase: a plain click (no drag) selects only that item.
        self._selection_model.select_item(position, True)
        self._selection_anchor = position

    def _on_grid_cell_stopped(self, _gesture: Gtk.GestureClick) -> None:
        # The press turned into a drag or was cancelled: keep the group.
        self._set_grid_rubberband_enabled(True)
        self._pending_grid_collapse = None

    def _grid_focus_position(self) -> Optional[int]:
        child = self._grid_view.get_focus_child()
        while child is not None:
            position = getattr(child, "drag_position", None)
            if position is not None:
                return position if 0 <= position < len(self._entries) else None
            child = child.get_first_child()
        return None

    _GRID_NAV_KEYS = frozenset(
        getattr(Gdk, name, None)
        for name in (
            "KEY_Left", "KEY_Right", "KEY_Up", "KEY_Down",
            "KEY_Home", "KEY_End", "KEY_Page_Up", "KEY_Page_Down",
            "KEY_KP_Left", "KEY_KP_Right", "KEY_KP_Up", "KEY_KP_Down",
            "KEY_KP_Home", "KEY_KP_End", "KEY_KP_Page_Up", "KEY_KP_Page_Down",
        )
    ) - {None}

    def _on_grid_nav_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        """Track GTK's keyboard anchor for the grid's custom Shift+click.

        Runs in the capture phase, before GridView moves focus. A plain arrow
        selects the item it lands on, which becomes the anchor; a Shift+arrow
        after that extends from where focus was before the move.
        """
        if keyval not in self._GRID_NAV_KEYS:
            return False
        if state & Gdk.ModifierType.CONTROL_MASK:
            return False
        if state & Gdk.ModifierType.SHIFT_MASK:
            if self._anchor_follows_focus:
                focused = self._grid_focus_position()
                if focused is not None:
                    self._selection_anchor = focused
                self._anchor_follows_focus = False
        else:
            self._anchor_follows_focus = True
        return False

    def _on_selection_changed(self, model, position, n_items):
        self._update_menu_state()

    def _setup_sorting_actions(self) -> None:
        """Set up sorting actions for the split button menu."""
        # Create actions for sorting
        self._menu_actions["sort-by-name"] = Gio.SimpleAction.new("sort-by-name", None)
        self._menu_actions["sort-by-size"] = Gio.SimpleAction.new("sort-by-size", None)
        self._menu_actions["sort-by-modified"] = Gio.SimpleAction.new("sort-by-modified", None)
        self._menu_actions["sort-by-type"] = Gio.SimpleAction.new("sort-by-type", None)

        # Create stateful actions for sort direction (radio buttons)
        self._menu_actions["sort-direction-asc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-asc", None, GLib.Variant.new_boolean(not self._sort_descending)
        )
        self._menu_actions["sort-direction-desc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-desc", None, GLib.Variant.new_boolean(self._sort_descending)
        )

        # Connect action handlers
        self._menu_actions["sort-by-name"].connect("activate", lambda *_: self._on_sort_by("name"))
        self._menu_actions["sort-by-size"].connect("activate", lambda *_: self._on_sort_by("size"))
        self._menu_actions["sort-by-modified"].connect("activate", lambda *_: self._on_sort_by("modified"))
        self._menu_actions["sort-by-type"].connect("activate", lambda *_: self._on_sort_by("type"))
        self._menu_actions["sort-direction-asc"].connect("activate", lambda *_: self._on_sort_direction(False))
        self._menu_actions["sort-direction-desc"].connect("activate", lambda *_: self._on_sort_direction(True))

        # Add actions to action group
        for action in self._menu_actions.values():
            self._menu_action_group.add_action(action)

    def _on_sort_by(self, sort_key: str) -> None:
        """Handle sort by selection from menu."""
        if self._sort_key != sort_key:
            self._sort_key = sort_key
            self._refresh_sorted_entries(preserve_selection=True)
            self._sync_column_sort_indicator()

    def _on_sort_direction(self, descending: bool) -> None:
        """Handle sort direction selection from menu."""
        if self._sort_descending != descending:
            self._sort_descending = descending
            self._refresh_sorted_entries(preserve_selection=True)
            self._update_sort_direction_states()
            self._sync_column_sort_indicator()

    def _on_column_sorter_changed(self, sorter, *_args) -> None:
        """Keep Python sort in sync when the user clicks a column header."""
        if self._syncing_column_sort:
            return
        column = sorter.get_primary_sort_column()
        if column is None:
            return
        key = column.get_id()
        if key not in ("name", "size", "modified"):
            return
        descending = sorter.get_primary_sort_order() == Gtk.SortType.DESCENDING
        if key == self._sort_key and descending == self._sort_descending:
            return
        self._sort_key = key
        self._sort_descending = descending
        self._refresh_sorted_entries(preserve_selection=True)
        self._update_sort_direction_states()

    def _sync_column_sort_indicator(self) -> None:
        """Mirror the current sort key/direction onto the ColumnView headers."""
        view = getattr(self, "_list_view", None)
        columns = getattr(self, "_list_columns", None)
        if view is None or not columns:
            return
        # Type has no column; its sort then shows no header arrow.
        column = columns.get(self._sort_key)
        direction = (
            Gtk.SortType.DESCENDING if self._sort_descending else Gtk.SortType.ASCENDING
        )
        self._syncing_column_sort = True
        try:
            # Clear first to avoid a double triangle
            # (https://gitlab.gnome.org/GNOME/gtk/-/issues/4696).
            view.sort_by_column(None, Gtk.SortType.ASCENDING)
            if column is not None:
                view.sort_by_column(column, direction)
        except Exception:
            logger.debug("Failed to sync column sort indicator", exc_info=True)
        finally:
            self._syncing_column_sort = False

    def _update_view_button_icon(self) -> None:
        """Show the destination layout on the view toggle (Nautilus-style)."""
        self.toolbar._sync_view_toggle_appearance()

    def _update_sort_direction_states(self) -> None:
        """Update the radio button states for sort direction."""
        asc_action = self._menu_actions["sort-direction-asc"]
        desc_action = self._menu_actions["sort-direction-desc"]

        asc_action.set_state(GLib.Variant.new_boolean(not self._sort_descending))
        desc_action.set_state(GLib.Variant.new_boolean(self._sort_descending))

    def _create_menu_actions(self) -> None:
        def _add_action(name: str, callback: Callable[[], None]) -> None:
            if name not in self._menu_actions:
                action = Gio.SimpleAction.new(name, None)

                def _on_activate(_action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
                    try:
                        callback()
                    except Exception as e:
                        logger.error("Pane action %r failed: %s", name, e, exc_info=True)

                action.connect("activate", _on_activate)
                self._menu_action_group.add_action(action)
                self._menu_actions[name] = action

        _add_action("open", self._on_menu_open)
        _add_action("open_with", lambda: self._emit_entry_operation("open-with"))
        _add_action("copy_location", self._on_menu_copy_location)
        _add_action("select_all", self._on_menu_select_all)
        _add_action("copy_across", lambda: self._emit_entry_operation("copy-across"))
        _add_action("move_across", lambda: self._emit_entry_operation("move-across"))
        _add_action("copy", lambda: self._emit_entry_operation("copy"))
        _add_action("cut", lambda: self._emit_entry_operation("cut"))
        _add_action("paste", self._emit_paste_operation)
        _add_action("rename", lambda: self._emit_entry_operation("rename"))
        _add_action("delete", lambda: self._emit_entry_operation("delete"))
        _add_action("delete_permanently", lambda: self._emit_entry_operation("delete-permanently"))
        _add_action("new_folder", lambda: self.emit("request-operation", "mkdir", None))
        _add_action("new_file", lambda: self.emit("request-operation", "newfile", None))
        _add_action("properties", self._on_menu_properties)
        _add_action("open_terminal", self._on_menu_open_terminal)
        _add_action("open_in_files", self._on_menu_open_in_files)

    def _on_list_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, cell) -> None:
        """Handle right-click on a list item: select it and show context menu."""
        position = cell.get_position()
        if position is not None and 0 <= position < len(self._entries):
            # If the clicked item is not already selected, clear selection and
            # select only this item; otherwise keep the current selection.
            if not self._selection_model.is_selected(position):
                self._selection_model.unselect_all()
            self._selection_model.select_item(position, False)
            self._selection_anchor = position

        # Show context menu at click position
        box = cell.get_child()
        if box:
            # Convert coordinates to the view widget's coordinate space
            view_widget = self._list_view
            widget_x, widget_y = box.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                self._show_context_menu(box, x, y)

    def _on_grid_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        """Handle right-click on a grid item: select it and show context menu."""
        position = list_item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            if not self._selection_model.is_selected(position):
                self._selection_model.unselect_all()
            self._selection_model.select_item(position, False)
            self._selection_anchor = position

        # Show context menu at click position
        button = list_item.get_child()
        if button:
            view_widget = self._grid_view
            widget_x, widget_y = button.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                self._show_context_menu(button, x, y)

    def _add_context_controller(self, widget: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)

        def _on_pressed(_gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            # On empty space, keep the selection (as Nautilus does) and show
            # the folder menu instead of the selection menu.
            background = self._is_click_on_empty_space(widget, x, y)
            self._show_context_menu(widget, x, y, background=background)

        gesture.connect("pressed", _on_pressed)
        widget.add_controller(gesture)

        long_press = Gtk.GestureLongPress()

        def _on_long_press(_gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
            background = self._is_click_on_empty_space(widget, x, y)
            self._show_context_menu(widget, x, y, background=background)

        long_press.connect("pressed", _on_long_press)
        widget.add_controller(long_press)

        # GTK does not clear the selection on a background click; Nautilus
        # does for any button but the secondary one, unless Ctrl/Shift is held.
        background_click = Gtk.GestureClick()
        background_click.set_button(0)
        background_click.connect("pressed", self._on_view_background_pressed, widget)
        widget.add_controller(background_click)

    def _on_view_background_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        widget: Gtk.Widget,
    ) -> None:
        if gesture.get_current_button() == Gdk.BUTTON_SECONDARY:
            return
        state = gesture.get_current_event_state()
        selection_mask = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK
        if _is_macos():
            selection_mask |= Gdk.ModifierType.META_MASK
        if state is not None and state & selection_mask:
            return
        if not self._is_click_on_empty_space(widget, x, y):
            return
        widget.grab_focus()
        self._selection_model.unselect_all()
        self._selection_anchor = None
        self._pending_grid_collapse = None

    def _build_context_menu(self, selected_entries: List[FileEntry]) -> Gio.Menu:
        """The context menu model, laid out like Nautilus's.

        Labels only, grouped into sections; items activate the "pane."
        actions, and the accel attribute only displays the pane's own
        shortcut next to the item.
        """
        menu = Gio.Menu()

        def _section(*items: Optional[Gio.MenuItem]) -> None:
            section = Gio.Menu()
            for item in items:
                if item is not None:
                    section.append_item(item)
            if section.get_n_items():
                menu.append_section(None, section)

        def _item(label: str, action_name: str, accel: Optional[str] = None) -> Gio.MenuItem:
            item = Gio.MenuItem.new(label, f"pane.{action_name}")
            if accel:
                item.set_attribute_value("accel", GLib.Variant.new_string(accel))
            return item

        single = selected_entries[0] if len(selected_entries) == 1 else None
        paste = (
            _item(_("Paste"), "paste", "<Primary>v")
            if getattr(self, "_can_paste", False) else None
        )
        across = self._partner_visible()
        local = not self._is_remote

        if not selected_entries:
            _section(
                _item(_("New Folder…"), "new_folder", "<Shift><Primary>n"),
                _item(_("New File…"), "new_file"),
            )
            _section(
                _item(_("Open in Terminal"), "open_terminal") if local else None,
                _item(_("Open in Default File Manager"), "open_in_files") if local else None,
            )
            _section(
                paste,
                _item(_("Select All"), "select_all", "<Primary>a")
                if getattr(self, "_entries", None) else None,
            )
        else:
            _section(
                _item(_("Open"), "open", "Return") if single is not None else None,
                _item(_("Open With…"), "open_with")
                if single is not None and not single.is_dir and local else None,
            )
            _section(
                _item(_("Copy to Other Pane"), "copy_across", "F6") if across else None,
                _item(_("Move to Other Pane"), "move_across", "<Shift>F6") if across else None,
            )
            _section(
                _item(_("Cut"), "cut", "<Primary>x"),
                _item(_("Copy"), "copy", "<Primary>c"),
                paste,
            )
            # Rename is single-item only (no batch rename yet); Nautilus
            # hides it when the rename action is disabled for the selection.
            _section(
                _item(_("Rename…"), "rename", "F2") if single is not None else None,
                _item(_("Move to Trash"), "delete", "Delete") if self.backend.can_trash else None,
                _item(
                    _("Delete Permanently…") if self.backend.can_trash else _("Delete…"),
                    "delete_permanently",
                    "<Shift>Delete",
                ),
            )

        _section(_item(_("Copy Location"), "copy_location"))
        _section(_item(_("Properties"), "properties", "<Primary>i"))
        return menu

    def _partner_visible(self) -> bool:
        partner = self._partner_pane
        return partner is not None and partner.get_visible() and partner.get_mapped()

    def _show_context_menu(
        self, widget: Gtk.Widget, x: float, y: float, *, background: bool = False
    ) -> None:
        if getattr(self, '_suppress_next_context_menu', False):
            self._suppress_next_context_menu = False
            return
        if self._stack.get_visible_child_name() not in ("list", "grid"):
            return
        # Close the previous menu first, so this one owns the menu state.
        if self._menu_popover is not None:
            self._menu_popover.popdown()
        # Item gestures select the clicked item first; a background menu acts
        # on the current folder and leaves the selection untouched.
        self._menu_for_background = background
        self._update_menu_state()
        widget.grab_focus()

        if background or not self._entries:
            selected_entries: List[FileEntry] = []
        else:
            selected_entries = self.get_selected_entries()
        # As Nautilus does, a fresh popover per menu avoids showing the old
        # model while the new one loads; it opens to the right of the
        # pointer, without an arrow, and autohides on a click elsewhere.
        popover = Gtk.PopoverMenu.new_from_model(
            self._build_context_menu(selected_entries)
        )
        popover.set_has_arrow(False)
        popover.set_halign(Gtk.Align.START)
        popover.connect("closed", self._on_menu_popover_closed)
        # Parent to the pane, not the list or grid, as Nautilus parents its
        # menu to the files view: inside the view's scrolled window GTK
        # caps the menu's height and the last items scroll out of sight.
        popover.set_parent(self)
        translated = widget.translate_coordinates(self, x, y)
        if translated is not None and translated[0] is not None:
            x, y = translated
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        self._menu_popover = popover
        popover.popup()

    def _on_menu_popover_closed(self, popover: Gtk.PopoverMenu) -> None:
        # "closed" fires before the clicked item's action runs, so the
        # cleanup waits until idle: the action still needs the background
        # flag, and an unparented popover no longer finds the "pane." actions.
        if self._menu_popover is popover:
            self._menu_popover = None
            # Nautilus hands focus back to the view when its menu closes.
            view = self._visible_view()
            if view is not None:
                view.grab_focus()

        def _cleanup() -> bool:
            # A newer menu may have opened meanwhile; it owns the flag.
            if self._menu_popover is None:
                self._menu_for_background = False
            if popover.get_parent() is not None:
                popover.unparent()
            return False

        GLib.idle_add(_cleanup)

    # CSS node names of the widgets GtkColumnView/GtkGridView wrap each item
    # (or header) in; anything else inside the view is background.
    _ITEM_CSS_NAMES = frozenset({"row", "cell", "child", "header"})

    def _is_click_on_empty_space(self, widget: Gtk.Widget, x: float, y: float) -> bool:
        """Check if the click is on empty space (not on an item)."""
        visible_child = self._stack.get_visible_child()
        if visible_child is None:
            return True

        if isinstance(visible_child, Gtk.ScrolledWindow):
            view_widget = visible_child.get_child()
        else:
            view_widget = visible_child

        if view_widget is None:
            return True

        try:
            widget_x, widget_y = widget.translate_coordinates(view_widget, x, y)
            if widget_x is None or widget_y is None:
                return True

            picked = view_widget.pick(widget_x, widget_y, Gtk.PickFlags.DEFAULT)
            if picked is None:
                return True

            if view_widget not in (self._list_view, self._grid_view):
                return True

            current = picked
            while current is not None and current != view_widget:
                if hasattr(current, "drag_position") or hasattr(current, "_pane_entry"):
                    return False
                if current.get_css_name() in self._ITEM_CSS_NAMES:
                    return False
                current = current.get_parent()
            return True
        except Exception as e:
            # Unknown target: treat it as an item so a click never wipes the
            # selection by accident.
            logger.debug("Error checking if click is on empty space: %s", e)
            return False

    def _get_selected_indices(self) -> List[int]:
        indices: List[int] = []
        total = len(self._entries)
        for index in range(total):
            if self._selection_model.is_selected(index):
                indices.append(index)
        return indices

    def _get_primary_selection_index(self) -> Optional[int]:
        indices = self._get_selected_indices()
        return indices[0] if indices else None

    def get_selected_entries(self) -> List[FileEntry]:
        return [self._entries[index] for index in self._get_selected_indices()]

    def _update_menu_state(self) -> None:
        selected_entries = self.get_selected_entries()
        selection_count = len(selected_entries)
        has_selection = selection_count > 0
        single_selection = selection_count == 1
        can_paste = bool(getattr(self, "_can_paste", False))
        across = self._partner_visible() if hasattr(self, "_menu_actions") else False

        def _set_enabled(name: str, enabled: bool) -> None:
            action = self._menu_actions.get(name)
            if action is not None:
                action.set_enabled(enabled)

        def _set_button(name: str, enabled: bool) -> None:
            button = self._action_buttons.get(name)
            if button is not None:
                button.set_sensitive(enabled)

        _set_enabled("copy_across", has_selection and across)
        _set_enabled("move_across", has_selection and across)
        _set_enabled("copy", has_selection)
        _set_enabled("cut", has_selection)
        _set_enabled("paste", can_paste)
        _set_enabled("rename", single_selection)
        _set_enabled("delete", has_selection)
        _set_enabled("delete_permanently", has_selection)
        # Properties works for the current folder, a single item, or a multi
        # selection (Nautilus-style aggregated dialog).
        _set_enabled("properties", bool(self._current_path))

        _set_button("copy_across", has_selection)
        _set_button("move_across", has_selection)
        _set_button("copy", has_selection)
        _set_button("cut", has_selection)
        _set_button("paste", can_paste)
        _set_button("rename", single_selection)
        _set_button("delete", has_selection)
        delete_button = self._action_buttons.get("delete")
        if delete_button is not None and hasattr(self, "backend"):
            delete_button.set_tooltip_text(
                _("Move to Trash") if self.backend.can_trash else _("Delete")
            )
        self._update_status_text(selected_entries)

    def _update_status_text(self, selected: Optional[List[FileEntry]] = None) -> None:
        label = getattr(self, "_status_label", None)
        if label is None:
            return
        if selected is None:
            selected = self.get_selected_entries()
        if selected:
            files = [entry for entry in selected if not entry.is_dir]
            if len(selected) == 1 and files:
                text = _("“{name}” selected ({size})").format(
                    name=safe_display_text(selected[0].name), size=format_size(files[0].size)
                )
            elif len(selected) == 1:
                text = _("“{name}” selected").format(name=safe_display_text(selected[0].name))
            else:
                text = ngettext(
                    "{count} item selected", "{count} items selected", len(selected)
                ).format(count=len(selected))
                if files:
                    text += " (" + format_size(sum(entry.size for entry in files)) + ")"
        elif self._current_path and self._stack.get_visible_child_name() in ("list", "grid"):
            text = _item_count_text(len(self._entries))
        else:
            text = ""
        label.set_text(text)

    def _emit_entry_operation(self, action: str) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast(_("Select at least one item first"))
            return
        if action in ("rename", "open-with") and len(entries) != 1:
            self.show_toast(_("Select a single item"))
            return
        payload = {"entries": entries, "directory": self._current_path}
        self.emit("request-operation", action, payload)

    def _emit_paste_operation(self, *, force_move: bool = False) -> None:
        payload = {"directory": self._current_path}
        if force_move:
            payload["force_move"] = True
        self.emit("request-operation", "paste", payload)

    def _shortcut_operation(self, action: str, *, force_move: bool = False) -> bool:
        if action in {"copy", "cut", "rename", "delete", "copy-across", "move-across"}:
            if action.endswith("-across") and not self._partner_visible():
                return False
            self._emit_entry_operation(action)
            return True
        if action == "paste":
            self._emit_paste_operation(force_move=force_move)
            return True
        return False

    def set_can_paste(self, can_paste: bool) -> None:
        current = bool(getattr(self, "_can_paste", False))
        if current == can_paste:
            return
        self._can_paste = can_paste
        self._update_menu_state()

    def set_show_hidden(self, show_hidden: bool, *, preserve_selection: bool = True) -> None:
        """Update the hidden file visibility state and refresh entries."""

        self.toolbar.set_show_hidden_state(show_hidden)
        if self._show_hidden == show_hidden:
            return

        self._show_hidden = show_hidden
        get_settings().set("show_hidden", show_hidden)
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_menu_open(self) -> None:
        selected = self._get_selected_indices()
        if len(selected) == 1:
            self._navigate_to_entry(selected[0])

    def _selection_locations(self) -> List[str]:
        """Full locations of the selected items, or of the folder if none."""
        base = self._current_path or "/"
        if getattr(self, "_menu_for_background", False):
            entries: List[FileEntry] = []
        else:
            entries = self.get_selected_entries()
        paths = [base] if not entries else [self.backend.join(base, entry.name) for entry in entries]
        return [self.backend.location_uri(path) for path in paths]

    def _on_menu_copy_location(self) -> None:
        text = "\n".join(safe_display_text(path) for path in self._selection_locations())
        self.get_clipboard().set(text)
        self.show_toast(_("Location copied"))

    def _on_menu_open_terminal(self) -> None:
        self.emit("request-operation", "open-terminal", {"path": self._current_path or "/"})

    def _on_menu_open_in_files(self) -> None:
        self.emit("request-operation", "open-in-files", {"path": self._current_path or "/"})

    def _on_menu_select_all(self) -> None:
        self._selection_model.select_all()

    def get_selected_entry(self) -> Optional[FileEntry]:
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            return None
        return selected_entries[0]

    def set_view(self, view: "FileManagerView") -> None:
        """Associate this pane with the view that does its filesystem work."""

        self._view = view

    def _set_current_pathbar_text(self, path: str) -> None:
        """Set the path bar text with the home folder shortened to ``~``."""
        display_path = path
        if not self._is_remote:
            home = os.path.expanduser("~")
            if path == home or path.startswith(home + os.sep):
                display_path = "~" + path[len(home):]
        entry = self.toolbar.path_entry
        entry.set_text(safe_display_text(display_path))
        # Long paths show their end, where the current folder is.
        entry.set_position(-1)

    def _on_menu_properties(self) -> None:
        if getattr(self, "_menu_for_background", False):
            entries: List[FileEntry] = []
        else:
            entries = self.get_selected_entries()
        self.emit(
            "request-operation",
            "properties",
            {"entries": entries, "directory": self._current_path},
        )

    # -- public API -----------------------------------------------------

    def show_load_error(self, path: str, message: str) -> None:
        """Swap the file view for an error message + Retry pill after a
        failed directory load."""
        self._load_error_path = path
        self._load_error_label.set_text(
            safe_display_text(message) or _("Failed to load directory")
        )
        current = self._stack.get_visible_child_name()
        if current not in ("load-error", "connecting"):
            self._pre_error_view = current
        self._stack.set_visible_child_name("load-error")
        self._update_status_text()

    def show_connecting(self, message: str, *, cancellable: bool = True) -> None:
        """Show a spinner + status message while connecting."""
        self._connecting_label.set_text(
            safe_display_text(message) or _("Connecting…")
        )
        self._cancel_connect_button.set_visible(cancellable)
        current = self._stack.get_visible_child_name()
        if current not in ("load-error", "connecting"):
            self._pre_error_view = current
        self._stack.set_visible_child_name("connecting")
        self._update_status_text()

    def set_connecting_status(self, message: str) -> None:
        """Update the status line of the connecting view (if showing)."""
        if self._stack.get_visible_child_name() == "connecting":
            self._connecting_label.set_text(safe_display_text(message))

    def _clear_load_error(self) -> None:
        if self._stack.get_visible_child_name() in ("load-error", "connecting"):
            self._stack.set_visible_child_name(self._pre_error_view or self.toolbar._current_view)
        self._load_error_path = None

    def _on_error_home_clicked(self, _btn) -> None:
        if self._is_remote:
            # The server may be unreachable; go back to this computer.
            self.emit("request-operation", "cancel-connect", None)
        else:
            self.emit("path-changed", "~")

    def _on_retry_load_clicked(self, _btn) -> None:
        path = self._load_error_path or self._current_path or "~"
        self.emit("path-changed", path)

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        self._clear_load_error()
        entries_list = list(entries)
        held = getattr(self, "_held_removals", {}).get(path)
        if held:
            # A listing requested before a delete finished still names the
            # entries being deleted; keep them hidden until it completes.
            entries_list = [entry for entry in entries_list if entry.name not in held]

        previous_path = self._current_path
        self._current_path = path
        self._set_current_pathbar_text(path)
        self._cached_entries = entries_list
        # Like Nautilus: a reload keeps the selection, and moving up to an
        # ancestor selects the folder the user just came out of.
        reloading = _same_path(previous_path, path)
        self._apply_entry_filter(preserve_selection=reloading)
        if not reloading:
            child = _child_toward(path, previous_path)
            if child:
                self.highlight_entry(child)
            elif self._entries:
                self._scroll_to_position(0)
            self.emit("directory-shown", path)

    def remove_cached_entries(self, names: Iterable[str], *, hold: bool = False) -> int:
        """Drop entries from the visible listing without a reload.

        Used for optimistic delete: items disappear as soon as the user
        confirms, while the backend delete runs. Returns how many cached
        entries were removed. Callers should refresh on cancel/failure so
        surviving items come back.

        With ``hold``, listings of this directory keep hiding the names until
        :meth:`release_removed_entries`, so an asynchronous listing requested
        before the delete cannot bring the rows back while it runs.
        """
        name_set = {name for name in names if name}
        if hold and name_set and self._current_path:
            self._held_removals.setdefault(self._current_path, set()).update(name_set)
        if not name_set or not self._cached_entries:
            return 0
        before = len(self._cached_entries)
        self._cached_entries = [
            entry for entry in self._cached_entries if entry.name not in name_set
        ]
        removed = before - len(self._cached_entries)
        if removed:
            self._apply_entry_filter(preserve_selection=True)
        return removed

    def release_removed_entries(self, names: Iterable[str], path: Optional[str]) -> None:
        """Stop hiding *names* that :meth:`remove_cached_entries` held while
        *path* was the current directory. Other directories keep their holds,
        even for the same names (``README.md`` in two folders)."""
        held = self._held_removals
        if path not in held:
            return
        remaining = held[path] - set(names)
        if remaining:
            held[path] = remaining
        else:
            del held[path]

    def highlight_entry(self, name: str) -> None:
        if not name:
            return
        match: Optional[int] = None
        for index, entry in enumerate(self._entries):
            if entry.name == name:
                match = index
                break
        if match is None:
            return
        self._selection_model.unselect_all()
        self._selection_anchor = None
        self._selection_model.select_item(match, False)
        self._selection_anchor = match
        self._scroll_to_position(match)

    def select_names(self, names: Iterable[str]) -> None:
        """Select the entries called *names* (e.g. just pasted ones)."""
        wanted = set(names)
        first: Optional[int] = None
        self._selection_model.unselect_all()
        for index, entry in enumerate(self._entries):
            if entry.name in wanted:
                self._selection_model.select_item(index, False)
                if first is None:
                    first = index
        self._selection_anchor = first
        if first is not None:
            self._scroll_to_position(first)

    def _apply_entry_filter(self, *, preserve_selection: bool) -> None:
        selected_names: set[str] = set()
        if preserve_selection:
            for entry in self.get_selected_entries():
                selected_names.add(entry.name)

        # Filter for hidden files and store as raw entries
        self._raw_entries = [
            entry
            for entry in self._cached_entries
            if self._show_hidden or not entry.name.startswith(".")
        ]

        # Apply sorting to get final entries
        sorted_entries = self._sort_entries(self._raw_entries)

        # Build the row objects first. Names are sanitised on the way in
        # because GTK cannot encode the lone surrogates os.scandir() hands back
        # for local files whose bytes are not valid UTF-8. A row GTK still
        # refuses is dropped from both the store and ``self._entries`` so the
        # two stay index-aligned -- one bad entry must never blank the whole
        # pane.
        row_objects: List[Gtk.StringObject] = []
        shown_entries: List[FileEntry] = []
        restored_selection: List[int] = []
        for entry in sorted_entries:
            suffix = "/" if entry.is_dir else ""
            try:
                row_objects.append(
                    Gtk.StringObject.new(safe_display_text(entry.name) + suffix)
                )
            except Exception as exc:
                logger.debug("Skipping undisplayable entry %r: %s", entry.name, exc)
                continue
            if preserve_selection and entry.name in selected_names:
                restored_selection.append(len(shown_entries))
            shown_entries.append(entry)

        # ``self._entries`` must be current *before* the store changes: GTK
        # binds the new rows synchronously from inside the store mutation and
        # the cell factories resolve each row's metadata through
        # ``self._entries[position]``. Publishing the store first would bind the
        # new listing against the previous directory's entries.
        self._entries = shown_entries
        # One splice instead of remove_all() + N appends, so the view sees a
        # single items-changed for the whole listing.
        self._list_store.splice(0, self._list_store.get_n_items(), row_objects)

        self._selection_model.unselect_all()
        self._selection_anchor = None
        for index in restored_selection:
            self._selection_model.select_item(index, False)
        if restored_selection:
            self._selection_anchor = restored_selection[-1]

        self._update_menu_state()
        self._update_empty_state()

    def _update_empty_state(self) -> None:
        empty = (
            bool(self._current_path)
            and not self._entries
            and self._stack.get_visible_child_name() in ("list", "grid")
        )
        if empty:
            hidden = len(self._cached_entries) - len(self._raw_entries)
            self._empty_page.set_description(
                ngettext(
                    "{count} hidden item", "{count} hidden items", hidden
                ).format(count=hidden)
                if hidden else None
            )
        self._empty_page.set_visible(empty)

    # -- navigation helpers --------------------------------------------

    def _navigate_to_entry(self, position: Optional[int]) -> None:
        if position is None or not (0 <= position < len(self._entries)):
            return

        try:
            entry = self._entries[position]
        except IndexError:
            return

        target_path = self.backend.join(self._current_path or "/", entry.name)
        if entry.is_dir:
            self.emit("path-changed", target_path)
        else:
            self.emit(
                "request-operation",
                "open",
                {"entries": [entry], "directory": self._current_path},
            )

    def _on_list_activate(self, _list_view, position: int) -> None:
        self._navigate_to_entry(position)

    def _sort_entries(self, entries: Iterable[FileEntry]) -> List[FileEntry]:
        # Ties on size, date or type fall back to the name, as in Nautilus.
        def key_func(item: FileEntry):
            name_key = _natural_name_key(item.name)
            if self._sort_key == "size":
                size = item.item_count or 0 if item.is_dir else item.size
                return size, name_key
            if self._sort_key == "modified":
                return item.modified, name_key
            if self._sort_key == "type":
                return _type_sort_key(item.name), name_key
            return name_key

        dirs = [entry for entry in entries if entry.is_dir]
        files = [entry for entry in entries if not entry.is_dir]

        dirs_sorted = sorted(dirs, key=key_func, reverse=self._sort_descending)
        files_sorted = sorted(files, key=key_func, reverse=self._sort_descending)
        return dirs_sorted + files_sorted

    def _refresh_sorted_entries(self, *, preserve_selection: bool) -> None:
        # Simply re-apply the filter which now includes sorting
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_grid_activate(self, _grid_view: Gtk.GridView, position: int) -> None:
        self._navigate_to_entry(position)

    def _entries_for_drag_at_position(self, position: int) -> List[FileEntry]:
        """Return entries included in a drag starting at *position*."""
        if not (0 <= position < len(self._entries)):
            return []
        entry = self._entries[position]
        selected_indices = self._get_selected_indices()
        if position in selected_indices and len(selected_indices) > 1:
            return [self._entries[index] for index in selected_indices]
        return [entry]

    def _on_drag_prepare(self, drag_source: Gtk.DragSource, x: float, y: float) -> Optional[Gdk.ContentProvider]:
        """Prepare drag data when drag operation starts."""
        widget = drag_source.get_widget()
        position = getattr(widget, 'drag_position', None)
        if position is None or not (0 <= position < len(self._entries)):
            return None

        drag_entries = self._entries_for_drag_at_position(position)
        # A JSON payload keeps arbitrary characters in names intact and
        # tells the drop which pane (and folder) the items came from.
        payload = {
            "format": _DRAG_FORMAT,
            "pane_id": id(self),
            "path": self._current_path,
            "names": [entry.name for entry in drag_entries],
        }
        providers = [
            Gdk.ContentProvider.new_for_value(
                json.dumps(payload, separators=(",", ":"), sort_keys=True)
            )
        ]
        if not self._is_remote:
            # Other apps (Nautilus, editors, browsers) take local files.
            files = [Gio.File.new_for_path(self.path_for(entry)) for entry in drag_entries]
            providers.append(
                Gdk.ContentProvider.new_for_value(Gdk.FileList.new_from_list(files))
            )
        return Gdk.ContentProvider.new_union(providers)

    def _on_drag_begin(self, drag_source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        """Called when drag operation begins - set drag icon."""
        # Always set a custom icon. On macOS, string ContentProviders are
        # otherwise previewed as the JSON payload text beside the cursor.
        widget = drag_source.get_widget()
        if widget:
            paintable = Gtk.WidgetPaintable.new(widget)
            drag_source.set_icon(paintable, 0, 0)

    @staticmethod
    def _drop_modifiers(drop_target: Optional[Gtk.DropTarget]) -> Tuple[bool, bool]:
        """(copy, move) modifiers held during a drop: Ctrl copies, Shift moves."""
        if drop_target is None:
            return False, False
        try:
            event = drop_target.get_current_event()
            state = (
                event.get_modifier_state()
                if event is not None
                else Gdk.ModifierType(0)
            )
        except Exception:
            return False, False
        primary_mask = Gdk.ModifierType.CONTROL_MASK
        if _is_macos():
            primary_mask |= Gdk.ModifierType.META_MASK | Gdk.ModifierType.SUPER_MASK
        return bool(state & primary_mask), bool(state & Gdk.ModifierType.SHIFT_MASK)

    def _drop_destination(self, x: float, y: float) -> Optional[str]:
        """The folder a drop at (x, y) goes into: a folder row, else the pane's."""
        if not self._current_path:
            return None
        target_folder = self._resolve_drop_target_folder(x, y)
        if target_folder is not None:
            return self.backend.join(self._current_path, target_folder.name)
        return self._current_path

    def _on_drop(self, drop_target: Gtk.DropTarget, value, x: float, y: float) -> bool:
        """Handle a drop from this app or from another one."""
        self.remove_css_class("drop-target-active")
        destination = self._drop_destination(x, y)
        window = self._view
        if destination is None or window is None:
            return False
        force_copy, force_move = self._drop_modifiers(drop_target)

        if isinstance(value, Gdk.FileList):
            paths = [gfile.get_path() for gfile in value.get_files() if gfile.get_path()]
            if not paths:
                return False
            window.drop_local_files(paths, self, destination, move=force_move)
            return True

        if not isinstance(value, str):
            return False
        try:
            payload = json.loads(value)
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or payload.get("format") != _DRAG_FORMAT:
            paths = _local_paths_from_uri_list(value)
            if not paths:
                return False
            window.drop_local_files(paths, self, destination, move=force_move)
            return True

        try:
            source_pane = _PANES.get(int(payload.get("pane_id")))
        except (TypeError, ValueError):
            source_pane = None
        names = payload.get("names")
        if source_pane is None or not isinstance(names, list):
            return False
        if payload.get("path") != source_pane._current_path:
            self.show_toast(_("Dragged item is no longer available"))
            return False
        by_name = {entry.name: entry for entry in source_pane._cached_entries}
        entries = [by_name[name] for name in names if isinstance(name, str) and name in by_name]
        if len(entries) != len(names):
            self.show_toast(_("Dragged item is no longer available"))
            return False
        if source_pane is self and destination == self._current_path:
            return False  # dropped back where it came from

        # Like Nautilus: within one filesystem a drop moves, between two it
        # copies; Ctrl forces a copy and Shift a move.
        same_fs = source_pane.backend.key == self.backend.key
        move = (same_fs and not force_copy) or force_move
        window.transfer_entries(source_pane, entries, self, destination, move=move)
        return True

    def _on_drop_enter(self, drop_target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        self.add_css_class("drop-target-active")
        return Gdk.DragAction.COPY

    def _on_drop_motion(self, drop_target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        force_copy, force_move = self._drop_modifiers(drop_target)
        if force_move:
            return Gdk.DragAction.MOVE
        if force_copy:
            return Gdk.DragAction.COPY
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, drop_target: Gtk.DropTarget) -> None:
        self.remove_css_class("drop-target-active")

    def _resolve_drop_target_folder(self, x: float, y: float) -> Optional[FileEntry]:
        """Hit-test (x, y) and return the folder entry under the cursor, if any.

        Returns ``None`` when the cursor isn't over a directory row — callers
        then drop into the pane's current path. Walks up from the picked
        leaf widget looking for the per-row ``drag_position`` attribute set
        by the bind callbacks; that gives an index into ``self._entries``.
        """
        try:
            picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        except Exception:
            return None
        widget = picked
        entries = getattr(self, "_entries", None) or []
        while widget is not None and widget is not self:
            position = getattr(widget, "drag_position", None)
            if isinstance(position, int) and 0 <= position < len(entries):
                entry = entries[position]
                # Dropped on a file: fall back to the current path rather than
                # a nonsensical "copy into a file".
                return entry if entry.is_dir else None
            widget = widget.get_parent()
        return None

    def _on_up_clicked(self, _button) -> None:
        if not self._current_path:
            return
        parent = self.backend.dirname(self._current_path)
        # Avoid navigating past root repeatedly
        if parent != self._current_path:
            self.emit("path-changed", parent)

    def _on_back_clicked(self, _button) -> None:
        prev = self.pop_history()
        if prev:
            # Suppress history push for back navigation
            self._suppress_history_push = True
            self.emit("path-changed", prev)

    def _on_forward_clicked(self, _button) -> None:
        if not self._forward_history:
            return
        target = self._forward_history.pop()
        # Re-enter the target into the back history here rather than on
        # arrival, where a push would clear the rest of the forward stack.
        self._history.append(target)
        self._suppress_history_push = True
        self._update_history_buttons()
        self.emit("path-changed", target)

    def _on_refresh_clicked(self, _button) -> None:
        # Refresh the current directory
        self.emit("path-changed", self._current_path or "~")

    def push_history(self, path: str) -> None:
        if self._history and self._history[-1] == path:
            return
        self._history.append(path)
        # A new location ends the forward trail, as in a browser or Nautilus.
        self._forward_history.clear()
        self._update_history_buttons()

    def pop_history(self) -> Optional[str]:
        if len(self._history) > 1:
            self._forward_history.append(self._history.pop())
            self._update_history_buttons()
            return self._history[-1]
        return None

    def clear_history(self) -> None:
        self._history.clear()
        self._forward_history.clear()
        self._update_history_buttons()

    def _update_history_buttons(self) -> None:
        controls = getattr(getattr(self, "toolbar", None), "controls", None)
        if controls is None:
            return
        controls.back_button.set_sensitive(len(self._history) > 1)
        controls.forward_button.set_sensitive(bool(self._forward_history))

    def show_toast(self, text: str, timeout: int = -1) -> None:
        """Show a toast; messages too long for a toast escalate to an alert."""
        # Messages routinely embed a filename or path, which on the local side
        # may not be valid UTF-8; GTK refuses to encode those.
        text = safe_display_text(text)
        if toast_overflows(text):
            present_error_alert(self, text)
            return
        if self._current_toast:
            self._current_toast.dismiss()
            self._current_toast = None

        toast = Adw.Toast.new(text)
        if timeout >= 0:
            toast.set_timeout(timeout)
        self._overlay.add_toast(toast)
        self._current_toast = toast  # Keep reference for dismissal

    def dismiss_toasts(self) -> None:
        """Dismiss the current toast, if any."""
        if self._current_toast:
            self._current_toast.dismiss()
            self._current_toast = None

    # -- type-ahead search ----------------------------------------------

    def _current_time(self) -> float:
        return GLib.get_monotonic_time() / 1_000_000

    def _find_prefix_match(self, prefix: str, start_index: int) -> Optional[int]:
        if not prefix or not self._entries:
            return None

        total = len(self._entries)
        start = 0 if start_index is None else max(0, start_index)

        prefix_casefold = prefix.casefold()
        for offset in range(total):
            index = (start + offset) % total
            if self._entries[index].name.casefold().startswith(prefix_casefold):
                return index
        return None

    def _scroll_to_position(self, position: int) -> None:
        visible = self._stack.get_visible_child_name()
        if visible == "list":
            # Gtk.ColumnView: scroll_to(pos, column, flags, scroll)
            self._list_view.scroll_to(position, None, Gtk.ListScrollFlags.FOCUS, None)
        elif visible == "grid":
            self._grid_view.scroll_to(position, Gtk.ListScrollFlags.FOCUS, None)

    def _on_typeahead_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if not self._entries:
            return False

        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SUPER_MASK
        ):
            return False

        char_code = Gdk.keyval_to_unicode(keyval)
        if not char_code:
            return False

        char = chr(char_code)
        if not char or not char.isprintable():
            return False

        now = self._current_time()
        if now - self._typeahead_last_time > self._TYPEAHEAD_TIMEOUT:
            self._typeahead_buffer = ""

        self._typeahead_last_time = now

        repeat_cycle = (
            bool(self._typeahead_buffer)
            and len(self._typeahead_buffer) == 1
            and char.casefold() == self._typeahead_buffer.casefold()
        )

        selected = self._get_primary_selection_index()
        selected_index = 0 if selected is None or selected < 0 else selected

        start_index = selected_index
        match: Optional[int] = None
        prefix: Optional[str] = None

        if repeat_cycle:
            candidate = self._typeahead_buffer + char
            match = self._find_prefix_match(candidate, start_index)
            if match is not None:
                self._typeahead_buffer = candidate
            else:
                start_index += 1
                prefix = self._typeahead_buffer
        else:
            self._typeahead_buffer += char
            prefix = self._typeahead_buffer

        if match is None and prefix is not None:
            match = self._find_prefix_match(prefix, start_index)

        if match is None and not repeat_cycle:
            self._typeahead_buffer = char
            match = self._find_prefix_match(self._typeahead_buffer, selected_index)

        if match is None:
            return False

        self._selection_model.select_item(match, True)
        self._selection_anchor = match
        self._anchor_follows_focus = False

        self._scroll_to_position(match)
        return True


_ITALIC = Pango.AttrList()
_ITALIC.insert(Pango.attr_style_new(Pango.Style.ITALIC))
