"""A merged song stays merged when its file is read again (#120), with the music theme."""

import importlib.util
import shutil
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select

from tagalot.builtin_themes.music import MusicTheme, Song
from tagalot.core.merge import merge_items
from tagalot.core.models import Entity, EntityResource
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tests.themes.test_music import T0, Env, env, library

__all__ = ["env", "library"]  # fixtures

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def test_a_merged_song_stays_merged_through_scans(env: Env) -> None:
    copy = env.files / "Other" / "02.mp3"
    copy.parent.mkdir()
    shutil.copy(env.files / "Jazz Hits" / "02.mp3", copy)
    env.scan()
    type_id = MusicTheme.type_id_of(Song)
    with env.reader.connect() as conn:
        kept, other = conn.scalars(
            select(Entity.id)
            .where(Entity.type == type_id, Entity.title == "Blue")
            .order_by(Entity.id)
        ).all()
    env.writer.run(lambda conn: merge_items(conn, env.schema, kept, [other]))
    copy.write_bytes(copy.read_bytes() + b"x")  # changed: read again at the next scan
    report = env.scan(T0 + timedelta(hours=1))
    assert report.ingested >= 1
    assert env.titles(Song).count("Blue") == 1
    with env.reader.connect() as conn:
        files = conn.scalars(
            select(EntityResource.by_user).where(EntityResource.entity_id == kept)
        ).all()
    assert sorted(files) == [False, True]  # its own file, and the copy's


def test_a_merged_picture_stays_merged_through_scans(tmp_path: Path) -> None:
    """The 2D assets theme finds a picture's item through its file: a merged copy's file,
    left linked to nothing, isn't made an item again when it is read again."""
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        root = Path(session.root_path("assets"))
        copy = root / "Kenji Sato" / "forest copy.png"
        shutil.copy(root / "Aurora Studio" / "Backgrounds" / "forest.png", copy)
        session.scan_all()

        def titled(title: str) -> list[int]:
            with session.reader.connect() as conn:
                return list(conn.scalars(select(Entity.id).where(Entity.title == title)))

        [kept], [other] = titled("forest.png"), titled("forest copy.png")
        session.merge_items(kept, [other], {})
        copy.write_bytes(copy.read_bytes() + b"x")  # changed: read again
        [report] = session.scan_all()
        assert report.ingested >= 1
        assert titled("forest copy.png") == []
        assert titled("forest.png") == [kept]
