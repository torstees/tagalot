"""Tags' pictures (#506) and icons that are pictures (#507): kept in the keep, shrunk,
undoable, carried by merges and by tag export and import (a folder of pictures beside the
file)."""

import io
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import delete

from tagalot.core.models import Tag
from tagalot.core.tag_pictures import ICON_SIZE, STORED_SIZE, prepare_icon, prepare_picture
from tagalot.core.tag_yaml import export_tags, icon_files, picture_files
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


# --- icons that are pictures (#507) ---


def _gif(size: tuple[int, int], frames: int = 3) -> bytes:
    images = [Image.new("RGB", size, (i * 60, 0, 0)) for i in range(frames)]
    out = io.BytesIO()
    images[0].save(out, "GIF", save_all=True, append_images=images[1:], duration=80, loop=0)
    return out.getvalue()


def test_an_icon_picture_is_kept_small_and_animations_stay_animated() -> None:
    still = prepare_icon(_png((300, 150)))
    assert (still.width, still.height) == (ICON_SIZE, ICON_SIZE // 2)
    assert still.data.startswith(b"\x89PNG")
    moving = prepare_icon(_gif((200, 100)))
    assert (moving.width, moving.height) == (ICON_SIZE, ICON_SIZE // 2)
    with Image.open(io.BytesIO(moving.data)) as kept:
        assert (kept.format, getattr(kept, "n_frames", 1)) == ("GIF", 3)
    small = _gif((16, 16))
    assert prepare_icon(small).data == small  # small enough: as it is
    with pytest.raises(TagError, match="isn't a picture"):
        prepare_icon(b"not a picture")


def test_an_icon_is_an_emoji_or_a_picture_undoably(env: Env) -> None:
    picture = prepare_icon(_png((20, 20)))
    env.tags.set_icon(FANTASY, "🐉")
    env.tags.set_icon_picture(FANTASY, picture)  # replaces the emoji
    tree = env.tree()
    assert (tree.node(FANTASY).icon, tree.icon_picture(FANTASY)) == (None, picture)
    assert tree.has_icons
    assert tree.icon(FANTASY) == picture
    assert [b.icon for b in tree.badges([FANTASY])] == [picture]
    env.tags.undo()
    tree = env.tree()
    assert (tree.node(FANTASY).icon, tree.icon_picture(FANTASY)) == ("🐉", None)
    env.tags.redo()
    env.tags.set_icon(FANTASY, "⭐")  # and an emoji replaces the picture
    assert (env.tree().node(FANTASY).icon, env.tree().icon_picture(FANTASY)) == ("⭐", None)
    env.tags.set_icon_picture(FANTASY, picture)
    env.tags.set_icon(FANTASY, None)  # Clear clears either
    assert env.tree().own_icon(FANTASY) is None
    assert not env.tree().has_icons


def test_sub_tags_show_an_icon_picture_and_merges_carry_it(env: Env) -> None:
    picture = prepare_icon(_png((20, 20)))
    env.tags.set_icon_picture(MOOD, picture)
    tree = env.tree()
    assert tree.icon(CALM) == picture  # inherited
    assert tree.icon_source(CALM) == MOOD
    env.tags.set_icon_picture(CALM, prepare_icon(_png((10, 10), (0, 200, 0))))
    env.tags.set_icon_picture(MOOD, None)
    env.tags.merge(CALM, FANTASY)  # Fantasy has no icon: it takes Calm's
    assert env.tree().icon_picture(FANTASY) is not None


def test_export_and_import_carry_icon_pictures(env: Env, tmp_path: Path) -> None:
    picture = prepare_icon(_gif((16, 16)))
    env.tags.set_icon_picture(FANTASY, picture)
    env.tags.set_picture(FANTASY, prepare_picture(_png((60, 30))))
    tree = env.tree()
    pictures = picture_files(tree, "Tags pictures")
    icons = icon_files(tree, "Tags pictures", pictures.values())
    assert icons == {FANTASY: "Tags pictures/Genre/Fantasy icon.gif"}
    text = export_tags(tree, SCHEMA, pictures=pictures, icons=icons)
    assert "    Fantasy:\n      icon: Tags pictures/Genre/Fantasy icon.gif\n" in text
    folder = tmp_path / "export"
    for name, data in [
        (icons[FANTASY], picture.data),
        (pictures[FANTASY], tree.pictures()[FANTASY].data),
    ]:
        target = folder.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    assert not env.plan_from(text, folder).changes  # as exported: nothing to change

    env.tags.set_icon(FANTASY, "🐉")
    plan = env.plan_from(text, folder)
    assert [u.changes for u in plan.updated] == [("icon",)]
    env.tags.import_tags(text, "tags.yaml", base=folder)
    tree = env.tree()
    assert (tree.node(FANTASY).icon, tree.icon_picture(FANTASY)) == (None, picture)

    plan = env.plan_from(text.replace("Fantasy icon.gif", "missing.png"), folder)
    assert "its icon 'Tags pictures/Genre/missing.png' can't be read" in str(plan.problems[0])
