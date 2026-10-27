"""A gallery on a page (#386): groups for loose pictures, subfolders, and archives; an
archive's pictures listed and drawn in workers, a damaged one saying why, and a picture
opened from a temporary copy. On the 2D assets demo, whose theme scans archives."""

import zipfile
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QListWidget, QMenu
from pytestqt.qtbot import QtBot
from sqlalchemy import select

from tagalot.core.detail import DetailSection, FileRow
from tagalot.core.models import Resource, ResourceStatus
from tagalot.core.roots import local_path
from tagalot.core.session import KeepSession
from tagalot.ui.gallery_view import READING, GalleryView
from tagalot.ui.icons import icon_for
from tagalot.ui.main_window import MainWindow
from tests.core.media_files import png_bytes
from tests.ui.test_contents_search import session, window

pytestmark = pytest.mark.gui

__all__ = ["session", "window"]  # fixtures

PNG = png_bytes((40, 20))


def _files(session: KeepSession) -> tuple[str, Path]:
    root = session.keep.config.roots[0]
    base = Path(session.root_path(root.id))
    folder = base / "Gallery Test"
    (folder / "Disc 2").mkdir(parents=True)
    (folder / "a.png").write_bytes(PNG)
    (folder / "Disc 2" / "b.png").write_bytes(PNG)
    with zipfile.ZipFile(folder / "Extras.zip", "w") as zf:
        zf.writestr("shots/page10.png", PNG)
        zf.writestr("shots/page2.png", PNG)
        zf.writestr("inner.zip", b"PK\x05\x06" + b"\x00" * 18)
    (folder / "Broken.zip").write_bytes(b"PK\x03\x04 not a zip")
    session.scan_all()
    return root.id, base


def _rows(session: KeepSession, root_id: str, base: Path) -> tuple[FileRow, ...]:
    with session.reader.connect() as conn:
        found = conn.execute(
            select(Resource.id, Resource.relpath).where(
                Resource.relpath.like("Gallery Test/%"), Resource.kind == "file"
            )
        ).all()
    return tuple(
        FileRow(
            rid,
            "Assets",
            relpath,
            "file",
            1,
            ResourceStatus.OK,
            local_path(str(base), relpath),
            root_id=root_id,
        )
        for rid, relpath in found
    )


def _texts(gallery: QListWidget) -> list[str]:
    return [gallery.item(i).text() for i in range(gallery.count())]


def _view(qtbot: QtBot, window: MainWindow, session: KeepSession) -> GalleryView:
    root_id, base = _files(session)
    section = DetailSection("gallery", "Shots", files=_rows(session, root_id, base))
    view = GalleryView(section, session, 1, window.files, lambda file: QMenu(), None)
    qtbot.addWidget(view)
    view.resize(800, 600)
    view.show()
    return view


def test_a_gallery_shows_groups_and_an_archives_pictures(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    view = _view(qtbot, window, session)
    headers = [w.text() for w in view.findChildren(QLabel, "gallery_header")]
    assert headers == ["Disc 2", "Broken.zip", "Extras.zip"]  # loose pictures first
    assert _texts(view.lists[""]) == ["a.png"]
    assert view.lists[""].objectName() == "gallery_Shots"  # as before groups (#124)
    assert _texts(view.lists["Disc 2"]) == ["b.png"]

    qtbot.waitUntil(lambda: len(view.listings) == 2, timeout=5000)
    extras = view.lists["Extras.zip"]
    assert _texts(extras) == ["inner.zip", "page2.png", "page10.png"]
    plain = icon_for("image").cacheKey()
    qtbot.waitUntil(lambda: extras.item(1).icon().cacheKey() != plain, timeout=5000)
    assert extras.item(2).icon().cacheKey() != plain  # both pictures drawn
    notes = [w.text() for w in view.findChildren(QLabel, "gallery_note")]
    assert READING not in notes
    assert any(n.startswith("Couldn't read this archive: ") for n in notes)
    assert _texts(view.lists["Broken.zip"]) == ["Broken.zip"]  # the archive itself


def test_hiding_kinds_hides_files_and_empty_groups(
    qtbot: QtBot, window: MainWindow, session: KeepSession
) -> None:
    """The kind chips' part (#514): files of hidden types go, and groups left empty; an
    archive goes, group and all, with its type."""
    view = _view(qtbot, window, session)
    assert view.kinds() == {".png": 2, ".zip": 2}
    assert view.set_hidden({".png"}) == 2
    assert view.boxes[""].isHidden()
    assert view.boxes["Disc 2"].isHidden()
    assert not view.boxes["Extras.zip"].isHidden()
    assert view.set_hidden({".zip"}) == 2
    assert not view.boxes[""].isHidden()
    assert view.boxes["Extras.zip"].isHidden()
    assert view.boxes["Broken.zip"].isHidden()
    assert view.set_hidden(()) == 4
    assert all(not box.isHidden() for box in view.boxes.values())


def test_a_picture_opens_from_a_copy(
    qtbot: QtBot,
    window: MainWindow,
    session: KeepSession,
    nothing_is_launched: list[tuple[str, str]],
) -> None:
    view = _view(qtbot, window, session)
    qtbot.waitUntil(lambda: "Extras.zip" in view.listings, timeout=5000)
    archive = next(f for f in view.section.files if f.relpath.endswith("Extras.zip"))
    assert archive.path is not None
    before = Path(archive.path).read_bytes()
    menu = view.member_menu(archive, "shots/page2.png")
    assert [a.text() for a in menu.actions()] == ["Open", "Show the archive in file manager"]
    menu.actions()[0].trigger()
    qtbot.waitUntil(lambda: bool(nothing_is_launched), timeout=5000)
    [(how, path)] = nothing_is_launched
    assert how == "open"
    copy = Path(path)
    assert copy == session.temp_dir() / "archives" / str(archive.resource_id) / "shots/page2.png"
    assert copy.read_bytes() == PNG
    assert Path(archive.path).read_bytes() == before  # never touched
