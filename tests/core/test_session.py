"""Tests for KeepSession: opening a keep with its theme and services, and scanning it."""

from pathlib import Path

import pytest
from sqlalchemy import func, select

from tagalot.core.keep import KeepError, RootConfig, ThemeRef, create_keep
from tagalot.core.models import Entity
from tagalot.core.search import run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.theme_db import KeepThemeError


@pytest.fixture
def files(tmp_path: Path) -> Path:
    folder = tmp_path / "files"
    (folder / "Iceland").mkdir(parents=True)
    (folder / "Iceland/glacier.jpg").write_bytes(b"x" * 100)
    (folder / "notes.txt").write_bytes(b"y" * 10)
    return folder


def _keep(tmp_path: Path, root: Path, theme: str = "generic") -> Path:
    keep = create_keep(
        tmp_path / "k", "Files", ThemeRef(theme, 1), [RootConfig("r", "Files", str(root))]
    )
    return keep.dir


def test_open_scan_search_close(tmp_path: Path, files: Path) -> None:
    session = KeepSession.open(_keep(tmp_path, files), Settings())
    try:
        assert session.theme.id == "generic"
        messages: list[str] = []
        [report] = session.scan_all(progress=messages.append)
        assert (report.new, report.ingested) == (2, 2)
        assert any(m.startswith("Walking Files") for m in messages)
        assert any(m.startswith("Ingesting 2 of 2") for m in messages)
        with session.reader.connect() as conn:
            hits = run_search(conn, SearchSpec(text="glacier"), session.tag_cache.get())
        assert [h.title for h in hits] == ["glacier.jpg"]
        tag = session.tags.add(None, "Favorites")
        assert tag in session.tag_cache.get()
    finally:
        session.close()
    assert session.closed
    session.close()  # twice is fine


def test_a_root_override_is_used(tmp_path: Path, files: Path) -> None:
    keep_dir = _keep(tmp_path, tmp_path / "on-another-machine")
    settings = Settings()
    keep_id = KeepSession.open(keep_dir, settings).keep.config.id
    settings.set_root_override(keep_id, "r", str(files))
    with KeepSession.open(keep_dir, settings) as session:
        assert session.root_path("r") == str(files)
        [report] = session.scan_all()
    assert report.online
    assert report.new == 2


def test_an_unreachable_root_is_reported_not_raised(tmp_path: Path) -> None:
    with KeepSession.open(_keep(tmp_path, tmp_path / "unplugged"), Settings()) as session:
        [report] = session.scan_all()
        with session.reader.connect() as conn:
            assert conn.scalar(select(func.count()).select_from(Entity)) == 0
    assert not report.online


def test_opening_errors_are_keep_errors(tmp_path: Path, files: Path) -> None:
    with pytest.raises(KeepError, match="is not a keep"):
        KeepSession.open(tmp_path, Settings())
    with pytest.raises(KeepThemeError, match="'movies' theme, which isn't available"):
        KeepSession.open(_keep(tmp_path, files, theme="movies"), Settings())
