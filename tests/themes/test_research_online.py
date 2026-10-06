"""The research theme's online details (#341): Crossref by DOI, arXiv by arXiv ID, ranked
between a library export and the PDF, on the research demo. Answers come from
``tests/fixtures/online``; nothing goes online."""

import importlib.util
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from tagalot.builtin_themes.research import (
    Paper,
    ResearchTheme,
    arxiv_values,
    crossref_values,
    jats_text,
)
from tagalot.core.models import Entity, FieldProvenance, FieldSource
from tagalot.core.online import Reply
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.themes.api import EntityRef, Record

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "online"
ATTENTION = "Attention Is All You Need"
RESNET = "Deep Residual Learning for Image Recognition"
RESNET_URL = "https://api.crossref.org/works/10.1109%2Fcvpr.2016.90"
ATTENTION_URL = "https://export.arxiv.org/api/query?id_list=1706.03762"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Services:
    """Crossref and arXiv, answering from the fixtures (404 for anything else)."""

    def __init__(self) -> None:
        self.answers = {
            RESNET_URL: Reply(200, _fixture("crossref_resnet.json")),
            ATTENTION_URL: Reply(200, _fixture("arxiv_attention.xml")),
        }
        self.urls: list[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Reply:
        self.urls.append(url)
        return self.answers.get(url, Reply(404, b"Resource not found."))


@pytest.fixture
def services() -> Services:
    return Services()


@pytest.fixture
def session(tmp_path: Path, services: Services) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_research_demo(tmp_path), Settings()) as session:
        session.online_opener = services
        session.set_online_lookups("allow")
        yield session


def _paper(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(
            select(Entity.id).where(Entity.title == title, Entity.type == "research.paper")
        )
    assert found is not None
    return found


def _fields(session: KeepSession, paper: int) -> dict[str, Any]:
    table = session.schema.by_type_id("research.paper").table
    with session.reader.connect() as conn:
        row = conn.execute(select(table).where(table.c.id == paper)).one()
    return dict(row._mapping)


def _source(session: KeepSession, paper: int, name: str) -> FieldSource | None:
    with session.reader.connect() as conn:
        return conn.scalar(
            select(FieldProvenance.source).where(
                FieldProvenance.entity_id == paper, FieldProvenance.field == name
            )
        )


def _papers(session: KeepSession) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.type == "research.paper")))


def test_what_is_looked_up() -> None:
    theme = ResearchTheme()

    def record(**fields: Any) -> Record:
        return Record(EntityRef(1, "research.paper"), "A paper", fields, {})

    assert list(theme.online_requests(Paper, record(doi="10.1109/cvpr.2016.90"))) == [RESNET_URL]
    assert list(theme.online_requests(Paper, record(arxiv="1706.03762"))) == [ATTENTION_URL]
    both = record(doi="10.1/x y", arxiv="1706.03762")  # the published version's record
    assert list(theme.online_requests(Paper, both)) == [
        "https://api.crossref.org/works/10.1%2Fx%20y"
    ]
    assert list(theme.online_requests(Paper, record(arxiv="hep-th/9901001"))) == [
        "https://export.arxiv.org/api/query?id_list=hep-th/9901001"
    ]
    assert list(theme.online_requests(Paper, record())) == []


def test_reading_the_answers() -> None:
    import json

    crossref = crossref_values(json.loads(_fixture("crossref_resnet.json")))
    assert {k: v for k, (_, v) in crossref.items()} == {
        "title": RESNET,
        "authors": ["Kaiming He", "Xiangyu Zhang", "Shaoqing Ren", "Jian Sun"],
        "year": 2016,
        "kind": "conference paper",
        "venue": "2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)",
        "volume": None,
        "issue": None,
        "pages": "770-778",
        "abstract": "Deeper neural networks are more difficult to train. We present a "
        "residual learning framework & more.",
    }
    arxiv = arxiv_values(_fixture("arxiv_attention.xml").decode())
    assert arxiv is not None
    values = {k: v for k, (_, v) in arxiv.items()}
    assert (values["title"], values["year"], values["kind"]) == (ATTENTION, 2017, "preprint")
    assert len(values["authors"]) == 8
    assert values["abstract"].startswith("The dominant sequence transduction models")
    assert arxiv_values(_fixture("arxiv_unknown.xml").decode()) is None
    assert jats_text("<jats:p>a &lt; b</jats:p>") == "a < b"


def test_online_details_rank_below_the_library_export(
    session: KeepSession, services: Services
) -> None:
    report = session.look_up(_papers(session))
    assert sorted(services.urls) == [
        RESNET_URL,
        "https://api.crossref.org/works/10.5555%2F3600270.3602281",  # Crossref: not found
        ATTENTION_URL,
    ]
    assert (report.items, report.not_found) == (3, 1)
    resnet = _fields(session, _paper(session, RESNET))
    # What the export says stays; what no file says comes from Crossref.
    assert resnet["venue"] == "IEEE Conference on Computer Vision and Pattern Recognition"
    assert resnet["abstract"].startswith("Deeper neural networks are more difficult to train.")
    assert _source(session, _paper(session, RESNET), "abstract") is FieldSource.FETCHED
    attention = _fields(session, _paper(session, ATTENTION))
    assert attention["abstract"].startswith("The dominant sequence transduction models")
    assert attention["authors"] == (
        "Ashish Vaswani; Noam Shazeer; Niki Parmar; Jakob Uszkoreit"  # the export's four
    )


def test_online_details_outrank_the_pdf_and_survive_scans(
    session: KeepSession, services: Services
) -> None:
    session.look_up(_papers(session))
    export = Path(session.root_path("papers")) / "Zotero" / "My Library.bib"
    text = export.read_text(encoding="utf-8")
    start, end = text.index("@inproceedings{he_deep_2016"), text.index("@inproceedings{ouyang")
    export.write_text(text[:start] + text[end:], encoding="utf-8")
    session.scan_all()  # the export no longer describes ResNet: the PDF and Crossref do
    resnet = _fields(session, _paper(session, RESNET))
    assert resnet["venue"] == (
        "2016 IEEE Conference on Computer Vision and Pattern Recognition (CVPR)"
    )
    assert resnet["kind"] == "conference paper"
    assert resnet["abstract"].startswith("Deeper neural networks")  # kept through the scan
    assert "online:crossref" in resnet["origins"]


def test_a_service_that_no_longer_knows_the_paper_lets_go(
    session: KeepSession, services: Services
) -> None:
    attention = _paper(session, ATTENTION)
    session.look_up([attention])
    assert _fields(session, attention)["abstract"]
    services.answers[ATTENTION_URL] = Reply(200, _fixture("arxiv_unknown.xml"))
    session.look_up([attention], refresh=True)
    fields = _fields(session, attention)
    assert fields["abstract"] is None
    assert "online:arxiv" not in fields["origins"]
