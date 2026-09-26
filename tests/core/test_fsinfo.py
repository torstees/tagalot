"""Tests for network-path detection."""

from pathlib import Path

import pytest

from tagalot.core.fsinfo import (
    DRIVE_REMOTE,
    is_network_mount_path,
    is_network_path,
    is_network_windows_path,
    parse_bsd_mount_output,
    parse_proc_mounts,
)

DRIVE_FIXED = 3


def _drives(root: str) -> int:
    """Fake GetDriveTypeW: M: and N: are mapped network drives."""
    return DRIVE_REMOTE if root.upper() in {"M:\\", "N:\\"} else DRIVE_FIXED


@pytest.mark.parametrize(
    ("path", "network"),
    [
        (r"\\nas\music\Music.keep", True),  # UNC
        (r"\\?\UNC\nas\music\Music.keep", True),  # extended-length UNC
        (r"M:\Keeps\Music.keep", True),  # mapped drive
        (r"n:\Keeps", True),  # lowercase drive letter
        (r"C:\Users\ana\Music.keep", False),
        (r"\\?\C:\Users\ana\Music.keep", False),  # extended-length local path
        (r"relative\path", False),
    ],
)
def test_windows_rules(path: str, network: bool) -> None:
    assert is_network_windows_path(path, _drives) is network


LINUX_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
//nas/music /mnt/music cifs rw,vers=3.0 0 0
nas:/export/art /mnt/art\\040work nfs4 rw 0 0
/dev/sdb1 /mnt/music/local-cache ext4 rw 0 0
ana@host:/srv /home/ana/remote fuse.sshfs rw 0 0
C:\\134 /mnt/c 9p rw 0 0
"""


def test_parse_proc_mounts_unescapes_spaces() -> None:
    mounts = parse_proc_mounts(LINUX_MOUNTS)
    assert ("/mnt/art work", "nfs4") in mounts
    assert ("/mnt/music", "cifs") in mounts


@pytest.mark.parametrize(
    ("path", "network"),
    [
        ("/home/ana/Music.keep", False),
        ("/mnt/music/Music.keep", True),
        ("/mnt/music", True),
        ("/mnt/musicbox/Music.keep", False),  # prefix of a name is not containment
        ("/mnt/music/local-cache/Music.keep", False),  # deeper local mount wins
        ("/mnt/art work/Art.keep", True),
        ("/home/ana/remote/x.keep", True),
        ("/mnt/c/Users/ana/Music.keep", False),  # WSL's Windows drives are local disks
    ],
)
def test_linux_rules(path: str, network: bool) -> None:
    assert is_network_mount_path(path, parse_proc_mounts(LINUX_MOUNTS)) is network


MACOS_MOUNT_OUTPUT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse)
//ana@nas._smb._tcp.local/music on /Volumes/music (smbfs, nodev, nosuid, mounted by ana)
nas:/export/art on /Volumes/art share (nfs, nodev, nosuid)
"""


@pytest.mark.parametrize(
    ("path", "network"),
    [
        ("/Users/ana/Music.keep", False),
        ("/System/Volumes/Data/Users/ana/Music.keep", False),
        ("/Volumes/music/Music.keep", True),
        ("/Volumes/art share/Art.keep", True),
    ],
)
def test_macos_rules(path: str, network: bool) -> None:
    assert is_network_mount_path(path, parse_bsd_mount_output(MACOS_MOUNT_OUTPUT)) is network


def test_local_temp_dir_is_not_network(tmp_path: Path) -> None:
    assert is_network_path(tmp_path) is False
    assert is_network_path(tmp_path / "not" / "created" / "yet") is False
