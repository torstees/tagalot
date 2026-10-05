"""Research papers described by bibliography files (#320): sidecars and library exports
(Zotero), matched by file path or identifier, whichever is scanned first, with each
source's details kept and the best one winning."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.research import Paper, ResearchTheme, Venue
from tagalot.core.db import create_keep_engine, open_keep_database
from tagalot.core.keep import ThemeRef, create_keep
from tagalot.core.keywords import keywords_of
from tagalot.core.models import Entity, EntityResource, Resource
from tagalot.core.theme_db import open_theme
from tagalot.core.writer import DbWriter
from tagalot.themes.loader import validate_theme
from tests.core.book_files import write_text_pdf
from tests.themes.test_research import Env

T0 = datetime(2026, 10, 1, tzinfo=UTC)
STORAGE = "C:\\Users\\ann\\Zotero\\storage\\"

ZOTERO_EXPORT = (
    "@article{vaswani_attention_2017,\n"
    "  title = {Attention {Is} {All} {You} {Need}},\n"
    "  journal = {Advances in Neural Information Processing Systems},\n"
    "  volume = {30},\n"
    "  author = {Vaswani, Ashish and Shazeer, Noam},\n"
    "  year = {2017},\n"
    "  keywords = {Transformers},\n"
    "  file = {Full Text PDF:"
    + STORAGE.replace("\\", "\\\\").replace(":", "\\:")
    + "K7X2ABCD\\\\attention.pdf:application/pdf},\n"
    "}\n"
    "@inproceedings{he_deep_2016,\n"
    "  title = {Deep Residual Learning for Image Recognition},\n"
    "  booktitle = {CVPR},\n"
    "  author = {He, Kaiming and Sun, Jian},\n"
    "  doi = {10.1109/CVPR.2016.90},\n"
    "  year = {2016},\n"
    "}\n"
    "@article{nobody_2020,\n"
    "  title = {A Paper Nobody Downloaded},\n"
    "  doi = {10.1/none},\n"
    "}\n"
)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    files = tmp_path / "files"
    files.mkdir()
    keep, engine = open_keep_database(create_keep(tmp_path / "k", "R", ThemeRef("research", 1)))
    schema = open_theme(engine, keep, ResearchTheme).schema
    reader = create_keep_engine(keep.db_path, read_only=True)
    with DbWriter(engine) as writer:
        yield Env(writer, reader, schema, files)
    reader.dispose()
    engine.dispose()


def _roles(env: Env, title: str) -> dict[str, list[str]]:
    with env.reader.connect() as conn:
        rows = conn.execute(
            select(EntityResource.role, Resource.relpath)
            .join(Resource, Resource.id == EntityResource.resource_id)
            .join(Entity, Entity.id == EntityResource.entity_id)
            .where(Entity.title == title)
        ).all()
    found: dict[str, list[str]] = {}
    for name, relpath in rows:
        found.setdefault(name, []).append(relpath)
    return {k: sorted(v) for k, v in found.items()}


def _venue_of(env: Env, title: str) -> list[str]:
    from sqlalchemy.orm import aliased

    from tagalot.core.models import EntityContains

    parent = aliased(Entity)
    with env.reader.connect() as conn:
        return list(
            conn.scalars(
                select(parent.title)
                .join(EntityContains, EntityContains.parent_id == parent.id)
                .join(Entity, Entity.id == EntityContains.child_id)
                .where(Entity.title == title, parent.type == ResearchTheme.type_id_of(Venue))
            )
        )


def test_the_theme_reads_bibliographies_last() -> None:
    assert ResearchTheme.read_last == {".bib", ".ris", ".json"}
    assert validate_theme(ResearchTheme) == []

    class Bad(ResearchTheme):
        read_last = frozenset({".BIB"})

    assert "read_last extension '.BIB' must be lowercase and start with '.'" in validate_theme(Bad)


def test_a_sidecar_describes_its_pdf(env: Env) -> None:
    write_text_pdf(env.files / "Library/smith2020.pdf", ["no identifiers"])
    (env.files / "Library/smith2020.bib").write_text(
        "@article{smith2020,\n  title = {A Careful Study},\n  author = {Smith, Ann},\n"
        "  journal = {Journal of Tests},\n  volume = {12},\n  year = {2020},\n}\n",
        encoding="utf-8",
    )
    env.scan()
    assert env.titles(Paper) == ["A Careful Study"]  # not the file name "smith2020"
    paper = env.paper("A Careful Study")
    assert (paper["venue"], paper["volume"], paper["year"]) == ("Journal of Tests", "12", 2020)
    assert (paper["citekey"], paper["kind"]) == ("smith2020", "article")
    assert env.authors("A Careful Study") == ["Ann Smith"]
    assert _roles(env, "A Careful Study") == {
        "bibliography": ["Library/smith2020.bib"],
        "paper": ["Library/smith2020.pdf"],
    }
    assert env.titles(Venue) == ["Journal of Tests"]
    assert _venue_of(env, "A Careful Study") == ["Journal of Tests"]


def _zotero(env: Env) -> None:
    write_text_pdf(
        env.files / "storage/K7X2ABCD/attention.pdf", ["Attention", "no id"], Title="Draft"
    )
    write_text_pdf(
        env.files / "Downloads/resnet.pdf", ["Deep Residual Learning", "doi:10.1109/CVPR.2016.90"]
    )


def test_a_zotero_export_matches_by_storage_path_and_doi(env: Env) -> None:
    _zotero(env)
    (env.files / "My Library.bib").write_text(ZOTERO_EXPORT, encoding="utf-8")
    env.scan()
    # By the storage path: the export's details win over the PDF's ("Draft").
    attention = env.paper("Attention Is All You Need")
    assert attention["venue"] == "Advances in Neural Information Processing Systems"
    assert env.authors("Attention Is All You Need") == ["Ashish Vaswani", "Noam Shazeer"]
    assert _roles(env, "Attention Is All You Need")["bibliography"] == ["My Library.bib"]
    # By DOI.
    resnet = env.paper("Deep Residual Learning for Image Recognition")
    assert (resnet["venue"], resnet["kind"]) == ("CVPR", "conference paper")
    # An entry matching no file makes no paper.
    assert "A Paper Nobody Downloaded" not in env.titles(Paper)
    with env.reader.connect() as conn:
        paper_id = env.id_of("Attention Is All You Need")
        assert keywords_of(conn, [paper_id])[paper_id] == ["Transformers"]
    origins = json.loads(attention["origins"])
    assert len(origins) == 2  # the PDF's and the export's details, each kept


def test_an_export_read_before_its_pdf_is_read_again(env: Env) -> None:
    (env.files / "My Library.bib").write_text(ZOTERO_EXPORT, encoding="utf-8")
    env.scan()
    assert env.titles(Paper) == []  # nothing to describe yet
    _zotero(env)
    env.scan(T0 + timedelta(minutes=1))  # new PDFs: the unchanged export is read again
    assert env.paper("Attention Is All You Need")["volume"] == "30"
    assert "Deep Residual Learning for Image Recognition" in env.titles(Paper)


def test_an_entry_removed_from_the_export_lets_go(env: Env) -> None:
    _zotero(env)
    export = env.files / "My Library.bib"
    export.write_text(ZOTERO_EXPORT, encoding="utf-8")
    env.scan()
    start = ZOTERO_EXPORT.index("@inproceedings")
    export.write_text(ZOTERO_EXPORT[:start], encoding="utf-8")
    env.scan(T0 + timedelta(minutes=1))
    # ResNet's details are the PDF's again: its file name, no venue, no export link.
    resnet = env.paper("resnet")
    assert (resnet["doi"], resnet["venue"]) == ("10.1109/cvpr.2016.90", None)
    assert "bibliography" not in _roles(env, "resnet")
    assert env.titles(Venue) == ["Advances in Neural Information Processing Systems"]


def test_a_sidecar_outranks_an_export(env: Env) -> None:
    _zotero(env)
    (env.files / "My Library.bib").write_text(ZOTERO_EXPORT, encoding="utf-8")
    (env.files / "Downloads/resnet.bib").write_text(
        "@article{he2016,\n  title = {ResNet, as the Sidecar Says},\n"
        "  doi = {10.1109/CVPR.2016.90},\n}\n",
        encoding="utf-8",
    )
    env.scan()
    assert "ResNet, as the Sidecar Says" in env.titles(Paper)
    assert env.paper("ResNet, as the Sidecar Says")["venue"] == "CVPR"  # the export's still


def _read_at(env: Env, relpath: str) -> datetime | None:
    with env.reader.connect() as conn:
        found: datetime | None = conn.scalar(
            select(Resource.ingested_at).where(Resource.relpath == relpath)
        )
    return found


def test_an_export_is_read_again_only_when_its_folder_changed(env: Env) -> None:
    _zotero(env)
    (env.files / "My Library.bib").write_text(ZOTERO_EXPORT, encoding="utf-8")
    env.scan()
    first = _read_at(env, "My Library.bib")
    env.scan(T0 + timedelta(minutes=1))  # nothing changed: not read again
    assert _read_at(env, "My Library.bib") == first
    write_text_pdf(env.files / "Downloads/new.pdf", ["A new download"])
    env.scan(T0 + timedelta(minutes=2))  # a new file: the export is read again, after it
    assert _read_at(env, "My Library.bib") != first
