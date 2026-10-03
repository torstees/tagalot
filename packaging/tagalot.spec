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

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent  # noqa: F821 (PyInstaller defines SPECPATH)
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))
import tagalot  # noqa: E402

a = Analysis(  # noqa: F821
    [str(SRC / "tagalot" / "__main__.py")],
    pathex=[str(SRC)],
    # Built-in themes are found with pkgutil at run time, so nothing imports them by name.
    hiddenimports=collect_submodules("tagalot.builtin_themes"),
    # The theme template is copied as source by `tagalot --new-theme`.
    datas=[(str(SRC / "tagalot" / "themes" / "template.py"), "tagalot/themes")],
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
    disable_windowed_traceback=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="tagalot", upx=False)  # noqa: F821

if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll,
        name="Tagalot.app",
        bundle_identifier="io.github.torstees.tagalot",
        version=tagalot.__version__,
        info_plist={
            "CFBundleName": "Tagalot",
            "CFBundleDisplayName": "Tagalot",
            "CFBundleShortVersionString": tagalot.__version__,
            "NSHighResolutionCapable": True,
        },
    )
