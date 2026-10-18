"""The transfer engine on this computer, and to/from an SFTP server.

The SFTP cases need a server that accepts key logins without prompting; set
``MFATFM_TEST_SFTP_HOST`` (e.g. ``localhost``) to run them. They work in a
temporary folder on that server.
"""

import os
import posixpath
import uuid

import pytest

from mfatfm.backends import LocalBackend
from mfatfm.common import OperationCancelled
from mfatfm.locations import SftpTarget
from mfatfm.progress import OperationProgress
from mfatfm.transfer import (
    TransferItem,
    copy_name,
    existing_destinations,
    into_itself,
    run_delete,
    run_transfer,
)


def _tree(root):
    os.makedirs(os.path.join(root, "sub", "deep"))
    with open(os.path.join(root, "a.txt"), "w") as handle:
        handle.write("alpha")
    with open(os.path.join(root, "sub", "b.bin"), "wb") as handle:
        handle.write(os.urandom(300_000))
    with open(os.path.join(root, "sub", "deep", "c.txt"), "w") as handle:
        handle.write("gamma")
    os.symlink("a.txt", os.path.join(root, "link"))


def _snapshot(backend, root):
    dirs, files = backend.scan_tree(root)
    result = {"/".join(parts): None for parts in dirs}
    for item in files:
        if item.is_symlink:
            result["/".join(item.parts)] = "link"
            continue
        with backend.open_read(backend.join(root, *item.parts)) as handle:
            result["/".join(item.parts)] = handle.read()
    return result


def _item(backend_src, src_dir, backend_dst, dst_dir, name, is_dir):
    return TransferItem(
        name=name,
        src_path=backend_src.join(src_dir, name),
        dst_path=backend_dst.join(dst_dir, name),
        is_dir=is_dir,
    )


def test_copy_name_counts_up():
    assert copy_name("a.txt", []) == "a (copy).txt"
    assert copy_name("a.txt", ["a (copy).txt"]) == "a (copy 2).txt"
    assert copy_name("archive.tar.gz", []) == "archive (copy).tar.gz"
    assert copy_name(".bashrc", []) == ".bashrc (copy)"


def test_local_copy_and_move(tmp_path):
    local = LocalBackend()
    src = tmp_path / "src"
    _tree(str(src))
    dst = tmp_path / "dst"
    dst.mkdir()
    progress = OperationProgress(2)
    items = [
        _item(local, str(src), local, str(dst), "sub", True),
        _item(local, str(src), local, str(dst), "a.txt", False),
    ]
    assert run_transfer(local, local, items, move=False, progress=progress) == []
    assert progress.items_done == 2
    assert _snapshot(local, str(dst / "sub")) == _snapshot(local, str(src / "sub"))
    assert (dst / "a.txt").read_text() == "alpha"
    snap = progress.snapshot()
    assert snap.fraction == 1.0

    moved = tmp_path / "moved"
    moved.mkdir()
    items = [_item(local, str(src), local, str(moved), "sub", True)]
    assert run_transfer(local, local, items, move=True, progress=OperationProgress(1)) == []
    assert not (src / "sub").exists()
    assert (moved / "sub" / "deep" / "c.txt").read_text() == "gamma"


def test_move_merges_into_existing_folder(tmp_path):
    local = LocalBackend()
    src = tmp_path / "src"
    (src / "f").mkdir(parents=True)
    (src / "f" / "new.txt").write_text("new")
    dst = tmp_path / "dst"
    (dst / "f").mkdir(parents=True)
    (dst / "f" / "old.txt").write_text("old")
    items = [_item(local, str(src), local, str(dst), "f", True)]
    assert run_transfer(local, local, items, move=True, progress=OperationProgress(1)) == []
    assert sorted(os.listdir(dst / "f")) == ["new.txt", "old.txt"]
    assert not (src / "f").exists()


def test_folder_into_itself_fails(tmp_path):
    local = LocalBackend()
    (tmp_path / "f").mkdir()
    item = TransferItem("f", str(tmp_path / "f"), str(tmp_path / "f" / "f"), True)
    assert into_itself(local, local, item)
    failures = run_transfer(local, local, [item], move=False, progress=OperationProgress(1))
    assert len(failures) == 1 and failures[0][0] == "f"


def test_cancel_removes_partial_file(tmp_path):
    local = LocalBackend()
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(5 * 1024 * 1024))
    out = tmp_path / "out"
    out.mkdir()
    progress = OperationProgress(1)
    real = progress.item_bytes

    def cancel_midway(key, done, total):
        if done > 1024 * 1024:
            progress.cancel()
        real(key, done, total)

    progress.item_bytes = cancel_midway
    items = [_item(local, str(tmp_path), local, str(out), "big.bin", False)]
    with pytest.raises(OperationCancelled):
        run_transfer(local, local, items, move=False, progress=progress)
    assert not (out / "big.bin").exists()


def test_existing_and_delete(tmp_path):
    local = LocalBackend()
    _tree(str(tmp_path / "t"))
    items = [
        TransferItem("a.txt", "x", str(tmp_path / "t" / "a.txt"), False),
        TransferItem("zz", "x", str(tmp_path / "t" / "zz"), False),
    ]
    assert [item.name for item in existing_destinations(local, items)] == ["a.txt"]
    paths = [str(tmp_path / "t" / "sub"), str(tmp_path / "t" / "link")]
    assert run_delete(local, paths, trash=False, progress=OperationProgress(2)) == []
    assert sorted(os.listdir(tmp_path / "t")) == ["a.txt"]


