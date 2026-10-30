"""A linked place's row on an item's page (#598): "Also at …", indented, without a size."""

import pytest
from pytestqt.qtbot import QtBot

from tagalot.core.detail import FileRow
from tagalot.core.models import ResourceStatus
from tagalot.ui.detail_view import _file_label

pytestmark = pytest.mark.gui


def test_a_place_row_says_also_at(qtbot: QtBot) -> None:
    file = FileRow(1, "Assets", "Aurora/forest.png", "file", 2048, ResourceStatus.OK, "/a/f")
    place = FileRow(
        2, "Assets", "Kenji/forest.png", "file", 2048, ResourceStatus.OK, "/a/k", place_of=1
    )
    first, other = _file_label(file), _file_label(place)
    qtbot.addWidget(first)
    qtbot.addWidget(other)
    assert first.text().startswith("Aurora/forest.png")
    assert "2,048 bytes" in first.text()
    assert other.text().startswith("Also at Kenji/forest.png")
    assert "bytes" not in other.text()
    assert (first.indent(), other.indent()) == (-1, 16)
