"""mfatfm: a two-pane file manager for GNOME that can also browse SFTP servers.

Embedding (GTK 4 / libadwaita apps)::

    from mfatfm import FileManagerView, SftpBackend, SftpTarget

    view = FileManagerView(left="~", right="sftp://me@example.com/srv")
    window.insert_action_group("fm", view.actions)
    header_bar.pack_end(view.operations_button)

The GTK classes are imported on first use, so ``import mfatfm`` (and the
backends) work without a display.
"""

__version__ = "0.2.0"

from .locations import Location, SftpTarget, parse_location  # noqa: E402

_LAZY = {
    "FileManagerView": ".view",
    "FileManagerWindow": ".window",
    "launch_file_manager_window": ".window",
    "FilePane": ".pane",
    "Backend": ".backends",
    "LocalBackend": ".backends",
    "SftpBackend": ".backends.sftp",
    "Settings": ".settings",
    "use_settings": ".settings",
}

__all__ = ["Location", "SftpTarget", "parse_location", "__version__", *_LAZY]


def __getattr__(name):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'mfatfm' has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module, __name__), name)
    globals()[name] = value
    return value
