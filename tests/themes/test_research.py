"""The research theme (#318): papers from PDFs, keyed by identifier, with their authors in
order."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import aliased

from tagalot.builtin_themes.research import (
    Author,
    Paper,
    ResearchTheme,
    paper_details,
    paper_name,
)
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.keywords import keywords_of
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.relations import move_related
from tagalot.core.scanjob import ScanReport, scan_root
from tagalot.core.theme_db import open_theme
from tagalot.core.theme_schema import ThemeSchema
from tagalot.core.writer import DbWriter
from tagalot.themes.api import find_identifiers
from tagalot.themes.loader import validate_theme
from tests.core.book_files import write_text_pdf

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def test_the_theme_is_valid() -> None:
    assert validate_theme(ResearchTheme) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("doi: 10.1038/s41586-020-2649-2.", ("10.1038/s41586-020-2649-2", None, None)),
        ("https://doi.org/10.1109/CVPR.2016.90)", ("10.1109/cvpr.2016.90", None, None)),
        ("arXiv:1706.03762v5 [cs.CL] 6 Dec 2017", (None, "1706.03762", "v5")),
        ("see arxiv.org/abs/hep-th/9901001", (None, "hep-th/9901001", None)),
        ("DOI 10.48550/arXiv.2203.02155", (None, "2203.02155", None)),
        ("2101.01234v2", (None, "2101.01234", "v2")),
        ("no identifiers here, 2021.", (None, None, None)),
    ],
)
def test_find_identifiers(text: str, expected: tuple[Any, ...]) -> None:
    found = find_identifiers(text)
    assert (found["doi"], found["arxiv"], found["arxiv_version"]) == expected


def test_find_a_pubmed_id() -> None:
    assert find_identifiers("PMID: 32939066 PMCID: PMC7759461")["pmid"] == "32939066"


@pytest.mark.parametrize(
    ("relpath", "expected"),
    [
        ("A/Vaswani et al. - 2017 - Attention Is All You Need.pdf",
         ("Attention Is All You Need", ["Vaswani"], 2017, None)),
        ("He and Sun - 2016 - Deep Residual Learning.pdf",
         ("Deep Residual Learning", ["He", "Sun"], 2016, None)),
        ("Smith et al. (2020) A Study.pdf", ("A Study", ["Smith"], 2020, None)),
        ("1706.03762v7.pdf", ("1706.03762v7", [], None, "1706.03762")),
        ("Some_Paper Title.pdf", ("Some Paper Title", [], None, None)),
    ],
)  # fmt: skip
def test_paper_names(relpath: str, expected: tuple[Any, ...]) -> None:
    name = paper_name(relpath)
    assert (name["title"], name["authors"], name["year"], name["arxiv"]) == expected


def test_details_prefer_the_pdf_but_not_its_junk() -> None:
    info = {"title": "Microsoft Word - draft3.docx", "authors": ["jsmith"], "keywords": []}
    details = paper_details("Smith et al. - 2020 - A Study.pdf", info, "arXiv:2001.00001")
    assert (details["title"], details["authors"], details["year"]) == ("A Study", ["Smith"], 2020)
    assert details["arxiv"] == "2001.00001"
    good = {"title": "A Real Title", "authors": ["Ann Lee", "Bo Chen"], "keywords": ["NLP"]}
    details = paper_details("x.pdf", good, "")
    assert (details["title"], details["authors"], details["keywords"]) == (
        "A Real Title",
        ["Ann Lee", "Bo Chen"],
        ["NLP"],
    )
    assert paper_details("1706.03762v7.pdf", None, "")["year"] == 2017  # from the arXiv ID


@dataclass
class Env:
    writer: DbWriter
    reader: Engine
    schema: ThemeSchema
    files: Path

    def scan(self, when: datetime = T0) -> ScanReport:
        root = RootConfig("r", "Papers", str(self.files))
        return scan_root(
            self.writer, self.reader, root, root.path, when=when, theme=ResearchTheme,
            schema=self.schema,
        )  # fmt: skip

    def titles(self, entity: type) -> list[str]:
        type_id = ResearchTheme.type_id_of(entity)
        with self.reader.connect() as conn:
            return sorted(conn.scalars(select(Entity.title).where(Entity.type == type_id)))

    def paper(self, title: str) -> dict[str, Any]:
        table = self.schema.entities[Paper].table
        with self.reader.connect() as conn:
            row = conn.execute(
                select(table).join(Entity, Entity.id == table.c.id).where(Entity.title == title)
            ).one()
        return dict(row._mapping)

    def files_of(self, title: str) -> list[str]:
        with self.reader.connect() as conn:
            return sorted(
                conn.scalars(
                    select(Resource.relpath)
                    .join(EntityResource, EntityResource.resource_id == Resource.id)
                    .join(Entity, Entity.id == EntityResource.entity_id)
                    .where(Entity.title == title)
                )
            )

    def authors(self, title: str) -> list[str]:
        t = self.schema.relationships["authors"].table
        person = aliased(Entity)
        with self.reader.connect() as conn:
            return list(
                conn.scalars(
                    select(person.title)
                    .join(t, t.c.a_id == person.id)
                    .join(Entity, Entity.id == t.c.b_id)
                    .where(Entity.title == title)
                    .order_by(t.c.position)
                )
            )

    def id_of(self, title: str) -> int:
        with self.reader.connect() as conn:
            found = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert found is not None
        return found


VASWANI = "Ashish Vaswani; Noam Shazeer; Niki Parmar"
OUYANG = "Training language models to follow instructions with human feedback"


def library(files: Path) -> None:
    write_text_pdf(
        files / "Library/Vaswani et al. - 2017 - Attention Is All You Need.pdf",
        ["Attention Is All You Need", "arXiv:1706.03762v5 [cs.CL]"],
        Author=VASWANI,
    )
    write_text_pdf(  # a second download: the same paper
        files / "Downloads/1706.03762v7.pdf", ["Attention Is All You Need", "arXiv:1706.03762v7"]
    )
    write_text_pdf(
        files / "Library/He et al. - 2016 - Deep Residual Learning for Image Recognition.pdf",
        ["Deep Residual Learning", "doi:10.1109/CVPR.2016.90"],
        Title="Deep Residual Learning for Image Recognition",
        Author="Kaiming He; Xiangyu Zhang; Shaoqing Ren; Jian Sun",
        Keywords="Vision; Deep learning",
    )
    write_text_pdf(  # a preprint, whose published version comes later
        files / "Preprints/2203.02155v1.pdf",
        ["Training language models to follow instructions", "arXiv:2203.02155v1"],
        Title=OUYANG,
        Author="Long Ouyang; Jeff Wu",
    )
    write_text_pdf(files / "Notes/A Paper Without Metadata.pdf", ["Just some text."])


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    library(files)
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "R", ThemeRef("research", 1)))
    schema = open_theme(engine, keep, ResearchTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


def test_a_scan_makes_papers_and_authors(env: Env) -> None:
    env.scan()
    assert env.titles(Paper) == [
        "A Paper Without Metadata",
        "Attention Is All You Need",
        "Deep Residual Learning for Image Recognition",
        OUYANG,
    ]
    assert env.files_of("Attention Is All You Need") == [
        "Downloads/1706.03762v7.pdf",
        "Library/Vaswani et al. - 2017 - Attention Is All You Need.pdf",
    ]  # one paper, two versions
    assert env.authors("Attention Is All You Need") == [
        "Ashish Vaswani",
        "Noam Shazeer",
        "Niki Parmar",
    ]
    attention = env.paper("Attention Is All You Need")
    assert (attention["arxiv"], attention["year"], attention["kind"]) == (
        "1706.03762",
        2017,
        "preprint",
    )
    resnet = env.paper("Deep Residual Learning for Image Recognition")
    assert (resnet["doi"], resnet["kind"]) == ("10.1109/cvpr.2016.90", "article")
    assert resnet["authors"] == "Kaiming He; Xiangyu Zhang; Shaoqing Ren; Jian Sun"
    table = env.schema.entities[Author].table
    with env.reader.connect() as conn:
        assert "Vaswani, Ashish" in set(conn.scalars(select(table.c.sort_name)))
        paper_id = env.id_of("Deep Residual Learning for Image Recognition")
        assert keywords_of(conn, [paper_id])[paper_id] == ["Deep learning", "Vision"]


def test_the_published_version_joins_its_preprint(env: Env) -> None:
    env.scan()
    write_text_pdf(
        env.files / "Library/Ouyang et al. - 2022 - Training language models.pdf",
        ["Training language models", "https://doi.org/10.5555/3600270.3602281", "arXiv:2203.02155"],
    )
    env.scan(T0 + timedelta(minutes=1))
    assert env.files_of(OUYANG) == [
        "Library/Ouyang et al. - 2022 - Training language models.pdf",
        "Preprints/2203.02155v1.pdf",
    ]
    paper = env.paper(OUYANG)
    assert (paper["doi"], paper["arxiv"], paper["kind"]) == (
        "10.5555/3600270.3602281",
        "2203.02155",
        "article",
    )
    assert env.authors(OUYANG) == ["Long Ouyang", "Jeff Wu"]  # kept from the first file


def test_better_names_win_whichever_file_is_read_first(env: Env) -> None:
    # "Library/…" is read before "Preprints/…": the file-named version comes first, and the
    # preprint's document info then names the paper and its authors.
    write_text_pdf(
        env.files / "Library/Ouyang et al. - 2022 - Training language models.pdf",
        ["Training language models", "doi:10.5555/3600270.3602281", "arXiv:2203.02155"],
        Title="Microsoft Word - final_v3.docx",
    )
    env.scan()
    assert env.titles(Paper).count(OUYANG) == 1
    assert "Training language models" not in env.titles(Paper)
    assert env.authors(OUYANG) == ["Long Ouyang", "Jeff Wu"]
    assert "Ouyang" not in env.titles(Author)  # the file name's surname gave way


def test_a_changed_file_updates_its_paper_and_authors(env: Env) -> None:
    env.scan()
    title = "Deep Residual Learning for Image Recognition"
    write_text_pdf(
        env.files / f"Library/He et al. - 2016 - {title}.pdf",
        ["Deep Residual Learning", "doi:10.1109/CVPR.2016.90", "revised"],
        Title=title,
        Author="Kaiming He; Jian Sun",
    )
    env.scan(T0 + timedelta(minutes=1))
    assert env.authors(title) == ["Kaiming He", "Jian Sun"]
    assert "Xiangyu Zhang" not in env.titles(Author)  # left with no paper: gone


def test_a_hand_made_author_order_survives_a_rescan(env: Env) -> None:
    env.scan()
    title = "Attention Is All You Need"
    paper, parmar = env.id_of(title), env.id_of("Niki Parmar")
    env.writer.run(lambda conn: move_related(conn, env.schema, "authors", paper, [parmar], -2))
    write_text_pdf(  # the file changes; its author order doesn't win over the user's
        env.files / "Library/Vaswani et al. - 2017 - Attention Is All You Need.pdf",
        ["Attention Is All You Need", "arXiv:1706.03762v5", "revised"],
        Author=VASWANI,
    )
    env.scan(T0 + timedelta(minutes=1))
    assert env.authors(title) == ["Niki Parmar", "Ashish Vaswani", "Noam Shazeer"]


def test_files_without_metadata_and_unreadable_ones(env: Env) -> None:
    (env.files / "Broken.pdf").write_bytes(b"%PDF-1.4 nothing")
    env.scan()
    assert "A Paper Without Metadata" in env.titles(Paper)
    assert "Broken" in env.titles(Paper)


def _record(title: str, **fields: Any) -> Any:
    return SimpleNamespace(title=title, fields=fields)


def test_near_duplicates() -> None:
    theme = ResearchTheme()
    a = _record("Attention Is All You Need", arxiv="1706.03762", authors="Ashish Vaswani")
    b = _record("Attention is all you need!", doi="10.1/x", authors="Vaswani, A.")
    assert set(theme.blocking_keys(Paper, a)) == {"arxiv:1706.03762", "author:vaswani"}
    assert "author:vaswani" in set(theme.blocking_keys(Paper, b))
    assert theme.similarity(Paper, a, b) >= theme.near_duplicate_threshold
    c = _record("Attention Is All You Need", doi="10.1/y")
    assert theme.similarity(Paper, b, c) == 0.0  # different DOIs: different papers
    assert theme.similarity(Paper, a, _record("x", arxiv="1706.03762")) == 1.0
    assert list(theme.blocking_keys(Author, a)) == []
