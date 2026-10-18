"""Small persistent preferences, stored as JSON in the user's config folder."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_RECENT_SERVERS = 8


def _config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "mfatfm", "settings.json")


class Settings:
    """Read once, written on every change (the file is tiny)."""

    def __init__(self, path: str = "", *, persist: bool = True) -> None:
        self._path = path or _config_path()
        self._persist = persist
        self._data: Dict[str, Any] = {}
        if not persist:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                self._data = loaded
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable settings %s: %s", self._path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        if self._data.get(key) == value:
            return
        self._data[key] = value
        self._save()

    def _save(self) -> None:
        if not self._persist:
            return
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError as exc:
            logger.warning("Could not save settings: %s", exc)

    # -- recent servers ----------------------------------------------------

    def recent_servers(self) -> List[str]:
        value = self.get("recent_servers", [])
        return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

    def add_recent_server(self, uri: str) -> None:
        recent = [item for item in self.recent_servers() if item != uri]
        self.set("recent_servers", [uri] + recent[: _MAX_RECENT_SERVERS - 1])


_settings: "Settings | None" = None


def use_settings(settings: Settings) -> None:
    """Make mfatfm read and write *settings* (call before creating views).

    A host application can keep mfatfm's preferences in its own folder, or
    pass ``Settings(persist=False)`` to keep them in memory only.
    """
    global _settings
    _settings = settings


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
