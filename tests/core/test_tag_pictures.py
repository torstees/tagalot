"""Tags' pictures (#506): kept in the keep, shrunk, undoable, carried by merges and by tag
export and import (a folder of pictures beside the file)."""

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import delete

from tagalot.core.models import Tag
from tagalot.core.tag_pictures import STORED_SIZE, prepare_picture
from tagalot.core.tag_yaml import export_tags, picture_files
from tagalot.core.tags import TagError
from tests.core.test_tag_yaml import SCHEMA, Env, env

__all__ = ["env"]  # fixtures

FANTASY, MOOD, CALM = 2, 4, 5


def _png(size: tuple[int, int], color: tuple[int, ...] = (200, 30, 30), mode: str = "RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def test_a_picture_is_kept_small_and_upright() -> None:
    big = prepare_picture(_png((1600, 800)))
    assert (big.width, big.height) == (STORED_SIZE, STORED_SIZE // 2)
    assert big.data.startswith(b"\xff\xd8")  # no transparency: JPEG
    clear = prepare_picture(_png((40, 40), (0, 0, 0, 0), "RGBA"))
    assert clear.data.startswith(b"\x89PNG")  # transparency kept
    assert (clear.width, clear.height) == (40, 40)  # small ones stay as they are
    with pytest.raises(TagError, match="isn't a picture"):
        prepare_picture(b"not a picture")


def test_setting_a_picture_is_one_undoable_step(env: Env) -> None:
    picture = prepare_picture(_png((60, 30)))
    env.tags.set_picture(FANTASY, picture)
    assert env.tree().picture(FANTASY) == picture
    env.tags.undo()
    assert env.tree().picture(FANTASY) is None
    env.tags.redo()
    assert env.tree().picture(FANTASY) == picture
    env.tags.set_picture(FANTASY, None)
    assert env.tree().picture(FANTASY) is None


def test_a_deleted_tag_gets_its_picture_back_on_undo(env: Env) -> None:
    picture = prepare_picture(_png((60, 30)))
    env.tags.set_picture(CALM, picture)
    env.tags.delete(CALM)
    assert CALM not in env.tree()
    env.tags.undo()
    assert env.tree().picture(CALM) == picture


def test_a_merge_gives_the_target_the_sources_picture(env: Env) -> None:
    picture = prepare_picture(_png((60, 30)))
    env.tags.set_picture(CALM, picture)
    env.tags.merge(CALM, MOOD)  # Mood has none: it takes Calm's
    assert env.tree().picture(MOOD) == picture


def test_export_and_import_carry_pictures_beside_the_file(env: Env, tmp_path: Path) -> None:
    picture = prepare_picture(_png((60, 30)))
    env.tags.set_picture(FANTASY, picture)
    tree = env.tree()
    names = picture_files(tree, "Tags pictures")
    assert names == {FANTASY: "Tags pictures/Genre/Fantasy.jpg"}
    text = export_tags(tree, SCHEMA, pictures=names)
    assert "    Fantasy:\n      picture: Tags pictures/Genre/Fantasy.jpg\n" in text
    folder = tmp_path / "export"
    target = folder / "Tags pictures" / "Genre" / "Fantasy.jpg"
    target.parent.mkdir(parents=True)
    target.write_bytes(picture.data)

    # As exported: nothing to change.
    assert not env.plan_from(text, folder).changes

    # Into tags without it: the tag gets it.
    env.tags.set_picture(FANTASY, None)
    plan = env.plan_from(text, folder)
    assert any(u.changes == ("picture",) for u in plan.updated)
    env.tags.import_tags(text, "tags.yaml", base=folder)
    kept = env.tree().picture(FANTASY)
    assert kept is not None
    assert (kept.width, kept.height) == (60, 30)


def test_a_picture_that_isnt_there_is_a_problem(env: Env, tmp_path: Path) -> None:
    text = "Genre:\n  tags:\n    Fantasy:\n      picture: missing.png\n"
    plan = env.plan_from(text, tmp_path)
    assert len(plan.problems) == 1
    assert "its picture 'missing.png' can't be read" in str(plan.problems[0])


def test_a_tag_deleted_by_hand_takes_its_picture(env: Env) -> None:
    env.tags.set_picture(FANTASY, prepare_picture(_png((10, 10))))
    with env.engine.begin() as conn:
        conn.execute(delete(Tag).where(Tag.id == FANTASY))
    assert FANTASY not in env.tree().pictures()
