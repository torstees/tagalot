"""Notes typed in Tagalot and **Save as note…** (#383, DESIGN.md §9 *In-app notes and Save
as note…*): a typed note is kept in the keep, searchable, one undo step per edit; a note
file wins over it (reported when it says otherwise); Save as note… writes a new file only,
in the layout chosen, with a safe name, and the next scan reads it back."""

import ntpath
import re
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.fields import FieldEditError
from tagalot.core.keep import KeepConfig, KeepError, load_keep_config, save_keep_config
from tagalot.core.models import Entity, EntityNote, entity_fts
from tagalot.core.root_admin import with_notes_folder, with_notes_writing
from tagalot.core.roots import local_path
from tagalot.core.save_note import SaveNoteError, plan_note, safe_name, write_note
from tagalot.core.tag_service import TagService
from tagalot.core.tags import TagTreeCache, add_tag_path, tag_entities
from tests.core.test_keep_toml import _config
from tests.core.test_notes_folder import Env, env

__all__ = ["env"]


def _service(env: Env) -> TagService:
    return TagService(env.writer, TagTreeCache(env.reader), env.schema)


def _id(env: Env, title: str) -> int:
    with env.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None, title
    return found


def _indexed(env: Env, entity_id: int) -> str:
    with env.reader.connect() as conn:
        return conn.scalar(select(entity_fts.c.body).where(entity_fts.c.rowid == entity_id)) or ""


def _config_for(env: Env, **changes: object) -> KeepConfig:
    config = with_notes_folder(_config(env.root), "r", "Notes")
    return with_notes_writing(config, **changes)  # type: ignore[arg-type]


def test_a_typed_note_is_kept_searchable_and_undone_in_one_step(env: Env) -> None:
    env.scan()
    tags = _service(env)
    pacino = _id(env, "Al Pacino")
    change = tags.edit_note(pacino, "Born in **East Harlem**.\n\n")
    assert change.label == "Add a note to 'Al Pacino'"
    assert env.item("Al Pacino")["body"] == "Born in **East Harlem**."
    assert "East Harlem" in _indexed(env, pacino)
    assert tags.edit_note(pacino, "Heat, 1995.").label == "Edit the note of 'Al Pacino'"
    assert tags.undo() == "Edit the note of 'Al Pacino'"
    assert env.item("Al Pacino")["body"] == "Born in **East Harlem**."
    assert tags.edit_note(pacino, "  \n").label == "Remove the note of 'Al Pacino'"
    with env.reader.connect() as conn:
        assert (
            conn.scalar(select(EntityNote.entity_id).where(EntityNote.entity_id == pacino)) is None
        )
    assert "East Harlem" not in _indexed(env, pacino)
    tags.undo()
    assert env.item("Al Pacino")["body"] == "Born in **East Harlem**."
    tags.redo()
    assert env.item("Al Pacino")["body"] is None
    unchanged = tags.edit_note(pacino, "")
    assert unchanged.before == unchanged.after is None
    assert tags.undo_label == "Remove the note of 'Al Pacino'"  # no step for no change


def test_a_typed_note_stays_through_scans(env: Env) -> None:
    env.write("Aurora/Aurora.md", "A studio.\n")  # Aurora has a folder note
    env.scan()
    tags = _service(env)
    tags.edit_note(_id(env, "Al Pacino"), "Typed.")
    env.scan()
    env.scan()
    assert env.item("Al Pacino")["body"] == "Typed."
    with pytest.raises(FieldEditError, match="is a file: edit it with Open note"):
        tags.edit_note(_id(env, "Aurora"), "Typed over the file.")
    assert env.item("Aurora")["body"] == "A studio."


