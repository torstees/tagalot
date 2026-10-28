"""Google Fonts (#566, DESIGN.md §12 *Fonts*): the bundled list, and downloading a family into
the keep folder, from one host, only when asked."""

import urllib.error
from pathlib import Path

import pytest

from tagalot.core.fonts import read_font_files
from tagalot.core.google_fonts import (
    CATEGORIES,
    HOST,
    GoogleFont,
    GoogleFontError,
    download_font,
    downloaded,
    fetch_url,
    google_fonts,
    local_name,
)

LITERATA = GoogleFont(
    "Literata",
    "serif",
    "ofl/literata",
    ("Literata[opsz,wght].ttf", "Literata-Italic[opsz,wght].ttf"),
    "OFL.txt",
)


def test_the_bundled_list() -> None:
    fonts = google_fonts()
    assert fonts.source.startswith(f"https://{HOST}/google/fonts/")
    assert len(fonts.families) > 1000
    names = {f.name: f for f in fonts.families}
    assert names["Literata"].category == "serif"
    assert names["Literata"].files[0] == "Literata[opsz,wght].ttf"
    assert {f.category for f in fonts.families} <= set(CATEGORIES)
    assert all(f.files and f.license for f in fonts.families)


def test_names_in_the_keep() -> None:
    assert local_name("Literata-Italic[opsz,wght].ttf") == "Literata-Italic.ttf"
    assert local_name("Lato-Bold.ttf") == "Lato-Bold.ttf"
    assert LITERATA.value == "fonts/Literata/Literata.ttf"
    odd = GoogleFont('A:B "C"', "display", "ofl/abc", ("ABC-Regular.ttf",), "OFL.txt")
    assert odd.value == "fonts/AB C/ABC-Regular.ttf"


def test_downloading_a_family(tmp_path: Path) -> None:
    fetched: list[str] = []
    progress: list[tuple[int, int]] = []

    def fetch(url: str) -> bytes:
        fetched.append(url)
        return url.rsplit("/", 1)[1].encode()

    value = download_font(
        tmp_path,
        LITERATA,
        fetch=fetch,
        progress=lambda d, t: progress.append((d, t)),
        source="https://raw.githubusercontent.com/google/fonts/abc",
    )
    assert value == "fonts/Literata/Literata.ttf"
    folder = tmp_path / "fonts" / "Literata"
    assert sorted(p.name for p in folder.iterdir()) == [
        "Literata-Italic.ttf",
        "Literata.ttf",
        "OFL.txt",
    ]
    assert fetched[0] == (
        "https://raw.githubusercontent.com/google/fonts/abc/ofl/literata/Literata%5Bopsz%2Cwght%5D.ttf"
    )
    assert progress[0] == (0, 3)
    assert progress[-1] == (3, 3)
    assert downloaded(tmp_path, (LITERATA,)) == {"Literata"}

    # Again: what's there is kept, and nothing is fetched.
    fetched.clear()
    download_font(tmp_path, LITERATA, fetch=fetch)
    assert fetched == []


def test_a_familys_other_files_come_with_it(tmp_path: Path) -> None:
    folder = tmp_path / "fonts" / "Literata"
    folder.mkdir(parents=True)
    for name in ("Literata.ttf", "Literata-Italic.ttf", "OFL.txt"):
        (folder / name).write_bytes(name.encode())
    found = read_font_files(tmp_path, {"titles": "fonts/Literata/Literata.ttf"})
    assert found == {
        "fonts/Literata/Literata.ttf": b"Literata.ttf",
        "fonts/Literata/Literata-Italic.ttf": b"Literata-Italic.ttf",
    }


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (urllib.error.HTTPError("u", 404, "Not Found", {}, None), f"{HOST} answered 404"),  # type: ignore[arg-type]
        (urllib.error.URLError("offline"), f"Couldn't reach {HOST}: offline"),
        (TimeoutError("timed out"), f"Couldn't reach {HOST}"),
    ],
)
def test_downloads_that_fail(tmp_path: Path, error: Exception, message: str) -> None:
    def fetch(url: str) -> bytes:
        raise error

    with pytest.raises(GoogleFontError, match=message):
        download_font(tmp_path, LITERATA, fetch=fetch)
    assert not (tmp_path / "fonts" / "Literata" / "Literata.ttf").exists()


def test_fonts_come_only_from_their_host() -> None:
    with pytest.raises(GoogleFontError, match=f"only downloaded from {HOST}"):
        fetch_url("https://example.com/ofl/literata/OFL.txt")
    with pytest.raises(GoogleFontError):
        fetch_url(f"http://{HOST}/google/fonts/x")
