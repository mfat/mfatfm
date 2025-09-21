"""Backward compatibility shim for the legacy module location."""

from mfatfm.ui import FileManagerWindow, SFTPProgressDialog, launch_file_manager_window

__all__ = [
    "FileManagerWindow",
    "SFTPProgressDialog",
    "launch_file_manager_window",
]
