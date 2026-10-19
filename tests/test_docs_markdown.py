"""The Markdown docs show their placeholders: a bare ``<name>`` outside code is read as an
HTML tag, which GitHub, docsify, and editors hide, so "Show in <name>" reads "Show in".
Write ``\\<name>``, or put it in backticks."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

HTML = {"a", "br", "h1", "img", "p"}
"""Tags the docs use as HTML (the README's centered header and badges)."""

TAG = re.compile(r"(?<!\\)</?([A-Za-z][^<>\s/]*)[^<>]*>")
CODE = re.compile(r"(`+).+?\1")
LINK = re.compile(r"<(?:https?|mailto):[^<>\s]*>")


def _markdown_files() -> list[Path]:
    files = [*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md")]
    return sorted(files + list((ROOT / "tests" / "fixtures").rglob("*.md")))


def bare_tags(text: str) -> list[tuple[int, str]]:
    """Each ``<word>`` outside code blocks and code spans that isn't HTML the docs use or a
    link: line number and the text."""
    found = []
    fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        prose = LINK.sub("", CODE.sub("", line))
        found += [(n, m.group(0)) for m in TAG.finditer(prose) if m.group(1).lower() not in HTML]
    return found


def test_bare_tags_are_found() -> None:
    text = "Show in <name>.\n`<code>` and \\<escaped> and <https://x.org>\n```\n<fenced>\n```\n"
    assert bare_tags(text) == [(1, "<name>")]
    assert bare_tags('<p align="center"><img src="logo.svg"><br></p>') == []
    assert bare_tags("Remove from <title> and <summary>") == [(1, "<title>"), (1, "<summary>")]


def test_the_docs_have_no_bare_placeholders() -> None:
    problems = [
        f"{path.relative_to(ROOT).as_posix()}:{n}: {tag}"
        for path in _markdown_files()
        for n, tag in bare_tags(path.read_text(encoding="utf-8"))
    ]
    assert problems == [], "write \\<name>, or put it in backticks:\n" + "\n".join(problems)
