"""Turning a resource into a picture, by its kind (DESIGN.md §10 "Resolution").

Thumbnail providers only choose resources; a :class:`Renderer` for the resource's kind reads
the picture from it. Each renderer has an id and a version that go into the cache key, so
changing how a kind is read (bump ``version``) regenerates its thumbnails.
"""

from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from tagalot.themes.api import Kind, ResourceInfo, kind_of


@dataclass(frozen=True)
class Renderer:
    """Reads a picture from a file of one kind: ``load(path, size)`` returns the image, or
    ``None`` when the file has none (an archive without images). Errors are raised."""

    id: str
    version: int
    load: Callable[[str, int], Image.Image | None]


def load_image(path: str, size: int) -> Image.Image:
    """Decode an image file. JPEGs decode straight at a reduced scale when ``size`` allows,
    which is much faster for large photos."""
    with Image.open(path) as image:
        image.draft(None, (size, size))
        image.load()
        return image


RENDERERS: dict[Kind, Renderer] = {
    Kind.IMAGE: Renderer("image", 1, load_image),
}
"""The renderer for each resource kind; kinds without one never give a picture."""


def renderer_for(resource: ResourceInfo) -> Renderer | None:
    """The renderer for ``resource``'s kind, if there is one."""
    kind = kind_of(resource)
    return None if kind is None else RENDERERS.get(kind)
