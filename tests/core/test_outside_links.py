"""Links outside the keep's folders (#603): skipped until the keep says to read them,
reported by scans, and read as if they were where the link is; and the keep.toml
setting. Shortcuts stand in for links, since they need no special rights to make."""

import importlib.util
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import EntityResource, Resource, ResourceStatus
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.core.test_shortcuts import make_lnk

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
FILE_LINK = "Kenji Sato/outside.png.lnk"
FOLDER_LINK = "Kenji Sato/Extras.lnk"


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        root = Path(opened.root_path("assets"))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        shutil.copy(root / "Kenji Sato" / "tileset.png", elsewhere / "outside.png")
        (elsewhere / "extras").mkdir()
        shutil.copy(root / "Kenji Sato" / "sketch 01.jpg", elsewhere / "extras" / "far.jpg")
        (root / FILE_LINK).write_bytes(make_lnk(local=str(elsewhere / "outside.png")))
        (root / FOLDER_LINK).write_bytes(make_lnk(local=str(elsewhere / "extras"), is_dir=True))
        yield opened


def _state(session: KeepSession, relpath: str) -> tuple[ResourceStatus, bool] | None:
    """A resource's status, and whether an item uses it."""
    with session.reader.connect() as conn:
        row = conn.execute(
            select(Resource.id, Resource.status).where(Resource.relpath == relpath)
        ).first()
        if row is None:
            return None
        used = conn.scalar(
            select(EntityResource.entity_id).where(EntityResource.resource_id == row.id)
        )
    return row.status, used is not None


def test_outside_links_are_skipped_until_included(session: KeepSession) -> None:
    assert session.keep.config.outside_links is None  # not asked yet
    reports = session.scan_all()
    assert sorted(p for r in reports for p in r.outside_links) == [FOLDER_LINK, FILE_LINK]
    assert _state(session, FILE_LINK) is None
    assert _state(session, f"{FOLDER_LINK}/far.jpg") is None

    session.set_outside_links(True)
    assert load_keep_config(session.keep.toml_path).outside_links is True
    reports = session.scan_all()
    assert sorted(p for r in reports for p in r.outside_links) == [FOLDER_LINK, FILE_LINK]
    # Read as if they were where the link is: files of their own, used by items.
    assert _state(session, FILE_LINK) == (ResourceStatus.OK, True)
    assert _state(session, f"{FOLDER_LINK}/far.jpg") == (ResourceStatus.OK, True)

    session.set_outside_links(False)
    assert load_keep_config(session.keep.toml_path).outside_links is False
    session.scan_all()
    assert _state(session, FILE_LINK) == (ResourceStatus.MISSING, True)  # rule 7

    session.set_outside_links(None)  # ask again
    assert load_keep_config(session.keep.toml_path).outside_links is None
