"""Choosing what to read inside an archive (#519): the dialog's choices, Read everything
inside, Stop reading inside, hiding a member, and a folder's archives."""

import zipfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from tagalot.core.archive_choices import (
    ArchiveChoices,
    archive_choices,
    hide_members,
    is_archive,
    read_everything,
    read_folder_archives,
    save_choices,
    stop_reading,
)
from tagalot.core.archive_members import member_choices
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import ArchiveMode
from tagalot.core.skip_reasons import MEMBER_HIDDEN, ThemeScope, skip_reasons
from tests.core.test_archive_members import ALL, PACK, T0, Env, env

__all__ = ["env"]  # the fixture

REGULAR, BOLD, README = (
    f"{PACK}/{n}" for n in ("fonts/Regular.ttf", "fonts/Bold.ttf", "readme.md")
)


def _root(env: Env) -> RootConfig:
    return RootConfig("r", "Files", str(env.files), archives=env.archives)


def _choices(env: Env) -> ArchiveChoices:
    with env.reader.connect() as conn:
        found = archive_choices(conn, env.resource(PACK), [_root(env)])
    assert found is not None
    return found


def _add_member(env: Env, name: str, data: bytes) -> None:
    with zipfile.ZipFile(env.files / PACK, "a") as zf:
        zf.writestr(name, data)


def test_what_is_read_inside_an_archive(env: Env) -> None:
    env.scan()
    first = _choices(env)
    assert (first.mode, first.own, first.choices, first.name) == (
        ArchiveMode.NONE,
        None,
        {},
        "pack.zip",
    )
    assert first.hidden() == set()  # the dialog starts with everything shown
    env.archives = ["Fonts/**"]
    assert _choices(env).mode is ArchiveMode.ALL  # by the pattern

    env.scan()
    with env.reader.connect() as conn:  # not an archive, or inside one: nothing to choose
        assert archive_choices(conn, env.resource("Fonts/notes.txt"), [_root(env)]) is None
        assert archive_choices(conn, env.resource(REGULAR), [_root(env)]) is None
        assert archive_choices(conn, env.resource(PACK), []) is None  # no such root here
    assert is_archive("a/b.ZIP")
    assert not is_archive("a/.zip")
    assert not is_archive("a/b.txt")


def test_hiding_a_member_of_a_whole_archive_keeps_it_whole(env: Env) -> None:
    env.archives = ["Fonts/**"]
    env.scan()
    regular = env.entity_of(REGULAR)
    assert env.run(lambda conn: hide_members(conn, _choices(env), ["fonts/Regular.ttf"])) == 1
    assert env.shown() == ALL - {REGULAR}  # at once
    assert regular not in env.listed()  # its item hides, keeping its tags (#526)
    after = _choices(env)
    assert (after.mode, after.own) == (ArchiveMode.ALL, None)  # still the pattern's
    assert after.hidden() == {"fonts/Regular.ttf"}

    env.scan(when=T0 + timedelta(hours=1))  # unchanged: still hidden
    assert env.shown() == ALL - {REGULAR}

    # A member added later is read: the archive is still read whole.
    _add_member(env, "fonts/Italic.ttf", b"italic font")
    env.scan(when=T0 + timedelta(hours=2))
    assert env.shown() == (ALL - {REGULAR}) | {f"{PACK}/fonts/Italic.ttf"}

    with env.reader.connect() as conn:
        report = skip_reasons(conn, [_root(env)], ThemeScope(None))
    [rule] = report.rules
    assert rule.kind == MEMBER_HIDDEN
    assert [f.relpath for f in rule.files] == [REGULAR]
    assert "Choose what to read inside" in rule.where

    # Read everything inside shows it again, at the next scan.
    env.run(lambda conn: read_everything(conn, _choices(env)))
    assert _choices(env).hidden() == set()
    env.scan(when=T0 + timedelta(hours=3))
    assert REGULAR in env.shown()
    assert regular in env.listed()


