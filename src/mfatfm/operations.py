"""Running file operations, shown in a header-bar popover as Nautilus does.

Each copy, move or delete runs on a worker thread and is listed here with
its progress, speed and time left, and a button to cancel it. The popover
does not block the window, so browsing continues while files are copied.
Widgets are refreshed from a timer; the worker only writes the shared
:class:`~mfatfm.progress.OperationProgress`.
"""

from __future__ import annotations

import collections
import logging
import time
from gettext import gettext as _, ngettext
from typing import Callable, Deque, List, Optional, Tuple

from gi.repository import GLib, GObject, Gtk, Pango

from .format_utils import safe_display_text
from .progress import OperationProgress

logger = logging.getLogger(__name__)

_RENDER_INTERVAL_MS = 250
_SPEED_WINDOW_SECONDS = 5.0
# Successful operations leave the list after this long.
_DONE_LINGER_SECONDS = 4
# Quick operations finish before the popover would flash open.
_POPUP_DELAY_MS = 600


def format_bytes(size_bytes: float) -> str:
    if size_bytes >= 1024 ** 3:
        return _("{size:.1f} GB").format(size=size_bytes / 1024 ** 3)
    if size_bytes >= 1024 ** 2:
        return _("{size:.1f} MB").format(size=size_bytes / 1024 ** 2)
    if size_bytes >= 1024:
        return _("{size:.1f} KB").format(size=size_bytes / 1024)
    return ngettext("{size} byte", "{size} bytes", int(size_bytes)).format(size=int(size_bytes))


def format_speed(bps: float) -> str:
    return _("{speed}/s").format(speed=format_bytes(bps))


