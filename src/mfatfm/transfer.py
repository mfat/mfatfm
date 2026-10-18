"""Copy, move and delete between any two backends.

Runs on a worker thread. Every combination works: this computer to itself,
to or from a server, within one server (copied on the server), or between
two servers (streamed through this computer). Progress and cancellation go
through an :class:`~mfatfm.progress.OperationProgress`.
"""

from __future__ import annotations

import dataclasses
import errno
import logging
from typing import Iterable, List, Sequence, Tuple

from .backends import Backend, ProgressCallback, path_inside, stream_copy
from .common import OperationCancelled
from .format_utils import filename_extension_offset
from .progress import OperationProgress

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TransferItem:
    """One selected item: where it is and where it goes."""

    name: str
    src_path: str
    dst_path: str
    is_dir: bool
    size: int = 0


Failure = Tuple[str, BaseException]


def same_filesystem(src: Backend, dst: Backend) -> bool:
    return src is dst or src.key == dst.key


def copy_name(name: str, taken: Iterable[str]) -> str:
    """Nautilus-style name for a duplicate: "a (copy).txt", "a (copy 2).txt"."""
    taken = set(taken)
    offset = filename_extension_offset(name)
    stem, ext = name[:offset], name[offset:]
    candidate = f"{stem} (copy){ext}"
    number = 2
    while candidate in taken:
        candidate = f"{stem} (copy {number}){ext}"
        number += 1
    return candidate


def into_itself(src: Backend, dst: Backend, item: TransferItem) -> bool:
    """True when a folder would be copied or moved inside itself."""
    return (
        item.is_dir
        and same_filesystem(src, dst)
        and path_inside(item.src_path, item.dst_path, src.paths)
        and src.paths.normpath(item.src_path) != src.paths.normpath(item.dst_path)
    )


def _is_link(backend: Backend, path: str) -> bool:
    try:
        return backend.lstat(path).is_symlink
    except OSError:
        return False


def _copy_tree(
    src: Backend,
    dst: Backend,
    src_root: str,
    dst_root: str,
    callback: ProgressCallback,
    progress: OperationProgress,
) -> None:
    """Copy a folder file by file; folders on *dst* are merged into."""
    dirs, files = src.scan_tree(src_root)
    total = sum(item.size for item in files)
    dst.makedirs(dst_root)
    for parts in dirs:
        progress.check_cancel()
        dst.makedirs(dst.join(dst_root, *parts))
    done_before = 0
    callback(0, total)
    same = same_filesystem(src, dst)
    for item in files:
        progress.check_cancel()
        source = src.join(src_root, *item.parts)
        target = dst.join(dst_root, *item.parts)

        def _file_progress(done: int, _total: int, base: int = done_before) -> None:
            callback(base + done, total)

        try:
            if same:
                src.copy_file(source, target, _file_progress)
            elif item.is_symlink:
                # Across filesystems a link is copied as what it points to;
                # a link to a folder (or a dangling one) is skipped.
                try:
                    stream_copy(src, source, dst, target, _file_progress)
                except (IsADirectoryError, FileNotFoundError):
                    logger.debug("Skipping link %s", source)
                    continue
            else:
                stream_copy(src, source, dst, target, _file_progress, size_hint=item.size)
                dst.set_times(target, item.mtime)
        except OperationCancelled:
            _remove_partial(dst, target)
            raise
        done_before += item.size
        callback(done_before, total)


def _remove_partial(backend: Backend, path: str) -> None:
    try:
        backend.remove(path)
    except Exception:
        logger.debug("Could not remove partial %s", path, exc_info=True)


