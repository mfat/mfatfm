"""This computer's filesystem."""

from __future__ import annotations

import os
import shutil
import stat as _stat
from typing import BinaryIO, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from ..common import EntryStat, FileEntry
from .base import Backend, ProgressCallback

_COPY_CHUNK = 1024 * 1024


def _entry_stat(st: os.stat_result, *, is_symlink: bool = False) -> EntryStat:
    created = getattr(st, "st_birthtime", None)
    return EntryStat(
        mode=st.st_mode,
        size=st.st_size,
        modified=st.st_mtime,
        uid=getattr(st, "st_uid", None),
        gid=getattr(st, "st_gid", None),
        is_symlink=is_symlink,
        created=created,
    )


class LocalBackend(Backend):
    kind = "local"
    is_remote = False
    paths = os.path

    @property
    def key(self) -> str:
        return "local"

    @property
    def display_name(self) -> str:
        from gettext import gettext as _

        return _("This Computer")

    # -- paths -----------------------------------------------------------

    def home(self) -> str:
        return os.path.expanduser("~")

    def normalize(self, path: Optional[str]) -> str:
        path = os.path.expanduser(path or "~")
        if path.startswith("file://"):
            from urllib.parse import unquote, urlsplit

            path = unquote(urlsplit(path).path) or "/"
        return os.path.normpath(os.path.abspath(path))

    def dirname(self, path: str) -> str:
        return os.path.dirname(os.path.normpath(path)) or os.sep

    # -- reading ---------------------------------------------------------

    def list_dir(self, path: str) -> List[FileEntry]:
        if not os.path.isdir(path):
            raise NotADirectoryError(path)
        entries: List[FileEntry] = []
        with os.scandir(path) as it:
            for dirent in it:
                try:
                    st = dirent.stat(follow_symlinks=False)
                    is_symlink = _stat.S_ISLNK(st.st_mode)
                    if is_symlink:
                        # A link shows its target's type and size, so a
                        # linked folder can be opened like any other.
                        try:
                            st = dirent.stat(follow_symlinks=True)
                        except OSError:
                            pass
                    entries.append(
                        FileEntry(
                            name=dirent.name,
                            is_dir=_stat.S_ISDIR(st.st_mode),
                            size=st.st_size or 0,
                            modified=st.st_mtime or 0.0,
                            mode=st.st_mode,
                            is_symlink=is_symlink,
                        )
                    )
                except OSError:
                    # Skip entries we cannot stat
                    continue
        return entries

    def count_items(self, path: str, names: Sequence[str]) -> Iterator[Dict[str, int]]:
        batch: Dict[str, int] = {}
        for name in names:
            try:
                with os.scandir(os.path.join(path, name)) as dir_it:
                    batch[name] = sum(1 for _entry in dir_it)
            except OSError:
                continue
            if len(batch) >= 64:
                yield batch
                batch = {}
        if batch:
            yield batch

    def stat(self, path: str) -> EntryStat:
        return _entry_stat(os.stat(path), is_symlink=os.path.islink(path))

    def lstat(self, path: str) -> EntryStat:
        st = os.lstat(path)
        return _entry_stat(st, is_symlink=_stat.S_ISLNK(st.st_mode))

    def exists_many(self, paths: Sequence[str]) -> List[bool]:
        return [os.path.lexists(path) for path in paths]

    def walk(self, top: str) -> Iterator[Tuple[str, List[str], List[str]]]:
        pending = [top]
        while pending:
            current = pending.pop(0)
            dirnames: List[str] = []
            filenames: List[str] = []
            with os.scandir(current) as it:
                for dirent in it:
                    if dirent.is_dir(follow_symlinks=False):
                        dirnames.append(dirent.name)
                    else:
                        filenames.append(dirent.name)
            yield current, dirnames, filenames
            pending[0:0] = [os.path.join(current, name) for name in dirnames]

    def open_read(self, path: str) -> BinaryIO:
        return open(path, "rb")

    def open_write(self, path: str) -> BinaryIO:
        return open(path, "wb")

    def free_space(self, path: str) -> Optional[int]:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def owner_names(
        self, uids: Iterable[int], gids: Iterable[int]
    ) -> Tuple[Dict[int, str], Dict[int, str]]:
        users: Dict[int, str] = {}
        groups: Dict[int, str] = {}
        try:
            import grp
            import pwd
        except ImportError:  # pragma: no cover - Windows
            return users, groups
        for uid in uids:
            try:
                users[uid] = pwd.getpwuid(int(uid)).pw_name
            except (KeyError, ValueError):
                pass
        for gid in gids:
            try:
                groups[gid] = grp.getgrgid(int(gid)).gr_name
            except (KeyError, ValueError):
                pass
        return users, groups

    # -- changing --------------------------------------------------------

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def create_file(self, path: str) -> None:
        with open(path, "x"):
            pass

    def rename(self, old: str, new: str) -> None:
        if os.path.lexists(new):
            raise FileExistsError(new)
        os.rename(old, new)

    def replace(self, old: str, new: str) -> None:
        os.replace(old, new)

    def remove(self, path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.unlink(path)

    def chmod(self, path: str, mode: int) -> None:
        os.chmod(path, mode)

    def set_times(self, path: str, mtime: Optional[float]) -> None:
        if mtime is None:
            return
        try:
            os.utime(path, (mtime, mtime), follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass

    @property
    def can_trash(self) -> bool:
        return True

    def trash(self, path: str) -> None:
        from gi.repository import Gio, GLib

        try:
            Gio.File.new_for_path(path).trash(None)
        except GLib.Error as exc:
            raise OSError(exc.message) from exc

    def copy_file(self, src: str, dst: str, callback: Optional[ProgressCallback] = None) -> int:
        if os.path.islink(src):
            if os.path.lexists(dst):
                os.unlink(dst)
            os.symlink(os.readlink(src), dst)
            return 0
        total = os.path.getsize(src)
        done = 0
        with open(src, "rb") as reader, open(dst, "wb") as writer:
            while True:
                chunk = reader.read(_COPY_CHUNK)
                if not chunk:
                    break
                writer.write(chunk)
                done += len(chunk)
                if callback is not None:
                    callback(done, total)
        try:
            shutil.copystat(src, dst)
        except OSError:
            pass
        if callback is not None and total == 0:
            callback(0, 0)
        return done
