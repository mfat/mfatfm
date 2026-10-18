import pytest

from mfatfm.locations import (
    SftpTarget,
    gtk_bookmarks,
    parse_location,
    parse_server_address,
    ssh_config_hosts,
)


def test_uri_forms():
    loc = parse_location("sftp://me@example.com:2222/var/www")
    assert loc.target == SftpTarget("example.com", "me", 2222)
    assert loc.path == "/var/www"
    assert parse_location("sftp://example.com").path == "~"
    assert parse_location("sftp://example.com/~/src").path == "~/src"
    assert parse_location("ssh://[::1]:22/tmp").target == SftpTarget("::1", None, 22)
    assert parse_location("sftp://h/a%20b").path == "/a b"


def test_scp_form_needs_a_user_or_known_alias():
    loc = parse_location("me@host:/etc", known_hosts=[])
    assert loc.target == SftpTarget("host", "me") and loc.path == "/etc"
    assert parse_location("me@host:", known_hosts=[]).path == "~"
    assert parse_location("box:logs", known_hosts=["box"]).target == SftpTarget("box")
    # A local name with a colon stays local.
    assert parse_location("notes:draft.txt", known_hosts=[]).target is None
    assert parse_location("/tmp/a:b", known_hosts=["tmp"]).target is None


def test_local_forms():
    assert parse_location("~/Documents").path == "~/Documents"
    assert parse_location("file:///tmp/a%20b").path == "/tmp/a b"


def test_server_address():
    assert parse_server_address("box").target == SftpTarget("box")
    loc = parse_server_address("me@box:2200/srv")
    assert loc.target == SftpTarget("box", "me", 2200) and loc.path == "/srv"
    assert parse_server_address("fe80::1").target.host == "fe80::1"
    for bad in ("", "-oProxyCommand=x", "box:99999", "a b"):
        with pytest.raises(ValueError):
            parse_server_address(bad)


def test_target_strings():
    target = SftpTarget("::1", "me", 2222)
    assert target.key == "sftp://me@[::1]:2222"
    assert target.uri("/a b") == "sftp://me@[::1]:2222/a%20b"
    assert SftpTarget("h").ssh_destination == "h"
    assert parse_location(target.uri("/x")).target == target


def test_ssh_config_hosts(tmp_path):
    (tmp_path / "extra").write_text("Host included\n")
    config = tmp_path / "config"
    config.write_text(
        f"Host web db *.internal !bad\n  User me\nInclude {tmp_path / 'extra'}\nhost web\n"
    )
    assert ssh_config_hosts(str(config)) == ["web", "db", "included"]


def test_gtk_bookmarks(tmp_path):
    marks = tmp_path / "bookmarks"
    marks.write_text("file:///home/me/Projects Work\nsftp://me@box/srv\nfile:///opt\n")
    places = gtk_bookmarks(str(marks))
    assert [p.label for p in places] == ["Work", "me@box", "opt"]
    assert places[1].location.target == SftpTarget("box", "me")
