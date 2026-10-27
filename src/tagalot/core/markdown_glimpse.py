"""A glimpse of an item's own Markdown file, for its page's header (DESIGN.md §12, #554).

Not a reader: the header shows only the file's table of contents, if it has one, or else
its opening paragraphs. The rest opens in the file's own program.

- **A table of contents** is either a list whose every item links to a heading of the file
  (``- [Chapter 1](#chapter-1)``, as editors write them), with the heading just above it
  when that names it (*Contents*, *Table of contents*, *TOC*); or a marker that Markdown
  tools expand into one (``[TOC]``, ``[[_TOC_]]``, ``{:toc}``, ``<!-- toc -->``), shown as an
  outline of the file's headings.
- **Otherwise, its opening:** everything up to the end of its second paragraph or list
  (headings and pictures on the way included), at most :data:`MAX_GLIMPSE` characters.

A first ``# heading`` that repeats the item's title is left out. Headings inside fenced
code blocks don't count. Nothing here touches the disk: the page's worker reads the file
(:func:`~tagalot.core.notes.read_note`).
"""

import re

from tagalot.core.notes import body_without_title

MAX_GLIMPSE = 1500
"""The opening is cut (at a word, with "…") past this many characters."""
OPENING_BLOCKS = 2
"""The opening ends with this many paragraphs or lists."""
OUTLINE_LEVELS = 3
"""An outline from a ``[TOC]`` marker lists headings this deep."""

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_TOC_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[[^\]]+\]\(#[^)\s]+\)\s*$")
_TOC_MARKER = re.compile(
    r"^\s*(?:\[toc\]|\[\[_?toc_?\]\]|\{:\s*toc\s*\}|<!--\s*toc\s*-->|\$\{toc\})\s*$",
    re.IGNORECASE,
)
_TOC_NAME = re.compile(r"\b(contents|toc)\b", re.IGNORECASE)
_PICTURE_ONLY = re.compile(r"^\s*(?:!\[[^\]]*\]\([^)]*\)|!\[\[[^\]]*\]\]|<img\b[^>]*>)\s*$")
_COMMENT = re.compile(r"^\s*<!--.*-->\s*$")


def glimpse(body: str, title: str | None = None) -> str:
    """What the page shows of a Markdown file's ``body`` (its front matter already taken
    off): its table of contents, else its opening; ``""`` for an empty file."""
    body = body_without_title(body.replace("\r\n", "\n").strip("\n"), title)
    lines = body.split("\n")
    outside = _outside_code(lines)
    return _listed_contents(lines, outside) or _outline(lines, outside) or _opening(lines)


def _outside_code(lines: list[str]) -> list[bool]:
    """For each line, whether it is outside a fenced code block (fences count as inside)."""
    outside, fence = [], ""
    for line in lines:
        match = _FENCE.match(line)
        if fence:
            outside.append(False)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = ""
        elif match:
            outside.append(False)
            fence = match.group(1)
        else:
            outside.append(True)
    return outside


def _listed_contents(lines: list[str], outside: list[bool]) -> str:
    """The first list (two items or more) whose every item links to a heading here, with
    the heading above it when that names it."""
    i = 0
    while i < len(lines):
        if not (outside[i] and _TOC_ITEM.match(lines[i])):
            i += 1
            continue
        end = i
        while end < len(lines) and outside[end] and _TOC_ITEM.match(lines[end]):
            end += 1
        if end - i >= 2:
            start = i
            above = i - 1
            while above >= 0 and not lines[above].strip():
                above -= 1
            heading = _HEADING.match(lines[above]) if above >= 0 else None
            if heading and _TOC_NAME.search(heading.group(2)):
                start = above
            return "\n".join(lines[start:end]).strip("\n")
        i = end
    return ""


def _outline(lines: list[str], outside: list[bool]) -> str:
    """For a file with a ``[TOC]`` marker: its headings, as a nested list."""
    if not any(ok and _TOC_MARKER.match(line) for ok, line in zip(outside, lines, strict=True)):
        return ""
    headings = [
        (len(m.group(1)), m.group(2))
        for ok, line in zip(outside, lines, strict=True)
        if ok and (m := _HEADING.match(line)) and m.group(2)
    ]
    headings = [(level, text) for level, text in headings if level <= OUTLINE_LEVELS]
    if not headings:
        return ""
    top = min(level for level, _ in headings)
    return "\n".join(f"{'  ' * (level - top)}- {text}" for level, text in headings)


def _opening(lines: list[str]) -> str:
    """Everything up to the end of the second paragraph or list, cut if long."""
    blocks: list[list[str]] = []
    current: list[str] = []
    fence = ""
    for line in lines:  # blocks: runs of lines between blank lines; a code block is one
        match = _FENCE.match(line)
        if fence:
            current.append(line)
            if match and match.group(1)[0] == fence[0]:
                fence = ""
            continue
        if match:
            fence = match.group(1)
            current.append(line)
        elif line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    kept: list[str] = []
    texts = 0
    for block in blocks:
        kept.append("\n".join(block))
        if _is_text(block):
            texts += 1
            if texts >= OPENING_BLOCKS:
                break
    return _cut("\n\n".join(kept))


def _is_text(block: list[str]) -> bool:
    """A paragraph or list: not a heading, a picture, a comment, or a code block."""
    first = block[0]
    if _FENCE.match(first) or (len(block) == 1 and _HEADING.match(first)):
        return False
    return not all(_PICTURE_ONLY.match(line) or _COMMENT.match(line) for line in block)


def _cut(text: str) -> str:
    if len(text) <= MAX_GLIMPSE:
        return text
    cut = (
        text[:MAX_GLIMPSE].rsplit(maxsplit=1)[0]
        if " " in text[:MAX_GLIMPSE]
        else text[:MAX_GLIMPSE]
    )
    return cut.rstrip(" ,;:-") + "…"
