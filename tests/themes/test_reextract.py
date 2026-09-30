"""Re-reading chosen items from their files, keeping or replacing edits (#96)."""

from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from tagalot.builtin_themes.assets2d import Assets2DTheme, Font, Image
from tagalot.core.fields import user_fields
from tagalot.core.keep import RootConfig
from tagalot.core.models import Entity, Resource, ResourceStatus
from tagalot.core.reextract import ReextractReport, reextract
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagTreeCache
from tests.core.media_files import write_image
from tests.themes.test_assets2d import Env, env, library

__all__ = ["env", "library"]  # fixtures

FONT = "Aileron-Regular.ttf"


def _service(env: Env) -> TagService:
    return TagService(env.writer, TagTreeCache(env.reader), env.schema)


def _reread(env: Env, items: list[str | int], *, replace_edits: bool = False) -> ReextractReport:
    """Re-read items given by title or id."""
    root = RootConfig("r", "Assets", str(env.files))
    return reextract(
        env.writer,
        env.reader,
        env.schema,
        Assets2DTheme,
        [root],
        lambda _root_id: str(env.files),
        [env.entity(i) if isinstance(i, str) else i for i in items],
        replace_edits=replace_edits,
    )


def _edited(env: Env, entity_id: int) -> set[str]:
    with env.reader.connect() as conn:
        return user_fields(conn, entity_id)


def _values(env: Env, entity_id: int) -> dict[str, Any]:
    """Title and fields by id (the title is what some tests change)."""
    table = env.schema.entities[Font].table
    with env.reader.connect() as conn:
        row = conn.execute(select(table).where(table.c.id == entity_id)).one()
        title = conn.scalar(select(Entity.title).where(Entity.id == entity_id))
    return {**row._mapping, "title": title}


def test_rereading_keeps_what_the_user_edited(env: Env) -> None:
    env.scan()
    service = _service(env)
    font = env.entity(FONT)
    service.edit_field(font, "family", "Aileron Sans")
    report = _reread(env, [FONT])
    assert report.read == 1
    assert report.offline == 0
    assert _values(env, font)["family"] == "Aileron Sans"
    assert _edited(env, font) == {"family"}
    assert report.change.before == report.change.after  # nothing changed: no undo step


def test_replacing_edits_takes_the_files_values_and_can_be_undone(env: Env) -> None:
    env.scan()
    service = _service(env)
    font = env.entity(FONT)
    service.edit_field(font, "family", "Aileron Sans")
    service.edit_field(font, "title", "My font")
    report = _reread(env, [font], replace_edits=True)
    assert report.change.label == "Re-read 1 item from their files, replacing your edits"
    assert (_values(env, font)["family"], _values(env, font)["title"]) == ("Aileron", FONT)
    assert _edited(env, font) == set()

    service.record(report.change)
    assert service.undo() == report.change.label
    assert (_values(env, font)["family"], _values(env, font)["title"]) == (
        "Aileron Sans",
        "My font",
    )
    assert _edited(env, font) == {"family", "title"}
    service.redo()
    assert _values(env, font)["family"] == "Aileron"
    assert _edited(env, font) == set()


def test_a_file_changed_since_the_scan_is_read_again(env: Env) -> None:
    env.scan()
    write_image(env.files / "Aurora/sky.png", (640, 480))  # no scan since
    _reread(env, ["sky.png"])
    assert env.fields(Image, "sky.png")["dimensions"] == "640 \u00d7 480"


def test_offline_files_are_reported_not_read(env: Env) -> None:
    env.scan()
    env.writer.run(
        lambda conn: conn.execute(
            update(Resource)
            .where(Resource.relpath == "Aurora/sky.png")
            .values(status=ResourceStatus.OFFLINE)
        )
    )
    report = _reread(env, ["sky.png", FONT])
    assert (report.read, report.offline) == (1, 1)


def test_rereading_an_artist_reads_its_folder(env: Env, tmp_path: Path) -> None:
    env.scan()
    report = _reread(env, ["Aurora"])
    assert report.read == 1  # its folder resource
    assert report.errors == []
