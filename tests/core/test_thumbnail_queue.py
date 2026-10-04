"""Making a scan's thumbnails in the background (#241, DESIGN.md §6 step 7)."""

import importlib.util
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.keep import load_keep_config
from tagalot.core.models import Entity
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.thumbnails.queue import (
    THREADS,
    QueueResult,
    ThumbnailQueue,
    entities_needing_thumbnails,
)
from tests.core.media_files import write_image

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


@pytest.fixture
def assets(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        yield session


def _needing(session: KeepSession, since: datetime | None = None) -> list[int]:
    with session.reader.connect() as conn:
        return entities_needing_thumbnails(conn, since)


def _all_ids(session: KeepSession) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).order_by(Entity.id.desc())))


def _id(session: KeepSession, title: str) -> int:
    with session.reader.connect() as conn:
        found = conn.scalar(select(Entity.id).where(Entity.title == title))
    assert found is not None
    return found


def _settle(session: KeepSession) -> None:
    """Wait for the remembered sources the resolver queued on the writer."""
    session.writer.run(lambda conn: None)


def test_which_entities_need_thumbnails(assets: KeepSession) -> None:
    everything = _all_ids(assets)
    assert _needing(assets) == everything  # nothing resolved yet; newest first
    forest = _id(assets, "forest.png")
    assert assets.thumbnails.resolve(forest).thumbnail is not None
    _settle(assets)
    assert forest not in _needing(assets)  # remembered

    # Changed in place: its file is ingested again by the next scan, so it's queued again.
    path = Path(assets.root_path("assets")) / "Aurora Studio" / "Backgrounds" / "forest.png"
    write_image(path, (300, 200), "blue")
    started = datetime.now(UTC) - timedelta(seconds=1)
    assets.scan_all()
    assert forest in _needing(assets, started)
    assert forest not in _needing(assets, datetime.now(UTC) + timedelta(hours=1))


def test_the_queue_makes_and_remembers_thumbnails(assets: KeepSession) -> None:
    ids = _all_ids(assets)
    progress: list[tuple[int, int]] = []
    finished = threading.Event()
    results: list[QueueResult] = []

    def done(result: QueueResult) -> None:
        results.append(result)
        finished.set()

    before = assets.thumbnails.cache.stats().count
    queue = ThumbnailQueue(assets.thumbnails)
    queue.start(ids, lambda d, t: progress.append((d, t)), done)
    assert finished.wait(30)
    assert progress[-1] == (len(ids), len(ids))
    [result] = results
    assert (result.done, result.stopped) == (len(ids), False)
    assert result.pictures >= 12  # images, the font, archives, and artists with pictures
    assert assets.thumbnails.cache.stats().count > before
    _settle(assets)
    assert [i for i in _needing(assets) if i in ids] == []  # every one remembered
    assert queue.remaining == 0


def test_stopping_and_closing(assets: KeepSession) -> None:
    gate, started = threading.Event(), threading.Event()
    calls: list[int] = []

    class Slow:
        def resolve(self, entity_id: int) -> object:
            calls.append(entity_id)
            started.set()
            gate.wait(5)
            raise RuntimeError("stopped mid-way is fine")

    queue = ThumbnailQueue(Slow())  # type: ignore[arg-type]
    done: list[QueueResult] = []
    queue.start(list(range(1, 50)), done=done.append)
    assert started.wait(5)
    stopping = threading.Thread(target=queue.stop)  # waits for the threads
    stopping.start()
    gate.set()
    stopping.join(10)
    assert not queue.running
    # It stopped after the ones in hand: at most one per thread, plus the short window.
    assert len(calls) <= THREADS * 2
    assert not [t for t in threading.enumerate() if t.name.startswith("thumbnail-queue")]
    assert done == []  # a stopped run reports nothing
    queue.close()
    queue.start([1])
    assert not queue.running  # a closed queue starts nothing


def test_the_session_queues_after_a_scan_unless_turned_off(assets: KeepSession) -> None:
    assets.scan_all()
    finished = threading.Event()
    count = assets.queue_thumbnails(done=lambda result: finished.set())
    assert count == len(_all_ids(assets))
    assert finished.wait(30)

    assets.set_thumbnails_after_scan(False)
    assert load_keep_config(assets.keep.toml_path).thumbnails_after_scan is False
    assert "after_scan = false" in assets.keep.toml_path.read_text(encoding="utf-8")
    assets.scan_all()
    assert assets.queue_thumbnails() == 0
    assets.set_thumbnails_after_scan(True)
    assert load_keep_config(assets.keep.toml_path).thumbnails_after_scan is True


def test_closing_the_keep_stops_the_queue(assets: KeepSession) -> None:
    assets.scan_all()
    assets.queue_thumbnails()
    assets.close()
    assert not assets.thumbnail_queue.running
    assert assets.queue_thumbnails() == 0


def test_several_at_once_in_order(assets: KeepSession) -> None:
    """Thumbnails are made on several threads (#275), progress counts up one by one, and
    they are taken roughly in the list's order (newest first)."""
    seen: list[int] = []
    threads: set[int] = set()
    lock = threading.Lock()
    both = threading.Barrier(2, timeout=5)

    class Recording:
        def resolve(self, entity_id: int) -> object:
            with lock:
                seen.append(entity_id)
                threads.add(threading.get_ident())
            if entity_id in (1, 2):
                both.wait()  # only returns if two are being made at the same time
            return type("Result", (), {"thumbnail": None})()

    progress: list[int] = []
    finished = threading.Event()
    queue = ThumbnailQueue(Recording())  # type: ignore[arg-type]
    queue.start(list(range(1, 41)), lambda d, t: progress.append(d), lambda r: finished.set())
    assert finished.wait(10)
    assert sorted(seen) == list(range(1, 41))
    assert len(threads) > 1
    assert progress == list(range(1, 41))
    assert seen.index(40) > seen.index(1)  # roughly in order: never far ahead
    assert all(abs(seen.index(n) - (n - 1)) <= THREADS * 2 for n in range(1, 41))
