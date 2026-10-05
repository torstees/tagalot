"""Reading bibliography files (#319): BibTeX/BibLaTeX, RIS, and CSL-JSON as Zotero (and
Better BibTeX), JabRef, and Mendeley export them. The fixtures are described in
tests/fixtures/bibliography/README.md."""

from pathlib import Path
from typing import Any

import pytest

from tagalot.themes.api import BIBLIOGRAPHY_EXTENSIONS, read_bibliography
from tagalot.themes.bibliography import (
    bibtex_files,
    bibtex_names,
    latex_text,
    parse_bibtex,
    parse_csl_json,
    parse_ris,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bibliography"
KEYS = {
    "type", "kind", "citekey", "title", "authors", "editors", "year", "venue", "volume",
    "issue", "pages", "publisher", "doi", "arxiv", "pmid", "url", "abstract", "keywords",
    "files",
}  # fmt: skip


def _read(name: str) -> dict[str, dict[str, Any]]:
    entries = read_bibliography(str(FIXTURES / name))
    assert all(set(e) == KEYS for e in entries)  # the same keys, whatever the format
    return {e["citekey"] or e["title"]: e for e in entries}


def test_zotero_bibtex() -> None:
    entries = _read("zotero.bib")
    attention = entries["vaswani_attention_2017"]
    assert attention["title"] == "Attention Is All You Need"
    assert attention["authors"] == [
        "Ashish Vaswani",
        "Noam Shazeer",
        "Niki Parmar",
        "Jakob Uszkoreit",
    ]
    assert (attention["kind"], attention["arxiv"], attention["doi"]) == (
        "preprint",
        "1706.03762",
        None,
    )
    assert attention["venue"] is None  # "arXiv preprint arXiv:…" is no venue
    assert attention["year"] == 2017
    assert attention["keywords"] == [
        "Computer Science - Computation and Language",
        "Computer Science - Machine Learning",
    ]
    storage = "C:\\Users\\ann\\Zotero\\storage\\K7X2ABCD\\"
    assert attention["files"] == [
        storage + "Vaswani et al. - 2017 - Attention Is All You Need.pdf",
        storage + "1706.html",
    ]
    resnet = entries["he_deep_2016"]
    assert (resnet["kind"], resnet["doi"], resnet["pages"]) == (
        "conference paper",
        "10.1109/cvpr.2016.90",
        "770-778",
    )
    assert (
        resnet["venue"] == "2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)"
    )
    thesis = entries["muller_uber_2019"]
    assert thesis["title"] == "Über Größe und Schwére: a thesis & more"
    assert thesis["authors"] == ["Jörg Müller", "Søren Østergaard", "Barnes and Noble"]
    assert (thesis["kind"], thesis["publisher"]) == ("thesis", "Universität Zürich")


def test_better_bibtex_biblatex() -> None:
    entries = _read("betterbibtex.bib")
    numpy = entries["harris2020array"]
    assert numpy["title"] == "Array Programming with NumPy"
    assert numpy["authors"] == ["Charles R. Harris", "K. Jarrod Millman", "Stéfan J. van der Walt"]
    assert numpy["venue"] == "Nature Research"  # @string and #
    assert (numpy["year"], numpy["volume"], numpy["issue"]) == (2020, "585", "7825")
    assert (numpy["doi"], numpy["pmid"]) == ("10.1038/s41586-020-2649-2", "32939066")
    assert numpy["keywords"] == ["numpy", "python"]
    assert numpy["files"] == [
        "/home/ann/Zotero/storage/ABCD1234/Harris et al. - 2020 - Array programming with NumPy.pdf"
    ]
    ouyang = entries["ouyang2022training"]
    assert (ouyang["kind"], ouyang["arxiv"], ouyang["year"]) == ("preprint", "2203.02155", 2022)
    assert "broken" not in entries
    assert entries["after_broken"]["title"] == "Still Read After a Broken Entry"


def test_jabref_and_mendeley_files() -> None:
    entries = _read("jabref_mendeley.bib")
    assert entries["Lee2018"]["files"] == ["papers/Lee2018.pdf"]
    kim = entries["Kim2019"]
    assert kim["files"] == [
        "C:/Users/ann/Documents/Mendeley Desktop/Kim - 2019 - A Mendeley Entry.pdf"
    ]
    assert (kim["title"], kim["arxiv"]) == ("A Mendeley Entry", "1901.00001")


def test_zotero_ris() -> None:
    entries = _read("zotero.ris")
    numpy = entries["Array programming with NumPy"]
    assert (numpy["kind"], numpy["venue"], numpy["pages"]) == ("article", "Nature", "357-362")
    assert numpy["authors"] == ["Charles R. Harris", "K. Jarrod Millman"]
    assert numpy["abstract"] is not None
    assert numpy["abstract"].endswith("operating on data.")  # a value continued on the next line
    assert numpy["keywords"] == ["numpy", "Software"]
    assert numpy["files"] == [
        "C:/Users/ann/Zotero/storage/ABCD1234/Harris et al. - 2020 - Array programming.pdf"
    ]
    thesis = entries["A Thesis"]
    assert (thesis["kind"], thesis["year"], thesis["publisher"]) == (
        "thesis",
        2018,
        "Some University",
    )


def test_csl_json() -> None:
    entries = _read("library.json")
    numpy = entries["harris2020array"]
    assert numpy["authors"] == [
        "Charles R. Harris",
        "Stéfan J. van der Walt",
        "The NumPy Developers",
    ]
    assert (numpy["year"], numpy["pmid"], numpy["pages"]) == (2020, "32939066", "357-362")
    ouyang = entries["ouyang2022"]
    assert (ouyang["kind"], ouyang["arxiv"]) == ("preprint", "2203.02155")


def test_json_that_isnt_csl_gives_nothing() -> None:
    assert parse_csl_json('{"name": "package.json", "version": "1.0"}') == []
    assert parse_csl_json("[1, 2, 3]") == []
    assert parse_csl_json("not json") == []


def test_other_files_are_refused(tmp_path: Path) -> None:
    assert {".bib", ".ris", ".json"} == BIBLIOGRAPHY_EXTENSIONS
    other = tmp_path / "a.txt"
    other.write_text("@article{x, title={y}}", encoding="utf-8")
    with pytest.raises(ValueError, match="not a bibliography file"):
        read_bibliography(str(other))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (r"{\"o}", "ö"),
        (r"\'{e}t\'e", "été"),
        (r"\c{c}a \c c", "ça ç"),
        (r"{\v S}koda", "Škoda"),
        (r"na{\"\i}ve", "naïve"),
        (r"\aa{}ngstr\"om", "ångström"),
        (r"Tom \& Jerry 100\%", "Tom & Jerry 100%"),
        (r"pages 1--2, a---b", "pages 1\u20132, a\u2014b"),
        (r"\emph{very} \textbf{bold}", "very bold"),
        (r"{{NumPy}} and {BERT}", "NumPy and BERT"),
        (r"Fermi~level $\alpha$", "Fermi level"),
    ],
)
def test_latex_text(value: str, text: str) -> None:
    assert latex_text(value) == text


