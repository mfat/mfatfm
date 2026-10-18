#!/usr/bin/env python3
"""Run mfatfm from a source checkout, without installing it.

    ./run.py [LOCATION] [LOCATION]

Each location is a local path, sftp://[user@]host[:port]/path or
user@host:path (left pane, then right pane).

mfatsftp is taken from the environment if it is installed, else from a
sibling ``../mfatsftp`` checkout.
"""

import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(CURRENT_DIR, "src"))

try:
    import mfatsftp  # noqa: F401
except ImportError:
    sibling = os.path.join(os.path.dirname(CURRENT_DIR), "mfatsftp", "src")
    if os.path.isdir(sibling):
        sys.path.insert(0, sibling)

from mfatfm.app import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
