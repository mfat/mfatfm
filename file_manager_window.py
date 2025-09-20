"""In-app two-pane SFTP file manager window.

This module provides a libadwaita based window that mimics a traditional
file manager experience while running entirely inside sshPilot.  It exposes
two panes that can each browse an independent remote path.  All filesystem
operations are executed on background worker threads to keep the UI
responsive and results are marshalled back to the main GTK loop using
``GLib.idle_add``.  The implementation intentionally favours clarity over raw
performance – the goal is to provide a dependable fallback for situations
where a native GVFS/GIO based file manager is not available (e.g. Flatpak
deployments).

The window follows the GNOME HIG by composing libadwaita widgets such as
``Adw.ToolbarView`` and ``Adw.HeaderBar``.  Each pane exposes both list and
grid representations of directory contents, navigation controls, progress
indicators and toast based feedback.
"""

from __future__ import annotations

import dataclasses
import mimetypes
import os
import pathlib
import posixpath
import shutil
import stat
import threading
import time
from datetime import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Dict, Iterable, List, Optional, Tuple


import paramiko
from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk, Pango


_DROP_ZONE_CSS_PROVIDER: Optional[Gtk.CssProvider] = None


def _ensure_drop_zone_css() -> None:
    """Ensure the CSS used to highlight drop zones is loaded once."""

    global _DROP_ZONE_CSS_PROVIDER

    if _DROP_ZONE_CSS_PROVIDER is not None:
        return

    css_provider_cls = getattr(Gtk, "CssProvider", None)
    if css_provider_cls is None:
        _DROP_ZONE_CSS_PROVIDER = None
        return

    provider = css_provider_cls()
    css_lines = [
        b".file-pane-drop-zone {",
        b"    border: 2px dashed alpha(@accent_color, 0.5);",
        b"    border-radius: 24px;",
        b"    background-color: alpha(@accent_color, 0.15);",
        b"    padding: 12px 20px;",
        b"    box-shadow: 0 4px 12px alpha(@shade_color, 0.15);",
        b"    backdrop-filter: blur(8px);",
        b"    transition: all 150ms ease;",
        b"}",
        b"",
        b".file-pane-drop-zone.visible {",
        b"    border-style: solid;",
        b"    border-color: @accent_color;",
        b"    background-color: alpha(@accent_color, 0.25);",
        b"    box-shadow: 0 6px 16px alpha(@shade_color, 0.25);",
        b"    transform: translateY(-2px);",
        b"}",
        b"",
        b".file-pane-drop-zone .drop-zone-title {",
        b"    font-weight: 600;",
        b"    color: @accent_color;",
        b"}",
    ]
    css_data = b"\n".join(css_lines)

    try:
        provider.load_from_data(css_data)
    except Exception:
        _DROP_ZONE_CSS_PROVIDER = provider
        return

    display = None
    if hasattr(Gdk, "Display"):
        get_default = getattr(Gdk.Display, "get_default", None)
        if callable(get_default):
            try:
                display = get_default()
            except Exception:
                display = None

    style_context = getattr(Gtk, "StyleContext", None)
    add_provider = getattr(style_context, "add_provider_for_display", None) if style_context else None
    priority = getattr(Gtk, "STYLE_PROVIDER_PRIORITY_APPLICATION", 600)

    if display is not None and callable(add_provider):
        try:
            add_provider(display, provider, priority)
        except Exception:
            pass

    _DROP_ZONE_CSS_PROVIDER = provider


class TransferCancelledException(Exception):
    """Exception raised when a transfer is cancelled"""
    pass


# ---------------------------------------------------------------------------
# Utility data structures


class SFTPProgressDialog(Adw.Window):
    """GNOME HIG-compliant SFTP file transfer progress dialog"""
    
    def __init__(self, parent=None, operation_type="transfer"):
        super().__init__()
        
        # Window properties
        self.set_title("File Transfer")
        self.set_default_size(480, 320)
        self.set_modal(True)
        if parent:
            self.set_transient_for(parent)
        
        # Transfer state
        self.is_cancelled = False
        self.current_file = ""
        self.transferred_bytes = 0
        self.total_bytes = 0
        self.files_completed = 0
        self.total_files = 0
        self.start_time = time.time()
        self.operation_type = operation_type
        self._current_future = None
        
        self._build_ui()
        
    def _build_ui(self):
        """Build the HIG-compliant UI"""
        
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        # Header bar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        main_box.append(header)
        
        # Content area with proper spacing
        content_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24
        )
        main_box.append(content_box)
        
        # Status icon and title
        status_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER
        )
        content_box.append(status_box)
        
        # Transfer icon
        icon_name = "folder-download-symbolic" if self.operation_type == "download" else "folder-upload-symbolic"
        self.status_icon = Gtk.Image.new_from_icon_name(icon_name)
        self.status_icon.set_pixel_size(48)
        self.status_icon.add_css_class("accent")
        status_box.append(self.status_icon)
        
        # Main status label
        self.status_label = Gtk.Label()
        self.status_label.set_markup("<span size='large' weight='bold'>Preparing transfer…</span>")
        self.status_label.set_justify(Gtk.Justification.CENTER)
        status_box.append(self.status_label)
        
        # Current file label
        self.file_label = Gtk.Label()
        self.file_label.set_text("Scanning files...")
        self.file_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.file_label.add_css_class("dim-label")
        status_box.append(self.file_label)
        
        # Progress section
        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12
        )
        content_box.append(progress_box)
        
        # Main progress bar
        self.progress_bar = Gtk.ProgressBar()
        self.progress_bar.set_show_text(True)
        self.progress_bar.set_text("0%")
        progress_box.append(self.progress_bar)
        
        # Transfer details
        details_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6
        )
        progress_box.append(details_box)
        
        # Speed and time info
        info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        details_box.append(info_box)
        
        self.speed_label = Gtk.Label()
        self.speed_label.set_text("—")
        self.speed_label.set_halign(Gtk.Align.START)
        self.speed_label.add_css_class("caption")
        info_box.append(self.speed_label)
        
        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        info_box.append(spacer)
        
        self.time_label = Gtk.Label()
        self.time_label.set_text("—")
        self.time_label.set_halign(Gtk.Align.END)
        self.time_label.add_css_class("caption")
        info_box.append(self.time_label)
        
        # File counter
        self.counter_label = Gtk.Label()
        self.counter_label.set_text("0 of 0 files")
        self.counter_label.set_halign(Gtk.Align.CENTER)
        self.counter_label.add_css_class("caption")
        details_box.append(self.counter_label)
        
        # Button box
        button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.END
        )
        content_box.append(button_box)
        
        # Cancel button
        self.cancel_button = Gtk.Button()
        self.cancel_button.set_label("Cancel")
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        button_box.append(self.cancel_button)
        
        # Done button (hidden initially)
        self.done_button = Gtk.Button()
        self.done_button.set_label("Done")
        self.done_button.set_visible(False)
        self.done_button.add_css_class("suggested-action")
        self.done_button.connect("clicked", lambda w: self.close())
        button_box.append(self.done_button)
        
        # Set initial focus to cancel button
        self.cancel_button.grab_focus()
    
    def set_operation_details(self, total_files, filename=None):
        """Set the operation details"""
        self.total_files = total_files
        self.files_completed = 0
        
        if filename:
            self.current_file = filename
            self.file_label.set_text(filename)
        
        self.counter_label.set_text(f"0 of {total_files} files")
    
    def update_progress(self, fraction, message=None, current_file=None):
        """Update progress bar and status"""
        GLib.idle_add(self._update_progress_ui, fraction, message, current_file)
    
    def _update_progress_ui(self, fraction, message, current_file):
        """Update UI elements (must be called from main thread)"""
        
        # Update progress bar
        percentage = int(fraction * 100)
        self.progress_bar.set_fraction(fraction)
        self.progress_bar.set_text(f"{percentage}%")
        
        # Update status message
        if message:
            self.status_label.set_markup(f"<span size='large' weight='bold'>{message}</span>")
        
        # Update current file
        if current_file:
            self.current_file = current_file
            self.file_label.set_text(current_file)
        
        # Calculate and update speed/time estimates
        elapsed = time.time() - self.start_time
        if elapsed > 1.0 and fraction > 0:  # Wait at least 1 second for meaningful estimates
            # Calculate transferred bytes and speed
            if self.total_bytes > 0:
                transferred_bytes = int(self.total_bytes * fraction)
                bytes_per_second = transferred_bytes / elapsed
                
                # Update speed display
                if bytes_per_second > 1024 * 1024:  # MB/s
                    speed_text = f"{bytes_per_second / (1024 * 1024):.1f} MB/s"
                elif bytes_per_second > 1024:  # KB/s
                    speed_text = f"{bytes_per_second / 1024:.1f} KB/s"
                else:
                    speed_text = f"{bytes_per_second:.0f} B/s"
                
                self.speed_label.set_text(speed_text)
                
                # Show size information
                transferred_size = self._format_size(transferred_bytes)
                total_size = self._format_size(self.total_bytes)
                size_info = f"{transferred_size} of {total_size}"
                
                # Update file label to show size info
                if current_file:
                    self.file_label.set_text(f"{current_file} ({size_info})")
            
            # Estimate total time and remaining time
            estimated_total_time = elapsed / fraction
            remaining_time = estimated_total_time - elapsed
            
            if remaining_time > 0:
                self.time_label.set_text(self._format_time(remaining_time))
            else:
                self.time_label.set_text("Almost done…")
        
        return False
    
    def increment_file_count(self):
        """Increment completed file counter"""
        GLib.idle_add(self._increment_file_count_ui)
    
    def _increment_file_count_ui(self):
        """Update file counter (must be called from main thread)"""
        self.files_completed += 1
        self.counter_label.set_text(f"{self.files_completed} of {self.total_files} files")
        return False
    
    def set_future(self, future):
        """Set the current operation future for cancellation"""
        self._current_future = future
    
    def set_total_bytes(self, total_bytes):
        """Set the total bytes for the operation"""
        self.total_bytes = total_bytes
    
    def _format_time(self, seconds):
        """Format time remaining for display"""
        if seconds > 3600:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m remaining"
        elif seconds > 60:
            minutes = int(seconds // 60)
            return f"{minutes}m remaining"
        else:
            return f"{int(seconds)}s remaining"
    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"
    
    def show_completion(self, success=True, error_message=None):
        """Show completion state"""
        GLib.idle_add(self._show_completion_ui, success, error_message)
    
    def _show_completion_ui(self, success, error_message):
        """Update UI to show completion state"""
        if success:
            self.status_icon.set_from_icon_name("emblem-ok-symbolic")
            self.status_icon.remove_css_class("accent")
            self.status_icon.add_css_class("success")
            
            self.status_label.set_markup("<span size='large' weight='bold'>Transfer complete</span>")
            self.file_label.set_text(f"Successfully transferred {self.files_completed} files")
            
            self.progress_bar.set_fraction(1.0)
            self.progress_bar.set_text("100%")
        else:
            self.status_icon.set_from_icon_name("dialog-error-symbolic")
            self.status_icon.remove_css_class("accent")
            self.status_icon.add_css_class("error")
            
            self.status_label.set_markup("<span size='large' weight='bold'>Transfer failed</span>")
            if error_message:
                self.file_label.set_text(f"Error: {error_message}")
            else:
                self.file_label.set_text("An error occurred during transfer")
        
        # Switch buttons
        self.cancel_button.set_visible(False)
        self.done_button.set_visible(True)
        self.done_button.grab_focus()
        
        return False
    
    def _on_cancel_clicked(self, button):
        """Handle cancel button click"""
        self.is_cancelled = True
        
        # Cancel the future operation
        if self._current_future and not self._current_future.done():
            try:
                self._current_future.cancel()
                print("DEBUG: Future cancelled successfully")
            except Exception as e:
                print(f"DEBUG: Error cancelling future: {e}")
        
        # Update UI to show cancellation
        self.status_label.set_markup("<span size='large' weight='bold'>Cancelled</span>")
        self.file_label.set_text("Transfer was cancelled by user")
        
        # Change icon to indicate cancellation
        self.status_icon.set_from_icon_name("process-stop-symbolic")
        self.status_icon.remove_css_class("accent")
        self.status_icon.add_css_class("warning")
        
        # Switch buttons immediately
        button.set_visible(False)
        self.done_button.set_label("Close")
        self.done_button.set_visible(True)
        self.done_button.grab_focus()
        
        print("DEBUG: Cancel operation completed")


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None  # Number of items in directory (for folders only)


class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))


# ---------------------------------------------------------------------------
# Asynchronous SFTP layer


