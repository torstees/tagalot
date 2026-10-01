"""The dashboard's numbers (#113)."""

from sqlalchemy import insert

from tagalot.core.dashboard import RECENT, Dashboard, load_dashboard
from tagalot.core.models import EntityTag
from tagalot.core.tags import TagTreeCache, add_tag
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures


def _load(env: Env) -> Dashboard:
    tree = TagTreeCache(env.reader).get()
    with env.reader.connect() as conn:
        return load_dashboard(conn, env.schema, tree)


def test_counts_recent_items_and_folders(env: Env) -> None:
    env.scan()
    data = _load(env)
    assert [(t.plural, t.count) for t in data.types] == [
        ("Artists", 6),
        ("Albums", 4),
        ("Songs", 9),
    ]
    assert data.total == 19
    assert (data.untagged, data.untagged_share) == (19, 1.0)  # nothing tagged yet
    assert len(data.recent) == RECENT  # newest first
    assert data.recent[0].id > data.recent[-1].id
    assert data.roots["r"].online
    assert (data.most_used, data.least_used) == ((), ())


def test_most_and_least_used_tags(env: Env) -> None:
    env.scan()
    jazz = env.writer.run(lambda conn: add_tag(conn, None, "Jazz"))
    calm = env.writer.run(lambda conn: add_tag(conn, jazz, "Calm"))
    env.writer.run(lambda conn: add_tag(conn, None, "Unused"))
    rows = [
        {"entity_id": env.entity(title), "tag_id": tag}
        for title, tag in (("Kind of Blue", jazz), ("Jazz Hits", jazz), ("Blue", calm))
    ]
    env.writer.run(lambda conn: conn.execute(insert(EntityTag), rows))
    data = _load(env)
    assert [(u.path, u.count) for u in data.most_used] == [("Jazz", 2), ("Jazz / Calm", 1)]
    assert [(u.path, u.count) for u in data.least_used] == [("Unused", 0)]  # not repeated
    assert data.untagged == 16
