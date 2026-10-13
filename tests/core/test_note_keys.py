"""Ignored note keys (#381, DESIGN.md §9 *Notes*): built-in, the user's, and the keep's
lists apply together; a changed list reads the notes again at the next scan; ignoring a key
from an item's page removes the notes' fields under it at once, as one undo step."""

from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.extra_keys import DEFAULT_IGNORED_KEYS
from tagalot.core.fields import edit_extra
from tagalot.core.keep import KeepError, load_keep_config, save_keep_config
from tagalot.core.keep_settings import with_note_ignore_keys
from tagalot.core.models import EntityNote, FieldSource, NoteIgnoredKey
from tagalot.core.note_keys import (
    NoteKeyError,
    check_key,
    ignore_note_key,
    ignored_keys,
    restore_note_key,
    with_key,
)
from tagalot.core.settings import Settings, load_settings, save_settings
from tests.core.test_keep_toml import _config
from tests.core.test_notes import AURORA, Env, env

__all__ = ["env"]  # fixture

NOTE = "Aurora (2001)/Aurora (2001).md"
OTHER = """---
Website: https://brightwater.example
mood: calm
---
"""


def _applied(env: Env) -> set[str]:
    with env.reader.connect() as conn:
        return set(conn.scalars(select(NoteIgnoredKey.key)))


def test_the_lists_apply_together() -> None:
    keys = ignored_keys(["Mood-Board", " website "], ["mood-board", "tagalot-type", ""])
    assert keys == DEFAULT_IGNORED_KEYS | {"mood-board", "website"}
    assert "cssclasses" in ignored_keys()


@pytest.mark.parametrize(
    ("key", "message"), [("  ", "Type a key"), ("Tagalot-Type", "Tagalot's own")]
)
def test_keys_that_cant_be_ignored(key: str, message: str) -> None:
    with pytest.raises(NoteKeyError, match=message):
        check_key(key)


def test_adding_and_removing_a_key_ignores_case() -> None:
    assert with_key(["mood", "Website"], "website", True) == ["mood", "website"]
    assert with_key(["mood", "Website"], "WEBSITE", False) == ["mood"]


def test_keep_toml_lists_them(tmp_path: Path) -> None:
    config = with_note_ignore_keys(_config(), ["mood-board", "Mood-Board", " rating "])
    assert config.note_ignore_keys == ["mood-board", "rating"]
    path = tmp_path / "keep.toml"
    save_keep_config(config, path)
    assert "[notes]\nignore_keys = ['mood-board', 'rating']" in path.read_text(encoding="utf-8")
    assert load_keep_config(path).note_ignore_keys == ["mood-board", "rating"]
    save_keep_config(with_note_ignore_keys(config, []), path)
    assert "[notes]" not in path.read_text(encoding="utf-8")
    with pytest.raises(KeepError, match="Tagalot's own"):
        with_note_ignore_keys(config, ["tagalot-type"])


def test_settings_list_them(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "settings.toml"
    save_settings(Settings(note_ignore_keys=["mood-board"]), path)
    assert load_settings(path).note_ignore_keys == ["mood-board"]
    path.write_text("[notes]\nignore_keys = 3\nother = 1\n", encoding="utf-8")
    assert load_settings(path).note_ignore_keys == []
    assert "ignore_keys must be a list" in caplog.text
    assert "unknown key 'other'" in caplog.text


def test_a_changed_list_reads_the_notes_again(env: Env) -> None:
    env.note(NOTE, AURORA)
    env.note("Brightwater/index.md", OTHER)
    env.scan(ignored_keys())
    assert env.item("Aurora (2001)")["extra"] == {"website": "https://aurora.example"}
    assert _applied(env) == set(DEFAULT_IGNORED_KEYS)

    env.scan(ignored_keys(["WEBSITE"]))  # the notes didn't change; the list did
    assert env.item("Aurora (2001)")["extra"] == {}
    assert env.item("Brightwater")["extra"] == {"mood": "calm"}
    assert "extra:website" not in env.item("Aurora (2001)")["sources"]
    assert "website" in _applied(env)

    env.scan(ignored_keys(["website"]))  # the same list: nothing read again
    with env.reader.connect() as conn:
        assert None not in conn.scalars(select(EntityNote.size)).all()

    env.scan(ignored_keys())  # no longer ignored: back
    assert env.item("Aurora (2001)")["extra"] == {"website": "https://aurora.example"}
    assert env.item("Brightwater")["extra"] == {
        "Website": "https://brightwater.example",
        "mood": "calm",
    }


def test_a_scan_without_the_lists_keeps_what_was_applied(env: Env) -> None:
    env.note(NOTE, AURORA)
    env.scan(ignored_keys(["website"]))
    env.note(NOTE, AURORA + "\nMore.\n")
    env.scan()  # as re-reading chosen files does
    assert env.item("Aurora (2001)")["extra"] == {}


def test_ignoring_a_key_from_a_page(env: Env) -> None:
    env.note(NOTE, AURORA)
    env.note("Brightwater/index.md", OTHER)
    env.scan(ignored_keys())
    brightwater = env.item("Brightwater")["id"]
    env.writer.run(  # the user's own: stays
        lambda conn: edit_extra(conn, env.schema, brightwater, "mood", "stormy")
    )

    change = env.writer.run(lambda conn: ignore_note_key(conn, env.schema, "website"))
    assert change.label == "Ignore 'website' in every note (2 items)"
    assert env.item("Aurora (2001)")["extra"] == {}
    assert env.item("Brightwater")["extra"] == {"mood": "stormy"}
    assert env.found('"brightwater.example"') == []
    assert "website" in _applied(env)
    env.scan(ignored_keys(["website"]))  # the keep now lists it: nothing to read again
    with env.reader.connect() as conn:
        assert None not in conn.scalars(select(EntityNote.size)).all()

    env.writer.run(lambda conn: restore_note_key(conn, env.schema, change, forward=False))
    aurora = env.item("Aurora (2001)")
    assert aurora["extra"] == {"website": "https://aurora.example"}
    assert aurora["sources"]["extra:website"] == FieldSource.NOTE
    assert env.item("Brightwater")["extra"] == {
        "Website": "https://brightwater.example",
        "mood": "stormy",
    }
    assert env.found('"brightwater.example"') == ["Brightwater"]
    assert "website" not in _applied(env)
    env.writer.run(lambda conn: restore_note_key(conn, env.schema, change, forward=True))
    assert env.item("Aurora (2001)")["extra"] == {}


def test_ignoring_a_key_no_note_gives(env: Env) -> None:
    env.scan(ignored_keys())
    change = env.writer.run(lambda conn: ignore_note_key(conn, env.schema, "rating"))
    assert change.label == "Ignore 'rating' in every note"
    assert change.removed == ()
    with pytest.raises(NoteKeyError):
        env.writer.run(lambda conn: ignore_note_key(conn, env.schema, "tagalot-type"))
