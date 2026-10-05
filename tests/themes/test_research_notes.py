"""Literature notes (#321): Markdown files whose front matter names a paper (citation key,
DOI, arXiv ID, PubMed ID) are linked to it as its notes; what they say outranks the
bibliographies, and their tags are the paper's file keywords."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.research import NOTE, Paper, note_details
from tagalot.core.keywords import keywords_of
from tagalot.core.models import Resource
from tests.core.book_files import write_text_pdf
from tests.themes.test_research import Env
from tests.themes.test_research_bibliography import T0, ZOTERO_EXPORT, _roles, _zotero, env

__all__ = ["env"]  # the fixture


def _note(path: Path, front: str, body: str = "# My notes\n\nThoughts.\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{front}---\n{body}", encoding="utf-8")
    return path


def test_note_details() -> None:
    front = {
        "title": "Attention Is All You Need",
        "authors": ["Ashish Vaswani"],
        "keywords": ["to-read"],
        "fields": {"Title": "Attention Is All You Need", "citekey": "@vaswani2017"},
    }
    details = note_details(front)
    assert details is not None
    assert details["ids"]["citekey"] == "vaswani2017"  # Obsidian's "@" dropped
    assert details["values"]["title"] == (NOTE, "Attention Is All You Need")
    assert details["keywords"] == ["to-read"]
    assert note_details({"fields": {"title": "Just a note"}, "keywords": []}) is None
    by_doi = note_details({"fields": {"DOI": "https://doi.org/10.1109/CVPR.2016.90"}})
    assert by_doi is not None
    assert by_doi["ids"]["doi"] == "10.1109/cvpr.2016.90"


def test_a_note_links_to_its_paper_by_citation_key(env: Env) -> None:
    _zotero(env)
    (env.files / "Zotero/My Library.bib").parent.mkdir(parents=True, exist_ok=True)
    (env.files / "Zotero/My Library.bib").write_text(ZOTERO_EXPORT, encoding="utf-8")
    # "Notes/…" sorts before "Zotero/…", but bibliographies are read before notes.
    _note(
        env.files / "Notes/attention.md",
        "citekey: vaswani_attention_2017\ntags: [to-read, '#Transformers']\nyear: 2018\n",
    )
    env.scan()
    roles = _roles(env, "Attention Is All You Need")
    assert roles["notes"] == ["Notes/attention.md"]
    assert env.paper("Attention Is All You Need")["year"] == 2018  # the note outranks the export
    with env.reader.connect() as conn:
        paper = env.id_of("Attention Is All You Need")
        assert keywords_of(conn, [paper])[paper] == ["to-read", "Transformers"]


def test_a_note_by_doi_and_a_note_naming_no_paper(env: Env) -> None:
    _zotero(env)
    _note(env.files / "resnet notes.md", "doi: 10.1109/CVPR.2016.90\ntitle: ResNet\n")
    _note(env.files / "orphan.md", "doi: 10.1/nothing\n")
    _note(env.files / "plain.md", "title: Not a literature note\n")
    env.scan()
    assert _roles(env, "ResNet")["notes"] == ["resnet notes.md"]  # its title wins too
    titles = env.titles(Paper)
    assert "orphan" not in titles
    assert "plain" not in titles  # Markdown files are never papers themselves


def test_a_note_read_before_its_paper_finds_it_later(env: Env) -> None:
    _note(env.files / "Notes/resnet.md", "doi: 10.1109/CVPR.2016.90\n")
    env.scan()
    assert env.titles(Paper) == []
    write_text_pdf(env.files / "resnet.pdf", ["Deep Residual Learning", "doi:10.1109/CVPR.2016.90"])
    env.scan(T0 + timedelta(minutes=1))
    assert _roles(env, "resnet")["notes"] == ["Notes/resnet.md"]


def test_a_note_that_stops_naming_its_paper_lets_go(env: Env) -> None:
    _zotero(env)
    note = _note(env.files / "notes.md", "doi: 10.1109/CVPR.2016.90\ntitle: ResNet\n")
    env.scan()
    assert "ResNet" in env.titles(Paper)
    _note(note, "title: Something else now\n")
    env.scan(T0 + timedelta(minutes=1))
    assert "ResNet" not in env.titles(Paper)
    assert "notes" not in _roles(env, "resnet")  # back to its PDF's name


@pytest.mark.parametrize("ext", [".md", ".markdown"])
def test_markdown_extensions_are_read(env: Env, ext: str) -> None:
    _note(env.files / f"a{ext}", "title: x\n")
    env.scan()
    with env.reader.connect() as conn:
        assert conn.scalar(select(Resource.relpath).where(Resource.ext == ext)) == f"a{ext}"
