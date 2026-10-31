"""Waits get more room than their tests give (#635, ``tests/conftest.py``)."""

import pytest
from pytestqt.qtbot import QtBot

from tests.conftest import WAIT_SCALE

pytestmark = pytest.mark.gui


def test_a_wait_gets_more_room(qtbot: QtBot) -> None:
    with pytest.raises(qtbot.TimeoutError, match=f"timed out in {20 * WAIT_SCALE} milliseconds"):
        qtbot.waitUntil(lambda: False, timeout=20)