def test_a_note_file_replaces_a_typed_note_and_says_so(env: Env) -> None:
    env.scan()
    tags = _service(env)
    tags.edit_note(_id(env, "Al Pacino"), "Typed in Tagalot, " + "word " * 60)
    tags.edit_note(_id(env, "Jean Reno"), "Léon.")
    env.write("Notes/Actors/Al Pacino.md", "---\nborn: 1940\n---\nFrom the file.\n")
    env.write("Notes/Actors/Jean Reno.md", "# Jean Reno\n\nLéon.\n")  # the same text
    report = env.scan()
    assert env.item("Al Pacino")["body"] == "From the file."
    assert env.item("Al Pacino")["note"] == "Notes/Actors/Al Pacino.md"
    replaced = [w for w in report.ingest_warnings if "replaced the note typed" in w.message]
    assert [(w.root_id, w.relpath) for w in replaced] == [("r", "Notes/Actors/Al Pacino.md")]
    message = replaced[0].message
    assert message.startswith(
        "This note file replaced the note typed in Tagalot for 'Al Pacino': Typed in Tagalot,"
    )
    assert message.endswith("\u2026")
    assert len(message.partition(": ")[2]) == 200
    # Undoing the typing doesn't touch a note that is a file now.
    tags.undo()
    assert env.item("Jean Reno")["body"] == "Léon."
    # The file gone, the item has no note (the keep held only what it read).
    (env.files / "Notes" / "Actors" / "Al Pacino.md").unlink()
    env.scan()
    assert env.item("Al Pacino")["body"] is None


def test_safe_names() -> None:
    assert safe_name('AC/DC: "Live" <1991>?') == "AC_DC_ _Live_ _1991__"
    assert safe_name("  Mr.  Smith  goes. ") == "Mr. Smith goes"
    assert safe_name("CON") == "_CON"
    assert safe_name("nul.txt") == "_nul.txt"
    assert safe_name("Console") == "Console"
    assert safe_name("...") == "Untitled"
    assert safe_name("a\\b|c*d\te") == "a_b_c_d_e"
    assert len(safe_name("x" * 400)) == 120


def test_save_as_note_writes_a_new_note_the_scan_reads(env: Env) -> None:
    env.scan()
    service = _service(env)
    pacino = _id(env, "Al Pacino")
    service.edit_note(pacino, "Born in East Harlem.")
    tag = env.writer.run(lambda conn: add_tag_path(conn, ["Genre", "Crime"]))
    env.writer.run(lambda conn: tag_entities(conn, [pacino], [tag]))
    service.cache.invalidate()
    config = _config_for(env, writable=True)
    root_path = {"r": str(env.files)}.__getitem__
    with env.reader.connect() as conn:
        note = plan_note(conn, env.schema, service.cache.get(), config, root_path, pacino)
    assert note.relpath == "Notes/Actors/Al Pacino.md"
    assert note.text == "---\ntitle: Al Pacino\ntags:\n- Genre/Crime\n---\n\nBorn in East Harlem.\n"
    (env.files / "Notes").mkdir()
    write_note(note)
    assert (env.files / "Notes" / "Actors" / "Al Pacino.md").read_text("utf-8") == note.text
    env.scan()
    assert env.item("Al Pacino")["note"] == "Notes/Actors/Al Pacino.md"
    assert env.item("Al Pacino")["body"] == "Born in East Harlem."
    with pytest.raises(SaveNoteError, match="is a file already"), env.reader.connect() as conn:
        plan_note(conn, env.schema, service.cache.get(), config, root_path, pacino)


def test_save_as_note_never_replaces_a_file(env: Env) -> None:
    env.scan()
    service = _service(env)
    reno = _id(env, "Jean Reno")
    service.edit_note(reno, "Typed.")
    existing = env.write("Notes/Actors/Jean Reno.md", "Mine.\n")
    existing.unlink()  # not scanned yet: the keep doesn't know it
    existing.write_text("Mine.\n", encoding="utf-8")
    config = _config_for(env, writable=True)
    with env.reader.connect() as conn:
        note = plan_note(
            conn, env.schema, service.cache.get(), config, lambda _: str(env.files), reno
        )
    with pytest.raises(SaveNoteError, match=r"Notes/Actors/Jean Reno\.md is there already"):
        write_note(note)
    assert existing.read_text("utf-8") == "Mine.\n"


@pytest.mark.parametrize(
    ("layout", "theirs", "ours"),
    [
        ("one-folder", "Notes/Actors/Jean Reno.md", "Notes/Jean Reno.md"),
        ("folders", "Notes/Jean Reno.md", "Notes/Actors/Jean Reno.md"),
    ],
)
def test_save_as_note_never_makes_a_second_note(
    env: Env, layout: str, theirs: str, ours: str
) -> None:
    env.scan()
    service = _service(env)
    reno = _id(env, "Jean Reno")
    service.edit_note(reno, "Typed.")
    existing = env.write(theirs, "Mine.\n")  # where the other layout puts it, not scanned yet
    config = _config_for(env, writable=True, new_notes=layout)
    with env.reader.connect() as conn:
        note = plan_note(
            conn, env.schema, service.cache.get(), config, lambda _: str(env.files), reno
        )
    assert note.relpath == ours
    message = f"{theirs} is there already, and it would be the note of 'Jean Reno' too"
    with pytest.raises(SaveNoteError, match=re.escape(message)):
        write_note(note)
    assert not (env.files / ours).exists()
    assert existing.read_text("utf-8") == "Mine.\n"


