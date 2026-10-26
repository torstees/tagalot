"""Finding WinRAR and 7-Zip off PATH on Windows, and saying which tool reads RAR (#452)."""

import io
from pathlib import Path
from typing import Any

import pytest
import rarfile

from tagalot import selfcheck
from tagalot.core.rar_tools import (
    RarTool,
    configure_rar_tools,
    current_rar_tool,
    find_rar_tools,
)
from tagalot.core.thumbnails import archive
from tagalot.core.thumbnails.archive import NoRarTool
from tagalot.themes.readers import read_comic_info

PROGRAMS = {"ProgramW6432": r"C:\Program Files", "ProgramFiles(x86)": r"C:\Program Files (x86)"}
UNRAR = r"C:\Program Files\WinRAR\UnRAR.exe"
SEVENZIP = r"C:\Program Files (x86)\7-Zip\7z.exe"


def _find(on_path: set[str], installed: set[str], windows: bool = True) -> dict[str, str]:
    return find_rar_tools(
        windows=windows,
        env=PROGRAMS,
        which=lambda command: f"/usr/bin/{command}" if command in on_path else None,
        is_file=lambda path: path in installed,
    )


def test_tools_off_path_are_found_in_their_folders() -> None:
    assert _find(set(), {UNRAR, SEVENZIP}) == {"UNRAR_TOOL": UNRAR, "SEVENZIP_TOOL": SEVENZIP}
    assert _find({"unrar"}, {UNRAR, SEVENZIP}) == {"SEVENZIP_TOOL": SEVENZIP}  # on PATH: as is
    assert _find(set(), set()) == {}
    assert _find(set(), {UNRAR}, windows=False) == {}  # elsewhere, PATH is the place


def test_configuring_points_rarfile_at_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rarfile, "UNRAR_TOOL", "unrar")
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", rarfile.ToolSetup(rarfile.BSDTAR_CONFIG))
    assert configure_rar_tools({"UNRAR_TOOL": UNRAR}) == {"UNRAR_TOOL": UNRAR}
    assert rarfile.UNRAR_TOOL == UNRAR
    assert rarfile.CURRENT_SETUP is None  # chosen again when next needed


def test_the_tool_in_use_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rarfile, "UNRAR_TOOL", UNRAR)
    monkeypatch.setattr(rarfile, "CURRENT_SETUP", rarfile.ToolSetup(rarfile.UNRAR_CONFIG))
    assert current_rar_tool() == RarTool("unrar", UNRAR)
    assert "with unrar (C:\\Program Files\\WinRAR\\UnRAR.exe)" in selfcheck._rarfile()

    monkeypatch.setattr(rarfile, "CURRENT_SETUP", rarfile.ToolSetup(rarfile.BSDTAR_CONFIG))
    tool = current_rar_tool()
    assert tool is not None
    assert (tool.name, tool.weak) == ("bsdtar", True)
    assert "bsdtar can't unpack many RAR files: install WinRAR or 7-Zip" in selfcheck._rarfile()

    def none(**_: Any) -> None:
        raise rarfile.RarCannotExec("Cannot find working tool")

    monkeypatch.setattr(rarfile, "CURRENT_SETUP", None)
    monkeypatch.setattr(rarfile, "tool_setup", none)
    assert current_rar_tool() is None
    with pytest.raises(RuntimeError, match="install WinRAR or 7-Zip"):
        selfcheck._rarfile()


class _Info:
    def __init__(self, name: str, size: int) -> None:
        self.filename, self.file_size = name, size

    def is_dir(self) -> bool:
        return False

    def needs_password(self) -> bool:
        return False


class _ComicRar:
    """A .cbr whose ComicInfo.xml needs a tool that isn't there."""

    def __init__(self, path: str) -> None:
        self.path = path

    def is_solid(self) -> bool:
        return False

    def infolist(self) -> list[_Info]:
        return [_Info("ComicInfo.xml", 40), _Info("001.jpg", 1000)]

    def open(self, name: str) -> io.BytesIO:
        raise rarfile.RarCannotExec("Cannot find working tool")

    def close(self) -> None:
        pass


def test_a_comic_whose_info_cant_be_read_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It used to read as having no ComicInfo, silently (#452)."""
    monkeypatch.setattr(rarfile, "RarFile", _ComicRar)
    path = tmp_path / "Sandman #001 (1989).cbr"
    path.write_bytes(archive.RAR_MAGIC + b"\x00" * 20)
    with pytest.raises(NoRarTool, match=r"needs unrar or 7-Zip.*Re-read from file"):
        read_comic_info(str(path))
