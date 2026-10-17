"""The demo keep script used by manual-testing checklists must keep working."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import select

from tagalot.core.detail import load_detail
from tagalot.core.item_pictures import own_picture
from tagalot.core.models import Entity
from tagalot.core.search import count_by_type, run_search
from tagalot.core.search_spec import SearchSpec
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.tags import TagTree
from tagalot.themes.loader import load_themes

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "make_demo_keep.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_creates_a_scanned_and_tagged_keep(tmp_path: Path) -> None:
    keep_dir = _script().make_demo(tmp_path)
    assert keep_dir == tmp_path / "Demo.keep"
    with KeepSession.open(keep_dir, Settings()) as session:
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            titles = set(conn.scalars(select(Entity.title)))
            places = run_search(conn, SearchSpec(include=_ids(tree, "Places")), tree)
            money = run_search(conn, SearchSpec(include=_ids(tree, "Money")), tree)
        [report] = session.scan_all()
    # The .DS_Store, ._ and Thumbs.db files are on disk but skipped.
    assert len(titles) == 9
    assert "._sunset.jpg" not in titles
    assert (tmp_path / "demo-files" / "Photos" / "Beach" / "._sunset.jpg").exists()
    assert len(places) == 5  # Iceland's three and Beach's two, through the hierarchy
    assert {h.title for h in money} == {"tax 2025.pdf", "März.txt"}
    assert [s.alias for s in tree.suggest("finance")] == ["finance"]
    described = {tree.node(t).name: tree.node(t).description for t in _all(tree)}
    assert described["Iceland"] == "Summer 2019 road trip around the Ring Road"
    assert described["Beach"] is None
    assert report.online
    assert report.new == report.ingested == 0  # already scanned
    assert report.ingest_errors == []


def _ids(tree: TagTree, name: str) -> tuple[int, ...]:
    [suggestion] = [s for s in tree.suggest(name) if tree.node(s.tag_id).name == name]
    return (suggestion.tag_id,)


def test_refuses_to_overwrite_without_reset(tmp_path: Path) -> None:
    script = _script()
    script.make_demo(tmp_path)
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_demo(tmp_path)
    (tmp_path / "demo-files" / "extra.txt").write_text("x", encoding="utf-8")
    script.make_demo(tmp_path, reset=True)
    assert not (tmp_path / "demo-files" / "extra.txt").exists()  # reset starts clean


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows locks open files")
def test_reset_refuses_while_the_keep_is_open(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        session.scan_all()
        with pytest.raises(script.DemoInUseError, match="Nothing was deleted"):
            script.make_demo(tmp_path, reset=True)
        assert (keep_dir / "keep.toml").exists()  # untouched, not half-deleted
    assert script.make_demo(tmp_path, reset=True) == keep_dir  # fine once it's closed


def test_a_failed_reset_puts_everything_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    script.make_demo(tmp_path)
    rename = Path.rename

    def busy_files(self: Path, target: Path) -> Path:
        if self.name == "demo-files":
            raise PermissionError(13, "The process cannot access the file")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", busy_files)
    with pytest.raises(script.DemoInUseError, match="demo-files is in use"):
        script.make_demo(tmp_path, reset=True)
    assert (tmp_path / "Demo.keep" / "keep.toml").exists()  # renamed aside, then back
    assert not (tmp_path / "Demo.keep.deleting").exists()
    assert (tmp_path / "demo-files" / "readme.md").exists()


def test_creates_a_media_keep_with_several_types(tmp_path: Path) -> None:
    script = _script()
    themes = tmp_path / "themes"
    keep_dir = script.make_media_demo(tmp_path, themes_dir=themes)
    assert (themes / script.MEDIA_THEME_FILE).exists()
    catalog = load_themes(user_dir=themes)
    with KeepSession.open(keep_dir, Settings(), catalog=catalog) as session:
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            love = count_by_type(conn, SearchSpec(text="love"), tree)
            rock = count_by_type(conn, SearchSpec(include=_ids(tree, "Rock")), tree)
    assert everything == {"demo_media.artist": 5, "demo_media.album": 11, "demo_media.song": 6}
    assert love == {"demo_media.artist": 2, "demo_media.album": 6, "demo_media.song": 2}
    assert rock == {"demo_media.artist": 2, "demo_media.album": 2, "demo_media.song": 2}
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_media_demo(tmp_path, themes_dir=themes)
    assert script.make_media_demo(tmp_path, reset=True, themes_dir=themes) == keep_dir


def _all(tree: TagTree) -> list[int]:
    ids, stack = [], list(tree.children(None))
    while stack:
        tag_id = stack.pop()
        ids.append(tag_id)
        stack.extend(tree.children(tag_id))
    return ids


def test_media_leaves_the_demo_keep_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_user_folders: Path
) -> None:
    """--media builds only Media.keep, so an open Demo.keep can't block it."""
    script = _script()
    monkeypatch.setattr(script, "SCRATCH", tmp_path)
    assert script.main([]) == 0
    demo_toml = (tmp_path / "Demo.keep" / "keep.toml").read_bytes()
    assert script.main(["--reset", "--media"]) == 0
    assert (tmp_path / "Media.keep" / "keep.toml").exists()
    assert (tmp_path / "Demo.keep" / "keep.toml").read_bytes() == demo_toml  # untouched
    assert (private_user_folders / "themes" / script.MEDIA_THEME_FILE).exists()