SFTP_HOST = os.environ.get("MFATFM_TEST_SFTP_HOST")


@pytest.fixture
def sftp(tmp_path):
    if not SFTP_HOST:
        pytest.skip("set MFATFM_TEST_SFTP_HOST to run SFTP tests")
    from mfatfm.backends.sftp import SftpBackend

    backend = SftpBackend(SftpTarget(host=SFTP_HOST))
    backend.connect(env={"SSH_ASKPASS_REQUIRE": "never"})
    root = posixpath.join(backend.home(), f".mfatfm-test-{uuid.uuid4().hex[:8]}")
    backend.mkdir(root)
    try:
        yield backend, root
    finally:
        backend.remove(root)
        backend.close()


def test_sftp_round_trip(tmp_path, sftp):
    backend, root = sftp
    local = LocalBackend()
    src = tmp_path / "src"
    _tree(str(src))
    expected = _snapshot(local, str(src))
    expected["link"] = b"alpha"  # links are uploaded as the file they point to

    up = [_item(local, str(tmp_path), backend, root, "src", True)]
    assert run_transfer(local, backend, up, move=False, progress=OperationProgress(1)) == []
    assert _snapshot(backend, posixpath.join(root, "src")) == expected

    entries = {entry.name: entry for entry in backend.list_dir(posixpath.join(root, "src"))}
    assert entries["sub"].is_dir and entries["a.txt"].size == 5

    # Copy on the server, then move it, then bring it back down.
    copy = [_item(backend, root, backend, root, "src", True)]
    copy[0].dst_path = posixpath.join(root, "copy")
    assert run_transfer(backend, backend, copy, move=False, progress=OperationProgress(1)) == []
    move = [TransferItem("copy", posixpath.join(root, "copy"), posixpath.join(root, "moved"), True)]
    assert run_transfer(backend, backend, move, move=True, progress=OperationProgress(1)) == []
    assert backend.exists_many([posixpath.join(root, "copy"), posixpath.join(root, "moved")]) == [False, True]

    down_dir = tmp_path / "down"
    down_dir.mkdir()
    down = [_item(backend, root, local, str(down_dir), "moved", True)]
    progress = OperationProgress(1)
    assert run_transfer(backend, local, down, move=False, progress=progress) == []
    assert _snapshot(local, str(down_dir / "moved")) == expected
    assert progress.snapshot().fraction == 1.0

    counts = dict(pair for batch in backend.count_items(root, ["src", "moved"]) for pair in batch.items())
    assert counts == {"src": 3, "moved": 3}
    assert backend.directory_size(posixpath.join(root, "src")) == 300_000 + 15  # a.txt, c.txt and the uploaded link


@pytest.mark.parametrize("direction", ["upload", "download"])
def test_sftp_cancel_removes_partial(tmp_path, sftp, direction):
    backend, root = sftp
    local = LocalBackend()
    payload = os.urandom(20 * 1024 * 1024)
    if direction == "upload":
        (tmp_path / "big.bin").write_bytes(payload)
        src, src_dir, dst, dst_dir = local, str(tmp_path), backend, root
    else:
        with backend.open_write(posixpath.join(root, "big.bin")) as handle:
            handle.write(payload)
        out = tmp_path / "out"
        out.mkdir()
        src, src_dir, dst, dst_dir = backend, root, local, str(out)
    progress = OperationProgress(1)
    real = progress.item_bytes

    def cancel_midway(key, done, total):
        if done > 2 * 1024 * 1024:
            progress.cancel()
        real(key, done, total)

    progress.item_bytes = cancel_midway
    item = _item(src, src_dir, dst, dst_dir, "big.bin", False)
    item.size = len(payload)
    with pytest.raises(OperationCancelled):
        run_transfer(src, dst, [item], move=False, progress=progress)
    assert dst.exists_many([item.dst_path]) == [False]


def test_sftp_host_owned_client_and_connector(sftp):
    import mfatsftp

    from mfatfm.backends.sftp import SftpBackend

    _backend, root = sftp
    target = SftpTarget(host=SFTP_HOST)
    client = mfatsftp.connect(SFTP_HOST, ssh_options=["BatchMode=yes"])
    try:
        lent = SftpBackend.from_client(target, client)
        assert lent.connected
        assert any(entry.name == posixpath.basename(root) for entry in lent.list_dir(lent.home()))
        lent.close()
        assert not client.closed  # the host still owns it
        with pytest.raises(ConnectionError):
            lent.connect({})
    finally:
        client.close()

    calls = []

    def connector(env):
        calls.append(env)
        return mfatsftp.connect(SFTP_HOST, ssh_options=["BatchMode=yes"], env=env)

    own = SftpBackend(target, connector=connector)
    own.connect({"MFATFM_TEST": "1"})
    try:
        assert calls == [{"MFATFM_TEST": "1"}] and own.exists_many([root]) == [True]
    finally:
        own.close()
    assert own.closed
