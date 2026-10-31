# PyInstaller build of Tagalot (DESIGN.md §3 "Packaging", #129).
#
#   uv sync --group package
#   uv run pyinstaller packaging/tagalot.spec --noconfirm
#
# makes dist/tagalot/ (a folder with tagalot.exe or tagalot) on Windows and Linux, and
# dist/Tagalot.app on macOS. Check the build with `tagalot --check` (a windowed Windows
# build has no console: add `--output check.txt` and read the file).

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (PyInstaller defines SPECPATH)
SRC = ROOT / "src"
PACKAGING = ROOT / "packaging"

sys.path.insert(0, str(SRC))
import tagalot  # noqa: E402

a = Analysis(  # noqa: F821
    [str(SRC / "tagalot" / "__main__.py")],
    pathex=[str(SRC)],
    # pymediainfo's Linux wheel carries libmediainfo.so.0, which PyInstaller's hook misses
    # (its patterns end at .so) and replaces with a system copy, when there is one (#623).
    binaries=collect_dynamic_libs("pymediainfo", search_patterns=["lib*.so.*"]),
    # Built-in themes are found with pkgutil at run time, so nothing imports them by name.
    hiddenimports=collect_submodules("tagalot.builtin_themes"),
    # The theme template is copied as source by `tagalot --new-theme`.
    datas=[
        (str(SRC / "tagalot" / "themes" / "template.py"), "tagalot/themes"),
        # The window icon (ui.app), at each size, the About dialog's tower, and the knight.
        (str(SRC / "tagalot" / "resources" / "tagalot-*.png"), "tagalot/resources"),
        # Lucide's icons, for types, keeps, and the window (ui.icons), with their license.
        (str(SRC / "tagalot" / "resources" / "icons"), "tagalot/resources/icons"),
        # The list of Google Fonts the font picker searches (core.google_fonts).
        (str(SRC / "tagalot" / "resources" / "google_fonts.json"), "tagalot/resources"),
    ],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="tagalot",
    console=False,  # a desktop app: no console window on Windows
    icon=str(PACKAGING / "tagalot.ico"),  # the .exe's icon in Explorer and the taskbar
    disable_windowed_traceback=False,
    upx=False,
)
# A console twin for scheduled runs on Windows (#610): tagalot.exe is windowed, so it shows
# no output and a scheduler waiting on it learns little. tagalot-cli.exe runs the same code
# (__main__), prints, and returns the exit code. macOS and Linux need none: the usual
# command prints and returns its code there.
cli = (
    [
        EXE(  # noqa: F821
            pyz,
            a.scripts,
            [],
            exclude_binaries=True,
            name="tagalot-cli",
            console=True,
            icon=str(PACKAGING / "tagalot.ico"),
            upx=False,
        )
    ]
    if sys.platform == "win32"
    else []
)
coll = COLLECT(exe, *cli, a.binaries, a.datas, name="tagalot", upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="Tagalot.app",
        bundle_identifier="io.github.torstees.tagalot",
        icon=str(PACKAGING / "tagalot.icns"),
        version=tagalot.__version__,
        info_plist={
            "CFBundleName": "Tagalot",
            "CFBundleDisplayName": "Tagalot",
            "CFBundleShortVersionString": tagalot.__version__,
            "NSHighResolutionCapable": True,
        },
    )
