"""The session's problem log for the activity panel (#111)."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.activity import (
    INGEST,
    OFFLINE,
    READ,
    THUMBNAIL,
    WARNING,
    Problem,
    ProblemLog,
    problems_from_report,
    thumbnail_problems,
)
from tagalot.core.ingest import IngestWarning
from tagalot.core.models import Entity
from tagalot.core.scanjob import ScanReport
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def test_a_scan_report_as_problems() -> None:
    offline = ScanReport("nas", online=False, error="no such share")
    [down] = problems_from_report(offline)
    assert (down.kind, down.message, down.root_id, down.relpath) == (
        OFFLINE,
        "no such share",
        "nas",
        None,
    )
    report = ScanReport(
        "r",
        online=True,
        read_errors=[("Photos/locked", "Permission denied"), ("", "root unreadable")],
        ingest_errors=[("a.mp3", "boom")],
        ingest_warnings=[
            IngestWarning("r", "b.png", "Couldn't read its details"),
            IngestWarning(None, None, "option"),
        ],
    )
    found = [(p.kind, p.root_id, p.relpath, p.message) for p in problems_from_report(report)]
    assert found == [
        (READ, "r", "Photos/locked", "Permission denied"),
        (READ, "r", None, "root unreadable"),
        (INGEST, "r", "a.mp3", "boom"),
        (WARNING, "r", "b.png", "Couldn't read its details"),
        (WARNING, "r", None, "option"),
    ]


def test_thumbnail_problems() -> None:
    found = thumbnail_problems([("D:/x.png", "corrupt"), ("entity 3", "provider failed")])
    assert [(p.kind, p.path, p.message) for p in found] == [
        (THUMBNAIL, "D:/x.png", "corrupt"),
        (THUMBNAIL, None, "provider failed"),
    ]


def test_the_log_keeps_the_newest_and_counts_changes() -> None:
    log = ProblemLog(limit=3)
    version = log.version
    log.add([])
    assert log.version == version  # nothing added, nothing changed
    log.add([Problem(READ, str(i)) for i in range(5)])
    assert [p.message for p in log.items()] == ["2", "3", "4"]
    assert len(log) == 3
    assert log.version == version + 1
    log.clear()
    assert (len(log), log.version) == (0, version + 2)


@pytest.fixture
def assets(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        yield session


def test_scans_and_thumbnails_fill_the_sessions_log(assets: KeepSession) -> None:
    broken = Path(assets.root_path("assets")) / "Kenji Sato" / "broken.png"
    broken.write_bytes(b"not an image")
    assets.scan_all()
    [problem] = assets.problems.items()
    assert (problem.kind, problem.root_id, problem.relpath) == (
        WARNING,
        "assets",
        "Kenji Sato/broken.png",
    )
    with assets.reader.connect() as conn:
        entity = conn.scalar(select(Entity.id).where(Entity.title == "broken.png"))
    assert entity is not None
    assets.thumbnails.resolve(entity)
    thumbnail = assets.problems.items()[-1]
    assert (thumbnail.kind, thumbnail.path) == (THUMBNAIL, str(broken))
