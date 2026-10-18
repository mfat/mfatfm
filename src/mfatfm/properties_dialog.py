"""Nautilus-style properties dialog for local and remote entries."""

from __future__ import annotations

import logging
import os
from gettext import gettext as _
from gettext import ngettext
from typing import Dict, List, Optional, Sequence

from gi.repository import Adw, Gio, Gtk

from . import permissions as perms
from .backends import Backend
from .common import EntryStat, FileEntry
from .format_utils import (
    _human_size,
    _human_time,
    _item_count_text,
    _mode_to_octal,
    _mode_to_str,
    safe_display_text,
)
from .icons import icon_for_name
from .tasks import run_async

logger = logging.getLogger(__name__)

# Nautilus lists individual names up to this count, then falls back to "N Selected Items".
_MAX_LISTED_NAMES = 5


def _folder_calculating_text(item_count: Optional[int]) -> str:
    """Placeholder while a folder's deep size is still being measured.

    Nautilus shows a spinner beside a partial count; we approximate with an
    ellipsis until the final ``items, totalling size`` line is ready.
    """
    if not item_count:
        return "…"
    return _item_count_text(item_count) + "…"


def _folder_size_text(item_count: Optional[int], total_size: int) -> str:
    """Nautilus-style contents line for a folder (or deep-counted selection)."""
    if total_size < 0:
        if item_count is None:
            return _("Size unavailable")
        if item_count == 0:
            return _("Empty folder")
        return _("{items} (size unavailable)").format(
            items=_item_count_text(item_count)
        )
    if item_count is not None and item_count == 0 and total_size == 0:
        return _("Empty folder")
    if item_count is None:
        return _human_size(total_size)
    return _selection_size_text(item_count, total_size)


def _free_space_text(size: int) -> str:
    # Nautilus: "%s Free" (e.g. "100 MB Free")
    return _("{size} Free").format(size=_human_size(size))


def _selection_title(entries: Sequence[FileEntry]) -> str:
    """Nautilus-style header name for one or more selected entries."""
    count = len(entries)
    if count > _MAX_LISTED_NAMES:
        return ngettext(
            "{count} Selected Item",
            "{count} Selected Items",
            count,
        ).format(count=count)
    if count > 1:
        return ", ".join(safe_display_text(entry.name) for entry in entries)
    return safe_display_text(entries[0].name)


def _selection_size_text(item_count: int, total_size: int) -> str:
    """Nautilus-style size line for a multi-item / deep-counted selection."""
    if total_size < 0:
        return _("Size unavailable")
    size = _human_size(total_size)
    return ngettext(
        "{count} item, with size {size}",
        "{count} items, totalling {size}",
        item_count,
    ).format(count=item_count, size=size)


def _common_value(values: Sequence[Optional[str]]) -> Optional[str]:
    """Return the shared value when every entry agrees, else ``None``."""
    present = [value for value in values if value is not None]
    if not present:
        return None
    first = present[0]
    if all(value == first for value in present[1:]):
        return first
    return None


