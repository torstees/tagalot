"""Windows shortcuts read as links (#602): the ``.lnk`` reader, the walk, and a scan that
makes a shortcut a place of its target, with the 2D assets theme."""

import importlib.util
import os
import struct
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from tagalot.core.models import (
    Entity,
    EntityContains,
    LinkedPlace,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from tagalot.core.scanner import walk_root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.shortcuts import Shortcut, on_disk, parse_shortcut, shortcut_target

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"
CLSID = bytes.fromhex("0114020000000000c000000000000046")


def make_lnk(
    *,
    relative: str | None = None,
    local: str | None = None,
    network: tuple[str, str] | None = None,
    is_dir: bool = False,
) -> bytes:
    """A shortcut as Windows writes one, with what's given: a relative path (stored as
    Unicode), a local path, or a network share and the path in it."""
    flags = 0x80  # Unicode strings
    info = b""
    if local is not None or network is not None:
        flags |= 0x2
        body = b""
        local_at = network_at = 0
        header = 0x1C
        link_flags = 0
        if local is not None:
            link_flags |= 0x1
            volume = struct.pack("<IIII", 0x11, 3, 0, 0x10) + b"\0"
            volume_at = header
            local_at = header + len(volume)
            body += volume + local.encode("cp1252") + b"\0"
        else:
            volume_at = 0
        suffix = b"\0"
        if network is not None:
            link_flags |= 0x2
            network_at = header + len(body)
            share = network[0].encode("cp1252") + b"\0"
            body += struct.pack("<IIIII", 0x14 + len(share), 0, 0x14, 0, 0) + share
            suffix = network[1].encode("cp1252") + b"\0"
        suffix_at = header + len(body)
        body += suffix
        size = header + len(body)
        info = (
            struct.pack(
                "<IIIIIII", size, header, link_flags, volume_at, local_at, network_at, suffix_at
            )
            + body
        )
    strings = b""
    if relative is not None:
        flags |= 0x8
        strings = struct.pack("<H", len(relative)) + relative.encode("utf-16-le")
    head = struct.pack(
        "<I16sIIQQQIIIHHII",
        0x4C,
        CLSID,
        flags,
        0x10 if is_dir else 0x20,
        0,
        0,
        0,
        0,
        0,
        1,
        0,
        0,
        0,
        0,
    )
    assert len(head) == 0x4C
    return head + info + strings + struct.pack("<I", 0)


def test_reads_what_a_shortcut_says() -> None:
    assert parse_shortcut(make_lnk(relative="..\\Books\\x.pdf")) == Shortcut(
        relative="..\\Books\\x.pdf"
    )
    assert parse_shortcut(make_lnk(local="D:\\Books\\x.pdf", is_dir=False)) == Shortcut(
        local="D:\\Books\\x.pdf"
    )
    assert parse_shortcut(make_lnk(network=("\\\\nas\\media", "Books"), is_dir=True)) == Shortcut(
        network="\\\\nas\\media\\Books", is_dir=True
    )
    assert parse_shortcut(b"not a shortcut") is None
    assert parse_shortcut(b"L\0\0\0" + b"\0" * 100) is None


def test_finds_the_target_and_reads_through_folder_shortcuts(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "real" / "sub" / "b.txt").write_text("b")
    links = tmp_path / "links"
    links.mkdir()
    (links / "sub.lnk").write_bytes(make_lnk(relative="..\\real\\sub", is_dir=True))
    gone = str(tmp_path / "gone")
    (links / "both.lnk").write_bytes(make_lnk(relative="..\\nothing", local=str(tmp_path / "real")))
    (links / "broken.lnk").write_bytes(make_lnk(local=gone))
    assert shortcut_target(str(links / "sub.lnk")) == str(tmp_path / "real" / "sub")
    assert shortcut_target(str(links / "both.lnk")) == str(tmp_path / "real")  # the next one
    assert shortcut_target(str(links / "broken.lnk")) is None
    through = on_disk(str(links / "sub.lnk" / "b.txt"))
    assert through == str(tmp_path / "real" / "sub" / "b.txt")
    assert on_disk(str(links / "plain.txt")) == str(links / "plain.txt")


def test_the_walk_reads_shortcuts_as_links(tmp_path: Path) -> None:
    (tmp_path / "real" / "sub").mkdir(parents=True)
    (tmp_path / "real" / "a.png").write_bytes(b"png" * 10)
    (tmp_path / "real" / "sub" / "b.txt").write_text("b")
    links = tmp_path / "links"
    links.mkdir()
    (links / "a.png.lnk").write_bytes(make_lnk(relative="..\\real\\a.png"))
    (links / "sub.lnk").write_bytes(make_lnk(relative="..\\real\\sub", is_dir=True))
    (links / "broken.lnk").write_bytes(make_lnk(relative="..\\real\\gone.png"))
    (links / "odd.lnk").write_bytes(b"not a shortcut")
    entries = {
        e.relpath: e for e in walk_root(str(tmp_path), extensions={".png", ".txt"}, dirs=True)
    }
    real = os.path.realpath(tmp_path / "real")
    shortcut = entries["links/a.png.lnk"]
    assert (shortcut.kind, shortcut.ext, shortcut.how) == (ResourceKind.FILE, ".png", "shortcut")
    assert shortcut.target == os.path.join(real, "a.png")
    assert shortcut.size == 30  # the target's
    folder = entries["links/sub.lnk"]
    assert (folder.kind, folder.how, folder.target) == (
        ResourceKind.DIR,
        "shortcut",
        os.path.join(real, "sub"),
    )
    inside = entries["links/sub.lnk/b.txt"]
    assert (inside.how, inside.target) == ("inside", os.path.join(real, "sub", "b.txt"))
    assert "links/broken.lnk" not in entries  # its target is gone
    assert "links/odd.lnk" not in entries  # not a shortcut: a .lnk file, not wanted


@pytest.fixture
def session(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as opened:
        yield opened


def _items(session: KeepSession, title: str) -> list[int]:
    with session.reader.connect() as conn:
        return list(conn.scalars(select(Entity.id).where(Entity.title == title)))


def _artists(session: KeepSession, entity_id: int) -> set[str]:
    with session.reader.connect() as conn:
        titles = conn.scalars(
            select(Entity.title)
            .join(EntityContains, EntityContains.parent_id == Entity.id)
            .where(EntityContains.child_id == entity_id)
        )
        return {t.partition(" (")[0] for t in titles}


def _linked(session: KeepSession) -> dict[str, tuple[str, str]]:
    with session.reader.connect() as conn:
        paths = {i: p for i, p in conn.execute(select(Resource.id, Resource.relpath))}
        rows = conn.execute(select(LinkedPlace.resource_id, LinkedPlace.first_id, LinkedPlace.how))
        return {paths[a]: (paths[b], how) for a, b, how in rows}


def test_a_shortcut_is_a_place_of_its_target(session: KeepSession) -> None:
    root = Path(session.root_path("assets"))
    (root / "Kenji Sato" / "forest.png.lnk").write_bytes(
        make_lnk(relative="..\\Aurora Studio\\Backgrounds\\forest.png")
    )
    (root / "Kenji Sato" / "Backgrounds.lnk").write_bytes(
        make_lnk(relative="..\\Aurora Studio\\Backgrounds", is_dir=True)
    )
    session.scan_all()
    [forest] = _items(session, "forest.png")
    [_] = _items(session, "dusk sky.png")  # one item, though in two places
    assert _artists(session, forest) == {"Aurora Studio", "Kenji Sato"}
    linked = _linked(session)
    assert linked["Kenji Sato/forest.png.lnk"] == (
        "Aurora Studio/Backgrounds/forest.png",
        "shortcut",
    )
    assert linked["Kenji Sato/Backgrounds.lnk/dusk sky.png"] == (
        "Aurora Studio/Backgrounds/dusk sky.png",
        "inside",
    )

    (root / "Kenji Sato" / "forest.png.lnk").unlink()  # missing, not deleted (rule 7)
    session.scan_all()
    assert _items(session, "forest.png") == [forest]
    with session.reader.connect() as conn:
        status = conn.scalar(
            select(Resource.status).where(Resource.relpath == "Kenji Sato/forest.png.lnk")
        )
    assert status is ResourceStatus.MISSING
