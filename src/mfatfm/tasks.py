"""Running blocking work off the GTK main loop.

``run_async(func, on_done)`` runs ``func()`` on a worker thread and calls
``on_done(result, error)`` back on the main loop, with exactly one of the two
set. Long jobs (copies) get their own thread so they never queue behind each
other or behind directory listings.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from gi.repository import GLib

logger = logging.getLogger(__name__)

Done = Callable[[Any, Optional[BaseException]], None]

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="mfatfm")


def _deliver(on_done: Optional[Done], result: Any, error: Optional[BaseException]) -> None:
    if on_done is None:
        if error is not None:
            logger.warning("Background task failed: %s", error)
        return

    def _call() -> bool:
        try:
            on_done(result, error)
        except Exception:
            logger.exception("Error in task completion callback")
        return False

    GLib.idle_add(_call)


def _run(func: Callable[[], Any], on_done: Optional[Done]) -> None:
    try:
        result = func()
    except BaseException as exc:  # noqa: BLE001 - handed to the callback
        _deliver(on_done, None, exc)
    else:
        _deliver(on_done, result, None)


def run_async(func: Callable[[], Any], on_done: Optional[Done] = None) -> None:
    """Run a short blocking call (listing, stat, rename) on the shared pool."""
    _executor.submit(_run, func, on_done)


def run_in_thread(func: Callable[[], Any], on_done: Optional[Done] = None, name: str = "mfatfm-job") -> None:
    """Run a long job (copy, move, delete, connect) on its own thread."""
    threading.Thread(target=_run, args=(func, on_done), name=name, daemon=True).start()
