"""Bundle the list of Google Fonts (https://fonts.google.com) as
``src/tagalot/resources/google_fonts.json`` (DESIGN.md §12 *Fonts*), so Keep configuration's
font picker can search it offline. Only the list ships; a font is downloaded into a keep
when its user asks (``core/google_fonts.py``).

Run it to refresh the list: ``uv run python scripts/fetch_google_fonts.py``. It reads the
``google/fonts`` repository on GitHub at its newest commit (the files each family has and
its license) and Google Fonts' family metadata (categories and popularity), and pins the
list to that commit, so downloads find the files it names.
"""

import json
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

REPO = "google/fonts"
TARGET = (
    Path(__file__).resolve().parent.parent / "src" / "tagalot" / "resources" / "google_fonts.json"
)
LICENSE_FOLDERS = {"ofl": "OFL.txt", "apache": "LICENSE.txt", "ufl": "UFL.txt"}
CATEGORIES = {
    "Serif": "serif",
    "Sans Serif": "sans-serif",
    "Display": "display",
    "Handwriting": "handwriting",
    "Monospace": "monospace",
}


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "tagalot-fetch-google-fonts"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return bytes(response.read())


def chosen_files(names: list[str]) -> list[str]:
    """The files of a family to download, regular first: its variable fonts (upright, then
    italic), else its regular, bold, italic, and bold italic."""
    fonts = sorted(n for n in names if n.endswith(".ttf"))
    variable = [n for n in fonts if "[" in n]
    if variable:
        return sorted(variable, key=lambda n: "Italic" in n)
    wanted = []
    for style in ("Regular", "Bold", "Italic", "BoldItalic"):
        wanted += [n for n in fonts if n.endswith(f"-{style}.ttf")]
    return wanted or fonts[:1]


def main() -> int:
    commit = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/main"))["sha"]
    tree = json.loads(_get(f"https://api.github.com/repos/{REPO}/git/trees/{commit}?recursive=1"))
    if tree.get("truncated"):
        print("The repository's listing came back truncated", file=sys.stderr)
        return 1
    folders: dict[str, list[str]] = defaultdict(list)
    for entry in tree["tree"]:
        parts = entry["path"].split("/")
        if len(parts) == 3 and parts[0] in LICENSE_FOLDERS:
            folders[f"{parts[0]}/{parts[1]}"].append(parts[2])
    by_key = {folder.split("/")[1]: folder for folder in folders}
    metadata = _get("https://fonts.google.com/metadata/fonts").decode("utf-8")
    families = json.loads(metadata[metadata.index("{") :])["familyMetadataList"]
    families.sort(key=lambda f: f.get("popularity", 1_000_000))
    listed, missing = [], []
    for family in families:
        folder = by_key.get(re.sub(r"[^a-z0-9]", "", family["family"].lower()))
        files = chosen_files(folders[folder]) if folder else []
        license_file = LICENSE_FOLDERS[folder.split("/")[0]] if folder else ""
        if not folder or not files or license_file not in folders[folder]:
            missing.append(family["family"])
            continue
        category = CATEGORIES.get(family["category"], "display")
        listed.append([family["family"], category, folder, files, license_file])
    data = {
        "source": f"https://raw.githubusercontent.com/{REPO}/{commit}",
        "families": listed,
    }
    TARGET.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("],[", "],\n[") + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"{len(listed)} families at {commit[:12]} in {TARGET.name}")
    if missing:
        print(f"Left out (no files found): {', '.join(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