def test_save_as_note_in_one_folder(env: Env) -> None:
    env.scan()
    service = _service(env)
    deniro = _id(env, "Robert De Niro")
    config = _config_for(env, writable=True, new_notes="one-folder")
    with env.reader.connect() as conn:
        note = plan_note(
            conn, env.schema, service.cache.get(), config, lambda _: str(env.files), deniro
        )
    assert note.relpath == "Notes/Robert De Niro.md"
    assert note.text == "---\ntitle: Robert De Niro\ntagalot-type: Actor\n---\n"
    (env.files / "Notes").mkdir()
    write_note(note)
    env.scan()
    assert env.item("Robert De Niro")["note"] == "Notes/Robert De Niro.md"


def test_save_as_note_needs_a_writable_notes_folder_that_is_there(env: Env) -> None:
    env.scan()
    service = _service(env)
    pacino = _id(env, "Al Pacino")
    tree = service.cache.get()

    def plan(config: KeepConfig) -> None:
        with env.reader.connect() as conn:
            plan_note(conn, env.schema, tree, config, lambda _: str(env.files), pacino)

    with pytest.raises(SaveNoteError, match="has no notes folder"):
        plan(_config(env.root))
    with pytest.raises(SaveNoteError, match="may not write in the notes folder"):
        plan(_config_for(env))
    config = _config_for(env, writable=True)
    with env.reader.connect() as conn:
        note = plan_note(conn, env.schema, tree, config, lambda _: str(env.files), pacino)
    with pytest.raises(SaveNoteError, match="notes folder can't be reached"):
        write_note(note)  # Notes/ isn't there: Tagalot doesn't make it
    assert not (env.files / "Notes").exists()


def test_the_note_path_on_windows_and_unc() -> None:
    assert local_path(r"\\nas\films", "Notes/Actors/Al Pacino.md", ntpath) == (
        r"\\nas\films\Notes\Actors\Al Pacino.md"
    )


def test_keep_toml_notes_writing(tmp_path: Path, env: Env) -> None:
    config = with_notes_folder(_config(env.root), "r", "Notes")
    assert not config.notes_writable
    assert config.new_notes == "folders"
    config = with_notes_writing(config, writable=True, new_notes="one-folder")
    path = tmp_path / "keep.toml"
    save_keep_config(config, path)
    text = path.read_text("utf-8")
    assert "writable = true\nnew_notes = 'one-folder'" in text
    loaded = load_keep_config(path)
    assert loaded.notes_writable
    assert loaded.new_notes == "one-folder"
    # Another folder isn't writable until the user says so; the same one stays.
    assert with_notes_folder(loaded, "r", "Notes/").notes_writable
    assert not with_notes_folder(loaded, "r", "Other").notes_writable
    assert not with_notes_folder(loaded, None).notes_writable
    with pytest.raises(KeepError, match="Unknown layout"):
        with_notes_writing(loaded, new_notes="piles")
    path.write_text(text.replace("'one-folder'", "'piles'"), encoding="utf-8")
    with pytest.raises(KeepError, match="new_notes must be"):
        load_keep_config(path)


def test_keep_toml_notes_write_back(tmp_path: Path, env: Env) -> None:
    """Write to file… on notes has its own switch (#501): Save as note…'s doesn't give it."""
    config = with_notes_writing(with_notes_folder(_config(env.root), "r", "Notes"), writable=True)
    assert not config.notes_write_back
    config = with_notes_writing(config, write_back=True)
    path = tmp_path / "keep.toml"
    save_keep_config(config, path)
    assert "writable = true\nwrite_back = true" in path.read_text("utf-8")
    loaded = load_keep_config(path)
    assert loaded.notes_write_back
    assert with_notes_folder(loaded, "r", "Notes").notes_write_back
    assert not with_notes_folder(loaded, "r", "Other").notes_write_back
    assert not with_notes_folder(loaded, None).notes_write_back
    with pytest.raises(KeepError, match="no notes folder"):
        with_notes_writing(with_notes_folder(loaded, None), write_back=True)