class PropertiesDialog(Adw.Dialog):
    """Nautilus-style properties dialog using card-based design."""

    def __init__(
        self,
        entries: Sequence[FileEntry],
        directory: str,
        backend: Backend,
    ) -> None:
        super().__init__()
        entries = list(entries)
        if not entries:
            raise ValueError("PropertiesDialog requires at least one entry")
        self._entries = entries
        self._entry = entries[0]
        self._directory = directory
        self._backend = backend
        self._paths = [backend.join(directory, entry.name) for entry in entries]
        # Per-entry metadata, filled by the async stat.
        self._stats: List[Optional[EntryStat]] = [None] * len(entries)
        self._initial_modes: List[Optional[int]] = [None] * len(entries)
        self._user_names: Dict[int, str] = {}
        self._group_names: Dict[int, str] = {}
        self._owner_name_rows: Dict[str, Adw.ActionRow] = {}
        self._access_rows: list[tuple[Adw.ComboRow, perms.PermissionClass, bool, list[int]]] = []
        self._permissions_page: Optional[Adw.NavigationPage] = None
        self._syncing_permission_widgets = False
        self._closed = False
        self.connect("closed", self._on_closed)

        self.set_title(_("Properties"))
        self.set_content_width(420)
        self.set_content_height(560)
        self.navigation_view = Adw.NavigationView()
        self.set_child(self.navigation_view)
        self._build_main_page()
        self._start_metadata_fetch()

    def _on_closed(self, *_args) -> None:
        self._closed = True

    @property
    def _is_multi(self) -> bool:
        return len(self._entries) > 1

    @property
    def _all_folders(self) -> bool:
        return all(entry.is_dir for entry in self._entries)

    def _build_main_page(self) -> None:
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(12)
        content.set_margin_bottom(24)
        content.set_margin_start(18)
        content.set_margin_end(18)

        content.append(self._create_header_block())
        # Rows are grouped in boxed lists like Nautilus's preference groups.
        content.append(self._row_list(self._create_parent_folder_row()))
        content.append(self._row_list(*self._create_date_rows()))
        content.append(self._row_list(self._create_owner_row()))
        content.append(
            self._row_list(self._create_permissions_row(), self._create_execution_row())
        )

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        clamp = Adw.Clamp(maximum_size=520)
        clamp.set_child(content)
        scrolled.set_child(clamp)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(scrolled)
        self.navigation_view.add(
            Adw.NavigationPage(title=_("Properties"), tag="main", child=toolbar)
        )

    @staticmethod
    def _row_list(*rows: Gtk.Widget) -> Gtk.ListBox:
        box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        box.add_css_class("boxed-list")
        for row in rows:
            box.append(row)
        return box

    # -- header ------------------------------------------------------------

    def _create_header_block(self) -> Gtk.Widget:
        """Icon, name, size/contents and, for folders, free space."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, halign=Gtk.Align.CENTER)

        entries = self._entries
        if len(entries) == 1:
            gicon = icon_for_name(entries[0].name, entries[0].is_dir)
        elif self._all_folders:
            gicon = icon_for_name("", True)
        else:
            gicon = icon_for_name("", False)
        icon = Gtk.Image.new_from_gicon(gicon)
        icon.set_pixel_size(64)
        icon.add_css_class("icon-dropshadow")
        box.append(icon)

        # GTK cannot encode the lone surrogates a filename whose bytes are
        # not valid UTF-8 arrives with.
        name_label = Gtk.Label(label=_selection_title(entries))
        name_label.add_css_class("title-3")
        name_label.set_wrap(True)
        name_label.set_justify(Gtk.Justification.CENTER)
        box.append(name_label)

        self._size_label = Gtk.Label(label=self._initial_size_text())
        self._size_label.add_css_class("caption")
        self._size_label.set_wrap(True)
        self._size_label.set_justify(Gtk.Justification.CENTER)
        box.append(self._size_label)
        self._start_size_measurement()

        self._free_space_label = Gtk.Label(label="")
        self._free_space_label.add_css_class("caption")
        self._free_space_label.add_css_class("dim-label")
        self._free_space_label.set_visible(False)
        box.append(self._free_space_label)
        if self._all_folders:
            self._fill_free_space()
        return box

    def _initial_size_text(self) -> str:
        if self._is_multi:
            file_total = sum(entry.size for entry in self._entries if not entry.is_dir)
            text = _selection_size_text(len(self._entries), file_total)
            return text + "…" if any(entry.is_dir for entry in self._entries) else text
        if self._entry.is_dir:
            return _folder_calculating_text(self._entry.item_count)
        return _human_size(self._entry.size)

    def _set_size_text(self, text: str) -> None:
        if not self._closed:
            self._size_label.set_label(text)

    def _start_size_measurement(self) -> None:
        """Measure folders' deep size in the background."""
        folders = [path for entry, path in zip(self._entries, self._paths) if entry.is_dir]
        if not folders:
            return
        backend = self._backend
        file_total = sum(entry.size for entry in self._entries if not entry.is_dir)

        def _measure():
            total = file_total
            for path in folders:
                total += backend.directory_size(path)
            return total

        def _done(total, error) -> None:
            if error is not None:
                logger.debug("Folder size failed: %s", error)
                total = -1
            if self._is_multi:
                self._set_size_text(_selection_size_text(len(self._entries), total))
            else:
                self._set_size_text(_folder_size_text(self._entry.item_count, total))

        run_async(_measure, _done)

    def _fill_free_space(self) -> None:
        backend = self._backend
        path = self._paths[0]

        def _done(free, error) -> None:
            if error is not None or free is None or self._closed:
                return
            self._free_space_label.set_label(_free_space_text(free))
            self._free_space_label.set_visible(True)

        run_async(lambda: backend.free_space(path), _done)

    # -- rows --------------------------------------------------------------

    def _create_parent_folder_row(self) -> Gtk.Widget:
        location = self._backend.location_uri(self._directory) if self._backend.is_remote else self._directory
        row = Adw.ActionRow(title=_("Parent Folder"), subtitle=safe_display_text(location))
        row.add_css_class("property")
        row.set_subtitle_selectable(True)
        if not self._backend.is_remote:
            btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
            btn.set_tooltip_text(_("Open Parent Folder"))
            btn.set_valign(Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_open_parent)
            row.add_suffix(btn)
        return row

    def _create_date_rows(self) -> List[Gtk.Widget]:
        times = [_human_time(entry.modified) if entry.modified else None for entry in self._entries]
        self._modified_row = Adw.ActionRow(
            title=_("Modified"), subtitle=_common_value(times) or "—"
        )
        self._modified_row.add_css_class("property")
        # Creation time is per-file; filled in when the stat has one.
        self._created_row = Adw.ActionRow(title=_("Created"), subtitle="—")
        self._created_row.add_css_class("property")
        self._created_row.set_visible(False)
        return [self._modified_row, self._created_row]

    def _create_owner_row(self) -> Gtk.Widget:
        self._owner_row = Adw.ActionRow(title=_("Owner"), subtitle=_("Loading…"))
        self._owner_row.add_css_class("property")
        return self._owner_row

    def _create_permissions_row(self) -> Gtk.Widget:
        row = Adw.ActionRow(title=_("Permissions"), subtitle=_("Loading…"))
        row.add_css_class("property")
        row.set_activatable(False)  # until every selected mode is known
        self._permissions_arrow = Gtk.Image.new_from_icon_name("go-next-symbolic")
        self._permissions_arrow.set_visible(False)
        row.add_suffix(self._permissions_arrow)
        row.connect("activated", self._on_permissions_row_activated)
        self._permissions_row = row
        return row

    def _create_execution_row(self) -> Gtk.Widget:
        """Nautilus "Executable as Program" switch, single regular files only."""
        row = Adw.SwitchRow(title=_("Executable as Program"))
        row.set_visible(False)
        row.connect("notify::active", self._on_execution_toggled)
        self._execution_row = row
        return row

    def _should_show_execution_switch(self) -> bool:
        if self._is_multi or self._entry.is_dir:
            return False
        content_type, uncertain = Gio.content_type_guess(self._entry.name, None)
        return (
            uncertain
            or content_type == "application/octet-stream"
            or Gio.content_type_can_be_executable(content_type)
        )

    # -- metadata ----------------------------------------------------------

    def _start_metadata_fetch(self) -> None:
        backend = self._backend
        paths = list(self._paths)

        def _fetch():
            stats: List[Optional[EntryStat]] = []
            for path in paths:
                try:
                    stats.append(backend.stat(path))
                except OSError:
                    try:
                        stats.append(backend.lstat(path))
                    except OSError:
                        stats.append(None)
            uids = {st.uid for st in stats if st is not None and st.uid is not None}
            gids = {st.gid for st in stats if st is not None and st.gid is not None}
            try:
                names = backend.owner_names(sorted(uids), sorted(gids))
            except OSError:
                names = ({}, {})
            return stats, names

        def _done(result, error) -> None:
            if self._closed:
                return
            if error is not None:
                logger.debug("Stat failed: %s", error)
                self._owner_row.set_subtitle("—")
                self._permissions_row.set_subtitle("—")
                return
            stats, (users, groups) = result
            self._stats = stats
            self._user_names, self._group_names = users, groups
            self._on_stats_known()

        run_async(_fetch, _done)

    def _on_stats_known(self) -> None:
        stats = self._stats
        modified = [
            _human_time(st.modified) if st is not None and st.modified else None for st in stats
        ]
        common_modified = _common_value(modified)
        if common_modified:
            self._modified_row.set_subtitle(common_modified)
        if not self._is_multi and stats[0] is not None and stats[0].created:
            self._created_row.set_subtitle(_human_time(stats[0].created))
            self._created_row.set_visible(True)
        self._refresh_owner_text()
        self._initial_modes = [st.mode if st is not None else None for st in stats]
        if self._permissions_known:
            self._permissions_row.set_activatable(True)
            self._permissions_arrow.set_visible(True)
            self._execution_row.set_visible(self._should_show_execution_switch())
        self._sync_permission_widgets()

    @property
    def _modes(self) -> List[Optional[int]]:
        return [st.mode if st is not None else None for st in self._stats]

    @property
    def _permissions_known(self) -> bool:
        return all(mode is not None for mode in self._modes)

    def _owner_text(self, uid: Optional[int], gid: Optional[int]) -> Optional[str]:
        if uid is None or gid is None:
            return None
        user = self._user_names.get(uid, str(uid))
        group = self._group_names.get(gid, str(gid))
        return f"{user} : {group}"

    def _refresh_owner_text(self) -> None:
        owners = [
            self._owner_text(st.uid, st.gid) if st is not None else None for st in self._stats
        ]
        self._owner_row.set_subtitle(safe_display_text(_common_value(owners) or "—"))
        user, group = self._common_owner_parts()
        for key, value in (("user", user), ("group", group)):
            row = self._owner_name_rows.get(key)
            if row is not None:
                row.set_subtitle(safe_display_text(value))

    def _common_owner_parts(self) -> tuple[str, str]:
        users: List[Optional[str]] = []
        groups: List[Optional[str]] = []
        for st in self._stats:
            text = self._owner_text(st.uid, st.gid) if st is not None else None
            if text is None:
                users.append(None)
                groups.append(None)
                continue
            user, _sep, group = text.partition(" : ")
            users.append(user)
            groups.append(group)
        return _common_value(users) or "—", _common_value(groups) or "—"

    def _mode_summary_text(self) -> Optional[str]:
        texts = [
            f"{_mode_to_str(mode)} ({_mode_to_octal(mode)})" if mode is not None else None
            for mode in self._modes
        ]
        return _common_value(texts)

    def _can_set_permissions(self) -> bool:
        """Locally only the owner (or root) may chmod; remote servers decide."""
        if self._backend.is_remote:
            return True
        euid = os.geteuid() if hasattr(os, "geteuid") else None
        if euid is None or euid == 0:
            return True
        return all(st is not None and st.uid == euid for st in self._stats)

    def _permission_items(self) -> list[tuple[bool, int]]:
        return [
            (entry.is_dir, mode)
            for entry, mode in zip(self._entries, self._modes)
            if mode is not None
        ]

    def _sync_permission_widgets(self) -> None:
        """Show the current modes in the summary, switch and access combos."""
        summary = perms.summarize(self._permission_items())
        text = self._mode_summary_text()
        self._permissions_row.set_subtitle(text or "—")
        can_set = self._can_set_permissions()
        self._syncing_permission_widgets = True
        try:
            self._execution_row.set_active(summary.file_exec == perms.EXEC)
            self._execution_row.set_sensitive(can_set)
            for row, who, is_folder, values in self._access_rows:
                current = (summary.folder if is_folder else summary.file).get(who, 0)
                if current not in values:
                    # Nautilus lists an odd or mixed value as an extra choice.
                    values.append(current)
                    row.get_model().append(perms.permission_label(current, is_folder))
                row.set_selected(values.index(current))
                row.set_sensitive(can_set)
        finally:
            self._syncing_permission_widgets = False

    def _on_permissions_row_activated(self, _row) -> None:
        if not self._permissions_known:
            return
        if self._permissions_page is None:
            self._permissions_page = self._build_permissions_page()
            self._sync_permission_widgets()
        self.navigation_view.push(self._permissions_page)

    def _build_permissions_page(self) -> Adw.NavigationPage:
        """Nautilus "Set Custom Permissions" page."""
        items = self._permission_items()
        has_folders = any(is_dir for is_dir, _mode in items)
        has_files = any(not is_dir for is_dir, _mode in items)
        user, group = self._common_owner_parts()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        banner = Adw.Banner(title=_("Only the owner can edit these permissions"))
        banner.set_revealed(not self._can_set_permissions())
        box.append(banner)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        box.append(page)

        sections = (
            (perms.PermissionClass.OWNER, None, _("Owner"), user, "user"),
            (perms.PermissionClass.GROUP, None, _("Group"), group, "group"),
            (perms.PermissionClass.OTHERS, _("Other Users"), None, None, None),
        )
        for who, group_title, name_title, name, name_key in sections:
            pref_group = Adw.PreferencesGroup()
            if group_title:
                pref_group.set_title(group_title)
            if name_title:
                name_row = Adw.ActionRow(title=name_title, subtitle=safe_display_text(name))
                name_row.add_css_class("property")
                self._owner_name_rows[name_key] = name_row
                pref_group.add(name_row)
            if has_folders and has_files:
                pref_group.add(self._create_access_row(who, True, _("Folder Access")))
                pref_group.add(self._create_access_row(who, False, _("File Access")))
            else:
                pref_group.add(self._create_access_row(who, has_folders, _("Access")))
            page.add(pref_group)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(box)
        return Adw.NavigationPage(
            title=_("Set Custom Permissions"), tag="permissions", child=toolbar
        )

    def _create_access_row(
        self, who: perms.PermissionClass, is_folder: bool, title: str
    ) -> Adw.ComboRow:
        values = perms.permission_choices(who, is_folder)
        model = Gtk.StringList.new(
            [perms.permission_label(value, is_folder) for value in values]
        )
        row = Adw.ComboRow(title=title, model=model)
        self._access_rows.append((row, who, is_folder, values))
        row.connect("notify::selected", self._on_access_selected, who, is_folder, values)
        return row

    def _on_access_selected(self, row, _pspec, who, is_folder, values) -> None:
        if self._syncing_permission_widgets:
            return
        position = row.get_selected()
        if position == Gtk.INVALID_LIST_POSITION or position >= len(values):
            return
        value = values[position]
        mask = perms.class_mask(who, is_folder)
        new_modes = {}
        for index, entry in enumerate(self._entries):
            mode = self._modes[index]
            if mode is None or entry.is_dir != is_folder:
                continue
            if value & perms.INCONSISTENT:
                # The mixed "---" choice puts back what each item started with.
                source = self._initial_modes[index]
                bits = source if source is not None else mode
            else:
                bits = perms.permission_to_mode(who, value)
            new_modes[index] = perms.apply_bits(mode, bits, mask)
        self._change_modes(new_modes)

    def _on_execution_toggled(self, row, _pspec) -> None:
        if self._syncing_permission_widgets:
            return
        executable = row.get_active()
        new_modes = {
            index: perms.set_executable(mode, executable)
            for index, (entry, mode) in enumerate(zip(self._entries, self._modes))
            if mode is not None and not entry.is_dir
        }
        self._change_modes(new_modes)

    def _change_modes(self, new_modes: Dict[int, int]) -> None:
        """chmod each changed entry; failures revert and are reported once."""
        changes = {
            index: mode
            for index, mode in new_modes.items()
            if self._stats[index] is not None
            and (self._stats[index].mode & 0o7777) != mode
        }
        if not changes:
            return
        previous = {index: self._stats[index].mode for index in changes}
        for index, mode in changes.items():
            # Keep the file-type bits so the summary still shows "d" / "l".
            self._stats[index].mode = (previous[index] & ~0o7777) | mode
        backend = self._backend
        paths = self._paths

        def _apply():
            failures = []
            for index, mode in changes.items():
                try:
                    backend.chmod(paths[index], mode)
                except OSError as exc:
                    failures.append((index, exc))
            return failures

        def _done(failures, error) -> None:
            if error is not None:
                failures = [(index, error) for index in changes]
            for index, _exc in failures:
                self._stats[index].mode = previous[index]
            if self._closed:
                return
            self._sync_permission_widgets()
            if failures:
                index, exc = failures[0]
                self._report_permission_error(self._entries[index], exc)

        run_async(_apply, _done)

    def _report_permission_error(self, entry: FileEntry, exc: BaseException) -> None:
        detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        text = _("Could not change the permissions of “{name}”: {error}").format(
            name=entry.name, error=detail
        )
        from .pane import present_error_alert

        present_error_alert(self, text)

    def _on_open_parent(self, *_) -> None:
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(self._paths[0]))
        launcher.open_containing_folder(self.get_root(), None, None, None)