@pytest.mark.parametrize(
    ("value", "names"),
    [
        ("Doe, Jane and Smith, John", ["Jane Doe", "John Smith"]),
        ("Jane Doe AND John Smith", ["Jane Doe", "John Smith"]),
        ("{van der Berg}, Jan", ["Jan van der Berg"]),
        ("King, Jr, Martin Luther", ["Martin Luther King, Jr"]),
        ("{World Health Organization} and others", ["World Health Organization"]),
        ("", []),
    ],
)
def test_bibtex_names(value: str, names: list[str]) -> None:
    assert bibtex_names(value) == names


def test_bibtex_files() -> None:
    assert bibtex_files("/a/b.pdf;/c/d.pdf") == ["/a/b.pdf", "/c/d.pdf"]
    assert bibtex_files(r"PDF:C\:\\x\\y.pdf:application/pdf") == ["C:\\x\\y.pdf"]


def test_parsers_take_text() -> None:
    [entry] = parse_bibtex("@book{k, title = 1984, author = {Orwell, George}}")
    assert (entry["kind"], entry["title"], entry["authors"]) == ("book", "1984", ["George Orwell"])
    [entry] = parse_ris("TY  - RPRT\nTI  - A Report\nER  - \n")
    assert (entry["kind"], entry["title"]) == ("report", "A Report")
    assert parse_bibtex('@comment{anything} @preamble{"x"}') == []
