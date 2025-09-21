"""Local and remote filesystem helper utilities for mfatfm."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

import paramiko

@dataclass
class FileEntry:
    """Light weight description of a directory entry."""

    name: str
    is_dir: bool
    size: int
    modified: float
    item_count: Optional[int] = None

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

def normalize_local_path(path: Optional[str]) -> str:
    """Expand user and resolve an absolute local filesystem path."""
    expanded = os.path.expanduser(path or "/")
    return os.path.abspath(expanded)

def load_local_directory(path: str) -> Tuple[str, List[FileEntry]]:
    """Return normalized path and entries for a local directory."""
    normalized = normalize_local_path(path)
    if not os.path.isdir(normalized):
        raise NotADirectoryError(f"Not a directory: {normalized}")

    entries: List[FileEntry] = []
    with os.scandir(normalized) as it:
        for dirent in it:
            try:
                stat_result = dirent.stat(follow_symlinks=False)
                is_dir = dirent.is_dir(follow_symlinks=False)
                item_count: Optional[int] = None

                if is_dir:
                    try:
                        with os.scandir(dirent.path) as dir_it:
                            item_count = len(list(dir_it))
                    except Exception:
                        item_count = None

                entries.append(FileEntry(
                    name=dirent.name,
                    is_dir=is_dir,
                    size=getattr(stat_result, "st_size", 0) or 0,
                    modified=getattr(stat_result, "st_mtime", 0.0) or 0.0,
                    item_count=item_count,
                ))
            except Exception:
                continue

    return normalized, entries