def format_remaining(seconds: float) -> str:
    if seconds > 3600:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return _("{hours}h {minutes}m left").format(hours=hours, minutes=minutes)
    if seconds > 60:
        return _("{minutes}m left").format(minutes=int(seconds // 60))
    return _("{seconds}s left").format(seconds=max(1, int(seconds)))


class Operation(GObject.Object):
    """One copy, move or delete: its progress and how it ended."""

    __gsignals__ = {
        "finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    RUNNING, DONE, FAILED, CANCELLED = "running", "done", "failed", "cancelled"

    def __init__(self, title: str, total_items: int, icon_name: str = "edit-copy-symbolic") -> None:
        super().__init__()
        self.title = title
        self.icon_name = icon_name
        self.progress = OperationProgress(total_items)
        self.state = self.RUNNING
        self.message = ""
        self.started = time.monotonic()
        self._samples: Deque[Tuple[float, int]] = collections.deque()

    def cancel(self) -> None:
        self.progress.cancel()

    def finish(self, state: str, message: str = "") -> None:
        """Record how it ended (main thread)."""
        self.state = state
        self.message = message
        self.emit("finished")

    def speed(self, bytes_done: int) -> Optional[float]:
        """Recent throughput from a sliding window of samples."""
        now = time.monotonic()
        self._samples.append((now, bytes_done))
        cutoff = now - _SPEED_WINDOW_SECONDS
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            return None
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        if t1 - t0 <= 0 or b1 <= b0:
            return None
        return (b1 - b0) / (t1 - t0)


class OperationRow(Gtk.ListBoxRow):
    def __init__(self, operation: Operation, on_dismiss: Callable[["OperationRow"], None]) -> None:
        super().__init__()
        self.operation = operation
        self.set_activatable(False)
        self._on_dismiss = on_dismiss

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        box.set_size_request(380, -1)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(8)

        self._icon = Gtk.Image.new_from_icon_name(operation.icon_name)
        self._icon.set_valign(Gtk.Align.CENTER)
        box.append(self._icon)

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text.set_hexpand(True)
        self._title = Gtk.Label(label=safe_display_text(operation.title), xalign=0)
        self._title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._title.set_max_width_chars(36)
        self._title.add_css_class("heading")
        text.append(self._title)
        self._bar = Gtk.ProgressBar()
        text.append(self._bar)
        self._detail = Gtk.Label(xalign=0)
        self._detail.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._detail.set_max_width_chars(36)
        for css in ("caption", "dim-label", "numeric"):
            self._detail.add_css_class(css)
        text.append(self._detail)
        box.append(text)

        self._button = Gtk.Button.new_from_icon_name("process-stop-symbolic")
        self._button.set_valign(Gtk.Align.CENTER)
        self._button.add_css_class("flat")
        self._button.add_css_class("circular")
        self._button.set_tooltip_text(_("Cancel"))
        self._button.connect("clicked", self._on_button_clicked)
        box.append(self._button)
        self.set_child(box)
        self.render()

    def _on_button_clicked(self, _button) -> None:
        if self.operation.state == Operation.RUNNING:
            self.operation.cancel()
            self._button.set_sensitive(False)
            self._detail.set_text(_("Cancelling…"))
        else:
            self._on_dismiss(self)

    def render(self) -> None:
        operation = self.operation
        snap = operation.progress.snapshot()
        if operation.state != Operation.RUNNING:
            self._button.set_sensitive(True)
            self._button.set_icon_name("window-close-symbolic")
            self._button.set_tooltip_text(_("Dismiss"))
            if operation.state == Operation.DONE:
                self._bar.set_fraction(1.0)
                self._detail.set_text(operation.message or _("Done"))
            else:
                self._bar.add_css_class("error" if operation.state == Operation.FAILED else "dim-label")
                self._detail.set_text(
                    operation.message
                    or (_("Cancelled") if operation.state == Operation.CANCELLED else _("Failed"))
                )
                self._detail.remove_css_class("dim-label")
                if operation.state == Operation.FAILED:
                    self._detail.add_css_class("error")
                    self._detail.set_wrap(True)
                    self._detail.set_ellipsize(Pango.EllipsizeMode.NONE)
            return
        if operation.progress.cancelled:
            return

        if snap.fraction is None:
            self._bar.pulse()
        else:
            self._bar.set_fraction(snap.fraction)

        parts: List[str] = []
        progress = operation.progress
        if progress.total_items > 1:
            parts.append(
                _("{done} of {total}").format(
                    done=min(progress.total_items, progress.items_done + 1),
                    total=progress.total_items,
                )
            )
        if snap.bytes_done or snap.bytes_total:
            if snap.bytes_total:
                parts.append(
                    _("{done} of {total}").format(
                        done=format_bytes(snap.bytes_done), total=format_bytes(snap.bytes_total)
                    )
                )
            else:
                parts.append(format_bytes(snap.bytes_done))
            bps = operation.speed(snap.bytes_done)
            if bps:
                parts.append(format_speed(bps))
                if snap.bytes_total:
                    remaining = max(0, snap.bytes_total - snap.bytes_done)
                    parts.append(format_remaining(remaining / bps))
        elif progress.current_name:
            parts.append(safe_display_text(progress.current_name))
        self._detail.set_text(" · ".join(parts) or _("Preparing…"))
        name = progress.current_name
        self._bar.set_tooltip_text(safe_display_text(name) if name else None)


class OperationsButton(Gtk.MenuButton):
    """Header-bar button listing running and recent operations."""

    def __init__(self) -> None:
        super().__init__()
        self.set_tooltip_text(_("Show Operations"))
        self.set_visible(False)
        self._indicator = Gtk.DrawingArea()
        self._indicator.set_content_width(16)
        self._indicator.set_content_height(16)
        self._indicator.set_draw_func(self._draw_indicator)
        self.set_child(self._indicator)
        self._fraction = 0.0

        self._list = Gtk.ListBox()
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_max_content_height(420)
        scrolled.set_min_content_width(360)
        scrolled.set_child(self._list)
        scrolled.set_margin_top(6)
        scrolled.set_margin_bottom(6)
        scrolled.set_margin_start(6)
        scrolled.set_margin_end(6)
        popover = Gtk.Popover()
        popover.set_child(scrolled)
        self.set_popover(popover)
        self._rows: List[OperationRow] = []
        self._timer: Optional[int] = None

    @property
    def running(self) -> List[Operation]:
        return [row.operation for row in self._rows if row.operation.state == Operation.RUNNING]

    def add(self, operation: Operation) -> None:
        row = OperationRow(operation, self._remove_row)
        self._rows.append(row)
        self._list.prepend(row)
        self.set_visible(True)
        operation.connect("finished", self._on_finished, row)
        if self._timer is None:
            self._timer = GLib.timeout_add(_RENDER_INTERVAL_MS, self._tick)

        def _maybe_popup() -> bool:
            if operation.state == Operation.RUNNING and self.get_mapped():
                self.popup()
            return False

        GLib.timeout_add(_POPUP_DELAY_MS, _maybe_popup)

    def _on_finished(self, operation: Operation, row: OperationRow) -> None:
        row.render()
        if operation.state in (Operation.DONE, Operation.CANCELLED):
            GLib.timeout_add_seconds(_DONE_LINGER_SECONDS, lambda: self._remove_row(row) or False)
        elif operation.state == Operation.FAILED:
            self.popup()
        self._update_indicator()

    def _remove_row(self, row: OperationRow) -> None:
        if row not in self._rows:
            return
        self._rows.remove(row)
        self._list.remove(row)
        if not self._rows:
            self.popdown()
            self.set_visible(False)

    def _tick(self) -> bool:
        for row in self._rows:
            if row.operation.state == Operation.RUNNING:
                row.render()
        self._update_indicator()
        if not self.running:
            self._timer = None
            return False
        return True

    def _update_indicator(self) -> None:
        running = self.running
        fractions = [op.progress.snapshot().fraction or 0.0 for op in running]
        self._fraction = sum(fractions) / len(fractions) if fractions else 1.0
        self._indicator.queue_draw()

    def _draw_indicator(self, area, cr, width, height) -> None:
        """A pie that fills as the running operations progress."""
        import math

        color = area.get_color()
        radius = min(width, height) / 2 - 1
        cx, cy = width / 2, height / 2
        cr.set_source_rgba(color.red, color.green, color.blue, 0.25)
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(color.red, color.green, color.blue, 1.0)
        cr.move_to(cx, cy)
        start = -math.pi / 2
        cr.arc(cx, cy, radius, start, start + 2 * math.pi * max(0.0, min(1.0, self._fraction)))
        cr.close_path()
        cr.fill()
