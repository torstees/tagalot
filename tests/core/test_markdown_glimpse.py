"""A glimpse of an item's own Markdown file (#554, DESIGN.md §12): its table of contents,
else its opening paragraphs; never the whole file."""

import pytest

from tagalot.core.markdown_glimpse import MAX_GLIMPSE, glimpse


def test_the_opening_two_paragraphs() -> None:
    body = "# The Glass Tower\n\nFirst paragraph.\n\nSecond, over\ntwo lines.\n\nThird.\n"
    assert glimpse(body, "The Glass Tower") == "First paragraph.\n\nSecond, over\ntwo lines."


def test_headings_and_pictures_on_the_way_are_kept() -> None:
    opening = (
        "Written on a train.\n\n"
        "# The Lighthouse Keeper\n\n"
        "![Light](light.png)\n\n"
        "It was a dark night."
    )
    assert glimpse(opening + "\n\nMore.\n", "A Short Story") == opening


def test_a_list_counts_as_a_paragraph() -> None:
    body = "- one\n- two\n\nAfter the list.\n\nNot shown.\n"
    assert glimpse(body) == "- one\n- two\n\nAfter the list."


def test_a_code_block_is_kept_but_not_counted() -> None:
    body = "Intro.\n\n```\n# not a heading\n\nstill code\n```\n\nSecond.\n\nThird.\n"
    assert glimpse(body) == "Intro.\n\n```\n# not a heading\n\nstill code\n```\n\nSecond."


def test_a_long_opening_is_cut_at_a_word() -> None:
    word = "lighthouse "
    shown = glimpse(word * 400)
    assert len(shown) <= MAX_GLIMPSE + 1
    assert shown.endswith("lighthouse…")


def test_a_listed_table_of_contents() -> None:
    contents = (
        "## Contents\n\n"
        "- [Chapter 1](#chapter-1)\n"
        "- [Chapter 2](#chapter-2)\n"
        "  - [A scene](#a-scene)"
    )
    body = f"A novel in two parts.\n\n{contents}\n\n# Chapter 1\n\nThe tower caught the light.\n"
    assert glimpse(body) == contents


def test_a_listed_table_of_contents_without_its_own_heading() -> None:
    body = "1. [Start](#start)\n2. [End](#end)\n\n# Start\n\nText.\n"
    assert glimpse(body) == "1. [Start](#start)\n2. [End](#end)"


@pytest.mark.parametrize(
    "body",
    [
        "- [Only one](#one)\n\nText.\n",  # one link isn't a table of contents
        "- [Web](https://example.com)\n- [Other](https://example.org)\n\nText.\n",  # elsewhere
        "```\n- [A](#a)\n- [B](#b)\n```\n\nText.\n",  # in code
    ],
)
def test_not_a_table_of_contents(body: str) -> None:
    """Shown as the opening instead: the whole of these short files."""
    assert glimpse(body) == body.strip("\n")


@pytest.mark.parametrize("marker", ["[TOC]", "[[_TOC_]]", "{:toc}", "<!-- toc -->", "[toc]"])
def test_a_marker_gives_an_outline_of_the_headings(marker: str) -> None:
    body = (
        f"{marker}\n\n"
        "# Part one\n\nText.\n\n## A chapter\n\n#### Too deep\n\n"
        "```\n# not a heading\n```\n\n# Part two\n"
    )
    assert glimpse(body) == "- Part one\n  - A chapter\n- Part two"


def test_a_marker_without_headings_gives_the_opening() -> None:
    assert glimpse("[TOC]\n\nJust text.\n") == "[TOC]\n\nJust text."


def test_an_empty_file() -> None:
    assert glimpse("") == ""
    assert glimpse("# Title only\n", "Title only") == ""
