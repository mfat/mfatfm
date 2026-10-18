"""Parsing locations the user types, and the places offered in menus.

A location is either a local path or an SFTP target plus a path on it. The
path bar and the command line accept ``sftp://[user@]host[:port]/path`` URIs
and scp-style ``user@host:path``; the Connect to Server dialog also takes a
bare ``host`` or ``~/.ssh/config`` alias.
"""

from __future__ import annotations

import dataclasses
import glob
import os
import re
from typing import List, Optional, Tuple, Union
from urllib.parse import quote, unquote, urlsplit

_URI_SCHEMES = ("sftp", "ssh")


@dataclasses.dataclass(frozen=True)
class SftpTarget:
    """Where to connect: what ``ssh [-p port] [user@]host`` would reach."""

    host: str
    user: Optional[str] = None
    port: Optional[int] = None

    @property
    def ssh_destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    @property
    def _host_port(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{host}:{self.port}" if self.port else host

    @property
    def key(self) -> str:
        return f"sftp://{self.user + '@' if self.user else ''}{self._host_port}"

    @property
    def display_name(self) -> str:
        return f"{self.user + '@' if self.user else ''}{self._host_port}"

    def uri(self, path: str = "") -> str:
        path = path or "/"
        if not path.startswith("/"):
            path = "/" + path
        return self.key + quote(path, safe="/~")


@dataclasses.dataclass(frozen=True)
class Location:
    """A parsed location: ``target`` is ``None`` for this computer."""

    path: str
    target: Optional[SftpTarget] = None

    @property
    def is_remote(self) -> bool:
        return self.target is not None


def _split_user_host(text: str) -> Tuple[Optional[str], str]:
    user, sep, host = text.rpartition("@")
    return (user or None) if sep else None, host


def _parse_host_port(text: str) -> Tuple[str, Optional[int]]:
    """``host``, ``host:22``, ``[::1]`` or ``[::1]:22``."""
    if text.startswith("["):
        end = text.find("]")
        if end == -1:
            raise ValueError(f"Invalid address: {text}")
        host, rest = text[1:end], text[end + 1:]
        if rest and not rest.startswith(":"):
            raise ValueError(f"Invalid address: {text}")
        port_text = rest[1:]
    else:
        host, _sep, port_text = text.partition(":")
    port = None
    if port_text:
        if not port_text.isdigit() or not 0 < int(port_text) < 65536:
            raise ValueError(f"Invalid port: {port_text}")
        port = int(port_text)
    return host, port


def _valid_host(host: str) -> bool:
    return bool(host) and not host.startswith("-") and not any(c.isspace() for c in host)


def _remote_path_from_uri(path: str) -> str:
    path = unquote(path or "")
    if path in ("", "/"):
        return "~" if path == "" else "/"
    if path == "/~" or path.startswith("/~/"):
        return path[1:]
    return path


def parse_uri(text: str) -> Optional[Location]:
    """An ``sftp://`` (or ``ssh://``) URI, else ``None``."""
    scheme, sep, _rest = text.partition("://")
    if not sep or scheme.lower() not in _URI_SCHEMES:
        return None
    parts = urlsplit(text)
    netloc = parts.netloc
    user, hostport = _split_user_host(netloc)
    host, port = _parse_host_port(hostport)
    if not _valid_host(host):
        raise ValueError(f"Invalid address: {text}")
    return Location(
        path=_remote_path_from_uri(parts.path),
        target=SftpTarget(host=host, user=unquote(user) if user else None, port=port),
    )


_SCP_RE = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>\[[^\]]+\]|[^:/\s\[\]]+):(?P<path>.*)$")


def parse_location(text: str, known_hosts: Optional[List[str]] = None) -> Location:
    """Parse what the user typed into a path bar or passed on the command line.

    scp-style ``host:path`` is only taken as remote when it names a user
    (``user@host:path``) or a known ``~/.ssh/config`` alias, so a local file
    name with a colon is not mistaken for a server.
    """
    text = text.strip()
    uri = parse_uri(text)
    if uri is not None:
        return uri
    if text.startswith("file://"):
        return Location(path=unquote(urlsplit(text).path) or "/")
    match = _SCP_RE.match(text)
    if match and not text.startswith(("/", "~", ".")):
        user, host = match.group("user"), match.group("host")
        if host.startswith("["):
            host = host[1:-1]
        aliases = known_hosts if known_hosts is not None else ssh_config_hosts()
        if _valid_host(host) and (user or host in aliases):
            return Location(
                path=match.group("path") or "~",
                target=SftpTarget(host=host, user=user),
            )
    return Location(path=text or "~")


