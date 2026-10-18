"""The interface every pane backend implements.

A backend is a filesystem a pane can browse: this computer, or one SFTP
server. Every method is synchronous and may block, so the window runs them on
worker threads and hands the results back to the GTK main loop. Paths are
plain strings in the backend's own flavour (``os.path`` locally, POSIX on a
server); ``paths`` names the module that joins and splits them.
"""

from __future__ import annotations

import dataclasses
import os
import posixpath
from typing import BinaryIO, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..common import EntryStat, FileEntry

# ``callback(bytes_done, bytes_total)``; total is 0 when unknown. It may raise
# to abort the transfer (cancellation).
ProgressCallback = Callable[[int, int], None]

_COPY_CHUNK = 1024 * 1024


@dataclasses.dataclass
class TreeFile:
    """A file found by :meth:`Backend.scan_tree`."""

    parts: Tuple[str, ...]
    size: int
    mtime: Optional[float]
    is_symlink: bool = False


class Backend:
    """Base class: shared helpers plus the operations a pane needs."""

    #: ``"local"`` or ``"sftp"``.
    kind = ""
    is_remote = False
    paths = os.path
    #: The server, for remote backends (an ``SftpTarget``).
    target = None

    @property
    def key(self) -> str:
        """Identifies the filesystem; equal keys mean the same server."""
        raise NotImplementedError

    @property
    def display_name(self) -> str:
        """Short name for the pane's location button."""
        raise NotImplementedError

    def location_uri(self, path: str) -> str:
        """A string the user could type to come back to *path*."""
        return path

    # -- lifecycle -------------------------------------------------------

    def connect(self, env=None) -> None:
        """Connect (remote backends); *env* carries the askpass settings."""

    def close(self) -> None:
        """Release the connection, if any. Idempotent."""

    @property
    def closed(self) -> bool:
        return False

    @property
    def connected(self) -> bool:
        return not self.closed

    # -- paths -----------------------------------------------------------

    def home(self) -> str:
        raise NotImplementedError

    def normalize(self, path: Optional[str]) -> str:
        """Absolute, normalised form of *path* (``~`` expands to the home)."""
        raise NotImplementedError

    def join(self, *parts: str) -> str:
        return self.paths.join(*parts)

    def dirname(self, path: str) -> str:
        return self.paths.dirname(path.rstrip("/") or "/") or "/"

    def basename(self, path: str) -> str:
        return self.paths.basename(path.rstrip("/")) or path

    # -- reading ---------------------------------------------------------

    def list_dir(self, path: str) -> List[FileEntry]:
        raise NotImplementedError

    def count_items(self, path: str, names: Sequence[str]) -> Iterator[Dict[str, int]]:
        """Yield batches of ``{folder name: number of entries}``."""
        raise NotImplementedError

    def stat(self, path: str) -> EntryStat:
        """Metadata, following symlinks. Raises ``FileNotFoundError``."""
        raise NotImplementedError

    def lstat(self, path: str) -> EntryStat:
        raise NotImplementedError

    def exists_many(self, paths: Sequence[str]) -> List[bool]:
        """Whether each path exists (symlinks count, even dangling ones)."""
        raise NotImplementedError

    def walk(self, top: str) -> Iterator[Tuple[str, List[str], List[str]]]:
        """``os.walk``-style, top-down; symlinked folders are not followed."""
        raise NotImplementedError

    def open_read(self, path: str) -> BinaryIO:
        raise NotImplementedError

    def open_write(self, path: str) -> BinaryIO:
        raise NotImplementedError

    def scan_tree(self, root: str) -> Tuple[List[Tuple[str, ...]], List["TreeFile"]]:
        """Every folder and file below *root*, as paths relative to it.

        Folders come parents first. Symlinks are listed as files and never
        followed.
        """
        dirs: List[Tuple[str, ...]] = []
        files: List[TreeFile] = []
        parts_of = {self.paths.normpath(root): ()}
        for dirpath, dirnames, filenames in self.walk(root):
            rel = parts_of.get(self.paths.normpath(dirpath), ())
            for name in dirnames:
                parts_of[self.paths.normpath(self.join(dirpath, name))] = rel + (name,)
                dirs.append(rel + (name,))
            for name in filenames:
                try:
                    st = self.lstat(self.join(dirpath, name))
                except OSError:
                    continue
                files.append(
                    TreeFile(
                        parts=rel + (name,),
                        size=0 if st.is_symlink else st.size,
                        mtime=st.modified,
                        is_symlink=st.is_symlink,
                    )
                )
        return dirs, files

    def directory_size(self, path: str) -> int:
        """Total size of the regular files below *path*."""
        _dirs, files = self.scan_tree(path)
        return sum(item.size for item in files)

    def free_space(self, path: str) -> Optional[int]:
        """Bytes available to the user, or ``None`` when unknown."""
        return None

    def owner_names(
        self, uids: Iterable[int], gids: Iterable[int]
    ) -> Tuple[Dict[int, str], Dict[int, str]]:
        """Names for owner ids, as this filesystem's host knows them."""
        return {}, {}

    # -- changing --------------------------------------------------------

    def mkdir(self, path: str) -> None:
        raise NotImplementedError

    def makedirs(self, path: str) -> None:
        raise NotImplementedError

    def create_file(self, path: str) -> None:
        """Create an empty file; ``FileExistsError`` if *path* exists."""
        raise NotImplementedError

    def rename(self, old: str, new: str) -> None:
        """Rename within this filesystem; fails if *new* exists."""
        raise NotImplementedError

    def replace(self, old: str, new: str) -> None:
        """Rename over an existing *new* file."""
        raise NotImplementedError

    def remove(self, path: str) -> None:
        """Remove *path*: a file, a symlink, or a whole folder tree."""
        raise NotImplementedError

    def chmod(self, path: str, mode: int) -> None:
        raise NotImplementedError

    def set_times(self, path: str, mtime: Optional[float]) -> None:
        """Best effort: give *path* this modification time."""

    @property
    def can_trash(self) -> bool:
        return False

    def trash(self, path: str) -> None:
        raise OSError("This location has no trash")

    # -- copying within this filesystem ----------------------------------

    def copy_file(self, src: str, dst: str, callback: Optional[ProgressCallback] = None) -> int:
        """Copy one file inside this filesystem. Returns bytes copied."""
        return stream_copy(self, src, self, dst, callback)


def stream_copy(
    src_backend: Backend,
    src: str,
    dst_backend: Backend,
    dst: str,
    callback: Optional[ProgressCallback] = None,
    *,
    size_hint: int = 0,
) -> int:
    """Copy one file between any two backends by reading and writing it."""
    total = size_hint
    if not total:
        try:
            total = src_backend.stat(src).size
        except OSError:
            total = 0
    done = 0
    with src_backend.open_read(src) as reader, dst_backend.open_write(dst) as writer:
        while True:
            chunk = reader.read(_COPY_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            done += len(chunk)
            if callback is not None:
                callback(done, total)
    if callback is not None and done == 0:
        callback(0, total)
    return done


def path_inside(parent: str, child: str, paths=posixpath) -> bool:
    """True when *child* is *parent* or lies below it."""
    parent_n = paths.normpath(parent)
    child_n = paths.normpath(child)
    if parent_n == child_n:
        return True
    prefix = parent_n if parent_n.endswith(paths.sep) else parent_n + paths.sep
    return child_n.startswith(prefix)
