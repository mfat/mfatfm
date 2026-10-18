"""Shared, backend-agnostic data types for the file manager.

Importing this module pulls in no GTK and no SSH code: it is only the
dataclasses every layer passes around.
"""

from __future__ import annotations

import dataclasses
import stat as _stat
from typing import Optional


@dataclasses.dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None  # Number of items in directory (for folders only)
    mode: Optional[int] = None  # Type and permission bits when the listing has them
    is_symlink: bool = False


@dataclasses.dataclass
class EntryStat:
    """Metadata for one path, as the properties dialog needs it."""

    mode: int
    size: int
    modified: Optional[float]
    uid: Optional[int]
    gid: Optional[int]
    is_symlink: bool = False
    created: Optional[float] = None

    @property
    def is_dir(self) -> bool:
        return _stat.S_ISDIR(self.mode)


class OperationCancelled(Exception):
    """Raised inside a running operation when the user cancels it."""
