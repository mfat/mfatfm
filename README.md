# mfatfm

`mfatfm` provides a libadwaita-based, two-pane SFTP file manager window designed
for embedding inside other GTK applications. The window offloads remote
filesystem operations to worker threads, keeps the UI responsive via the main
GLib loop, and offers progress dialogs, drag-and-drop feedback, and toast-based
notifications out of the box.

## Installation

To install the package along with its runtime dependencies, clone this
repository and install it in an isolated environment:

```bash
python -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install .
```

Alternatively, you can build a source or wheel distribution using
[`build`](https://pypi.org/project/build/):

```bash
python -m build
```

This command creates distributable artifacts in the `dist/` directory that can
be uploaded to a package index or installed via `pip`.

## Usage

After installation the window can be imported from the `mfatfm` package:

```python
from mfatfm import SFTPProgressDialog
```

The `SFTPProgressDialog` class exposes a GNOME HIG-compliant dialog that can be
used to display progress and cancellation controls for long-running SFTP
transfers. See the source in `src/mfatfm/file_manager_window.py` for a full
example that includes additional window widgets and helper utilities.