def parse_server_address(text: str) -> Location:
    """Parse the Connect to Server field: a URI, ``user@host[:port]`` or an alias.

    Raises ``ValueError`` for something that cannot name a server.
    """
    text = text.strip()
    if not text:
        raise ValueError("Enter a server address")
    uri = parse_uri(text)
    if uri is not None:
        return uri
    address, slash, path = text.partition("/")
    user, hostport = _split_user_host(address)
    if ":" in hostport and not hostport.startswith("[") and hostport.count(":") > 1:
        host, port = hostport, None  # a bare IPv6 address
    else:
        host, port = _parse_host_port(hostport)
    if not _valid_host(host):
        raise ValueError(f"Invalid address: {text}")
    return Location(
        path=("/" + path) if slash else "~",
        target=SftpTarget(host=host, user=user, port=port),
    )


# -- ~/.ssh/config hosts ---------------------------------------------------


def _config_files(path: str, seen: set) -> List[str]:
    path = os.path.expanduser(path)
    if path in seen or not os.path.isfile(path):
        return []
    seen.add(path)
    files = [path]
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return files
    for line in lines:
        words = line.strip().split()
        if len(words) >= 2 and words[0].lower() == "include":
            for pattern in words[1:]:
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = os.path.join(os.path.expanduser("~/.ssh"), pattern)
                for included in sorted(glob.glob(pattern)):
                    files.extend(_config_files(included, seen))
    return files


def ssh_config_hosts(config_path: str = "~/.ssh/config") -> List[str]:
    """Concrete ``Host`` aliases from the user's ssh config (no patterns)."""
    hosts: List[str] = []
    for path in _config_files(config_path, set()):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            words = line.strip().split()
            if len(words) < 2 or words[0].lower() != "host":
                continue
            for alias in words[1:]:
                if any(char in alias for char in "*?!") or alias in hosts:
                    continue
                hosts.append(alias)
    return hosts


# -- places -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Place:
    label: str
    location: Union[str, Location]
    icon_name: str


def standard_places() -> List[Place]:
    """Home and the XDG user folders that exist."""
    from gettext import gettext as _

    from gi.repository import GLib

    home = os.path.expanduser("~")
    places = [Place(_("Home"), home, "user-home-symbolic")]
    special = (
        (GLib.UserDirectory.DIRECTORY_DESKTOP, "user-desktop-symbolic"),
        (GLib.UserDirectory.DIRECTORY_DOCUMENTS, "folder-documents-symbolic"),
        (GLib.UserDirectory.DIRECTORY_DOWNLOAD, "folder-download-symbolic"),
        (GLib.UserDirectory.DIRECTORY_MUSIC, "folder-music-symbolic"),
        (GLib.UserDirectory.DIRECTORY_PICTURES, "folder-pictures-symbolic"),
        (GLib.UserDirectory.DIRECTORY_VIDEOS, "folder-videos-symbolic"),
    )
    for directory, icon in special:
        path = GLib.get_user_special_dir(directory)
        if path and os.path.isdir(path) and os.path.normpath(path) != home:
            places.append(Place(os.path.basename(path), path, icon))
    places.append(Place(_("Computer"), "/", "drive-harddisk-symbolic"))
    return places


def gtk_bookmarks(path: Optional[str] = None) -> List[Place]:
    """The bookmarks Nautilus and the GTK file chooser share."""
    path = path or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "gtk-3.0",
        "bookmarks",
    )
    places: List[Place] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return places
    for line in lines:
        uri, _sep, label = line.strip().partition(" ")
        if not uri:
            continue
        if uri.startswith("file://"):
            local = unquote(urlsplit(uri).path)
            places.append(
                Place(label or os.path.basename(local.rstrip("/")) or local, local, "folder-symbolic")
            )
            continue
        try:
            location = parse_uri(uri)
        except ValueError:
            continue
        if location is not None:
            places.append(
                Place(label or location.target.display_name, location, "folder-remote-symbolic")
            )
    return places