class AsyncSFTPManager(GObject.GObject):
    """Small wrapper around :mod:`paramiko` that performs operations in
    worker threads.

    The class exposes a queue of operations and emits signals when important
    events happen.  Tests can monkeypatch :class:`paramiko.SSHClient` to avoid
    talking to a real server.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "operation-error": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str,),
        ),
        "directory-loaded": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(
        self,
        host: str,
        username: str,
        port: int = 22,
        password: Optional[str] = None,
        *,
        dispatcher: Callable[[Callable, tuple, dict], None] | None = None,
    ) -> None:
        super().__init__()
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._executor = ThreadPoolExecutor(max_workers=4)
        self._dispatcher = dispatcher or (
            lambda cb, args=(), kwargs=None: _MainThreadDispatcher.dispatch(
                cb, *args, **(kwargs or {})
            )
        )
        self._lock = threading.Lock()
        self._cancelled_operations = set()  # Track cancelled operation IDs
    
    def _format_size(self, size_bytes):
        """Format file size for display"""
        if size_bytes >= 1024 * 1024 * 1024:  # GB
            return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif size_bytes >= 1024 * 1024:  # MB
            return f"{size_bytes / (1024 * 1024):.1f} MB"
        elif size_bytes >= 1024:  # KB
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes} bytes"

    # -- connection -----------------------------------------------------

    def connect_to_server(self) -> None:
        self._submit(
            self._connect_impl,
            on_success=lambda *_: self.emit("connected"),
            on_error=lambda exc: self.emit("connection-error", str(exc)),
        )

    def close(self) -> None:
        with self._lock:
            if self._sftp is not None:
                self._sftp.close()
                self._sftp = None
            if self._client is not None:
                self._client.close()
                self._client = None
        self._executor.shutdown(wait=False)

    # -- helpers --------------------------------------------------------

    def _submit(
        self,
        func: Callable[[], object],
        *,
        on_success: Optional[Callable[[object], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
    ) -> Future:
        future = self._executor.submit(func)

        def _done(fut: Future) -> None:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - errors handled uniformly
                if on_error:
                    self._dispatcher(on_error, (exc,), {})
                else:
                    self._dispatcher(self.emit, ("operation-error", str(exc)), {})
            else:
                if on_success:
                    self._dispatcher(on_success, (result,), {})

        future.add_done_callback(_done)
        return future

    # -- actual work ----------------------------------------------------

    def _connect_impl(self) -> None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=self._host,
            username=self._username,
            password=self._password,
            port=self._port,
            allow_agent=True,
            look_for_keys=True,
            timeout=15,
        )
        sftp = client.open_sftp()
        with self._lock:
            self._client = client
            self._sftp = sftp

    # -- public operations ----------------------------------------------

    def listdir(self, path: str) -> None:
        def _impl() -> Tuple[str, List[FileEntry]]:
            entries: List[FileEntry] = []
            assert self._sftp is not None
            
            # Expand ~ to user's home directory
            expanded_path = path
            if path == "~" or path.startswith("~/"):
                # Use the most reliable method to get home directory
                # The SFTP normalize method with "." should give us the initial directory
                # which is typically the user's home directory
                try:
                    if path == "~":
                        # For just ~, resolve to the absolute home directory
                        expanded_path = self._sftp.normalize(".")
                    else:
                        # For ~/subpath, we need to resolve the home directory first
                        # Try to get the actual home directory path
                        home_path = self._sftp.normalize(".")
                        expanded_path = home_path + path[1:]  # Replace ~ with home_path
                except Exception:
                    # If normalize fails, try common patterns
                    try:
                        possible_homes = [
                            f"/home/{self._username}",
                            f"/Users/{self._username}",  # macOS
                            f"/export/home/{self._username}",  # Solaris
                        ]
                        for possible_home in possible_homes:
                            try:
                                # Test if this directory exists
                                self._sftp.listdir_attr(possible_home)
                                if path == "~":
                                    expanded_path = possible_home
                                else:
                                    expanded_path = possible_home + path[1:]
                                break
                            except Exception:
                                continue
                        else:
                            # Final fallback
                            expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                    except Exception:
                        # Ultimate fallback
                        expanded_path = f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
            
            for attr in self._sftp.listdir_attr(expanded_path):
                is_dir = stat_isdir(attr)
                item_count = None
                
                # Count items in directory
                if is_dir:
                    try:
                        dir_path = os.path.join(expanded_path, attr.filename)
                        dir_attrs = self._sftp.listdir_attr(dir_path)
                        item_count = len(dir_attrs)
                    except Exception:
                        # If we can't read the directory, set count to None
                        item_count = None
                
                entries.append(
                    FileEntry(
                        name=attr.filename,
                        is_dir=is_dir,
                        size=attr.st_size,
                        modified=attr.st_mtime,
                        item_count=item_count,
                    )
                )
            return expanded_path, entries

        self._submit(
            _impl,
            on_success=lambda result: self.emit("directory-loaded", *result),
            on_error=lambda exc: self.emit("operation-error", str(exc)),
        )

    def mkdir(self, path: str) -> Future:
        return self._submit(
            lambda: self._sftp.mkdir(path),
            on_success=lambda *_: self.listdir(os.path.dirname(path) or "/"),
        )

    def remove(self, path: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            try:
                self._sftp.remove(path)
            except IOError:
                # fallback to directory remove
                for entry in self._sftp.listdir(path):
                    self.remove(os.path.join(path, entry))
                self._sftp.rmdir(path)

        parent = os.path.dirname(path) or "/"
        return self._submit(_impl, on_success=lambda *_: self.listdir(parent))

    def rename(self, source: str, target: str) -> Future:
        return self._submit(
            lambda: self._sftp.rename(source, target),
            on_success=lambda *_: self.listdir(os.path.dirname(target) or "/"),
        )

    def download(self, source: str, destination: pathlib.Path) -> Future:
        destination.parent.mkdir(parents=True, exist_ok=True)
        operation_id = f"download_{id(self)}_{time.time()}"

        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting download…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Download was cancelled")
                    
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Downloaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Downloaded {transferred_size}")
            
            try:
                self._sftp.get(source, str(destination), callback=progress_callback)
                # Only emit completion if not cancelled
                if operation_id not in self._cancelled_operations:
                    self.emit("progress", 1.0, "Download complete")
            except TransferCancelledException:
                # Clean up partial download on cancellation
                try:
                    if destination.exists():
                        destination.unlink()
                        print(f"DEBUG: Cleaned up partial download: {destination}")
                except Exception:
                    pass
                self.emit("progress", 0.0, "Download cancelled")
                print(f"DEBUG: Download operation {operation_id} was cancelled")
            finally:
                # Clean up the cancellation flag
                self._cancelled_operations.discard(operation_id)

        future = self._submit(_impl)
        
        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            print(f"DEBUG: Cancelling download operation {operation_id}")
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        
        return future

    def upload(self, source: pathlib.Path, destination: str) -> Future:
        operation_id = f"upload_{id(self)}_{time.time()}"
        
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Starting upload…")
            
            def progress_callback(transferred: int, total: int) -> None:
                # Check if this operation was cancelled
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Upload was cancelled")
                    
                if total > 0:
                    progress = transferred / total
                    transferred_size = self._format_size(transferred)
                    total_size = self._format_size(total)
                    self.emit("progress", progress, f"Uploaded {transferred_size} of {total_size}")
                else:
                    transferred_size = self._format_size(transferred)
                    self.emit("progress", 0.0, f"Uploaded {transferred_size}")
            
            try:
                self._sftp.put(str(source), destination, callback=progress_callback)
                # Only emit completion if not cancelled
                if operation_id not in self._cancelled_operations:
                    self.emit("progress", 1.0, "Upload complete")
            except TransferCancelledException:
                self.emit("progress", 0.0, "Upload cancelled")
                print(f"DEBUG: Upload operation {operation_id} was cancelled")
            finally:
                # Clean up the cancellation flag
                self._cancelled_operations.discard(operation_id)

        future = self._submit(_impl)
        
        # Store the operation ID so we can cancel it
        original_cancel = future.cancel
        def cancel_with_cleanup():
            print(f"DEBUG: Cancelling upload operation {operation_id}")
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = cancel_with_cleanup
        
        return future

    # Helpers for directory recursion – these are intentionally simplistic
    # and rely on Paramiko's high level API.

    def download_directory(self, source: str, destination: pathlib.Path) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing download…")
            
            # First, collect all files to get total count
            all_files = []
            for root, dirs, files in walk_remote(self._sftp, source):
                rel_root = os.path.relpath(root, source)
                target_root = destination / rel_root
                target_root.mkdir(parents=True, exist_ok=True)
                for name in files:
                    all_files.append((os.path.join(root, name), str(target_root / name)))
            
            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory downloaded (no files)")
                return
            
            # Download files with progress tracking
            for i, (remote_path, local_path) in enumerate(all_files):
                file_progress = i / total_files
                self.emit("progress", file_progress, f"Downloading {os.path.basename(remote_path)}...")
                
                def progress_callback(transferred: int, total: int) -> None:
                    if total > 0:
                        file_progress = transferred / total
                        overall_progress = (i + file_progress) / total_files
                        self.emit("progress", overall_progress, 
                                f"Downloading {os.path.basename(remote_path)} ({transferred:,}/{total:,} bytes)")
                
                self._sftp.get(remote_path, local_path, callback=progress_callback)
            
            self.emit("progress", 1.0, "Directory downloaded")

        return self._submit(_impl)

    def upload_directory(self, source: pathlib.Path, destination: str) -> Future:
        def _impl() -> None:
            assert self._sftp is not None
            self.emit("progress", 0.0, "Preparing upload…")
            
            # First, collect all files to get total count
            all_files = []
            for root, dirs, files in os.walk(source):
                rel_root = os.path.relpath(root, str(source))
                remote_root = (
                    destination if rel_root == "." else os.path.join(destination, rel_root)
                )
                try:
                    self._sftp.mkdir(remote_root)
                except IOError:
                    pass
                for name in files:
                    local_path = os.path.join(root, name)
                    remote_path = os.path.join(remote_root, name)
                    all_files.append((local_path, remote_path))
            
            total_files = len(all_files)
            if total_files == 0:
                self.emit("progress", 1.0, "Directory uploaded (no files)")
                return
            
            # Upload files with progress tracking
            for i, (local_path, remote_path) in enumerate(all_files):
                file_progress = i / total_files
                self.emit("progress", file_progress, f"Uploading {os.path.basename(local_path)}...")
                
                def progress_callback(transferred: int, total: int) -> None:
                    if total > 0:
                        file_progress = transferred / total
                        overall_progress = (i + file_progress) / total_files
                        self.emit("progress", overall_progress, 
                                f"Uploading {os.path.basename(local_path)} ({transferred:,}/{total:,} bytes)")
                
                self._sftp.put(local_path, remote_path, callback=progress_callback)
            
            self.emit("progress", 1.0, "Directory uploaded")

        return self._submit(_impl)


def stat_isdir(attr: paramiko.SFTPAttributes) -> bool:
    """Return ``True`` when the attribute represents a directory."""

    return bool(attr.st_mode & 0o40000)


def walk_remote(sftp: paramiko.SFTPClient, root: str) -> Iterable[Tuple[str, List[str], List[str]]]:
    """Yield a remote directory tree similar to :func:`os.walk`."""

    dirs: List[str] = []
    files: List[str] = []
    for entry in sftp.listdir_attr(root):
        if stat_isdir(entry):
            dirs.append(entry.filename)
        else:
            files.append(entry.filename)
    yield root, dirs, files
    for directory in dirs:
        new_root = os.path.join(root, directory)
        yield from walk_remote(sftp, new_root)


# ---------------------------------------------------------------------------
# UI widgets


class PathEntry(Gtk.Entry):
    """Simple entry used for the editable pathbar."""

    def __init__(self) -> None:
        super().__init__()
        # Don't set hexpand here - we'll set it explicitly in the toolbar
        # self.set_hexpand(True)
        self.set_placeholder_text("/remote/path")
        # Remove minimum width constraint to allow full expansion
        # self.set_size_request(200, -1)  # Commented out to allow full width


class PaneControls(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.set_valign(Gtk.Align.CENTER)
        self.back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self.up_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.new_folder_button = Gtk.Button.new_from_icon_name("folder-new-symbolic")
        for widget in (
            self.back_button,
            self.up_button,
            self.refresh_button,
            self.new_folder_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)
        for widget in (self.back_button, self.up_button, self.refresh_button, self.new_folder_button):
            widget.add_css_class("flat")
        self.append(self.back_button)
        self.append(self.up_button)
        self.append(self.refresh_button)
        self.append(self.new_folder_button)


class PaneToolbar(Gtk.Box):
    __gsignals__ = {
        "view-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        # Build a custom top bar: WindowHandle -> Box [ left | ENTRY (expands) | right ]
        handle = Gtk.WindowHandle()                    # gives draggable area like a headerbar
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        handle.set_child(bar)

        # Left side (compact)
        self._pane_label = Gtk.Label()
        self._pane_label.set_css_classes(["title"])
        self.controls = PaneControls()
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        left.set_margin_start(12)  # Add margin before Remote/Local labels
        left.append(self._pane_label)
        left.append(self.controls)
        bar.append(left)

        # Entry (fills all remaining space)
        self.path_entry = PathEntry()
        self.path_entry.set_hexpand(True)
        self.path_entry.set_halign(Gtk.Align.FILL)
        self.path_entry.set_width_chars(0)
        self.path_entry.set_max_width_chars(0)
        bar.append(self.path_entry)

        # Right side (compact, flush-right)
        right = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._current_view = "list"
        self.sort_split_button = self._create_sort_split_button()
        right.append(self.sort_split_button)
        bar.append(right)

        # Wrap the bar in ToolbarView so it looks native
        tv = Adw.ToolbarView()
        tv.add_top_bar(handle)
        # NOTE: Put your pane's main scroller/content with tv.set_content(content) elsewhere.
        self.append(tv)

    # Keep your factory
    def _create_sort_split_button(self) -> Adw.SplitButton:
        menu_model = Gio.Menu()
        sort_section = Gio.Menu()
        sort_section.append("Name", "pane.sort-by-name")
        sort_section.append("Size", "pane.sort-by-size")
        sort_section.append("Modified", "pane.sort-by-modified")
        menu_model.append_section("Sort by", sort_section)
        direction_section = Gio.Menu()
        direction_section.append("Ascending", "pane.sort-direction-asc")
        direction_section.append("Descending", "pane.sort-direction-desc")
        menu_model.append_section("Order", direction_section)
        split_button = Adw.SplitButton()
        split_button.set_menu_model(menu_model)
        split_button.set_tooltip_text("Toggle view mode")
        split_button.set_dropdown_tooltip("Sort files and folders")
        split_button.set_icon_name("view-list-symbolic")
        split_button.connect("clicked", self._on_view_toggle_clicked)
        return split_button

    # Example handler
    def _on_view_toggle_clicked(self, *_):
        self._current_view = "grid" if self._current_view == "list" else "list"
        self.sort_split_button.set_icon_name("view-grid-symbolic" if self._current_view == "grid" else "view-list-symbolic")
        self.emit("view-changed", self._current_view)
    
    def get_header_bar(self):
        """Get the actual header bar for toolbar view."""
        return None  # No longer using Adw.HeaderBar


# ---------- Helper functions for properties dialog ----------
def _human_size(n: int) -> str:
    """Convert bytes to human readable format."""
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if n < 1024 or unit == "PB":
            return f"{n:.0f} {unit}" if n >= 10 or unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return "0 B"


def _human_time(ts: float) -> str:
    """Convert timestamp to human readable format."""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _mode_to_str(mode: int) -> str:
    """Convert file mode to string representation like -rw-r--r--."""
    is_dir = "d" if stat.S_ISDIR(mode) else "-"
    perm = ""
    for who, shift in (("USR", 6), ("GRP", 3), ("OTH", 0)):
        r = "r" if mode & (4 << shift) else "-"
        w = "w" if mode & (2 << shift) else "-"
        x = "x" if mode & (1 << shift) else "-"
        perm += r + w + x
    return is_dir + perm


class PropertiesDialog(Adw.Window):
    """Nautilus-style properties dialog using card-based design."""
    __gtype_name__ = "PropertiesDialog"

    def __init__(self, entry: "FileEntry", current_path: str, parent: Gtk.Window):
        super().__init__()
        self._entry = entry
        self._current_path = current_path
        self._parent_window = parent
        self.set_title("Properties")
        
        # Set window properties
        self.set_default_size(400, 500)
        self.set_resizable(True)
        self.set_modal(True)
        self.set_transient_for(parent)
        
        # Position window relative to parent
        if parent:
            try:
                # Get parent window position and size
                parent_alloc = parent.get_allocation()
                parent_width = parent_alloc.width
                parent_height = parent_alloc.height
                
                # Center the dialog on the parent window
                # For GTK4, we'll let the window manager handle positioning
                # The modal and transient_for properties should handle this
            except Exception:
                # Fallback: let window manager handle positioning
                pass

        # Build the dialog content
        self._build_dialog()

    def _build_dialog(self) -> None:
        """Build the Nautilus-style properties dialog content."""
        # Create AdwToolbarView as the main content (proper Adw.Window structure)
        toolbar_view = Adw.ToolbarView()
        
        # Create proper header bar for dragging
        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label="Properties"))
        
        # Add header bar to toolbar view
        toolbar_view.add_top_bar(header_bar)
        
        # Main content box
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                         margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        
        # Header with icon and name
        content.append(self._create_header_block())
        
        # Parent folder row
        content.append(self._create_parent_folder_row())
        
        # Size row
        content.append(self._create_size_row())
        
        # Modified and Created rows
        content.append(self._create_modified_row())
        content.append(self._create_created_row())
        
        # Permissions row
        content.append(self._create_permissions_row())
        
        # Set content in toolbar view
        toolbar_view.set_content(content)
        
        # Set the toolbar view as the window content
        self.set_content(toolbar_view)


    def _create_header_block(self) -> Gtk.Widget:
        """Create the header block with icon, name, and summary."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER)
        
        # Icon
        if self._entry.is_dir:
            icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        else:
            icon = Gtk.Image.new_from_icon_name("text-x-generic-symbolic")
        # Set a larger custom size instead of using predefined sizes
        icon.set_pixel_size(64)
        icon.add_css_class("icon-dropshadow")
        icon.add_css_class("card")
        box.append(icon)
        
        # Name (centered, bold)
        name_label = Gtk.Label(label=self._entry.name)
        name_label.add_css_class("title-3")
        box.append(name_label)
        
        # Summary
        summary_parts = []
        if self._entry.is_dir:
            if self._entry.item_count is not None:
                summary_parts.append(f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''}")
            else:
                summary_parts.append("Folder")
        else:
            if self._entry.size:
                summary_parts.append(_human_size(self._entry.size))
        
        # Add free space for local files
        if not self._is_remote_file():
            try:
                path = os.path.join(self._current_path, self._entry.name)
                if os.path.exists(path):
                    stat = os.statvfs(path)
                    free = stat.f_bavail * stat.f_frsize
                    summary_parts.append(f"{_human_size(free)} Free")
            except Exception:
                pass
        
        summary_text = " — ".join(summary_parts) if summary_parts else ""
        summary_label = Gtk.Label(label=summary_text)
        summary_label.add_css_class("dim-label")
        box.append(summary_label)
        
        return box

    def _create_size_row(self) -> Gtk.Widget:
        """Create the size row."""
        if self._entry.is_dir:
            if self._entry.item_count is not None:
                size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''}"
                # For local folders, start calculating actual size
                if not self._is_remote_file():
                    size_text += " (calculating size...)"
                    self._start_folder_size_calculation()
            else:
                size_text = "—"
        else:
            size_text = _human_size(self._entry.size) if self._entry.size else "—"
        
        # Store reference to size row for updating
        self._size_row = Adw.ActionRow(title="Size", subtitle=size_text)
        self._size_row.add_css_class("card")
        return self._size_row

    def _create_parent_folder_row(self) -> Gtk.Widget:
        """Create the parent folder row."""
        parent_path = os.path.dirname(os.path.join(self._current_path, self._entry.name))
        if not parent_path:
            parent_path = "/"
        
        row = Adw.ActionRow(title="Parent Folder", subtitle=parent_path)
        row.add_css_class("card")
        
        # Add folder open button for local files
        if not self._is_remote_file():
            btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_open_parent)
            row.add_suffix(btn)
            row.set_activatable_widget(btn)
        
        return row

    def _create_modified_row(self) -> Gtk.Widget:
        """Create the modified date row."""
        modified_time = _human_time(self._entry.modified) if self._entry.modified else "—"
        row = Adw.ActionRow(title="Modified", subtitle=modified_time)
        row.add_css_class("card")
        return row

    def _create_created_row(self) -> Gtk.Widget:
        """Create the created date row (if available)."""
        # For remote files, we typically don't have creation time
        if self._is_remote_file():
            return Gtk.Box()  # Empty box widget
        
        # Try to get creation time for local files
        try:
            path = os.path.join(self._current_path, self._entry.name)
            if os.path.exists(path):
                stat_result = os.stat(path)
                if hasattr(stat_result, 'st_birthtime'):  # macOS
                    created_time = _human_time(stat_result.st_birthtime)
                elif hasattr(stat_result, 'st_ctime'):  # Linux
                    created_time = _human_time(stat_result.st_ctime)
                else:
                    return Gtk.Box()  # Empty box widget
            else:
                return Gtk.Box()  # Empty box widget
        except Exception:
            return Gtk.Box()  # Empty box widget
        
        row = Adw.ActionRow(title="Created", subtitle=created_time)
        row.add_css_class("card")
        return row

    def _create_permissions_row(self) -> Gtk.Widget:
        """Create the permissions row."""
        # Get actual permissions for local files
        if not self._is_remote_file():
            try:
                path = os.path.join(self._current_path, self._entry.name)
                if os.path.exists(path):
                    stat_result = os.stat(path)
                    mode = stat_result.st_mode
                    perms_text = _mode_to_str(mode)
                else:
                    perms_text = "—"
            except Exception:
                perms_text = "—"
        else:
            # For remote files, show simplified permissions
            if self._entry.is_dir:
                perms_text = "Create and Delete Files"
            else:
                perms_text = "Read and Write"
        
        row = Adw.ActionRow(title="Permissions", subtitle=perms_text)
        row.add_css_class("card")
        
        return row

    def _is_remote_file(self) -> bool:
        """Check if this is a remote file (from SFTP)."""
        # Simple heuristic - in a real implementation, you'd pass connection info
        return "://" in self._current_path or (self._current_path.startswith("/") and 
                not os.path.exists(os.path.join(self._current_path, self._entry.name)))

    def _on_open_parent(self, *_) -> None:
        """Open parent directory in system file manager."""
        try:
            if not self._is_remote_file():
                parent_dir = os.path.dirname(os.path.join(self._current_path, self._entry.name))
                if os.path.exists(parent_dir):
                    Gio.AppInfo.launch_default_for_uri(f"file://{parent_dir}", None)
        except Exception:
            pass

    def _start_folder_size_calculation(self):
        """Start calculating folder size in background thread."""
        import threading
        
        folder_path = os.path.join(self._current_path, self._entry.name)
        
        # Create and start the background thread
        thread = threading.Thread(target=self._calculate_folder_size, args=(folder_path,))
        thread.daemon = True  # Allows main program to exit even if thread is running
        thread.start()

    def _calculate_folder_size(self, path):
        """
        Recursively calculates the size of a folder.
        THIS RUNS ON A BACKGROUND THREAD.
        """
        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    # Skip if it is a symlink or file doesn't exist
                    if not os.path.islink(fp):
                        try:
                            total_size += os.path.getsize(fp)
                        except FileNotFoundError:
                            # File might have been deleted while scanning
                            pass
                        except OSError:
                            # Permissions error, etc.
                            pass

        except Exception:
            total_size = -1  # Use a negative value to signal an error

        # When done, schedule the UI update on the main GTK thread
        GLib.idle_add(self._update_folder_size_ui, total_size)
        
    def _update_folder_size_ui(self, total_size):
        """
        Updates the size row with the final folder size.
        THIS RUNS ON THE MAIN GTK THREAD.
        """
        if hasattr(self, '_size_row') and self._size_row:
            if total_size >= 0:
                human_readable_size = _human_size(total_size)
                if self._entry.item_count is not None:
                    size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''} ({human_readable_size})"
                else:
                    size_text = human_readable_size
            else:
                if self._entry.item_count is not None:
                    size_text = f"{self._entry.item_count} item{'s' if self._entry.item_count != 1 else ''} (size unavailable)"
                else:
                    size_text = "Size unavailable"
            
            self._size_row.set_subtitle(size_text)
            
        # Returning GLib.SOURCE_REMOVE ensures this function only runs once
        return GLib.SOURCE_REMOVE


