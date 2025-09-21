"""Asynchronous Paramiko connection helpers for mfatfm."""

from __future__ import annotations

import os
import pathlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Iterable, List, Optional, Tuple

import paramiko
from gi.repository import GLib, GObject

from .fileops import FileEntry, stat_isdir, walk_remote

class TransferCancelledException(Exception):
    """Exception raised when a transfer is cancelled"""
    pass

class _MainThreadDispatcher:
    """Helper that marshals callbacks back to the GTK main loop."""

    @staticmethod
    def dispatch(func: Callable, *args, **kwargs) -> None:
        GLib.idle_add(lambda: func(*args, **kwargs))

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
