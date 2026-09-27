"""Tests for the startup check that SQLite supports what Tagalot needs."""

import sqlite3

import pytest

from tagalot.core.db import MIN_SQLITE_VERSION, check_sqlite_support


def test_this_python_is_supported() -> None:
    assert sqlite3.sqlite_version_info >= MIN_SQLITE_VERSION
    assert check_sqlite_support() is None


@pytest.mark.parametrize("version", [(3, 44, 2), (3, 31, 1), (2, 99, 99)])
def test_old_sqlite_is_rejected(version: tuple[int, int, int]) -> None:
    message = check_sqlite_support(version_info=version)
    assert message is not None
    assert "3.45.0 or newer" in message
    assert ".".join(map(str, version)) in message
    assert "uv python install" in message


@pytest.mark.parametrize("version", [(3, 45, 0), (3, 49, 1), (4, 0, 0)])
def test_new_enough_versions_pass(version: tuple[int, int, int]) -> None:
    assert check_sqlite_support(version_info=version) is None


class _NoFts5Connection:
    """A connection whose SQLite was compiled without FTS5."""

    def execute(self, sql: str) -> None:
        raise sqlite3.OperationalError("no such module: fts5")

    def close(self) -> None:
        pass


def test_missing_fts5_is_rejected() -> None:
    message = check_sqlite_support(
        version_info=(3, 49, 1),
        connect=lambda _: _NoFts5Connection(),  # type: ignore[arg-type,return-value]
    )
    assert message is not None
    assert "FTS5" in message
    assert "no such module: fts5" in message
