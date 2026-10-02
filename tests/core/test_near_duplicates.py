"""Near-duplicates: blocking keys and similarity (#118), and re-reading after a theme
version change."""

import importlib.util
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image as PILImage
from sqlalchemy import select, update

from tagalot.builtin_themes.assets2d import (
    MAX_COLOR_DISTANCE,
    Assets2DTheme,
    Image,
    color_distance,
    hash_distance,
    picture_color,
    picture_hash,
)
from tagalot.builtin_themes.music import MusicTheme
from tagalot.core.dedupe import near_duplicates
from tagalot.core.models import Root
from tagalot.core.session import KeepSession
from tagalot.core.settings import Settings
from tagalot.core.theme_options import effective_options
from tagalot.core.theme_schema import build_theme_schema
from tagalot.themes.api import EntityRef, Record
from tests.themes.test_music import Env, env, library

__all__ = ["env", "library"]  # fixtures

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_demo_keep.py"


def _picture(size: tuple[int, int], shift: int = 0) -> PILImage.Image:
    """A picture with some structure (a gradient and a block), so hashes mean something."""
    image = PILImage.new("L", size)
    w, h = size
    image.putdata([((x * 255) // w + shift) % 256 for y in range(h) for x in range(w)])
    block = PILImage.new("L", (w // 3, h // 3), 255)
    image.paste(block, (w // 3, h // 3))
    return image


def test_recolored_pictures_of_one_layout_dont_count() -> None:
    """The difference hash sees only light and dark: the demo's icons share a shape in
    different colors, and must not pair."""
    theme = Assets2DTheme()
    layout = _picture((120, 80))
    red = PILImage.merge(
        "RGB", (layout, layout.point(lambda v: v // 4), layout.point(lambda v: v // 4))
    )
    blue = PILImage.merge(
        "RGB", (layout.point(lambda v: v // 4), layout.point(lambda v: v // 4), layout)
    )
    hashes = [picture_hash(i) + picture_color(i.copy()) for i in (red, blue, red.resize((60, 40)))]
    assert hash_distance(hashes[0], hashes[1]) <= 6  # the same layout…
    assert color_distance(hashes[0], hashes[1]) > MAX_COLOR_DISTANCE  # …in other colors

    def record(n: int) -> Record:
        return Record(EntityRef(n, "assets2d.image"), str(n), {"phash": hashes[n]}, {})

    assert theme.similarity(Image, record(0), record(1)) == 0.0
    assert theme.similarity(Image, record(0), record(2)) >= 58 / 64  # resized: alike


def test_picture_hashes() -> None:
    big = picture_hash(_picture((300, 200)))
    small = picture_hash(_picture((150, 100)))  # the same picture, resized
    other = picture_hash(_picture((300, 200)).transpose(PILImage.Transpose.FLIP_LEFT_RIGHT))
    assert len(big) == 16
    assert picture_color(PILImage.new("RGB", (4, 4), (60, 40, 120))) == "3c2878"
    assert picture_color(PILImage.new("RGBA", (4, 4), (0, 0, 0, 0))) == "808080"
    assert hash_distance(big, small) <= 6
    assert hash_distance(big, other) > 20
    assert hash_distance(big, big) == 0


def test_fingerprints_carry_the_theme_version_only_past_1() -> None:
    options = effective_options(MusicTheme, {}, {})
    assert options.fingerprint() == options.fingerprint(1) == "{}"  # as recorded before
    assert options.fingerprint(2) == '{"__theme_version__": 2}'


def test_music_finds_the_same_song_in_two_places(env: Env) -> None:
    copy = env.files / "Other" / "02.mp3"
    copy.parent.mkdir()
    shutil.copy(env.files / "Jazz Hits" / "02.mp3", copy)  # "Blue" by Joni Mitchell, again
    env.scan()
    with env.reader.connect() as conn:
        found = near_duplicates(conn, build_theme_schema(MusicTheme))
    assert [(p.type_id, p.a[1], p.b[1], p.score) for p in found.pairs] == [
        ("music.song", "Blue", "Blue", 1.0)
    ]
    assert found.skipped_keys == 0
    with env.reader.connect() as conn:
        tight = near_duplicates(conn, build_theme_schema(MusicTheme), max_bucket=1)
    assert (tight.pairs, tight.skipped_keys) == ([], 2)  # the Blue and Intro keys, 2 each


@pytest.fixture
def assets(tmp_path: Path) -> Iterator[KeepSession]:
    spec = importlib.util.spec_from_file_location("make_demo_keep", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with KeepSession.open(module.make_assets_demo(tmp_path), Settings()) as session:
        yield session


def test_a_resized_picture_is_a_near_duplicate(assets: KeepSession) -> None:
    root = Path(assets.root_path("assets"))
    with PILImage.open(root / "Aurora Studio" / "Backgrounds" / "forest.png") as image:
        image.resize((400, 225)).save(root / "Kenji Sato" / "forest small.png")
    assets.scan_all()
    with assets.reader.connect() as conn:
        found = near_duplicates(conn, assets.schema)
    titles = {(p.a[1], p.b[1]) for p in found.pairs}
    # the demo's icons share layouts in other colors: they don't pair
    assert titles == {("forest.png", "forest small.png")}
    forest = next(p for p in found.pairs if p.b[1] == "forest small.png")
    assert forest.score >= 58 / 64
    assert forest.type_id == "assets2d.image"


def test_a_new_theme_version_reads_every_file_again_once(assets: KeepSession) -> None:
    """The keep was ingested by version 1 (which had no picture hash): its record says so,
    so the next scan reads everything again, and the one after doesn't."""
    old = effective_options(type(assets.theme()), {}, {}).fingerprint(1)
    assets.writer.run(lambda conn: conn.execute(update(Root).values(ingest_options=old)))
    [report] = assets.scan_all()
    assert report.ingested > 0  # every file again
    with assets.reader.connect() as conn:
        recorded = conn.scalar(select(Root.ingest_options))
    assert recorded is not None
    assert '"__theme_version__": 2' in recorded
    [again] = assets.scan_all()
    assert again.ingested == 0