def test_the_dialog_reads_an_archive_member_by_member(env: Env) -> None:
    env.scan()
    hidden = env.run(
        lambda conn: save_choices(
            conn, _choices(env), ["fonts/Regular.ttf"], ["fonts/Bold.ttf", "readme.md"]
        )
    )
    assert hidden == 2
    chosen = _choices(env)
    assert chosen.mode is ArchiveMode.CHOSEN
    assert chosen.hidden() == {"fonts/Bold.ttf", "readme.md"}
    env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == {REGULAR}

    # A member added later waits, undecided; still hidden in the dialog, it stays so.
    _add_member(env, "fonts/Italic.ttf", b"italic font")
    env.scan(when=T0 + timedelta(hours=2))
    assert env.shown() == {REGULAR}
    with env.reader.connect() as conn:
        assert member_choices(conn, env.resource(PACK))["fonts/Italic.ttf"] is None
    current = _choices(env)
    assert "fonts/Italic.ttf" in current.hidden()
    env.run(
        lambda conn: save_choices(
            conn,
            current,
            ["fonts/Regular.ttf", "readme.md"],
            ["fonts/Bold.ttf", "fonts/Italic.ttf"],
        )
    )
    with env.reader.connect() as conn:
        choices = member_choices(conn, env.resource(PACK))
    assert choices == {
        "fonts/Regular.ttf": True,
        "readme.md": True,
        "fonts/Bold.ttf": False,
        "fonts/Italic.ttf": None,  # still for Triage (#520)
    }
    env.scan(when=T0 + timedelta(hours=3))
    assert env.shown() == {REGULAR, README}

    # Hiding a read one in the dialog skips it at once.
    env.run(
        lambda conn: save_choices(
            conn, _choices(env), ["readme.md"], ["fonts/Regular.ttf", "fonts/Bold.ttf"]
        )
    )
    assert env.shown() == {README}


def test_the_dialog_on_a_whole_archive_keeps_it_whole(env: Env) -> None:
    env.archives = ["Fonts/**"]
    env.scan()
    env.run(
        lambda conn: save_choices(
            conn, _choices(env), ["fonts/Regular.ttf", "readme.md"], ["fonts/Bold.ttf"]
        )
    )
    assert env.shown() == ALL - {BOLD}
    assert _choices(env).mode is ArchiveMode.ALL
    # Showing it again reads it at the next scan.
    env.run(
        lambda conn: save_choices(
            conn, _choices(env), ["fonts/Regular.ttf", "readme.md", "fonts/Bold.ttf"], []
        )
    )
    assert _choices(env).hidden() == set()
    env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == ALL


def test_stop_reading_inside(env: Env) -> None:
    env.archives = ["Fonts/**"]
    env.scan()
    regular = env.entity_of(REGULAR)
    env.run(lambda conn: stop_reading(conn, _choices(env)))
    assert env.shown() == set()  # at once
    assert regular not in env.listed()
    assert _choices(env).mode is ArchiveMode.NONE  # its own choice, over the pattern
    env.scan(when=T0 + timedelta(hours=1))
    assert env.shown() == set()
    env.run(lambda conn: read_everything(conn, _choices(env)))
    env.scan(when=T0 + timedelta(hours=2))
    assert env.shown() == ALL
    assert regular in env.listed()  # the same item, back


def test_reading_a_folders_archives(tmp_path: Path) -> None:
    (tmp_path / "files").mkdir()
    keep = create_keep(tmp_path / "k", "K", ThemeRef("generic", 1))
    config = replace(keep.config, roots=[RootConfig("r", "Files", str(tmp_path / "files"))])
    new, pattern = read_folder_archives(config, keep.dir, "r", "Fonts/Aurora #2 [old]")
    assert pattern == "Fonts/Aurora [#]2 [[]old]/**"
    assert new.roots[0].archives == [pattern]
    again, _ = read_folder_archives(new, keep.dir, "r", "Fonts/Aurora #2 [old]")
    assert again.roots[0].archives == [pattern]  # not twice
    whole, everything = read_folder_archives(config, keep.dir, "r", "")
    assert (everything, whole.roots[0].archives) == ("**", ["**"])
    assert config.roots[0].archives == []  # unchanged