class FilePane(Gtk.Box):
    """Represents a single pane in the manager."""

    _TYPEAHEAD_TIMEOUT = 1.0

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.toolbar = PaneToolbar()
        self.toolbar._pane_label.set_text(label)
        self.append(self.toolbar)

        self._is_remote = label.lower() == "remote"

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._list_store = Gio.ListStore(item_type=Gtk.StringObject)
        self._selection_model = Gtk.MultiSelection.new(self._list_store)

        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", self._on_list_setup)
        list_factory.connect("bind", self._on_list_bind)
        list_view = Gtk.ListView(model=self._selection_model, factory=list_factory)
        list_view.add_css_class("rich-list")
        list_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        # Navigate on row activation (double click / Enter)
        self._list_view = list_view
        list_view.connect("activate", self._on_list_activate)
        self._list_drag_source = Gtk.DragSource()
        self._list_drag_source.set_actions(Gdk.DragAction.COPY)
        self._list_drag_source.connect("prepare", self._on_drag_prepare)
        self._list_drag_source.connect("drag-begin", self._on_drag_begin)
        self._list_drag_source.connect("drag-end", self._on_drag_end)
        list_view.add_controller(self._list_drag_source)

        # Wrap list view in a scrolled window for proper scrolling
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_child(list_view)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=6,
        )
        grid_view.set_enable_rubberband(True)
        grid_view.add_css_class("iconview")
        grid_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        self._grid_view = grid_view
        # Navigate on grid item activation (double click / Enter)
        grid_view.connect("activate", self._on_grid_activate)
        self._grid_drag_source = Gtk.DragSource()
        self._grid_drag_source.set_actions(Gdk.DragAction.COPY)
        self._grid_drag_source.connect("prepare", self._on_drag_prepare)
        self._grid_drag_source.connect("drag-begin", self._on_drag_begin)
        self._grid_drag_source.connect("drag-end", self._on_drag_end)
        grid_view.add_controller(self._grid_drag_source)

        # Wrap grid view in a scrolled window for proper scrolling
        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scrolled.set_child(grid_view)

        self._stack.add_named(list_scrolled, "list")
        self._stack.add_named(grid_scrolled, "grid")

        _ensure_drop_zone_css()

        overlay = Adw.ToastOverlay()
        self._overlay = overlay

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self._stack)

        drop_zone_revealer = Gtk.Revealer()
        drop_zone_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        drop_zone_revealer.set_halign(Gtk.Align.CENTER)
        drop_zone_revealer.set_valign(Gtk.Align.END)
        if hasattr(drop_zone_revealer, "set_can_target"):
            drop_zone_revealer.set_can_target(False)

        drop_zone_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
        )
        drop_zone_box.set_hexpand(False)
        drop_zone_box.set_vexpand(False)
        drop_zone_box.set_halign(Gtk.Align.CENTER)
        drop_zone_box.set_valign(Gtk.Align.CENTER)
        drop_zone_box.set_margin_top(12)
        drop_zone_box.set_margin_bottom(24)
        drop_zone_box.set_margin_start(16)
        drop_zone_box.set_margin_end(16)
        if hasattr(drop_zone_box, "set_can_target"):
            drop_zone_box.set_can_target(False)
        drop_zone_box.add_css_class("file-pane-drop-zone")

        # Create a compact horizontal layout
        icon_name = "folder-upload-symbolic" if self._is_remote else "folder-download-symbolic"
        drop_zone_icon = Gtk.Image.new_from_icon_name(icon_name)
        drop_zone_icon.set_pixel_size(24)
        drop_zone_icon.add_css_class("accent")
        drop_zone_box.append(drop_zone_icon)

        drop_zone_title = Gtk.Label()
        drop_zone_title.set_text("Drop files to upload" if self._is_remote else "Drop items here")
        drop_zone_title.set_justify(Gtk.Justification.CENTER)
        drop_zone_title.set_halign(Gtk.Align.CENTER)
        drop_zone_title.add_css_class("drop-zone-title")
        drop_zone_title.add_css_class("heading")
        drop_zone_box.append(drop_zone_title)

        drop_zone_revealer.set_child(drop_zone_box)
        drop_zone_revealer.set_reveal_child(False)

        content_overlay.add_overlay(drop_zone_revealer)
        overlay.set_child(content_overlay)
        self.append(overlay)

        self._drop_zone_revealer = drop_zone_revealer
        self._drop_zone_box = drop_zone_box
        self._drop_zone_visible = False
        self._drop_zone_forced = False
        self._drop_zone_pointer = False
        self._partner_pane: Optional["FilePane"] = None
        self._drop_target: Optional[Gtk.DropTarget] = None
        self._drag_sources: List[Gtk.DragSource] = []
        self._current_drag_file: Optional[Gio.File] = None

        self._action_buttons: Dict[str, Gtk.Button] = {}
        action_bar = Gtk.ActionBar()
        action_bar.add_css_class("inline-toolbar")

        def _create_action_button(
            name: str,
            icon_name: str,
            label: str,
            callback: Callable[[Gtk.Button], None],
        ) -> Gtk.Button:
            button = Gtk.Button()
            
            # Only upload and download buttons get text labels
            if name in ["upload", "download"]:
                content = Adw.ButtonContent()
                content.set_icon_name(icon_name)
                content.set_label(label)
                button.set_child(content)
            else:
                # Icon-only buttons for other actions
                button.set_icon_name(icon_name)
                button.set_tooltip_text(label)
            
            # Improve button alignment and styling
            button.set_valign(Gtk.Align.CENTER)
            button.set_has_frame(False)
            button.add_css_class("flat")
            
            button.connect("clicked", callback)
            self._action_buttons[name] = button
            return button

        download_button = _create_action_button(
            "download",
            "document-save-symbolic",
            "Download",
            lambda _button: self._on_download_clicked(_button),
        )
        upload_button = _create_action_button(
            "upload",
            "document-send-symbolic",
            "Upload",
            lambda _button: self._on_upload_clicked(_button),
        )
        rename_button = _create_action_button(
            "rename",
            "document-edit-symbolic",
            "Rename",
            lambda _button: self._emit_entry_operation("rename"),
        )
        delete_button = _create_action_button(
            "delete",
            "user-trash-symbolic",
            "Delete",
            lambda _button: self._emit_entry_operation("delete"),
        )
        download_button.set_visible(self._is_remote)
        upload_button.set_visible(not self._is_remote)

        action_bar.pack_start(upload_button)
        action_bar.pack_start(download_button)
        action_bar.pack_end(delete_button)
        action_bar.pack_end(rename_button)

        self._action_bar = action_bar
        self.append(action_bar)

        # Connect to view-changed signal from toolbar
        self.toolbar.connect("view-changed", self._on_view_toggle)
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        # Wire navigation buttons
        self.toolbar.controls.up_button.connect("clicked", self._on_up_clicked)
        self.toolbar.controls.back_button.connect("clicked", self._on_back_clicked)
        self.toolbar.controls.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )
        # Upload/download functionality is now available through action bar and context menu only

        self._history: List[str] = []
        self._current_path = "/"
        self._entries: List[FileEntry] = []
        self._cached_entries: List[FileEntry] = []
        self._raw_entries: List[FileEntry] = []
        self._show_hidden = False
        self._sort_key = "name"  # Default sort by name
        self._sort_descending = False  # Default ascending order
        self._drag_in_progress = False
        self._drag_payload: Optional[object] = None

        self._suppress_history_push: bool = False
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        self._menu_actions: Dict[str, Gio.SimpleAction] = {}
        self._menu_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._menu_action_group)
        self._menu_popover: Gtk.PopoverMenu = self._create_menu_model()
        self._add_context_controller(list_view)
        self._add_context_controller(grid_view)

        for view in (list_view, grid_view):
            controller = Gtk.EventControllerKey.new()
            controller.connect("key-pressed", self._on_typeahead_key_pressed)
            view.add_controller(controller)
            self._attach_shortcuts(view)

        # Drag and drop controllers – these provide the visual affordance and
        # forward requests to the window which understands the context.
        # Accept multiple formats: Gio.File, text/uri-list, and text/plain
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop_target.set_gtypes([Gio.File, GObject.TYPE_STRING])
        drop_target.connect("accept", self._on_drop_accept)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("motion", self._on_drop_motion)
        drop_target.connect("leave", self._on_drop_leave)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)
        self._drop_target = drop_target

        # Set up drag sources for both local and remote panes
        self._setup_drag_sources((list_view, grid_view))

        self._update_menu_state()
        # Set up sorting actions for the split button
        self._setup_sorting_actions()
        
        # Initialize view button icon and direction states
        self._update_view_button_icon()
        self._update_sort_direction_states()

        self._typeahead_buffer: str = ""
        self._typeahead_last_time: float = 0.0

    # -- drop zone & drag support -------------------------------------

    def set_partner_pane(self, partner: Optional["FilePane"]) -> None:
        self._partner_pane = partner

    def show_drop_zone(self) -> None:
        self._set_drop_zone_forced(True)

    def hide_drop_zone(self) -> None:
        self._set_drop_zone_forced(False)

    def _set_drop_zone_forced(self, forced: bool) -> None:
        if getattr(self, "_drop_zone_forced", False) == forced:
            return
        self._drop_zone_forced = forced
        self._update_drop_zone_visibility()

    def _set_drop_zone_pointer(self, active: bool) -> None:
        if getattr(self, "_drop_zone_pointer", False) == active:
            return
        self._drop_zone_pointer = active
        self._update_drop_zone_visibility()

    def _update_drop_zone_visibility(self) -> None:
        revealer = getattr(self, "_drop_zone_revealer", None)
        if revealer is None:
            return
        should_show = bool(getattr(self, "_drop_zone_forced", False) or getattr(self, "_drop_zone_pointer", False))
        if getattr(self, "_drop_zone_visible", False) == should_show:
            return
        self._drop_zone_visible = should_show
        try:
            revealer.set_reveal_child(should_show)
        except Exception:
            pass
        box = getattr(self, "_drop_zone_box", None)
        if box is not None and hasattr(box, "add_css_class") and hasattr(box, "remove_css_class"):
            try:
                if should_show:
                    box.add_css_class("visible")
                else:
                    box.remove_css_class("visible")
            except Exception:
                pass

    def _setup_drag_sources(self, views: Iterable[Gtk.Widget]) -> None:
        for view in views:
            drag_source = Gtk.DragSource()
            drag_source.set_actions(Gdk.DragAction.COPY)
            drag_source.connect("prepare", self._on_drag_prepare)
            drag_source.connect("drag-begin", self._on_drag_source_begin)
            drag_source.connect("drag-end", self._on_drag_source_end)
            try:
                drag_source.connect("drag-cancel", self._on_drag_source_cancel)
            except (TypeError, AttributeError):
                pass
            view.add_controller(drag_source)
            self._drag_sources.append(drag_source)

    def _on_drag_prepare(self, _source: Gtk.DragSource, _x: float, _y: float):
        print(f"=== DRAG PREPARE CALLED on {'remote' if self._is_remote else 'local'} pane ===")
        
        try:
            entries = self.get_selected_entries()
            print(f"Selected entries: {[e.name for e in entries] if entries else 'None'}")
            if not entries:
                print("No entries selected, returning None")
                return None

            window = self.get_root()
            print(f"Window type: {type(window)}")
            if not isinstance(window, FileManagerWindow):
                print("No FileManagerWindow found, returning None")
                return None
                
            print(f"Window is FileManagerWindow: {isinstance(window, FileManagerWindow)}")
            
        except Exception as e:
            print(f"Exception in drag prepare early checks: {e}")
            import traceback
            traceback.print_exc()
            return None

        if self._is_remote:
            # For remote panes, create a string content provider
            # According to GTK4 docs, we need to use proper GType formats
            file_names = [entry.name for entry in entries]
            payload = "\n".join(file_names)
            print(f"Creating remote drag payload: {payload}")
            
            try:
                # Method 1: Try new_typed if available (GTK 4.6+)
                if hasattr(Gdk.ContentProvider, 'new_typed'):
                    provider = Gdk.ContentProvider.new_typed(GObject.TYPE_STRING, payload)
                    if provider is not None:
                        print(f"Created new_typed string provider for remote files: {file_names}")
                        return provider
                
                # Method 2: Use new_for_value with proper GValue
                value = GObject.Value()
                value.init(GObject.TYPE_STRING)
                value.set_string(payload)
                provider = Gdk.ContentProvider.new_for_value(value)
                if provider is not None:
                    print(f"Created GValue string provider for remote files: {file_names}")
                    return provider
                    
                print("Both typed methods failed, trying fallback")
                
            except Exception as e:
                print(f"Error creating typed content provider: {e}")
            
            # Fallback: Use text/plain MIME type with bytes
            try:
                data = GLib.Bytes.new(payload.encode("utf-8"))
                provider = Gdk.ContentProvider.new_for_bytes("text/plain", data)
                if provider is not None:
                    print(f"Created text/plain bytes provider for remote files: {file_names}")
                    return provider
                print("text/plain provider creation failed")
                
            except Exception as e:
                print(f"Error creating bytes content provider: {e}")
                
            return None
        else:
            # For local panes, create URI list as before
            base_dir = window._normalize_local_path(self.toolbar.path_entry.get_text())
            uris: List[str] = []
            files: List[Gio.File] = []
            for entry in entries:
                local_path = os.path.join(base_dir, entry.name)
                if not os.path.exists(local_path):
                    continue
                try:
                    gfile = Gio.File.new_for_path(local_path)
                except Exception:
                    continue
                uri = gfile.get_uri()
                if not uri:
                    continue
                uris.append(uri)
                files.append(gfile)

            if not uris:
                return None

            # Create content provider for URI list
            try:
                # Use GLib.Bytes for proper data handling
                payload = ("\r\n".join(uris) + "\r\n").encode("utf-8")
                data = GLib.Bytes.new(payload)
                
                # Create content provider with proper MIME type
                provider = Gdk.ContentProvider.new_for_bytes("text/uri-list", data)
                if provider is None:
                    return None
                    
            except Exception as e:
                print(f"Error creating drag content provider: {e}")
                return None

            self._current_drag_file = files[0] if files else None
            return provider

    def _on_drag_source_begin(self, _source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        print(f"Drag begin from {'remote' if self._is_remote else 'local'} pane")
        
        # Check if we have selected entries
        selected = self.get_selected_entries()
        print(f"Selected entries at drag begin: {[e.name for e in selected] if selected else 'None'}")
        
        partner = getattr(self, "_partner_pane", None)
        if partner is not None:
            print(f"Showing drop zone on {'remote' if partner._is_remote else 'local'} partner pane")
            partner.show_drop_zone()
        else:
            print("No partner pane found!")

        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._register_drag_begin(self)


    def _on_drag_source_end(self, _source: Gtk.DragSource, _drag: Gdk.Drag, _delete: bool) -> None:
        print(f"Drag end from {'remote' if self._is_remote else 'local'} pane, delete={_delete}")
        self._current_drag_file = None
        partner = getattr(self, "_partner_pane", None)
        if partner is not None:
            partner.hide_drop_zone()
            # Also reset pointer state to ensure drop zone hides
            partner._set_drop_zone_pointer(False)

        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._register_drag_finish(self)


    def _on_drag_source_cancel(self, _source: Gtk.DragSource, _drag: Gdk.Drag, _reason) -> None:
        print(f"Drag cancel from {'remote' if self._is_remote else 'local'} pane, reason={_reason}")
        self._current_drag_file = None
        partner = getattr(self, "_partner_pane", None)
        if partner is not None:
            partner.hide_drop_zone()
            # Also reset pointer state to ensure drop zone hides
            partner._set_drop_zone_pointer(False)

        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._register_drag_finish(self)


    def _on_drop_accept(self, target: Gtk.DropTarget, drop: Gdk.Drop) -> bool:
        """Accept drops that contain files, URI lists, or plain text."""
        try:
            formats = drop.get_formats()
            print(f"Drop accept check on {'remote' if self._is_remote else 'local'} pane")
            print(f"Available formats: {[formats.to_string()]}")
            
            has_file = formats.contain_gtype(Gio.File)
            has_uri_list = formats.contain_mime_type("text/uri-list")
            has_plain_text = formats.contain_mime_type("text/plain")
            has_string = formats.contain_gtype(GObject.TYPE_STRING)
            
            print(f"Format check: File={has_file}, URI-list={has_uri_list}, Plain-text={has_plain_text}, String={has_string}")
            
            result = has_file or has_uri_list or has_plain_text or has_string
            print(f"Drop accept result: {result}")
            return result
        except Exception as e:
            print(f"Drop accept error: {e}")
            return False

    def _on_drop_enter(self, _target: Gtk.DropTarget, _x: float, _y: float):
        print(f"Drop enter on {'remote' if self._is_remote else 'local'} pane")
        self._set_drop_zone_pointer(True)
        return Gdk.DragAction.COPY

    def _on_drop_motion(self, _target: Gtk.DropTarget, _x: float, _y: float):
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, _target: Gtk.DropTarget) -> None:
        print(f"Drop leave on {'remote' if self._is_remote else 'local'} pane")
        self._set_drop_zone_pointer(False)
        # Ensure drop zone is hidden when drag leaves
        if not getattr(self, "_drop_zone_forced", False):
            self.hide_drop_zone()

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

        delete_triggers = [
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType.SHIFT_MASK),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType.SHIFT_MASK),
        ]
        for trigger in delete_triggers:
            add_shortcut(trigger, self._shortcut_delete)

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

    def _on_view_toggle(self, toolbar, view_name: str) -> None:
        self._stack.set_visible_child_name(view_name)
        # Update the split button icon to reflect current view
        self._update_view_button_icon()

    def _on_path_entry(self, entry: Gtk.Entry) -> None:
        self.emit("path-changed", entry.get_text() or "/")

    def _on_list_setup(self, factory: Gtk.SignalListItemFactory, item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = Gtk.Image.new_from_icon_name("folder-symbolic")
        icon.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(xalign=0)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name_label.set_max_width_chars(40)
        name_label.set_hexpand(True)
        metadata_label = Gtk.Label(xalign=1)
        metadata_label.set_halign(Gtk.Align.END)
        metadata_label.set_ellipsize(Pango.EllipsizeMode.END)
        metadata_label.add_css_class("dim-label")
        box.append(icon)
        box.append(name_label)
        box.append(metadata_label)
        box.set_hexpand(True)
        # Store references as Python attributes instead of deprecated set_data
        box.icon = icon
        box.name_label = name_label
        box.metadata_label = metadata_label
        item.set_child(box)

    def _on_list_bind(self, factory, item):
        box = item.get_child()
        # Access references as Python attributes instead of deprecated get_data
        icon: Gtk.Image = box.icon
        name_label: Gtk.Label = box.name_label
        metadata_label: Gtk.Label = box.metadata_label

        position = item.get_position()
        entry: Optional[FileEntry] = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]

        if entry is None:
            value = item.get_item().get_string()
            name_label.set_text(value)
            name_label.set_tooltip_text(value)
            metadata_label.set_text("—")
            metadata_label.set_tooltip_text(None)
            icon.set_from_icon_name("folder-symbolic" if value.endswith('/') else "text-x-generic-symbolic")
            return

        display_name = entry.name + ("/" if entry.is_dir else "")
        name_label.set_text(display_name)
        name_label.set_tooltip_text(display_name)

        if entry.is_dir:
            if entry.item_count is not None:
                count_text = f"{entry.item_count} items"
                metadata_label.set_text(count_text)
                metadata_label.set_tooltip_text(count_text)
            else:
                metadata_label.set_text("—")
                metadata_label.set_tooltip_text(None)
        else:
            size_text = self._format_size(entry.size)
            metadata_label.set_text(size_text)
            metadata_label.set_tooltip_text(size_text)

        if entry.is_dir:
            icon.set_from_icon_name("folder-symbolic")
        else:
            icon.set_from_icon_name("text-x-generic-symbolic")

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        units = ["KB", "MB", "GB", "TB", "PB"]
        value = float(size_bytes)
        for unit in units:
            value /= 1024.0
            if value < 1024.0:
                return f"{value:.1f} {unit}"
        return f"{value:.1f} EB"

    def _on_grid_setup(self, factory, item):
        button = Gtk.Button()
        button.set_has_frame(False)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)

        image = Gtk.Image.new_from_icon_name("folder-symbolic")
        image.set_pixel_size(64)
        image.set_halign(Gtk.Align.CENTER)
        content.append(image)

        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(2)
        content.append(label)

        button.set_child(content)
        item.set_child(button)

    def _on_grid_bind(self, factory, item):
        # Grid view uses the same icon for now but honours the entry name as
        # tooltip so users can differentiate.
        button = item.get_child()
        content = button.get_child()
        image = content.get_first_child()
        label = content.get_last_child()

        value = item.get_item().get_string()
        display_text = value[:-1] if value.endswith('/') else value

        label.set_text(display_text)
        label.set_tooltip_text(display_text)
        button.set_tooltip_text(display_text)

        # Update the image icon based on type
        if value.endswith('/'):
            image.set_from_icon_name("folder-symbolic")
        else:
            image.set_from_icon_name("text-x-generic-symbolic")

    def _on_selection_changed(self, model, position, n_items):
        self._update_menu_state()

    def _setup_sorting_actions(self) -> None:
        """Set up sorting actions for the split button menu."""
        # Create actions for sorting
        self._menu_actions["sort-by-name"] = Gio.SimpleAction.new("sort-by-name", None)
        self._menu_actions["sort-by-size"] = Gio.SimpleAction.new("sort-by-size", None)
        self._menu_actions["sort-by-modified"] = Gio.SimpleAction.new("sort-by-modified", None)
        
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

    def _on_sort_direction(self, descending: bool) -> None:
        """Handle sort direction selection from menu."""
        if self._sort_descending != descending:
            self._sort_descending = descending
            self._refresh_sorted_entries(preserve_selection=True)
            self._update_sort_direction_states()

    def _update_view_button_icon(self) -> None:
        """Update the split button icon based on current view mode."""
        # Check which view is currently active
        if hasattr(self.toolbar, '_current_view') and self.toolbar._current_view == "list":
            icon = "view-list-symbolic"
        else:
            icon = "view-grid-symbolic"
        
        self.toolbar.sort_split_button.set_icon_name(icon)

    def _update_sort_direction_states(self) -> None:
        """Update the radio button states for sort direction."""
        asc_action = self._menu_actions["sort-direction-asc"]
        desc_action = self._menu_actions["sort-direction-desc"]
        
        asc_action.set_state(GLib.Variant.new_boolean(not self._sort_descending))
        desc_action.set_state(GLib.Variant.new_boolean(self._sort_descending))

    def _create_menu_model(self) -> Gtk.PopoverMenu:
        # Create menu actions first
        def _add_action(name: str, callback: Callable[[], None]) -> None:
            if name not in self._menu_actions:
                action = Gio.SimpleAction.new(name, None)

                def _on_activate(_action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
                    callback()

                action.connect("activate", _on_activate)
                self._menu_action_group.add_action(action)
                self._menu_actions[name] = action

        _add_action("download", self._on_menu_download)
        _add_action("upload", self._on_menu_upload)
        _add_action("rename", lambda: self._emit_entry_operation("rename"))
        _add_action("delete", lambda: self._emit_entry_operation("delete"))
        _add_action("new_folder", lambda: self.emit("request-operation", "mkdir", None))
        _add_action("properties", self._on_menu_properties)

        # Create menu model dynamically based on pane type and selection state
        menu_model = self._create_context_menu_model()

        # Create popover and connect action group
        popover = Gtk.PopoverMenu.new_from_model(menu_model)
        popover.set_has_arrow(True)
        popover.insert_action_group("pane", self._menu_action_group)
        return popover

    def _create_context_menu_model(self) -> Gio.Menu:
        """Create context menu model based on current selection state."""
        menu_model = Gio.Menu()
        
        # Check if items are selected
        try:
            # Check if _entries is initialized
            if not hasattr(self, '_entries') or not self._entries:
                has_selection = False
            else:
                selected_entries = self.get_selected_entries()
                has_selection = len(selected_entries) > 0
        except AttributeError:
            # Handle case where _entries is not initialized yet (during testing)
            has_selection = False
        
        # Add Download/Upload based on pane type and selection
        if self._is_remote and has_selection:
            menu_model.append("Download", "pane.download")
        elif not self._is_remote and has_selection:
            menu_model.append("Upload…", "pane.upload")

        # Add management section only if items are selected
        if has_selection:
            manage_section = Gio.Menu()
            manage_section.append("Rename…", "pane.rename")
            manage_section.append("Delete", "pane.delete")
            menu_model.append_section(None, manage_section)
        
        # Always add Properties (it will be enabled/disabled by _update_menu_state)
        menu_model.append("Properties…", "pane.properties")
        
        # Add New Folder only if no items are selected (this is the main change)
        if not has_selection:
            menu_model.append("New Folder", "pane.new_folder")
        
        return menu_model

    def _add_context_controller(self, widget: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)

        def _on_pressed(_gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            self._show_context_menu(widget, x, y)

        gesture.connect("pressed", _on_pressed)
        widget.add_controller(gesture)

        long_press = Gtk.GestureLongPress()

        def _on_long_press(_gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
            self._show_context_menu(widget, x, y)

        long_press.connect("pressed", _on_long_press)
        widget.add_controller(long_press)

    def _build_drag_payload(self) -> Optional[object]:
        entries = self.get_selected_entries()
        if not entries:
            return None

        if self._is_remote:
            return {
                "type": "remote",
                "directory": self._current_path,
                "entries": [dataclasses.asdict(entry) for entry in entries],
            }

        window = self.get_root()
        base_dir_text = self.toolbar.path_entry.get_text()
        base_dir = base_dir_text or "/"
        if isinstance(window, FileManagerWindow):
            base_dir = window._normalize_local_path(base_dir)
        else:
            base_dir = os.path.abspath(os.path.expanduser(base_dir))

        return [pathlib.Path(os.path.join(base_dir, entry.name)) for entry in entries]

    def _on_drag_prepare(self, drag_source: Gtk.DragSource, _x: float, _y: float):
        print(f"=== OLD DRAG PREPARE CALLED on {'remote' if self._is_remote else 'local'} pane ===")
        
        payload = self._build_drag_payload()
        print(f"Built drag payload: {payload}")
        if payload is None:
            print("No payload built, canceling drag")
            self._drag_payload = None
            try:
                drag_source.drag_cancel()
            except (AttributeError, TypeError):
                try:
                    drag_source.drag_cancel(Gdk.DragCancelReason.NO_TARGET)
                except Exception:
                    pass
            return None

        self._drag_payload = payload
        
        # Create proper content provider instead of PyObject
        if self._is_remote:
            # For remote files, create string content with file names
            try:
                if hasattr(payload, '__iter__') and not isinstance(payload, str):
                    file_names = [getattr(item, 'name', str(item)) for item in payload]
                else:
                    file_names = [str(payload)]
                
                content = "\n".join(file_names)
                print(f"Creating string content provider with: {content}")
                
                # Try multiple methods for string content provider
                try:
                    value = GObject.Value()
                    value.init(GObject.TYPE_STRING)
                    value.set_string(content)
                    provider = Gdk.ContentProvider.new_for_value(value)
                    print("Created GValue string provider")
                    return provider
                except Exception as e:
                    print(f"GValue method failed: {e}")
                    # Fallback to bytes
                    data = GLib.Bytes.new(content.encode("utf-8"))
                    provider = Gdk.ContentProvider.new_for_bytes("text/plain", data)
                    print("Created text/plain bytes provider")
                    return provider
                    
            except Exception as e:
                print(f"Error creating remote content provider: {e}")
                # Final fallback to PyObject (but we'll know why)
                print("Falling back to PyObject provider")
                return Gdk.ContentProvider.new_for_value(
                    GObject.Value(GObject.TYPE_PYOBJECT, payload)
                )
        else:
            # For local files, try to create URI list
            try:
                if hasattr(payload, '__iter__') and not isinstance(payload, str):
                    # Assume payload contains file entries with paths
                    uris = []
                    for item in payload:
                        if hasattr(item, 'name'):
                            # Build full path and convert to URI
                            window = self.get_root()
                            if isinstance(window, FileManagerWindow):
                                base_dir = window._normalize_local_path(self.toolbar.path_entry.get_text())
                                full_path = os.path.join(base_dir, item.name)
                                if os.path.exists(full_path):
                                    gfile = Gio.File.new_for_path(full_path)
                                    uri = gfile.get_uri()
                                    if uri:
                                        uris.append(uri)
                    
                    if uris:
                        uri_list = "\r\n".join(uris) + "\r\n"
                        data = GLib.Bytes.new(uri_list.encode("utf-8"))
                        provider = Gdk.ContentProvider.new_for_bytes("text/uri-list", data)
                        print(f"Created URI list provider with {len(uris)} URIs")
                        return provider
                
                print("Could not create URI list, falling back to PyObject")
                        
            except Exception as e:
                print(f"Error creating local content provider: {e}")
            
            # Fallback to original PyObject method for local
            return Gdk.ContentProvider.new_for_value(
                GObject.Value(GObject.TYPE_PYOBJECT, payload)
            )

    def _on_drag_begin(self, drag_source: Gtk.DragSource, _drag: Gdk.Drag) -> None:
        if self._drag_payload is None:
            try:
                drag_source.drag_cancel()
            except (AttributeError, TypeError):
                try:
                    drag_source.drag_cancel(Gdk.DragCancelReason.NO_TARGET)
                except Exception:
                    pass
            return

        self._drag_in_progress = True

        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._register_drag_begin(self)


    def _on_drag_end(
        self,
        _drag_source: Gtk.DragSource,
        _drag: Optional[Gdk.Drag],
        _delete_data: bool,
    ) -> None:
        self._drag_in_progress = False
        self._drag_payload = None

        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._register_drag_finish(self)


    def _show_context_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        self._update_selection_for_menu(widget, x, y)
        self._update_menu_state()
        try:
            widget.grab_focus()
        except Exception:
            pass
        
        # Create a new menu model based on current selection state
        new_menu_model = self._create_context_menu_model()
        self._menu_popover.set_menu_model(new_menu_model)
        
        # Create a rectangle for the popover positioning
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        
        # Set parent and show popover
        if self._menu_popover.get_parent() != widget:
            self._menu_popover.set_parent(widget)
        
        self._menu_popover.set_pointing_to(rect)
        self._menu_popover.popup()

    def _update_selection_for_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        # In GTK4, we can't easily get the item at a specific position
        # Instead, we'll show the context menu based on the current selection
        # The user should select items first, then right-click for context menu
        # This is actually more consistent with modern file manager behavior
        
        # Keep the current selection as-is for the context menu
        pass

    def _get_selected_indices(self) -> List[int]:
        indices: List[int] = []
        total = len(self._entries)
        if hasattr(self._selection_model, "is_selected"):
            for index in range(total):
                try:
                    if self._selection_model.is_selected(index):
                        indices.append(index)
                except AttributeError:
                    break
        else:
            getter = getattr(self._selection_model, "get_selected", None)
            if callable(getter):
                try:
                    selected_index = getter()
                except Exception:
                    selected_index = None
                if isinstance(selected_index, int) and 0 <= selected_index < total:
                    indices.append(selected_index)
        return indices

    def _get_primary_selection_index(self) -> Optional[int]:
        indices = self._get_selected_indices()
        return indices[0] if indices else None

    def get_selected_entries(self) -> List[FileEntry]:
        return [self._entries[index] for index in self._get_selected_indices()]

    def is_drag_in_progress(self) -> bool:
        return self._drag_in_progress

    def get_drag_payload(self) -> Optional[object]:
        return self._drag_payload

    def _update_menu_state(self) -> None:
        selected_entries = self.get_selected_entries()
        selection_count = len(selected_entries)
        has_selection = selection_count > 0
        single_selection = selection_count == 1

        def _set_enabled(name: str, enabled: bool) -> None:
            action = self._menu_actions.get(name)
            if action is not None:
                action.set_enabled(enabled)

        def _set_button(name: str, enabled: bool) -> None:
            button = self._action_buttons.get(name)
            if button is not None:
                button.set_sensitive(enabled)


        # For context menu, actions are always enabled since menu items are shown/hidden dynamically
        _set_enabled("download", self._is_remote and has_selection)
        _set_enabled("upload", (not self._is_remote) and has_selection)
        _set_enabled("rename", single_selection)
        _set_enabled("delete", has_selection)
        _set_enabled("properties", single_selection)
        # new_folder is available in context menu only now

        # Action bar buttons still use the old logic
        _set_button("download", self._is_remote and has_selection)
        _set_button("upload", (not self._is_remote) and has_selection)
        _set_button("rename", single_selection)
        _set_button("delete", has_selection)

    def _emit_entry_operation(self, action: str) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select at least one item first")
            return
        if action == "rename" and len(entries) != 1:
            self.show_toast("Select a single item to rename")
            return
        payload = {"entries": entries, "directory": self._current_path}
        self.emit("request-operation", action, payload)

    def _on_menu_download(self) -> None:
        if not self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download first")
            return
        self._on_download_clicked(None)

    def _on_menu_upload(self) -> None:
        if self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to upload first")
            return
        self._on_upload_clicked(None)

    def _on_drop(self, target: Gtk.DropTarget, value, x: float, y: float):
        print(f"Drop received on {'remote' if self._is_remote else 'local'} pane")
        print(f"Drop value type: {type(value)}, value: {repr(value)}")
        
        self._set_drop_zone_pointer(False)
        self.hide_drop_zone()
        
        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            origin = window.get_active_drag_source()
            print(f"Drop origin: {'remote' if origin and origin._is_remote else 'local' if origin else 'None'}")
            
            if origin is self:
                print("Ignoring drop on same pane")
                return False

            # Handle remote-to-local file transfers
            if origin and origin._is_remote and not self._is_remote:
                print(f"=== PROCESSING REMOTE-TO-LOCAL DROP ===")
                print(f"Value received: {repr(value)}")
                print(f"Value type: {type(value)}")
                
                # For remote files, the value is a plain text list of filenames
                if isinstance(value, str):
                    file_names = [name.strip() for name in value.strip().split('\n') if name.strip()]
                    print(f"Parsed file names: {file_names}")
                    if file_names:
                        # Get the selected entries from the origin pane
                        selected_entries = origin.get_selected_entries()
                        print(f"Selected entries from origin: {[e.name for e in selected_entries]}")
                        
                        # Get current directory on local pane (destination)
                        local_dir = self.toolbar.path_entry.get_text() or os.path.expanduser("~")
                        destination = pathlib.Path(window._normalize_local_path(local_dir))
                        print(f"Local destination directory: {destination}")
                        
                        # Test if files would conflict
                        for entry in selected_entries:
                            target_path = destination / entry.name
                            exists = target_path.exists()
                            print(f"  {entry.name} -> {target_path} (exists: {exists})")
                        
                        # Create proper download payload
                        payload = {
                            "entries": selected_entries,
                            "destination": destination,
                            "directory": origin.toolbar.path_entry.get_text() or "/"
                        }
                        print(f"Download payload: entries={len(payload['entries'])}, destination={payload['destination']}")
                        
                        # Emit download operation with proper payload
                        print("=== EMITTING DOWNLOAD REQUEST-OPERATION ===")
                        print(f"Payload being emitted: {payload}")
                        self.emit("request-operation", "download", payload)
                        return True
                print("No valid file names found in remote drop")
                return False

        # Handle regular file drops (local-to-remote or external files)
        print("Processing regular file drop")
        file_to_upload = None
        if isinstance(value, Gio.File):
            file_to_upload = value
            print(f"Direct Gio.File: {file_to_upload.get_path()}")
        elif isinstance(value, str):
            # Handle URI list format
            try:
                # Take the first URI from the list
                uri = value.strip().split('\n')[0].strip()
                print(f"Parsing URI: {uri}")
                if uri.startswith('file://'):
                    file_to_upload = Gio.File.new_for_uri(uri)
                    print(f"Created Gio.File from URI: {file_to_upload.get_path()}")
            except Exception as e:
                print(f"Error parsing URI from drop: {e}")
                return False
        
        if file_to_upload is None:
            print("No file to upload found")
            return False

        print("Emitting upload request-operation")
        self.emit("request-operation", "upload", file_to_upload)
        return True

    def get_selected_entry(self) -> Optional[FileEntry]:
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            return None
        return selected_entries[0]

    def _on_upload_clicked(self, _button: Gtk.Button) -> None:
        window = self.get_root()
        if not isinstance(window, FileManagerWindow):
            return

        local_pane = getattr(window, "_left_pane", None)
        if not isinstance(local_pane, FilePane):
            self.show_toast("Local pane is unavailable")
            return

        destination_pane: Optional[FilePane]
        if self._is_remote:
            destination_pane = self
        else:
            destination_pane = getattr(window, "_right_pane", None)
            if not isinstance(destination_pane, FilePane) or not destination_pane._is_remote:
                destination_pane = None

        if destination_pane is None:
            self.show_toast("Remote pane is unavailable")
            return

        entries = local_pane.get_selected_entries()
        if not entries:
            self.show_toast("Select items in the local pane to upload")
            return

        base_dir = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        source_paths = [pathlib.Path(os.path.join(base_dir, entry.name)) for entry in entries]

        destination = destination_pane.toolbar.path_entry.get_text() or "/"
        payload = {"paths": source_paths, "destination": destination}
        self.emit("request-operation", "upload", payload)
        if len(entries) == 1:
            self.show_toast(f"Uploading {entries[0].name}…")
        else:
            self.show_toast(f"Uploading {len(entries)} items…")


    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download")
            return

        window = self.get_root()
        if not isinstance(window, FileManagerWindow):
            return

        local_pane = getattr(window, "_left_pane", None)
        if local_pane is None:
            self.show_toast("Local pane is unavailable")
            return

        destination_root = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        if not os.path.isdir(destination_root):
            self.show_toast("Local destination is not accessible")
            return
        payload = {
            "entries": entries,
            "directory": self._current_path,
            "destination": pathlib.Path(destination_root),
        }
        self.emit("request-operation", "download", payload)
        if len(entries) == 1:
            self.show_toast(f"Downloading {entries[0].name}…")
        else:
            self.show_toast(f"Downloading {len(entries)} items…")

    @staticmethod
    def _dialog_dismissed(error: GLib.Error) -> bool:
        dialog_error = getattr(Gtk, "DialogError", None)
        if dialog_error is not None and error.matches(dialog_error, dialog_error.DISMISSED):
            return True
        return error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)

    def _build_properties_details(self, entry: FileEntry) -> Dict[str, str]:
        base_path = self._current_path or "/"
        location = os.path.join(base_path, entry.name)

        entry_type = "Folder" if entry.is_dir else "File"
        if entry.is_dir:
            size_text = "—"
        else:
            size_text = self._format_size(entry.size)

        try:
            modified_dt = datetime.fromtimestamp(entry.modified)
            modified_text = modified_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError, TypeError):
            modified_text = "Unknown"

        return {
            "name": entry.name,
            "type": entry_type,
            "size": size_text,
            "modified": modified_text,
            "location": location,
        }

    def _on_menu_properties(self) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            self.show_toast("Select a single item to view properties")
            return
        details = self._build_properties_details(entry)
        self._show_properties_dialog(entry, details)

    def _show_properties_dialog(self, entry: FileEntry, details: Dict[str, str]) -> None:
        """Show modern properties dialog."""
        window = self.get_root()
        if window is None:
            return
        
        try:
            # Create and show the modern properties dialog
            dialog = PropertiesDialog(entry, self._current_path, window)
            dialog.present()
        except Exception as e:
            # Fallback to simple message dialog if modern dialog fails
            self._show_fallback_properties_dialog(entry, details, window)

    def _show_fallback_properties_dialog(self, entry: FileEntry, details: Dict[str, str], window: Gtk.Window) -> None:
        """Fallback to simple properties dialog if modern dialog fails."""
        heading = f"{entry.name} Properties" if entry.name else "Properties"
        body_lines = [
            f"Name: {details['name']}",
            f"Type: {details['type']}",
            f"Size: {details['size']}",
            f"Modified: {details['modified']}",
            f"Location: {details['location']}",
        ]
        body_text = "\n".join(body_lines)

        try:
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=heading,
                body=body_text
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()
        except Exception:
            # Final fallback to basic Gtk dialog
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=heading,
                secondary_text=body_text
            )
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()

    # -- public API -----------------------------------------------------

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        self._current_path = path
        self.toolbar.path_entry.set_text(path)
        self._cached_entries = list(entries)
        self._apply_entry_filter(preserve_selection=False)

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
        self._selection_model.select_item(match, False)
        self._scroll_to_position(match)

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
        self._entries = self._sort_entries(self._raw_entries)
        
        # Update the list store
        self._list_store.remove_all()
        restored_selection: List[int] = []
        for idx, entry in enumerate(self._entries):
            suffix = "/" if entry.is_dir else ""
            self._list_store.append(Gtk.StringObject.new(entry.name + suffix))
            if preserve_selection and entry.name in selected_names:
                restored_selection.append(idx)

        self._selection_model.unselect_all()
        for index in restored_selection:
            self._selection_model.select_item(index, False)

        self._update_menu_state()



    # -- navigation helpers --------------------------------------------

    def _on_list_activate(self, _list_view: Gtk.ListView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _sort_entries(self, entries: Iterable[FileEntry]) -> List[FileEntry]:
        def key_func(item: FileEntry):
            if self._sort_key == "size":
                return item.size
            if self._sort_key == "modified":
                return item.modified
            return item.name.casefold()

        dirs = [entry for entry in entries if entry.is_dir]
        files = [entry for entry in entries if not entry.is_dir]

        dirs_sorted = sorted(dirs, key=key_func, reverse=self._sort_descending)
        files_sorted = sorted(files, key=key_func, reverse=self._sort_descending)
        return dirs_sorted + files_sorted

    def _refresh_sorted_entries(self, *, preserve_selection: bool) -> None:
        # Simply re-apply the filter which now includes sorting
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_grid_activate(self, _grid_view: Gtk.GridView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _on_up_clicked(self, _button) -> None:
        parent = os.path.dirname(self._current_path.rstrip('/')) or '/'
        # Avoid navigating past root repeatedly
        if parent != self._current_path:
            self.emit("path-changed", parent)

    def _on_back_clicked(self, _button) -> None:
        prev = self.pop_history()
        if prev:
            # Suppress history push for back navigation
            self._suppress_history_push = True
            self.emit("path-changed", prev)

    def _on_refresh_clicked(self, _button) -> None:
        # Refresh the current directory
        current_path = self._current_path or "/"
        self.emit("path-changed", current_path)

    def push_history(self, path: str) -> None:
        if self._history and self._history[-1] == path:
            return
        self._history.append(path)

    def pop_history(self) -> Optional[str]:
        if len(self._history) > 1:
            self._history.pop()
            return self._history[-1]
        return None

    def show_toast(self, text: str) -> None:
        """Show a toast message safely."""
        try:
            toast = Adw.Toast.new(text)
            self._overlay.add_toast(toast)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    # -- type-ahead search ----------------------------------------------

    def _current_time(self) -> float:
        getter = getattr(GLib, "get_monotonic_time", None)
        if callable(getter):
            try:
                return getter() / 1_000_000
            except Exception:
                pass
        return time.monotonic()

    def _find_prefix_match(self, prefix: str, start_index: int) -> Optional[int]:
        if not prefix or not self._entries:
            return None

        total = len(self._entries)
        if total <= 0:
            return None

        start = 0 if start_index is None else start_index
        if start < 0:
            start = 0

        prefix_casefold = prefix.casefold()
        for offset in range(total):
            index = (start + offset) % total
            if self._entries[index].name.casefold().startswith(prefix_casefold):
                return index
        return None

    def _scroll_to_position(self, position: int) -> None:
        visible = self._stack.get_visible_child_name()
        view: Optional[Gtk.Widget] = None
        if visible == "list":
            view = self._list_view
        elif visible == "grid":
            view = self._grid_view

        if view is None:
            return

        scroll_to = getattr(view, "scroll_to", None)
        if callable(scroll_to):
            flags = getattr(Gtk, "ListScrollFlags", None)
            focus_flag = getattr(flags, "FOCUS", 1) if flags is not None else 1
            try:
                scroll_to(position, focus_flag)
            except Exception:
                pass

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
            | getattr(Gdk.ModifierType, "ALT_MASK", 0)
            | getattr(Gdk.ModifierType, "SUPER_MASK", 0)
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
        if selected is None or selected < 0:
            selected_index = 0
        else:
            selected_index = selected

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

        setter = getattr(self._selection_model, "select_item", None)
        if callable(setter):
            setter(match, True)
        else:
            fallback = getattr(self._selection_model, "set_selected", None)
            if callable(fallback):
                fallback(match)

        self._scroll_to_position(match)
        return True


class FileManagerWindow(Adw.Window):
    """Top-level window hosting two :class:`FilePane` instances."""

    def __init__(
        self,
        application: Adw.Application,
        *,
        host: str,
        username: str,
        port: int = 22,
        initial_path: str = "~",
    ) -> None:
        super().__init__(title="")
        # Set default and minimum sizes following GNOME HIG
        self.set_default_size(1000, 640)
        # Set minimum size to ensure usability (GNOME HIG recommends minimum 360px width)
        self.set_size_request(600, 400)
        # Ensure window is resizable (this is the default, but being explicit)
        self.set_resizable(True)
        # Ensure window decorations are shown (minimize, maximize, close buttons)
        self.set_decorated(True)
        
        # Progress state
        self._current_future: Optional[Future] = None

        # Use ToolbarView like other Adw.Window instances
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        
        # Create header bar with window controls
        header_bar = Adw.HeaderBar()
        header_bar.set_title_widget(Gtk.Label(label=f"{username}@{host}"))
        # Enable window controls (minimize, maximize, close) following GNOME HIG
        header_bar.set_show_start_title_buttons(True)
        header_bar.set_show_end_title_buttons(True)
        
        # Add toggle button to hide/show local pane
        self._local_pane_toggle = Gtk.ToggleButton()
        self._local_pane_toggle.set_icon_name("view-dual-symbolic")
        self._local_pane_toggle.set_tooltip_text("Hide Local Pane")
        self._local_pane_toggle.set_active(False)  # Start unselected
        self._local_pane_toggle.add_css_class("flat")  # Flat style
        self._local_pane_toggle.connect("toggled", self._on_local_pane_toggle)
        header_bar.pack_start(self._local_pane_toggle)
        
        # Create menu button for headerbar
        self._create_headerbar_menu(header_bar)
        
        # Create toast overlay first and set it as toolbar content
        self._toast_overlay = Adw.ToastOverlay()
        self._progress_dialog: Optional[SFTPProgressDialog] = None
        self._connection_error_reported = False
        toolbar_view.set_content(self._toast_overlay)
        toolbar_view.add_top_bar(header_bar)

        # Create the main content area and set it as toast overlay child
        panes = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        panes.set_wide_handle(True)
        # Set position to split evenly by default (50%)
        panes.set_position(500)  # This will be adjusted when window is resized
        # Enable resizing and shrinking for both panes following GNOME HIG
        panes.set_resize_start_child(True)
        panes.set_resize_end_child(True)
        panes.set_shrink_start_child(False)
        panes.set_shrink_end_child(False)
        
        # Set panes as the child of toast overlay
        self._toast_overlay.set_child(panes)

        self._left_pane = FilePane("Local")
        self._right_pane = FilePane("Remote")
        self._left_pane.set_partner_pane(self._right_pane)
        self._right_pane.set_partner_pane(self._left_pane)
        panes.set_start_child(self._left_pane)
        panes.set_end_child(self._right_pane)
        
        # Store reference to panes for resize handling
        self._panes = panes
        
        # Connect to size-allocate to maintain proportional split
        self.connect("notify::default-width", self._on_window_resize)

        # Initialize panes: left is LOCAL home, right is REMOTE home (~)
        self._pending_paths: Dict[FilePane, Optional[str]] = {
            self._left_pane: None,
            self._right_pane: initial_path,
        }
        self._pending_highlights: Dict[FilePane, Optional[str]] = {
            self._left_pane: None,
            self._right_pane: None,
        }

        self._active_drag_source: Optional[FilePane] = None


        # Prime the left (local) pane immediately with local home directory
        try:
            local_home = os.path.expanduser("~")
            self._load_local(local_home)
            self._left_pane.push_history(local_home)
        except Exception as exc:
            self._left_pane.show_toast(f"Failed to load local home: {exc}")

        # Connect pane signals
        for pane in (self._left_pane, self._right_pane):
            pane.connect("path-changed", self._on_path_changed, pane)
            pane.connect("request-operation", self._on_request_operation, pane)

        # Initialize SFTP manager and connect signals
        self._manager = AsyncSFTPManager(host, username, port)
        
        # Connect signals with error handling
        try:
            self._manager.connect("connected", self._on_connected)
            self._manager.connect("connection-error", self._on_connection_error)
            self._manager.connect("progress", self._on_progress)
            self._manager.connect("operation-error", self._on_operation_error)
            self._manager.connect("directory-loaded", self._on_directory_loaded)
        except Exception as exc:
            print(f"Error connecting signals: {exc}")
        
        # Show initial progress before connecting
        try:
            self._show_progress(0.1, "Connecting…")
        except Exception as exc:
            print(f"Error showing progress: {exc}")
        
        # Start connection after everything is set up
        try:
            self._manager.connect_to_server()
        except Exception as exc:
            print(f"Error connecting to server: {exc}")

    # -- signal handlers ------------------------------------------------

    def _register_drag_begin(self, pane: FilePane) -> None:
        self._active_drag_source = pane

    def _register_drag_finish(self, pane: FilePane) -> None:
        if self._active_drag_source is pane:
            self._active_drag_source = None

    def get_active_drag_source(self) -> Optional[FilePane]:
        return self._active_drag_source


    def _clear_progress_toast(self) -> None:
        """Clear the progress dialog safely."""
        if hasattr(self, '_progress_dialog') and self._progress_dialog is not None:
            try:
                self._progress_dialog.close()
            except (AttributeError, RuntimeError, GLib.GError):
                # Dialog might be destroyed or invalid, ignore
                pass
            finally:
                self._progress_dialog = None


    def _show_progress(self, fraction: float, message: str) -> None:
        """Update progress dialog if active."""
        if hasattr(self, '_progress_dialog') and self._progress_dialog is not None:
            try:
                self._progress_dialog.update_progress(fraction, message)
            except (AttributeError, RuntimeError, GLib.GError):
                # Dialog might be destroyed or invalid, ignore
                pass

    def _create_headerbar_menu(self, header_bar: Adw.HeaderBar) -> None:
        """Create and add menu button to headerbar."""
        # Create menu model
        menu_model = Gio.Menu()
        
        # Add "Show Hidden Files" toggle action
        menu_model.append("Show Hidden Files", "win.show-hidden")
        
        # Create action group for window actions
        self._window_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("win", self._window_action_group)
        
        # Create action for show hidden files
        self._show_hidden_action = Gio.SimpleAction.new_stateful(
            "show-hidden", 
            None, 
            GLib.Variant.new_boolean(False)
        )
        self._show_hidden_action.connect("activate", self._on_show_hidden_action)
        self._window_action_group.add_action(self._show_hidden_action)
        
        # Create menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_menu_model(menu_model)
        menu_button.set_tooltip_text("Main menu")
        
        # Add to headerbar
        header_bar.pack_end(menu_button)

    def _on_show_hidden_action(self, action: Gio.SimpleAction, _parameter: Optional[GLib.Variant]) -> None:
        """Handle show hidden files action from menu."""
        # Toggle the state
        current_state = action.get_state().get_boolean()
        new_state = not current_state
        action.set_state(GLib.Variant.new_boolean(new_state))
        
        # Apply to both panes
        self._left_pane._show_hidden = new_state
        self._right_pane._show_hidden = new_state
        
        # Refresh both panes
        self._left_pane._apply_entry_filter(preserve_selection=True)
        self._right_pane._apply_entry_filter(preserve_selection=True)

    def _on_local_pane_toggle(self, toggle_button: Gtk.ToggleButton) -> None:
        """Handle local pane toggle button."""
        is_active = toggle_button.get_active()
        
        if is_active:
            # Hide local pane (button is pressed/selected)
            self._left_pane.set_visible(False)
            toggle_button.set_tooltip_text("Show Local Pane")
        else:
            # Show local pane (button is unpressed/unselected)
            self._left_pane.set_visible(True)
            toggle_button.set_tooltip_text("Hide Local Pane")

    def _on_connected(self, *_args) -> None:
        self._show_progress(0.4, "Connected")
        # Trigger directory loads for all panes that have a pending initial path
        for pane, pending in self._pending_paths.items():
            if pending:
                self._manager.listdir(pending)


    def _on_progress(self, _manager, fraction: float, message: str) -> None:
        self._show_progress(fraction, message)

    def _on_operation_error(self, _manager, message: str) -> None:
        """Handle operation error with toast."""
        try:
            toast = Adw.Toast.new(message)
            toast.set_priority(Adw.ToastPriority.HIGH)
            self._toast_overlay.add_toast(toast)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    def _on_connection_error(self, _manager, message: str) -> None:
        """Handle connection error with toast."""
        self._clear_progress_toast()
        if self._connection_error_reported:
            return
        
        try:
            toast = Adw.Toast.new(message or "Connection failed")
            toast.set_priority(Adw.ToastPriority.HIGH)
            self._toast_overlay.add_toast(toast)
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass
        finally:
            self._connection_error_reported = True

    def _on_directory_loaded(
        self, _manager, path: str, entries: Iterable[FileEntry]
    ) -> None:
        # Prefer the pane explicitly waiting for this exact path; otherwise
        # assign to the next pane that still has a pending request. This makes
        # initial dual loads robust even if the backend normalizes paths.
        target = next((pane for pane, pending in self._pending_paths.items() if pending == path), None)
        if target is None:
            target = next((pane for pane, pending in self._pending_paths.items() if pending is not None), self._left_pane)
        # Clear the pending flag for the resolved pane
        self._pending_paths[target] = None

        target.show_entries(path, entries)
        self._apply_pending_highlight(target)
        target.push_history(path)
        target.show_toast(f"Loaded {path}")

    # -- local filesystem helpers ---------------------------------------

    def _load_local(self, path: str) -> None:
        """Load local directory contents into the left pane.

        This is a synchronous operation using the local filesystem.
        """
        try:
            path = os.path.expanduser(path or "~")
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            if not os.path.isdir(path):
                raise NotADirectoryError(f"Not a directory: {path}")

            entries: List[FileEntry] = []
            with os.scandir(path) as it:
                for dirent in it:
                    try:
                        stat = dirent.stat(follow_symlinks=False)
                        is_dir = dirent.is_dir(follow_symlinks=False)
                        item_count = None
                        
                        # Count items in directory
                        if is_dir:
                            try:
                                with os.scandir(dirent.path) as dir_it:
                                    item_count = len(list(dir_it))
                            except Exception:
                                # If we can't read the directory, set count to None
                                item_count = None
                        
                        entries.append(
                            FileEntry(
                                name=dirent.name,
                                is_dir=is_dir,
                                size=getattr(stat, "st_size", 0) or 0,
                                modified=getattr(stat, "st_mtime", 0.0) or 0.0,
                                item_count=item_count,
                            )
                        )
                    except Exception:
                        # Skip entries we cannot stat
                        continue

            # Show results in the left pane
            self._left_pane.show_entries(path, entries)
            self._apply_pending_highlight(self._left_pane)
        except Exception as exc:
            self._left_pane.show_toast(str(exc))

    def _on_path_changed(self, pane: FilePane, path: str, user_data=None) -> None:
        # Route local vs remote browsing
        if pane is self._left_pane:
            # Local pane: expand ~ and navigate local filesystem
            local_path = os.path.expanduser(path) if path.startswith("~") else path
            if not local_path:
                local_path = os.path.expanduser("~")
            try:
                self._load_local(local_path)
                # Only push history if not triggered by Back
                if getattr(pane, "_suppress_history_push", False):
                    pane._suppress_history_push = False
                else:
                    pane.push_history(local_path)
            except Exception as exc:
                pane.show_toast(str(exc))
        else:
            # Remote pane: use SFTP manager
            self._pending_paths[pane] = path
            # Only push history if not triggered by Back
            if getattr(pane, "_suppress_history_push", False):
                pane._suppress_history_push = False
            else:
                pane.push_history(path)
            self._manager.listdir(path)

    def _check_file_conflicts(self, files_to_transfer: List[Tuple[str, str]], operation_type: str, callback: Callable[[List[Tuple[str, str]]], None]) -> None:
        """Check for file conflicts and show resolution dialog if needed.
        
        Args:
            files_to_transfer: List of (source, destination) tuples
            operation_type: "upload" or "download"
            callback: Function to call with resolved file list
        """
        print(f"=== CHECKING FILE CONFLICTS ===")
        print(f"Operation type: {operation_type}")
        print(f"Files to transfer: {files_to_transfer}")
        
        conflicts = []
        
        # Check for conflicts
        for source, dest in files_to_transfer:
            print(f"Checking: {source} -> {dest}")
            if operation_type == "download":
                # For downloads, check if local file exists
                exists = os.path.exists(dest)
                print(f"  Local file exists: {exists}")
                if exists:
                    conflicts.append((source, dest))
                    print(f"  CONFLICT DETECTED: {dest}")
            else:  # upload
                # For uploads, we'd need to check remote files - this is more complex
                # For now, let the upload proceed (remote conflict handling would require SFTP stat calls)
                print(f"  Upload conflict checking not implemented yet")
                pass
        
        print(f"Total conflicts found: {len(conflicts)}")
        
        if not conflicts:
            # No conflicts, proceed with all transfers
            print("No conflicts, proceeding with transfers")
            callback(files_to_transfer)
            return
            
        # Show conflict resolution dialog
        conflict_count = len(conflicts)
        total_count = len(files_to_transfer)
        
        if conflict_count == 1:
            filename = os.path.basename(conflicts[0][1])
            title = "File Already Exists"
            message = f"'{filename}' already exists in the destination folder."
        else:
            title = "Files Already Exist"
            message = f"{conflict_count} of {total_count} files already exist in the destination folder."
            
        dialog = Adw.AlertDialog.new(title, message)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("skip", "Skip Existing")
        dialog.add_response("replace", "Replace All")
        dialog.set_default_response("skip")
        dialog.set_close_response("cancel")
        
        def _on_conflict_response(_dialog, response: str) -> None:
            dialog.close()
            
            if response == "cancel":
                return
            elif response == "skip":
                # Only transfer files that don't conflict
                non_conflicting = [item for item in files_to_transfer if item not in conflicts]
                if non_conflicting:
                    callback(non_conflicting)
                    # Show toast about skipped files
                    if conflict_count == 1:
                        filename = os.path.basename(conflicts[0][1])
                        self._left_pane.show_toast(f"Skipped existing file: {filename}")
                    else:
                        self._left_pane.show_toast(f"Skipped {conflict_count} existing files")
            elif response == "replace":
                # Transfer all files, replacing existing ones
                callback(files_to_transfer)
                
        dialog.connect("response", _on_conflict_response)
        dialog.present()

    def _on_request_operation(self, pane: FilePane, action: str, payload, user_data=None) -> None:
        if action == "mkdir":
            dialog = Adw.AlertDialog.new("New Folder", "Enter a name for the new folder")
            entry = Gtk.Entry()
            dialog.set_extra_child(entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Create")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_response(_dialog, response: str) -> None:
                if response == "ok":
                    name = entry.get_text().strip()
                    if name:
                        current_dir = pane.toolbar.path_entry.get_text() or "/"
                        if pane is self._left_pane:
                            target_dir = self._normalize_local_path(current_dir)
                            new_path = os.path.join(target_dir, name)
                        else:
                            new_path = posixpath.join(current_dir or "/", name)
                        if pane is self._left_pane:
                            try:
                                os.makedirs(new_path, exist_ok=False)
                            except FileExistsError:
                                pane.show_toast("Folder already exists")
                            except Exception as exc:
                                pane.show_toast(str(exc))
                            else:
                                # Refresh local listing
                                self._pending_highlights[self._left_pane] = name
                                self._load_local(os.path.dirname(new_path) or "/")
                        else:
                            future = self._manager.mkdir(new_path)
                            self._attach_refresh(
                                future,
                                refresh_remote=pane,
                                highlight_name=name,
                            )
                dialog.close()

            dialog.connect("response", _on_response)
            dialog.present()
        elif action == "rename" and isinstance(payload, dict):
            entries = payload.get("entries") or []
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not entries:
                return

            entry = entries[0]

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
                source = os.path.join(base_dir, entry.name)
                join = os.path.join
            else:
                base_dir = directory or "/"
                source = posixpath.join(base_dir, entry.name)
                join = posixpath.join

            dialog = Adw.AlertDialog.new("Rename Item", f"Enter a new name for {entry.name}")
            name_entry = Gtk.Entry()
            name_entry.set_text(entry.name)
            dialog.set_extra_child(name_entry)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Rename")
            dialog.set_default_response("ok")
            dialog.set_close_response("cancel")

            def _on_rename(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.close()
                    return
                new_name = name_entry.get_text().strip()
                if not new_name:
                    pane.show_toast("Name cannot be empty")
                    dialog.close()
                    return
                if new_name == entry.name:
                    dialog.close()
                    return
                target = join(base_dir, new_name)
                if pane is self._left_pane:
                    try:
                        os.rename(source, target)
                    except Exception as exc:
                        pane.show_toast(str(exc))
                    else:
                        pane.show_toast(f"Renamed to {new_name}")
                        self._pending_highlights[self._left_pane] = new_name
                        self._load_local(base_dir)
                else:
                    future = self._manager.rename(source, target)
                    self._attach_refresh(
                        future,
                        refresh_remote=pane,
                        highlight_name=new_name,
                    )
                    pane.show_toast(f"Renaming to {new_name}…")
                dialog.close()

            dialog.connect("response", _on_rename)
            dialog.present()
        elif action == "delete" and isinstance(payload, dict):
            entries = payload.get("entries") or []
            directory = payload.get("directory") or pane.toolbar.path_entry.get_text() or "/"
            if not entries:
                return

            if pane is self._left_pane:
                base_dir = self._normalize_local_path(directory)
            else:
                base_dir = directory or "/"

            count = len(entries)
            if count == 1:
                message = f"Delete {entries[0].name}?"
                title = "Delete Item"
            else:
                message = f"Delete {count} items?"
                title = "Delete Items"

            dialog = Adw.AlertDialog.new(title, message)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("ok", "Delete")
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def _on_delete(_dialog, response: str) -> None:
                if response != "ok":
                    dialog.close()
                    return
                if pane is self._left_pane:
                    deleted = 0
                    errors: List[str] = []
                    for selected_entry in entries:
                        target_path = os.path.join(base_dir, selected_entry.name)
                        try:
                            if selected_entry.is_dir:
                                shutil.rmtree(target_path)
                            else:
                                os.remove(target_path)
                            deleted += 1
                        except FileNotFoundError:
                            errors.append(f"{selected_entry.name} no longer exists")
                        except Exception as exc:
                            errors.append(str(exc))
                    if deleted:
                        message = (
                            "Deleted 1 item"
                            if deleted == 1
                            else f"Deleted {deleted} items"
                        )
                        pane.show_toast(message)
                        self._load_local(base_dir)
                    if errors:
                        pane.show_toast(errors[0])
                else:
                    for selected_entry in entries:
                        target_path = posixpath.join(base_dir, selected_entry.name)
                        future = self._manager.remove(target_path)
                        self._attach_refresh(future, refresh_remote=pane)
                    pane.show_toast(
                        "Deleting 1 item…" if count == 1 else f"Deleting {count} items…"
                    )
                dialog.close()

            dialog.connect("response", _on_delete)
            dialog.present()
        elif action == "upload":
            # Upload can be triggered from either pane, but we need to determine the target pane
            if pane is self._left_pane:
                # Upload from local to remote
                target_pane = self._right_pane
            elif pane is self._right_pane:
                # Upload from local to remote (when triggered from remote pane)
                target_pane = pane
            else:
                return

            remote_root = target_pane.toolbar.path_entry.get_text() or "/"
            raw_items: object | None = None

            if isinstance(payload, dict):
                destination = payload.get("destination")
                if isinstance(destination, pathlib.Path):
                    remote_root = destination.as_posix()
                elif isinstance(destination, str) and destination:
                    remote_root = destination
                raw_items = payload.get("paths")
            else:
                raw_items = payload

            paths: List[pathlib.Path] = []

            def _collect(item: object | None) -> None:
                if item is None:
                    return
                if isinstance(item, (list, tuple, set, frozenset)):
                    for value in item:
                        _collect(value)
                    return
                if isinstance(item, pathlib.Path):
                    paths.append(item)
                elif isinstance(item, Gio.File):
                    local_path = item.get_path()
                    if local_path:
                        paths.append(pathlib.Path(local_path))
                elif isinstance(item, str):
                    paths.append(pathlib.Path(item))

            _collect(raw_items)

            if not paths:
                pane.show_toast("No files selected for upload")
                return

            available_paths: List[pathlib.Path] = []
            missing: List[pathlib.Path] = []
            for candidate in paths:
                try:
                    if candidate.exists():
                        available_paths.append(candidate)
                    else:
                        missing.append(candidate)
                except OSError:
                    missing.append(candidate)

            if missing and not available_paths:
                pane.show_toast("Selected items are not accessible")
                return
            if missing and available_paths:
                pane.show_toast(f"Skipping inaccessible items: {missing[0].name}")

            # Prepare list of files to transfer for conflict checking
            files_to_transfer = []
            for path_obj in available_paths:
                destination = posixpath.join(remote_root or "/", path_obj.name)
                files_to_transfer.append((str(path_obj), destination))
            
            # Check for conflicts and handle accordingly  
            def _proceed_with_upload(resolved_files: List[Tuple[str, str]]) -> None:
                for local_path_str, destination in resolved_files:
                    path_obj = pathlib.Path(local_path_str)
                    
                    try:
                        if path_obj.is_dir():
                            future = self._manager.upload_directory(path_obj, destination)
                        else:
                            future = self._manager.upload(path_obj, destination)

                        # Show progress dialog for upload
                        self._show_progress_dialog("upload", path_obj.name, future)
                        self._attach_refresh(
                            future,
                            refresh_remote=target_pane,
                            highlight_name=path_obj.name,
                        )
                    except Exception as e:
                        pane.show_toast(f"Error uploading {path_obj.name}: {str(e)}")
            
            self._check_file_conflicts(files_to_transfer, "upload", _proceed_with_upload)
        elif action == "download" and isinstance(payload, dict):
            print(f"=== DOWNLOAD OPERATION CALLED ===")
            print(f"Payload: {payload}")
            
            if pane is self._left_pane and payload.get("entries"):
                remote_pane = getattr(self, "_right_pane", None)
                if isinstance(remote_pane, FilePane):
                    pane = remote_pane

            entries = payload.get("entries") or []
            directory = payload.get("directory")
            print(f"Entries to download: {[e.name for e in entries]}")
            print(f"Directory: {directory}")
            if not directory:
                if pane is self._right_pane:
                    directory = pane.toolbar.path_entry.get_text() or "/"
                else:
                    remote_pane = getattr(self, "_right_pane", None)
                    if isinstance(remote_pane, FilePane):
                        directory = remote_pane.toolbar.path_entry.get_text() or "/"
                    else:
                        directory = "/"
            destination_base = payload.get("destination")

            if not entries or destination_base is None:
                pane.show_toast("Invalid download request")
                return

            if not isinstance(destination_base, pathlib.Path):
                destination_base = pathlib.Path(destination_base)

            # Prepare list of files to transfer for conflict checking
            files_to_transfer = []
            for entry in entries:
                source = posixpath.join(directory or "/", entry.name)
                target_path = destination_base / entry.name
                files_to_transfer.append((source, str(target_path)))
            
            # Check for conflicts and handle accordingly
            def _proceed_with_download(resolved_files: List[Tuple[str, str]]) -> None:
                for source, target_path_str in resolved_files:
                    target_path = pathlib.Path(target_path_str)
                    entry_name = os.path.basename(target_path_str)
                    
                    # Find the original entry to check if it's a directory
                    entry_is_dir = False
                    for entry in entries:
                        if entry.name == entry_name:
                            entry_is_dir = entry.is_dir
                            break
                    
                    try:
                        if entry_is_dir:
                            future = self._manager.download_directory(source, target_path)
                        else:
                            future = self._manager.download(source, target_path)
                        self._show_progress_dialog("download", entry_name, future)
                        self._attach_refresh(
                            future,
                            refresh_local_path=str(destination_base),
                            highlight_name=entry_name,
                        )
                    except Exception as e:
                        pane.show_toast(f"Error downloading {entry_name}: {str(e)}")
            
            self._check_file_conflicts(files_to_transfer, "download", _proceed_with_download)

    def _on_window_resize(self, window, pspec) -> None:
        """Maintain proportional paned split when window is resized following GNOME HIG"""
        # Get current window width
        width = self.get_width()
        if width > 0:
            # Set paned position to half the window width (maintaining 50/50 split)
            self._panes.set_position(width // 2)

    def _attach_refresh(
        self,
        future: Optional[Future],
        *,
        refresh_remote: Optional[FilePane] = None,
        refresh_local_path: Optional[str] = None,
        highlight_name: Optional[str] = None,
    ) -> None:
        if future is None:
            return

        def _on_done(completed: Future) -> None:
            try:
                completed.result()
            except Exception:
                return
            if highlight_name:
                if refresh_remote is not None:
                    self._pending_highlights[refresh_remote] = highlight_name
                elif refresh_local_path is not None:
                    self._pending_highlights[self._left_pane] = highlight_name
            if refresh_remote is not None:
                GLib.idle_add(self._refresh_remote_listing, refresh_remote)
            if refresh_local_path:
                GLib.idle_add(self._refresh_local_listing, refresh_local_path)

        future.add_done_callback(_on_done)

    def _apply_pending_highlight(self, pane: FilePane) -> None:
        name = self._pending_highlights.get(pane)
        if not name:
            return
        self._pending_highlights[pane] = None
        pane.highlight_entry(name)

    def _refresh_remote_listing(self, pane: FilePane) -> bool:
        path = pane.toolbar.path_entry.get_text() or "/"
        self._pending_paths[pane] = path
        self._manager.listdir(path)
        return False

    def _refresh_local_listing(self, path: str) -> bool:
        target = self._normalize_local_path(path)
        current = self._normalize_local_path(self._left_pane.toolbar.path_entry.get_text())
        if target == current:
            self._load_local(target)
        else:
            self._pending_highlights[self._left_pane] = None
        return False

    
    def _show_progress_dialog(self, operation_type: str, filename: str, future: Future) -> None:
        """Show and manage the progress dialog for a file operation."""
        try:
            print(f"DEBUG: _show_progress_dialog called for {operation_type} {filename}")
            
            # Dismiss any existing progress dialog
            if hasattr(self, '_progress_dialog') and self._progress_dialog:
                try:
                    self._progress_dialog.close()
                except (AttributeError, RuntimeError):
                    pass
                self._progress_dialog = None
            
            # Create new progress dialog
            print(f"DEBUG: Creating progress dialog")
            self._progress_dialog = SFTPProgressDialog(parent=self, operation_type=operation_type)
            self._progress_dialog.set_operation_details(total_files=1, filename=filename)
            self._progress_dialog.set_future(future)
            
            # Try to get file size for better progress display
            try:
                if operation_type == "download":
                    # For downloads, we'll get the size from the SFTP manager if available
                    # This is a rough estimate, the actual implementation would need to
                    # query the remote file size
                    self._progress_dialog.total_bytes = 1024 * 1024  # Default to 1MB estimate
                else:  # upload
                    # For uploads, we could get the local file size
                    self._progress_dialog.total_bytes = 1024 * 1024  # Default to 1MB estimate
            except Exception:
                self._progress_dialog.total_bytes = 0
            
            # Show the dialog
            self._progress_dialog.present()
            print(f"DEBUG: Progress dialog created and shown successfully")
            
        except Exception as exc:
            print(f"DEBUG: Error in _show_progress_dialog: {exc}")
            import traceback
            traceback.print_exc()
            return
        
        # Store references for cleanup
        self._progress_handler_id = None
        
        # Connect progress signal
        def _on_progress(manager, progress: float, message: str) -> None:
            # Check if dialog exists and operation hasn't been cancelled
            if (self._progress_dialog and 
                not self._progress_dialog.is_cancelled and
                self._current_future and 
                not self._current_future.cancelled()):
                try:
                    self._progress_dialog.update_progress(progress, message)
                except (AttributeError, RuntimeError, GLib.GError):
                    # Dialog may have been destroyed
                    pass
        
        # Connect progress signal and store handler ID
        self._progress_handler_id = self._manager.connect("progress", _on_progress)
        
        def _on_complete(future_result) -> None:
            # Use GLib.idle_add to ensure we're on the main thread
            def _cleanup():
                # Disconnect progress signal
                if hasattr(self, '_progress_handler_id') and self._progress_handler_id:
                    try:
                        self._manager.disconnect(self._progress_handler_id)
                    except (TypeError, RuntimeError):
                        pass
                    self._progress_handler_id = None
                
                # Update dialog to show completion
                if self._progress_dialog:
                    try:
                        if future_result.exception():
                            error_msg = str(future_result.exception())
                            self._progress_dialog.show_completion(success=False, error_message=error_msg)
                        else:
                            self._progress_dialog.increment_file_count()
                            self._progress_dialog.show_completion(success=True)
                    except (AttributeError, RuntimeError, GLib.GError):
                        # Dialog may have been destroyed
                        pass
                
                self._current_future = None
            
            GLib.idle_add(_cleanup)
        
        # Connect future completion
        future.add_done_callback(_on_complete)

    @staticmethod
    def _normalize_local_path(path: Optional[str]) -> str:
        expanded = os.path.expanduser(path or "/")
        return os.path.abspath(expanded)


def launch_file_manager_window(
    *,
    host: str,
    username: str,
    port: int = 22,
    path: str = "~",
    parent: Optional[Gtk.Window] = None,
    transient_for_parent: bool = True,
) -> FileManagerWindow:
    """Create and present the :class:`FileManagerWindow`.

    The function obtains the default application instance (``Gtk.Application``)
    if available; otherwise the caller must ensure the returned window remains
    referenced for the duration of its lifetime.

    Parameters
    ----------
    host, username, port, path
        Connection details used by :class:`FileManagerWindow`.
    parent
        Optional window that should act as the logical parent for stacking
        purposes.  When provided the new window may be set as transient for
        this parent depending on ``transient_for_parent``.
    transient_for_parent
        Set to ``False`` to avoid establishing a transient relationship with
        ``parent``.  This allows callers to request a free-floating window even
        when a parent reference is supplied.
    """

    app = Gtk.Application.get_default()
    if app is None:
        raise RuntimeError("An application instance is required to show the window")

    window = FileManagerWindow(
        application=app,
        host=host,
        username=username,
        port=port,
        initial_path=path,
    )
    if parent is not None and transient_for_parent:
        window.set_transient_for(parent)
    window.present()
    return window


__all__ = [
    "AsyncSFTPManager",
    "FileEntry",
    "FileManagerWindow",
    "SFTPProgressDialog",
    "launch_file_manager_window",
]

