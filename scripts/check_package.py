"""Run ``tagalot --check`` on the build in ``dist/`` (``just package``), as CI does."""

import subprocess
import sys
import tempfile
from pathlib import Path

DIST = Path(__file__).resolve().parents[1] / "dist"


def built_app() -> Path:
    """The packaged executable for this platform."""
    if sys.platform == "win32":
        return DIST / "tagalot" / "tagalot.exe"
    if sys.platform == "darwin":
        return DIST / "Tagalot.app" / "Contents" / "MacOS" / "tagalot"
    return DIST / "tagalot" / "tagalot"


def main() -> int:
    app = built_app()
    if not app.exists():
        print(f"No build at {app}: run `just package` first.")
        return 1
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "check.txt"
        code = subprocess.run(
            [str(app), "--check", "--output", str(report)], check=False
        ).returncode
        print(report.read_text(encoding="utf-8") if report.exists() else "(no report)", end="")
    print(f"{app}: {'ok' if code == 0 else f'check failed ({code})'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