def copy_item(
    src: Backend,
    dst: Backend,
    item: TransferItem,
    callback: ProgressCallback,
    progress: OperationProgress,
) -> None:
    """Copy *item* to its destination, picking the fastest route."""
    if not src.is_remote and dst.is_remote:
        try:
            dst.upload(item.src_path, item.dst_path, item.is_dir, callback)
        except OperationCancelled:
            if not item.is_dir:
                _remove_partial(dst, item.dst_path)
            raise
        return
    if src.is_remote and not dst.is_remote:
        try:
            src.download(item.src_path, item.dst_path, item.is_dir, callback)
        except OperationCancelled:
            if not item.is_dir:
                _remove_partial(dst, item.dst_path)
            raise
        return
    if item.is_dir and not _is_link(src, item.src_path):
        _copy_tree(src, dst, item.src_path, item.dst_path, callback, progress)
        return
    try:
        if same_filesystem(src, dst):
            src.copy_file(item.src_path, item.dst_path, callback)
        else:
            stream_copy(src, item.src_path, dst, item.dst_path, callback, size_hint=item.size)
    except OperationCancelled:
        _remove_partial(dst, item.dst_path)
        raise


def _move_within(
    backend: Backend,
    item: TransferItem,
    replace: bool,
    callback: ProgressCallback,
    progress: OperationProgress,
) -> None:
    """Move inside one filesystem: a rename, unless that cannot work."""
    paths = backend.paths
    if paths.normpath(item.src_path) == paths.normpath(item.dst_path):
        return
    exists = backend.exists_many([item.dst_path])[0]
    if not exists:
        try:
            backend.rename(item.src_path, item.dst_path)
            return
        except (FileNotFoundError, PermissionError):
            raise
        except OSError as exc:
            # Across devices (or on a server that will not rename across
            # its filesystems) fall back to copy and delete.
            logger.debug("Rename failed (%s); copying instead", exc)
    elif not item.is_dir:
        if not replace:
            raise FileExistsError(errno.EEXIST, "File exists", item.dst_path)
        try:
            backend.replace(item.src_path, item.dst_path)
            return
        except (FileNotFoundError, PermissionError):
            raise
        except OSError as exc:
            logger.debug("Replace failed (%s); copying instead", exc)
    # Merging into an existing folder, or a rename that cannot work.
    copy_item(backend, backend, item, callback, progress)
    progress.check_cancel()
    backend.remove(item.src_path)


def run_transfer(
    src: Backend,
    dst: Backend,
    items: Sequence[TransferItem],
    *,
    move: bool,
    progress: OperationProgress,
    replace: bool = True,
) -> List[Failure]:
    """Copy or move *items* from *src* to *dst*.

    Returns the items that failed with their errors. Cancelling stops at the
    item in progress (a partial file is removed) and raises
    :class:`OperationCancelled`.
    """
    failures: List[Failure] = []
    for index, item in enumerate(items):
        key = f"item-{index}"
        progress.expect(key, None if item.is_dir else item.size)
    for index, item in enumerate(items):
        progress.check_cancel()
        key = f"item-{index}"
        progress.start_item(key, item.name)
        callback = progress.callback_for(key)
        try:
            if into_itself(src, dst, item):
                raise OSError(errno.EINVAL, "Cannot copy a folder into itself", item.dst_path)
            if move and same_filesystem(src, dst):
                _move_within(src, item, replace, callback, progress)
            else:
                copy_item(src, dst, item, callback, progress)
                if move:
                    progress.check_cancel()
                    src.remove(item.src_path)
        except OperationCancelled:
            raise
        except Exception as exc:
            logger.warning("Failed to %s %s: %s", "move" if move else "copy", item.src_path, exc)
            failures.append((item.name, exc))
        finally:
            progress.finish_item(key)
    return failures


def run_delete(
    backend: Backend,
    paths: Sequence[str],
    *,
    trash: bool,
    progress: OperationProgress,
) -> List[Failure]:
    """Delete (or move to the trash) every path; returns the failures."""
    failures: List[Failure] = []
    for index, path in enumerate(paths):
        progress.check_cancel()
        key = f"item-{index}"
        name = backend.basename(path)
        progress.expect(key, 0)
        progress.start_item(key, name)
        try:
            if trash:
                backend.trash(path)
            else:
                backend.remove(path)
        except Exception as exc:
            logger.warning("Failed to delete %s: %s", path, exc)
            failures.append((name, exc))
        finally:
            progress.finish_item(key)
    return failures


def existing_destinations(dst: Backend, items: Sequence[TransferItem]) -> List[TransferItem]:
    """The items whose destination already exists."""
    found = dst.exists_many([item.dst_path for item in items])
    return [item for item, exists in zip(items, found) if exists]
