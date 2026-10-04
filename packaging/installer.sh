#!/usr/bin/env bash
# Make this OS's installer from the PyInstaller build (DESIGN.md §3 "Packaging", #269).
#
#   uv run pyinstaller packaging/tagalot.spec --noconfirm
#   bash packaging/installer.sh
#
# writes, into dist/:
#   Windows  Tagalot-<version>-Windows-setup.exe       (Inno Setup 6: iscc on PATH or in
#                                                       Program Files; `winget install
#                                                       JRSoftware.InnoSetup`)
#   macOS    Tagalot-<version>-macOS-<arch>.dmg         (drag Tagalot to Applications)
#   Linux    Tagalot-<version>-Linux-<arch>.AppImage    (downloads appimagetool into build/)
#
# and prints the installer's path last. The builds aren't signed (#279).
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
version="$(uv run --no-sync python -c "import tagalot; print(tagalot.__version__)")"
arch="$(uname -m)"

case "$(uname -s)" in
  MINGW* | MSYS* | CYGWIN* | Windows_NT)
    iscc="$(command -v iscc || true)"
    for candidate in "/c/Program Files (x86)/Inno Setup 6/ISCC.exe" \
      "/c/Program Files/Inno Setup 6/ISCC.exe" \
      "${LOCALAPPDATA:-}/Programs/Inno Setup 6/ISCC.exe"; do
      if [ -z "$iscc" ] && [ -f "$candidate" ]; then iscc="$candidate"; fi
    done
    if [ -z "$iscc" ]; then
      echo "Inno Setup 6 isn't installed (winget install JRSoftware.InnoSetup)" >&2
      exit 1
    fi
    # //: Git Bash would otherwise turn /Q into a path.
    "$iscc" //Q "//DAppVersion=$version" packaging/tagalot.iss
    out="dist/Tagalot-$version-Windows-setup.exe"
    ;;
  Darwin)
    stage="build/dmg"
    rm -rf "$stage"
    mkdir -p "$stage"
    cp -R dist/Tagalot.app "$stage/"
    ln -s /Applications "$stage/Applications"
    out="dist/Tagalot-$version-macOS-$arch.dmg"
    rm -f "$out"
    hdiutil create -volname "Tagalot $version" -srcfolder "$stage" -ov -format UDZO "$out"
    ;;
  Linux)
    appdir="build/Tagalot.AppDir"
    rm -rf "$appdir"
    mkdir -p "$appdir/usr/bin"
    cp -R dist/tagalot/. "$appdir/usr/bin/"
    cp packaging/tagalot.desktop "$appdir/tagalot.desktop"
    cp packaging/tagalot.png "$appdir/tagalot.png"
    cat > "$appdir/AppRun" << 'EOF'
#!/bin/sh
here="$(dirname "$(readlink -f "$0")")"
exec "$here/usr/bin/tagalot" "$@"
EOF
    chmod +x "$appdir/AppRun"
    tool="build/appimagetool-$arch.AppImage"
    if [ ! -x "$tool" ]; then
      curl -fsSL -o "$tool" \
        "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$arch.AppImage"
      chmod +x "$tool"
    fi
    out="dist/Tagalot-$version-Linux-$arch.AppImage"
    # No FUSE needed (CI runners and containers lack it): the tool unpacks itself and runs.
    APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$arch" "$tool" --no-appstream "$appdir" "$out"
    ;;
  *)
    echo "No installer for $(uname -s)" >&2
    exit 1
    ;;
esac

ls -l "$out"
echo "$out"
