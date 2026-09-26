"""File-system facts about paths, such as whether a folder lives on a network share.

Detection is best effort: any failure answers "not network" and is logged, never raised.
"""

import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath

logger = logging.getLogger(__name__)

DRIVE_REMOTE = 4
"""``GetDriveTypeW`` result for a network (mapped) drive."""

NETWORK_FILESYSTEMS = frozenset(
    {
        "afpfs",
        "afs",
        "ceph",
        "cifs",
        "davfs",
        "fuse.davfs2",
        "fuse.glusterfs",
        "fuse.rclone",
        "fuse.sshfs",
        "glusterfs",
        "ncpfs",
        "nfs",
        "nfs4",
        "smb3",
        "smbfs",
        "webdav",
    }
)
"""Mount types treated as network file systems on Linux and macOS."""

Mount = tuple[str, str]
"""A mount point and its file-system type."""


def is_network_path(path: Path) -> bool:
    """Return whether ``path`` (which need not exist yet) is on a network share."""
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path.absolute())
    try:
        return _is_network_resolved(resolved)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        logger.warning("Could not tell whether %s is on a network share: %s", path, e)
        return False


def is_network_windows_path(path: str, drive_type: Callable[[str], int]) -> bool:
    """Windows rules: UNC paths are network; drive letters ask ``drive_type("X:\\\\")``."""
    drive = PureWindowsPath(path).drive
    upper = drive.upper()
    if upper.startswith("\\\\?\\UNC\\"):
        return True
    if upper.startswith("\\\\?\\"):  # extended-length local path, e.g. \\?\C:
        drive = drive[4:]
    elif drive.startswith("\\\\"):
        return True
    if len(drive) == 2 and drive[1] == ":":
        return drive_type(drive + "\\") == DRIVE_REMOTE
    return False


def is_network_mount_path(path: str, mounts: list[Mount]) -> bool:
    """POSIX rules: find the mount that contains ``path`` and check its file-system type."""
    best: Mount | None = None
    for mount_point, fstype in mounts:
        prefix = mount_point.rstrip("/") + "/"
        if (path == mount_point or path.startswith(prefix)) and (
            best is None or len(mount_point) > len(best[0])
        ):
            best = (mount_point, fstype)
    return best is not None and best[1] in NETWORK_FILESYSTEMS


def parse_proc_mounts(text: str) -> list[Mount]:
    """Parse Linux ``/proc/self/mounts``; spaces in paths appear as octal escapes (``\\040``)."""
    mounts: list[Mount] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 3:
            mounts.append((_unescape_octal(fields[1]), fields[2]))
    return mounts


def parse_bsd_mount_output(text: str) -> list[Mount]:
    """Parse macOS/BSD ``mount`` output: ``//u@nas/music on /Volumes/music (smbfs, ...)``."""
    mounts: list[Mount] = []
    for line in text.splitlines():
        head, sep, options = line.rpartition(" (")
        _, on, mount_point = head.partition(" on ")
        if sep and on:
            mounts.append((mount_point, options.split(",")[0].rstrip(")").strip()))
    return mounts


def _unescape_octal(field: str) -> str:
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and field[i + 1 : i + 4].isdigit() and len(field[i + 1 : i + 4]) == 3:
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def _read_mounts() -> list[Mount]:
    proc = Path("/proc/self/mounts")
    if proc.exists():
        return parse_proc_mounts(proc.read_text(encoding="utf-8", errors="replace"))
    result = subprocess.run(["mount"], capture_output=True, text=True, timeout=5, check=True)
    return parse_bsd_mount_output(result.stdout)


if sys.platform == "win32":
    import ctypes

    def _drive_type(root: str) -> int:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))

    def _is_network_resolved(path: str) -> bool:
        return is_network_windows_path(path, _drive_type)

else:

    def _is_network_resolved(path: str) -> bool:
        return is_network_mount_path(path, _read_mounts())