def test_creates_an_assets_keep(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_assets_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.theme.id == "assets2d"
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            pixel = count_by_type(conn, SearchSpec(include=_ids(tree, "Pixel art")), tree)
    assert everything == {
        "assets2d.artist": 2,
        "assets2d.image": 9,
        "assets2d.font": 1,
        "assets2d.archive": 2,
    }
    assert pixel == {"assets2d.image": 3}
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_assets_demo(tmp_path)
    assert script.make_assets_demo(tmp_path, reset=True) == keep_dir


def test_creates_a_movies_keep(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_movies_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.theme.id == "movies"
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            scifi = count_by_type(conn, SearchSpec(include=_ids(tree, "Sci-Fi")), tree)
    assert everything == {
        "movies.actor": 8,  # Alice and Grandma, named by folders' notes (#449)
        "movies.archive": 2,  # Old scans.zip, Screens.zip (#446)
        "movies.collection": 3,
        "movies.folder": 8,  # folders above pictures: Home Movies, Alice, 3rd Birthday… (#447)
        "movies.movie": 12,  # with Alice's home videos (#446)
        "movies.picture": 9,  # family pictures, and screenshots folders' stills
    }
    assert scifi == {"movies.movie": 3}
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_movies_demo(tmp_path)


def test_alices_note_is_hers_and_her_folders_is_the_folders(tmp_path: Path) -> None:
    """The person Alice takes her note from the notes folder; her folder's note describes
    the Folder (docs: Notes, *Where a note goes*)."""
    script = _script()
    with (
        KeepSession.open(script.make_movies_demo(tmp_path), Settings()) as session,
        session.reader.connect() as conn,
    ):
        alice = {
            type_: load_detail(conn, session.schema, id_, session.root_path)
            for id_, type_ in conn.execute(
                select(Entity.id, Entity.type).where(Entity.title == "Alice")
            )
        }
    person, folder = alice["movies.actor"], alice["movies.folder"]
    assert person is not None
    assert folder is not None
    born = [f.value for s in person.sections for f in s.fields if f.name == "born"]
    assert [str(value) for value in born] == ["2022-01-30"]
    assert person.note_body.startswith("Our youngest.")
    assert folder.note_body.startswith("Alice's first years")


def test_creates_a_books_keep(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_books_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.theme.id == "books"
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            # Humour is on the Discworld series; its books count with inherited tags.
            humour = SearchSpec(
                types=("books.book",), include=_ids(tree, "Humour"), inherit_tags=True
            )
            funny = count_by_type(conn, humour, tree)
            # Fantasy comes from the books' own subjects (file keywords).
            fantasy = count_by_type(conn, SearchSpec(include=_ids(tree, "Fantasy")), tree)
    assert everything == {
        "books.author": 15,  # Stephen Baxter, of a two-author creator; pirateaba (#418)
        "books.archive": 1,  # The Glass Tower's sketches (#446)
        "books.book": 22,  # Small Gods: its EPUB and PDF are one book; Field Notes (#426)
        "books.collection": 1,
        "books.comic": 4,
        "books.folder": 8,  # folders above pictures: Writing, The Glass Tower, renders… (#447)
        "books.picture": 5,  # covers named by front matter, renders (#446)
        "books.series": 6,
        "books.universe": 2,
    }
    assert funny == {"books.book": 4}  # Discworld, tagged, and its books
    assert fantasy == {"books.book": 8}  # from EPUBs, PDFs, Markdown, and a Word document
    # It opens ready to search inside documents: the text is read and indexed.
    assert _inside(keep_dir, "luggage") == ["The Colour of Magic"]  # an EPUB's chapter
    assert _inside(keep_dir, "explode") == ["Notes on Dragons"]  # an ODT's text
    assert _inside(keep_dir, "tortoise") == ["Small Gods"]  # its PDF and its Kindle copy
    assert _inside(keep_dir, "melons") == ["Small Gods"]  # a Kindle book's chapter
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_books_demo(tmp_path)


def test_creates_a_research_keep(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_research_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.theme.id == "research"
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            # Vision comes from a PDF's keywords (file keywords).
            vision = count_by_type(conn, SearchSpec(include=_ids(tree, "Vision")), tree)
    assert everything == {
        "research.author": 12,  # with Jane Doe, from the sidecar
        "research.paper": 4,  # the export's entry without a PDF makes none
        "research.venue": 3,
    }
    assert vision == {"research.paper": 1}
    assert _inside(keep_dir, "recurrence") == ["Attention Is All You Need"]  # the PDF's text
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_research_demo(tmp_path)


def test_creates_a_music_keep(tmp_path: Path) -> None:
    script = _script()
    keep_dir = script.make_music_demo(tmp_path)
    with KeepSession.open(keep_dir, Settings()) as session:
        assert session.theme.id == "music"
        tree = session.tag_cache.get()
        with session.reader.connect() as conn:
            everything = count_by_type(conn, SearchSpec(), tree)
            calm = count_by_type(conn, SearchSpec(include=_ids(tree, "Calm")), tree)
    assert everything == {"music.artist": 7, "music.album": 5, "music.song": 12}
    assert calm == {"music.song": 3}
    with pytest.raises(FileExistsError, match="--reset"):
        script.make_music_demo(tmp_path)
    assert script.make_music_demo(tmp_path, reset=True) == keep_dir


def _inside(keep_dir: Path, text: str) -> list[str]:
    """Titles of the items whose documents contain ``text`` (In documents)."""
    with KeepSession.open(keep_dir, Settings()) as session:
        stats = session.contents_stats()
        assert stats is not None
        assert stats.failed == 0  # every demo document has text to read
        spec = SearchSpec(text=text, contents=True)
        with session.reader.connect() as conn:
            scope = session.contents_scope(conn, spec)
            assert scope is not None
            hits = run_search(conn, spec, session.tag_cache.get(), contents=scope)
    return [h.title for h in hits]


def _galleries(session: KeepSession, title: str) -> dict[str, list[str]]:
    """The item's page's galleries: title -> file names."""
    with session.reader.connect() as conn:
        item = conn.scalar(select(Entity.id).where(Entity.title == title))
        assert item is not None, title
        detail = load_detail(conn, session.schema, item, session.root_path)
    assert detail is not None
    return {
        s.title: sorted(f.relpath.rpartition("/")[2] for f in s.files)
        for s in detail.sections
        if s.kind == "gallery"
    }


def test_the_demos_have_galleries(tmp_path: Path) -> None:
    """A movie's Screenshots folder and Screens.zip, an actor's photos beside her note, and
    an album's scans and booklet (#389)."""
    script = _script()
    with KeepSession.open(script.make_movies_demo(tmp_path), Settings()) as session:
        assert _galleries(session, "The Matrix") == {
            "Screenshots": ["Screens.zip", "lobby 1.jpg", "lobby 2.jpg", "rooftop.jpg"]
        }
        assert _galleries(session, "Sigourney Weaver") == {
            "Photos": ["ripley 1.jpg", "ripley 2.jpg"]
        }
        assert _galleries(session, "Al Pacino") == {}  # no photos: no empty gallery
        with session.reader.connect() as conn:  # beside her note: her picture (#439)
            weaver = conn.scalar(select(Entity.id).where(Entity.title == "Sigourney Weaver"))
            assert weaver is not None
            picture = own_picture(conn, weaver)
        assert picture is not None
        assert (picture.relpath, picture.source) == ("Notes/Actors/Sigourney Weaver.jpg", "note")
    with KeepSession.open(script.make_music_demo(tmp_path), Settings()) as session:
        assert _galleries(session, "Kind of Blue") == {
            "Scans": ["Booklet.zip", "back.jpg", "front.jpg"]
        }
