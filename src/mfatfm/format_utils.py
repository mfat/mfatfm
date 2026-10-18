"""Small formatting helpers used by the file-manager UI (sizes, times, mode)."""

from __future__ import annotations

import stat
from datetime import datetime
from gettext import ngettext


def _item_count_text(count: int) -> str:
    """Return a localized item count for a file-manager folder."""

    return ngettext(
        "{count} item",
        "{count} items",
        count,
    ).format(count=count)


def _type_char(mode: int) -> str:
    """Leading ``ls -l`` style type character."""
    if stat.S_ISDIR(mode):
        return "d"
    if stat.S_ISLNK(mode):
        return "l"
    if stat.S_ISBLK(mode):
        return "b"
    if stat.S_ISCHR(mode):
        return "c"
    if stat.S_ISSOCK(mode):
        return "s"
    if stat.S_ISFIFO(mode):
        return "p"
    return "-"


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
    perm = ""
    for shift in (6, 3, 0):
        r = "r" if mode & (4 << shift) else "-"
        w = "w" if mode & (2 << shift) else "-"
        x = "x" if mode & (1 << shift) else "-"
        perm += r + w + x
    return _type_char(mode) + perm


def _mode_to_octal(mode: int) -> str:
    """Convert file mode to octal representation like 755."""
    # Extract only the permission bits (last 9 bits)
    perm_bits = mode & 0o777
    return oct(perm_bits)[2:]  # Remove '0o' prefix


def format_size(size_bytes: int) -> str:
    """Size as the list view shows it: "512 B", "1.5 MB"."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB", "TB", "PB"]
    value = float(size_bytes)
    for unit in units:
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} EB"


def safe_display_text(value: str) -> str:
    """Return *value* with un-encodable lone surrogates replaced by U+FFFD.

    Names read from the local filesystem come back surrogate-escaped when their
    bytes are not valid UTF-8 (``os.scandir`` decodes with ``surrogateescape``).
    PyGObject encodes every Python string to UTF-8 for the C call and raises
    ``UnicodeEncodeError`` on surrogates, so a single badly-encoded filename
    would otherwise take down a whole directory listing.  Every string handed to
    GTK/Pango must pass through here; callers keep the original value for
    filesystem operations.
    """

    if not isinstance(value, str):
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        pass
    try:
        return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        # Surrogates outside the 0xDC80-0xDCFF range the escape handler covers.
        return "".join(
            "�" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value
        )


def filename_extension_offset(name: str) -> int:
    """Where *name*'s extension starts, or its length if it has none.

    Mirrors Nautilus's nautilus_filename_get_extension(): the first
    character never starts an extension (".bashrc" has none), an extension
    holding whitespace is not one, and ".tar.*" counts as one extension.
    """
    if not name:
        return 0
    dot = name.rfind(".", 1)
    if dot == -1 or dot == len(name) - 1:
        return len(name)
    if any(char.isspace() for char in name[dot:]):
        return len(name)
    if dot > len(".tar") and name[dot - len(".tar"):dot] == ".tar":
        return dot - len(".tar")
    return dot
