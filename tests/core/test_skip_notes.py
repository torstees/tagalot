"""Notes on Skip entries (#334): why a pattern is in a folder's Skip list, saved in
``[roots.exclude_notes]`` (so ``exclude`` stays the plain list older builds read), shown and
edited in Keep configuration's Skip box after `` # ``, and written by Tagalot when it skips a
file itself."""

import uuid
from pathlib import Path

import pytest

from tagalot.core.keep import (
    KeepConfig,
    KeepConfigError,
    RootConfig,
    ThemeRef,
    load_keep_config,
    save_keep_config,
)
from tagalot.core.root_admin import add_skipped, edit_root, parse_skip_lines, skip_lines
from tagalot.core.scanner import compile_excludes
from tagalot.core.triage import exact_pattern


def _config(root: RootConfig) -> KeepConfig:
    return KeepConfig(
        id=uuid.UUID("0b6e3c1e-6f0a-4b54-9a8e-2b2d7f1c9d41"),
        name="Papers",
        theme=ThemeRef(id="research", version=1),
        roots=[root],
    )


def test_notes_round_trip_beside_a_plain_exclude_list(tmp_path: Path) -> None:
    root = RootConfig(
        "r",
        "Papers",
        str(tmp_path / "files"),
        exclude=["**/.DS_Store", "Exports/papers.bib"],
        exclude_notes={"Exports/papers.bib": "Saved here by Export BibTeX…", "gone": "dropped"},
    )
    path = tmp_path / "keep.toml"
    save_keep_config(_config(root), path)
    text = path.read_text(encoding="utf-8")
    assert "exclude = ['**/.DS_Store', 'Exports/papers.bib']" in text  # still plain strings
    assert "[roots.exclude_notes]\n'Exports/papers.bib' = 'Saved here by Export BibTeX…'" in text
    assert "dropped" not in text  # a note on no pattern isn't kept
    loaded = load_keep_config(path).roots[0]
    assert loaded.exclude == root.exclude
    assert loaded.exclude_notes == {"Exports/papers.bib": "Saved here by Export BibTeX…"}


def test_bad_notes_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "keep.toml"
    save_keep_config(_config(RootConfig("r", "P", str(tmp_path / "f"), exclude=["x"])), path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n[roots.exclude_notes]\nx = 3\n", encoding="utf-8"
    )
    with pytest.raises(KeepConfigError, match="exclude_notes must map patterns to notes"):
        load_keep_config(path)


def test_the_skip_box_format() -> None:
    root = RootConfig(
        "r", "P", "/p", exclude=["**/cache/**", "a.bib"], exclude_notes={"a.bib": "an export"}
    )
    assert skip_lines(root) == ["**/cache/**", "a.bib # an export"]
    patterns, notes = parse_skip_lines(
        ["**/cache/**", "a.bib # an export", "  b.pdf   #   mine ", "", "# a comment line"]
    )
    assert patterns == ["**/cache/**", "a.bib", "b.pdf"]
    assert notes == {"a.bib": "an export", "b.pdf": "mine"}


def test_a_hash_in_a_file_name_is_bracketed_and_still_matches() -> None:
    pattern = exact_pattern("Notes/C# tricks [draft].md")
    assert pattern == "Notes/C[#] tricks [[]draft].md"
    assert parse_skip_lines([f"{pattern} # skipped"])[0] == [pattern]
    matcher = compile_excludes([pattern])
    assert matcher is not None
    assert matcher.fullmatch("Notes/C# tricks [draft].md")


def test_add_skipped_keeps_existing_notes(tmp_path: Path) -> None:
    root = RootConfig("r", "P", str(tmp_path / "f"), exclude=["a"], exclude_notes={"a": "first"})
    config = add_skipped(_config(root), tmp_path / "k", "r", ["a", "b"], "Skipped from Triage")
    [changed] = config.roots
    assert changed.exclude == ["a", "b"]
    assert changed.exclude_notes == {"a": "first", "b": "Skipped from Triage"}
    # Removing a pattern drops its note.
    config = edit_root(config, tmp_path / "k", "r", exclude=["b"])
    assert config.roots[0].exclude_notes == {"b": "Skipped from Triage"}
