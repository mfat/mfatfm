"""Progress state shared between a worker thread and the UI.

The worker updates an :class:`OperationProgress` as it goes; the UI polls it
on a timer. Folding per-item byte counts into one batch total is a pure
function (:func:`aggregate_batch_bytes`) so the bar never treats the latest
file as the whole job.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from .common import OperationCancelled


@dataclass(frozen=True)
class BatchByteProgress:
    """Aggregated byte progress for one UI transfer batch."""

    bytes_done: int
    bytes_total: Optional[int]
    fraction: Optional[float]


def aggregate_batch_bytes(
    *,
    expected: Mapping[str, Optional[int]],
    active: Mapping[str, Tuple[int, int]],
    settled: Mapping[str, Tuple[int, int]],
    files_completed: int = 0,
    total_files: int = 0,
) -> BatchByteProgress:
    """Fold per-transfer slices into batch done/total/fraction.

    ``expected`` lists every key in the batch (including queued and settled).
    ``active`` is the latest ``(done, reported_total)`` for in-flight keys
    (``reported_total`` of 0 means unknown). ``settled`` holds final
    ``(done, total)`` for finished keys.

    When every key has a known total, ``fraction`` is byte-based. Otherwise it
    falls back to file-count math using active per-file fractions where known,
    so the bar cannot jump backwards.
    """
    bytes_done = 0
    bytes_total = 0
    unknown = False

    for key, exp in expected.items():
        exp_n = int(exp) if exp is not None and exp > 0 else None
        if key in settled:
            done, total = settled[key]
            done_n = max(0, int(done or 0))
            total_n = max(0, int(total or 0))
            bytes_done += done_n
            bytes_total += total_n if total_n > 0 else done_n
            continue
        if key in active:
            done, total = active[key]
            done_n = max(0, int(done or 0))
            total_n = max(0, int(total or 0))
            bytes_done += done_n
            if total_n > 0:
                bytes_total += total_n
            elif exp_n is not None:
                bytes_total += max(exp_n, done_n)
            else:
                unknown = True
            continue
        # Queued / not yet reporting.
        if exp_n is not None:
            bytes_total += exp_n
        elif exp is None:
            unknown = True

    if not unknown and bytes_total > 0:
        fraction = max(0.0, min(1.0, bytes_done / bytes_total))
        return BatchByteProgress(bytes_done, bytes_total, fraction)

    # Incomplete size knowledge: keep honest byte counts but use file-count
    # fraction so the bar stays monotonic.
    fraction: Optional[float] = None
    if total_files > 0:
        partial = 0.0
        for done, total in active.values():
            total_n = max(0, int(total or 0))
            done_n = max(0, int(done or 0))
            if total_n > 0:
                partial += min(1.0, done_n / total_n)
        overall = (max(0, int(files_completed)) + partial) / total_files
        if files_completed < total_files:
            overall = min(overall, (total_files - 1) / total_files)
        fraction = max(0.0, min(1.0, overall))

    return BatchByteProgress(
        bytes_done,
        None if unknown else (bytes_total if bytes_total > 0 else None),
        fraction,
    )


class OperationProgress:
    """What one running operation has done so far.

    Written by the worker thread, read by the UI. Plain attribute updates
    under a lock; the UI takes :meth:`snapshot` on its own schedule.
    """

    def __init__(self, total_items: int) -> None:
        self.total_items = total_items
        self.items_done = 0
        self.current_name = ""
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._expected: Dict[str, Optional[int]] = {}
        self._active: Dict[str, Tuple[int, int]] = {}
        self._settled: Dict[str, Tuple[int, int]] = {}

    # -- cancellation ------------------------------------------------------

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise OperationCancelled()

    # -- worker side -------------------------------------------------------

    def expect(self, key: str, size: Optional[int]) -> None:
        """Declare an item before it starts; ``None`` size means unknown."""
        with self._lock:
            self._expected[key] = size

    def start_item(self, key: str, name: str) -> None:
        with self._lock:
            self.current_name = name
            self._expected.setdefault(key, None)
            self._active[key] = (0, 0)

    def item_bytes(self, key: str, done: int, total: int) -> None:
        """Byte progress for *key*; raises :class:`OperationCancelled`."""
        with self._lock:
            self._active[key] = (done, total)
        self.check_cancel()

    def finish_item(self, key: str) -> None:
        with self._lock:
            done, total = self._active.pop(key, (0, 0))
            if total <= 0:
                total = done
            self._settled[key] = (done, total)
            self.items_done += 1

    def callback_for(self, key: str):
        """A transfer callback feeding :meth:`item_bytes` for *key*."""
        return lambda done, total: self.item_bytes(key, done, total)

    # -- UI side -----------------------------------------------------------

    def snapshot(self) -> BatchByteProgress:
        with self._lock:
            return aggregate_batch_bytes(
                expected=dict(self._expected),
                active=dict(self._active),
                settled=dict(self._settled),
                files_completed=self.items_done,
                total_files=self.total_items,
            )
