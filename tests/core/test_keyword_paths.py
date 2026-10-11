"""A tag path in a note's ``tags`` list is one keyword, matched as a path (#420).

On the movies demo: Leonardo DiCaprio's notes-folder note names ``Locale > Fantasy >
Castle`` while the keep also has Genre > Fantasy, so a ``Fantasy`` on its own would match
neither.
"""

import importlib.util
from pathlib import Path

from sqlalchemy import select

from tagalot.core.keywords import keywords_of
from tagalot.core.models import Entity, EntityTag
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"

NOTE = """---
tagalot-type: actor
tags:
  - Locale > Fantasy > Castle
  - Science Fiction
---
Cobb in Inception.
"""


def test_a_path_in_a_notes_tags_list(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    keep = module.make_movies_demo(tmp_path)
    with KeepSession.open(keep, Settings()) as session:
        tags = session.tags
        tree = session.tag_cache.get()
        assert any(tree.node(t).name == "Fantasy" for t in tree)  # the demo's Genre > Fantasy
        castle = tags.add(tags.add(tags.add(None, "Locale"), "Fantasy"), "Castle")
        (tmp_path / "movie-files" / "Notes" / "Leonardo DiCaprio.md").write_text(
            NOTE, encoding="utf-8"
        )
        session.scan_all()
        with session.reader.connect() as conn:
            leo = conn.scalar(select(Entity.id).where(Entity.title == "Leonardo DiCaprio"))
            assert leo is not None
            assert keywords_of(conn, [leo])[leo] == ["Locale > Fantasy > Castle", "Science Fiction"]
            tagged = set(conn.scalars(select(EntityTag.tag_id).where(EntityTag.entity_id == leo)))
        assert tagged == {castle}  # not Locale, and no unmatched Fantasy
