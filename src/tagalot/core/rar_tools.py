"""The external tool RAR archives are read with (#452, DESIGN.md §10 *Archives*).

rarfile lists a RAR archive itself but needs a program to unpack compressed members:
``unrar``, ``unar``, 7-Zip, or ``bsdtar``, the first that runs, looked for on PATH. On Windows
WinRAR and 7-Zip are often installed without being on PATH, so rarfile fell back to a
``bsdtar`` that can't unpack many RAR files, or found nothing. :func:`configure_rar_tools`
(run at start-up) points rarfile at them in their usual folders; :func:`current_rar_tool`
names the one used, for ``tagalot --check``.
"""

import logging
import os
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath

import rarfile

logger = logging.getLogger(__name__)

WINDOWS_TOOLS: tuple[tuple[str, str, str], ...] = (
    ("UNRAR_TOOL", "unrar", r"WinRAR\UnRAR.exe"),
    ("SEVENZIP_TOOL", "7z", r"7-Zip\7z.exe"),
)
"""(rarfile's setting, the command on PATH, where its installer puts it under a programs
folder), in rarfile's order of preference."""

PROGRAM_FOLDERS = ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)")
"""Environment variables naming Windows' programs folders (64-bit first)."""

TOOL_NAMES = {
    "UNRAR_TOOL": "unrar",
    "UNAR_TOOL": "unar",
    "SEVENZIP_TOOL": "7-Zip",
    "SEVENZIP2_TOOL": "7-Zip",
    "BSDTAR_TOOL": "bsdtar",
}
"""How people know each tool, by rarfile's setting."""


def find_rar_tools(
    *,
    windows: bool = sys.platform == "win32",
    env: Mapping[str, str] = os.environ,
    which: Callable[[str], str | None] = shutil.which,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> dict[str, str]:
    """The tools to point rarfile at, ``{setting: full path}``: on Windows, each of WinRAR's
    UnRAR and 7-Zip that isn't on PATH but is in its usual folder. Elsewhere, none."""
    if not windows:
        return {}
    found: dict[str, str] = {}
    for setting, command, relative in WINDOWS_TOOLS:
        if which(command):
            continue  # rarfile finds it itself
        for variable in PROGRAM_FOLDERS:
            folder = env.get(variable)
            if not folder:
                continue
            path = str(PureWindowsPath(folder) / relative)
            if is_file(path):
                found[setting] = path
                break
    return found


def configure_rar_tools(found: Mapping[str, str] | None = None) -> dict[str, str]:
    """Point rarfile at the tools :func:`find_rar_tools` finds (or ``found``), and have it
    choose its tool again; returns them."""
    found = dict(find_rar_tools() if found is None else found)
    for setting, path in found.items():
        setattr(rarfile, setting, path)
        logger.info("RAR archives: using %s, which isn't on PATH", path)
    if found:
        rarfile.CURRENT_SETUP = None  # chosen again, with these, when next needed
    return found


@dataclass(frozen=True)
class RarTool:
    """The tool rarfile unpacks RAR members with."""

    name: str
    """``unrar``, ``unar``, ``7-Zip``, or ``bsdtar``."""
    command: str
    """What is run: a command on PATH, or a full path."""

    @property
    def weak(self) -> bool:
        """bsdtar (libarchive) can't unpack solid archives, newer compression, or passwords,
        so many RAR files fail with it."""
        return self.name == "bsdtar"


def current_rar_tool() -> RarTool | None:
    """The tool rarfile uses (choosing it now if it hasn't), or ``None`` when none runs."""
    try:
        setup = rarfile.tool_setup()
    except rarfile.RarCannotExec:
        return None
    setting = setup.setup["open_cmd"][0]
    return RarTool(TOOL_NAMES.get(setting, setting), str(getattr(rarfile, setting, setting)))
