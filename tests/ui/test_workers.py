"""Tests for background jobs: results, errors, and scan progress arrive on the GUI thread."""

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QThread, QThreadPool
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from tagalot.core.db import create_keep_engine
from tagalot.core.keep import RootConfig, ThemeRef, create_keep
from tagalot.core.models import Base
from tagalot.core.scanjob import ScanReport
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.writer import DbWriter
from tagalot.ui.workers import ScanController, run_in_pool, watch_future

pytestmark = pytest.mark.gui


def _running(controller: ScanController) -> bool:
    return controller.running  # read fresh (mypy would narrow the property)


def _on_gui_thread() -> bool:
    return QThread.currentThread() is QApplication.instance().thread()  # type: ignore[union-attr]


def test_results_arrive_on_the_gui_thread(qtbot: QtBot) -> None:
    worker_thread: list[bool] = []
    received: list[tuple[int, bool]] = []

    def work() -> int:
        worker_thread.append(threading.current_thread() is threading.main_thread())
        return 42

    run_in_pool(work, on_done=lambda value: received.append((value, _on_gui_thread())))
    qtbot.waitUntil(lambda: bool(received), timeout=5000)
    assert worker_thread == [False]  # the work ran off the GUI thread
    assert received == [(42, True)]  # the result came back to it


def test_errors_arrive_on_the_gui_thread(qtbot: QtBot) -> None:
    errors: list[tuple[str, bool]] = []

    def boom() -> None:
        raise OSError("share unreachable")

    run_in_pool(boom, on_error=lambda e: errors.append((str(e), _on_gui_thread())))
    qtbot.waitUntil(lambda: bool(errors), timeout=5000)
    assert errors == [("share unreachable", True)]


def test_unhandled_errors_are_logged(qtbot: QtBot, caplog: pytest.LogCaptureFixture) -> None:
    def boom() -> None:
        raise ValueError("nobody listening")

    run_in_pool(boom)
    qtbot.waitUntil(lambda: "Background job failed" in caplog.text, timeout=5000)


def test_the_gui_stays_responsive_during_slow_work(qtbot: QtBot) -> None:
    gate = threading.Event()
    done: list[bool] = []
    run_in_pool(lambda: gate.wait(5), on_done=lambda _: done.append(True))
    start = time.monotonic()
    qtbot.wait(50)  # the event loop keeps running while the job waits
    assert time.monotonic() - start < 1
    assert done == []
    gate.set()
    qtbot.waitUntil(lambda: done == [True], timeout=5000)


def test_writer_futures(qtbot: QtBot, tmp_path: Path) -> None:
    engine = create_keep_engine(tmp_path / "keep.db")
    Base.metadata.create_all(engine)
    received: list[tuple[object, bool]] = []
    with DbWriter(engine) as writer:
        watch_future(
            writer.submit(lambda conn: 7), on_done=lambda v: received.append((v, _on_gui_thread()))
        )

        def fail(conn: object) -> None:
            raise RuntimeError("rolled back")

        watch_future(
            writer.submit(fail), on_error=lambda e: received.append((str(e), _on_gui_thread()))
        )
        qtbot.waitUntil(lambda: len(received) == 2, timeout=5000)
    assert set(received) == {(7, True), ("rolled back", True)}


# --- Scan now ---


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    files = tmp_path / "files"
    files.mkdir()
    for i in range(5):
        (files / f"photo{i}.jpg").write_bytes(b"x")
    keep = create_keep(
        tmp_path / "k", "K", ThemeRef("generic", 1), [RootConfig("r", "Files", str(files))]
    )
    with KeepSession.open(keep.dir, Settings()) as session:
        yield session


def test_scan_now_reports_progress_and_results(qtbot: QtBot, session: KeepSession) -> None:
    controller = ScanController(QThreadPool())
    progress: list[tuple[str, bool]] = []
    controller.progress.connect(lambda m: progress.append((m, _on_gui_thread())))
    with qtbot.waitSignal(controller.started, timeout=1000):
        assert controller.scan(session)
    assert _running(controller)
    with qtbot.waitSignal(controller.finished, timeout=10_000) as finished:
        pass
    [report] = finished.args[0]
    assert isinstance(report, ScanReport)
    assert (report.new, report.ingested) == (5, 5)
    assert not _running(controller)
    assert progress
    assert all(on_gui for _, on_gui in progress)
    assert any(m.startswith("Ingesting 5 of 5") for m, _ in progress)


def test_only_one_scan_at_a_time(qtbot: QtBot, session: KeepSession) -> None:
    controller = ScanController(QThreadPool())
    assert controller.scan(session)
    assert not controller.scan(session)  # refused while running
    with qtbot.waitSignal(controller.finished, timeout=10_000):
        pass
    with qtbot.waitSignal(controller.finished, timeout=10_000):
        assert controller.scan(session)  # allowed again afterwards


def test_a_failed_scan_is_reported(qtbot: QtBot, session: KeepSession) -> None:
    controller = ScanController(QThreadPool())
    session.writer.close()  # a writer that refuses work makes the scan fail
    with qtbot.waitSignal(controller.failed, timeout=10_000) as failed:
        controller.scan(session)
    assert "closed" in str(failed.args[0])
    assert not _running(controller)
