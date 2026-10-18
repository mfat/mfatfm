"""An SFTP server, reached through the system's OpenSSH via mfatsftp."""

from __future__ import annotations

import logging
import posixpath
import stat as _stat
import threading
from typing import BinaryIO, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import mfatsftp

from ..common import EntryStat, FileEntry
from ..locations import SftpTarget
from .base import Backend, ProgressCallback, TreeFile

logger = logging.getLogger(__name__)

# Folders listed per batched request when counting or sizing a tree.
_LIST_BATCH = 50

_SSH_OPTIONS = (
    "ConnectTimeout=15",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=3",
)


def _entry_stat(attr: "mfatsftp.SFTPAttributes", *, is_symlink: bool = False) -> EntryStat:
    return EntryStat(
        mode=attr.st_mode or 0,
        size=attr.st_size or 0,
        modified=float(attr.st_mtime) if attr.st_mtime is not None else None,
        uid=attr.st_uid,
        gid=attr.st_gid,
        is_symlink=is_symlink,
    )


# ``connector(env)`` starts a session the host's way and returns the client;
# *env* holds the askpass variables to pass to ssh.
Connector = Callable[[Mapping[str, str]], "mfatsftp.SFTPClient"]


class SftpBackend(Backend):
    """One SFTP server.

    By default :meth:`connect` runs ``ssh [user@]host -s sftp`` through
    mfatsftp. A host application can change how the session starts:

    - ``ssh_options``: extra ``-o`` options (e.g. ``ControlPath=...`` to
      ride an existing ControlMaster);
    - ``connector``: a callable that returns a connected
      ``mfatsftp.SFTPClient`` (e.g. from ``mfatsftp.from_command``);
    - :meth:`from_client`: wrap a client the host already has.
    """

    kind = "sftp"
    is_remote = True
    paths = posixpath

    def __init__(
        self,
        target: SftpTarget,
        *,
        ssh_options: Sequence[str] = (),
        connector: Optional[Connector] = None,
    ) -> None:
        self.target = target
        self._ssh_options = tuple(ssh_options)
        self._connector = connector
        self._owns_client = True
        self._client: Optional[mfatsftp.SFTPClient] = None
        self._home: Optional[str] = None
        self._lock = threading.Lock()

    @classmethod
    def from_client(
        cls,
        target: SftpTarget,
        client: "mfatsftp.SFTPClient",
        *,
        owns_client: bool = False,
        connector: Optional[Connector] = None,
    ) -> "SftpBackend":
        """Use an already connected client.

        Unless *owns_client*, :meth:`close` leaves the client open for its
        owner. Without a *connector* the backend cannot reconnect once the
        client is closed.
        """
        backend = cls(target, connector=connector)
        backend._client = client
        backend._owns_client = owns_client
        return backend

    @property
    def key(self) -> str:
        return self.target.key

    @property
    def display_name(self) -> str:
        return self.target.display_name

    def location_uri(self, path: str) -> str:
        return self.target.uri(path)

    # -- lifecycle -------------------------------------------------------

    def connect(self, env: Optional[Mapping[str, str]] = None) -> None:
        """Start ssh and the SFTP session. Blocks while the user logs in."""
        target = self.target
        if self._connector is not None:
            client = self._connector(dict(env or {}))
        elif not self._owns_client:
            raise ConnectionError(f"The connection to {target.display_name} was closed")
        else:
            client = mfatsftp.connect(
                target.ssh_destination,
                port=target.port,
                ssh_options=list(_SSH_OPTIONS + self._ssh_options),
                env=dict(env or {}),
            )
        try:
            home = client.realpath(".")
        except Exception:
            client.close()
            raise
        with self._lock:
            self._client = client
            self._home = home
            self._owns_client = True
        logger.info("Connected to %s (home %s)", target.display_name, home)

    @property
    def connected(self) -> bool:
        client = self._client
        return client is not None and not client.closed

    @property
    def closed(self) -> bool:
        return not self.connected

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None and self._owns_client:
            try:
                client.close()
            except Exception:
                logger.debug("Error closing SFTP client", exc_info=True)

    @property
    def client(self) -> mfatsftp.SFTPClient:
        client = self._client
        if client is None or client.closed:
            raise ConnectionError(f"Not connected to {self.display_name}")
        return client

    # -- paths -----------------------------------------------------------

    def home(self) -> str:
        if self._home is None:
            self._home = self.client.realpath(".")
        return self._home

    def normalize(self, path: Optional[str]) -> str:
        path = (path or "~").strip() or "~"
        if path == "~" or path.startswith("~/"):
            path = self.home() + path[1:]
        elif not path.startswith("/"):
            path = posixpath.join(self.home(), path)
        return posixpath.normpath(path) if path != "/" else "/"

    # -- reading ---------------------------------------------------------

    def list_dir(self, path: str) -> List[FileEntry]:
        client = self.client
        attrs = client.listdir_attr(path)
        links = [attr for attr in attrs if attr.is_symlink()]
        targets: Dict[str, Optional[mfatsftp.SFTPAttributes]] = {}
        if links:
            resolved = client.stat_many(
                [posixpath.join(path, attr.filename) for attr in links],
                missing_on_error=True,
            )
            targets = {attr.filename: target for attr, target in zip(links, resolved)}
        entries: List[FileEntry] = []
        for attr in attrs:
            name = attr.filename or ""
            if name in (".", ".."):
                continue
            is_symlink = attr.is_symlink()
            shown = targets.get(name) if is_symlink else attr
            if shown is None:
                shown = attr  # a dangling link shows as the link itself
            entries.append(
                FileEntry(
                    name=name,
                    is_dir=shown.is_dir(),
                    size=shown.st_size or 0,
                    modified=float(shown.st_mtime or 0),
                    mode=shown.st_mode,
                    is_symlink=is_symlink,
                )
            )
        return entries

    def _list_many(self, paths: Sequence[str]) -> List[Optional[List["mfatsftp.SFTPAttributes"]]]:
        """List several folders; ``None`` for one that cannot be read."""
        client = self.client
        try:
            return list(client.listdir_many(list(paths)))
        except mfatsftp.SFTPConnectionError:
            raise
        except OSError:
            results: List[Optional[List[mfatsftp.SFTPAttributes]]] = []
            for path in paths:
                try:
                    results.append(client.listdir_attr(path))
                except mfatsftp.SFTPConnectionError:
                    raise
                except OSError:
                    results.append(None)
            return results

    def count_items(self, path: str, names: Sequence[str]) -> Iterator[Dict[str, int]]:
        names = list(names)
        for start in range(0, len(names), _LIST_BATCH):
            chunk = names[start:start + _LIST_BATCH]
            listings = self._list_many([posixpath.join(path, name) for name in chunk])
            yield {
                name: len(listing)
                for name, listing in zip(chunk, listings)
                if listing is not None
            }

    def stat(self, path: str) -> EntryStat:
        client = self.client
        attr = client.stat(path)
        is_symlink = False
        try:
            is_symlink = client.lstat(path).is_symlink()
        except OSError:
            pass
        return _entry_stat(attr, is_symlink=is_symlink)

    def lstat(self, path: str) -> EntryStat:
        attr = self.client.lstat(path)
        return _entry_stat(attr, is_symlink=attr.is_symlink())

    def exists_many(self, paths: Sequence[str]) -> List[bool]:
        if not paths:
            return []
        found = self.client.stat_many(list(paths), missing_on_error=True)
        return [attr is not None for attr in found]

    def walk(self, top: str) -> Iterator[Tuple[str, List[str], List[str]]]:
        return self.client.walk(top)

    def open_read(self, path: str) -> BinaryIO:
        return self.client.open(path, "rb")

    def open_write(self, path: str) -> BinaryIO:
        handle = self.client.open(path, "wb")
        handle.set_pipelined(True)
        return handle

    def scan_tree(self, root: str) -> Tuple[List[Tuple[str, ...]], List[TreeFile]]:
        # Breadth first, listing a batch of folders per round trip.
        dirs: List[Tuple[str, ...]] = []
        files: List[TreeFile] = []
        pending: List[Tuple[str, Tuple[str, ...]]] = [(root, ())]
        while pending:
            batch, pending = pending[:_LIST_BATCH], pending[_LIST_BATCH:]
            listings = self._list_many([folder for folder, _parts in batch])
            for (folder, parts), listing in zip(batch, listings):
                if listing is None:
                    if not parts:
                        raise PermissionError(f"Cannot read {folder}")
                    continue
                for attr in sorted(listing, key=lambda item: item.filename or ""):
                    name = attr.filename
                    if name in (None, ".", ".."):
                        continue
                    child = parts + (name,)
                    if attr.is_dir():
                        dirs.append(child)
                        pending.append((posixpath.join(folder, name), child))
                    elif attr.is_symlink() or _stat.S_ISREG(attr.st_mode or 0):
                        files.append(
                            TreeFile(
                                parts=child,
                                size=0 if attr.is_symlink() else attr.st_size or 0,
                                mtime=float(attr.st_mtime) if attr.st_mtime is not None else None,
                                is_symlink=attr.is_symlink(),
                            )
                        )
        return dirs, files

    def free_space(self, path: str) -> Optional[int]:
        client = self.client
        if not client.supports_statvfs():
            return None
        info = client.statvfs(path)
        return info.f_bavail * info.f_frsize

    def owner_names(
        self, uids: Iterable[int], gids: Iterable[int]
    ) -> Tuple[Dict[int, str], Dict[int, str]]:
        client = self.client
        uids, gids = list(uids), list(gids)
        if not client.supports_users_groups_by_id() or not (uids or gids):
            return {}, {}
        users, groups = client.users_groups_by_id(uids, gids)
        return (
            {uid: name for uid, name in zip(uids, users) if name},
            {gid: name for gid, name in zip(gids, groups) if name},
        )

    # -- changing --------------------------------------------------------

    def mkdir(self, path: str) -> None:
        self.client.mkdir(path)

    def makedirs(self, path: str) -> None:
        self.client.makedirs(path)

    def create_file(self, path: str) -> None:
        self.client.open(path, "x").close()

    def rename(self, old: str, new: str) -> None:
        self.client.rename(old, new)

    def replace(self, old: str, new: str) -> None:
        self.client.atomic_rename(old, new)

    def remove(self, path: str) -> None:
        client = self.client
        if client.lstat(path).is_dir():
            client.rmtree(path)
        else:
            client.remove(path)

    def chmod(self, path: str, mode: int) -> None:
        self.client.chmod(path, mode)

    def set_times(self, path: str, mtime: Optional[float]) -> None:
        if mtime is None:
            return
        try:
            self.client.utime(path, (int(mtime), int(mtime)))
        except OSError:
            pass

    def copy_file(self, src: str, dst: str, callback: Optional[ProgressCallback] = None) -> int:
        client = self.client
        attr = client.lstat(src)
        if attr.is_symlink():
            if self.exists_many([dst])[0]:
                client.remove(dst)
            client.symlink(client.readlink(src), dst)
            return 0
        size = attr.st_size or 0
        if callback is not None:
            callback(0, size)
        client.copy(src, dst)
        if callback is not None:
            callback(size, size)
        return size

    # -- transfers to and from this computer -----------------------------

    def upload(self, local: str, remote: str, is_dir: bool, callback: ProgressCallback) -> int:
        if is_dir:
            return self.client.put_dir(local, remote, callback=callback)
        return self.client.put(local, remote, callback=callback)

    def download(self, remote: str, local: str, is_dir: bool, callback: ProgressCallback) -> int:
        if is_dir:
            return self.client.get_dir(remote, local, callback=callback)
        return self.client.get(remote, local, callback=callback)
