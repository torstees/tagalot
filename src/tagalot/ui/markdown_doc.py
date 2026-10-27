"""Markdown as an item's page shows it (DESIGN.md §12, #553).

Qt reads the Markdown (GitHub's flavour: tables, task lists, strikethrough, autolinks) into
a document; :func:`render` then styles it from the palette, so it suits light and dark
colors alike: tables with borders, headings set off, code shaded, quotes dimmed, links in
the link color. :func:`collapsed` makes the short form shown before **Show more**: the
main text only, its paragraphs and lists with their bold, italics, and links, without the
bulkier parts (headings, pictures, tables, code blocks, horizontal rules), counted in a
:class:`Hidden`. Both come from one parse, so they never disagree. HTML in the Markdown is
read as Qt reads it: its pictures and tables are hidden like Markdown's.

Nothing here loads a picture: the document asks its parent (the page's browser), which has
only those read in a worker. :func:`fit_pictures` scales them down to the width shown.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import unquote

from PySide6.QtCore import QObject
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontDatabase,
    QImage,
    QPalette,
    QTextBlock,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextFrame,
    QTextFrameFormat,
    QTextTable,
)

HEADING_SPACE = 10
"""Space above a heading, in pixels."""
QUOTE_INDENT = 16
"""A quote's indent per level, in pixels."""
_DROP = QTextFormat.Property.UserProperty.value + 1
"""Marks a block :func:`collapsed` takes out."""


@dataclass(frozen=True)
class Hidden:
    """What :func:`collapsed` left out, counted."""

    headings: int = 0
    pictures: int = 0
    tables: int = 0
    code: int = 0
    rules: int = 0
    """Horizontal rules: left out, but not worth a **Show more**."""

    def __bool__(self) -> bool:
        return bool(self.headings or self.pictures or self.tables or self.code)

    def summary(self) -> str:
        """What there is, for a note with nothing else: "A table and 2 pictures"."""
        parts = [
            _count(n, word)
            for n, word in (
                (self.tables, "table"),
                (self.pictures, "picture"),
                (self.code, "code block"),
                (self.headings, "heading"),
            )
            if n
        ]
        if not parts:
            return ""
        text = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
        return text[0].upper() + text[1:]


def _count(n: int, word: str) -> str:
    return f"a {word}" if n == 1 else f"{n} {word}s"


def shade(palette: QPalette, amount: float = 0.08) -> QColor:
    """The window's color moved ``amount`` of the way to the text's: code's background."""
    back, text = palette.color(QPalette.ColorRole.Window), palette.color(QPalette.ColorRole.Text)
    return QColor.fromRgbF(
        back.redF() + (text.redF() - back.redF()) * amount,
        back.greenF() + (text.greenF() - back.greenF()) * amount,
        back.blueF() + (text.blueF() - back.blueF()) * amount,
    )


def render(markdown: str, palette: QPalette, parent: QObject | None = None) -> QTextDocument:
    """``markdown`` as a document, styled from ``palette``. ``parent`` loads its pictures
    (a ``QTextBrowser``'s ``loadResource``)."""
    document = QTextDocument(parent)
    document.setDocumentMargin(0)
    document.setMarkdown(markdown)
    _style(document, palette)
    return document


def collapsed(document: QTextDocument, palette: QPalette) -> tuple[QTextDocument, Hidden]:
    """A copy of ``document`` with only its main text, and what was left out. When nothing
    is left, the copy says what there is (:meth:`Hidden.summary`), dimmed."""
    short = document.clone(document.parent() or document)  # lives as long as the full one
    tables = _drop_tables(short)
    pictures = _drop_pictures(short)
    headings = code = rules = 0
    in_code = False
    cursor = QTextCursor(short)
    for block in _blocks(short):  # marked first: dropping one moves the others
        form = block.blockFormat()
        is_code = form.hasProperty(QTextFormat.Property.BlockCodeLanguage)
        if form.headingLevel() > 0:
            headings += 1
        elif is_code:
            code += not in_code  # a code block's lines are blocks of their own
        elif form.hasProperty(QTextFormat.Property.BlockTrailingHorizontalRulerWidth):
            rules += 1
        elif block.text().strip():
            in_code = False
            continue
        in_code = is_code
        form.setProperty(_DROP, True)
        cursor.setPosition(block.position())
        cursor.setBlockFormat(form)
    while (marked := next((b for b in _blocks(short) if _marked(b)), None)) is not None:
        _drop_block(marked)
    hidden = Hidden(headings, pictures, tables, code, rules)
    if not short.toPlainText().strip() and hidden:
        short.setPlainText(hidden.summary())
        cursor = QTextCursor(short)
        cursor.select(QTextCursor.SelectionType.Document)
        dim = QTextCharFormat()
        dim.setFontItalic(True)
        dim.setForeground(palette.color(QPalette.ColorRole.PlaceholderText))
        cursor.mergeCharFormat(dim)
    return short, hidden


def fit_pictures(document: QTextDocument, width: int, pictures: Mapping[str, QImage]) -> None:
    """Show each picture of ``pictures`` (by the name written, or that name unquoted) at
    its own size, or scaled down to ``width``."""
    for block in _blocks(document):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            form = fragment.charFormat()
            if not form.isImageFormat():
                continue
            image = form.toImageFormat()
            picture = pictures.get(image.name()) or pictures.get(unquote(image.name()))
            if picture is None or picture.width() <= 0:
                continue
            shown = min(picture.width(), max(width, 1))
            if image.width() == shown:
                continue
            image.setWidth(shown)
            image.setHeight(picture.height() * shown / picture.width())
            cursor = QTextCursor(document)
            cursor.setPosition(fragment.position())
            cursor.setPosition(
                fragment.position() + fragment.length(), QTextCursor.MoveMode.KeepAnchor
            )
            cursor.setCharFormat(image)


# --- styling ---


def _style(document: QTextDocument, palette: QPalette) -> None:
    code_back = QBrush(shade(palette))
    link = palette.color(QPalette.ColorRole.Link)
    dimmed = palette.color(QPalette.ColorRole.PlaceholderText)
    cursor = QTextCursor(document)
    # The text's own color, not left to the widget showing it: a widget with a style
    # sheet can keep the old colors when the system's change.
    _merge(
        document,
        0,
        document.characterCount() - 1,
        foreground=palette.color(QPalette.ColorRole.Text),
    )
    for block in _blocks(document):
        form = block.blockFormat()
        cursor.setPosition(block.position())
        if form.headingLevel() > 0:
            form.setTopMargin(HEADING_SPACE)
            form.setBottomMargin(HEADING_SPACE / 2)
            cursor.setBlockFormat(form)
        elif form.hasProperty(QTextFormat.Property.BlockCodeLanguage):
            form.setBackground(code_back)
            form.setLeftMargin(4)
            cursor.setBlockFormat(form)
            _merge(document, block.position(), block.length() - 1, fixed=True)
            continue
        elif (level := form.intProperty(QTextFormat.Property.BlockQuoteLevel)) > 0:
            form.setLeftMargin(QUOTE_INDENT * level)
            form.setRightMargin(0)
            cursor.setBlockFormat(form)
            _merge(document, block.position(), block.length() - 1, foreground=dimmed)
            continue
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            char = fragment.charFormat()
            if char.isAnchor() and char.anchorHref():
                _merge(document, fragment.position(), fragment.length(), foreground=link)
            elif char.fontFixedPitch():
                _merge(
                    document,
                    fragment.position(),
                    fragment.length(),
                    background=code_back,
                    fixed=True,
                )
    for table in _tables(document.rootFrame()):
        _style_table(table, palette)


def _merge(
    document: QTextDocument,
    position: int,
    length: int,
    foreground: QColor | None = None,
    background: QBrush | None = None,
    fixed: bool = False,
) -> None:
    """Merge colors, or the system's fixed-width font (``fixed``: code), into a span."""
    form = QTextCharFormat()
    if fixed:
        form.setFontFamilies(
            [QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()]
        )
    if foreground is not None:
        form.setForeground(foreground)
    if background is not None:
        form.setBackground(background)
    cursor = QTextCursor(document)
    cursor.setPosition(position)
    cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
    cursor.mergeCharFormat(form)


def _style_table(table: QTextTable, palette: QPalette) -> None:
    form = table.format()
    form.setBorder(1)
    form.setBorderStyle(QTextFrameFormat.BorderStyle.BorderStyle_Solid)
    form.setBorderBrush(palette.color(QPalette.ColorRole.Mid))
    form.setBorderCollapse(True)
    form.setCellPadding(4)
    form.setCellSpacing(0)
    table.setFormat(form)
    if form.headerRowCount() < 1:
        return
    head = QTextCharFormat()
    head.setFontWeight(QFont.Weight.Bold)
    for column in range(table.columns()):
        cell = table.cellAt(0, column)
        cell_form = cell.format()
        cell_form.setBackground(QBrush(shade(palette, 0.05)))
        cell.setFormat(cell_form)
        cursor = cell.firstCursorPosition()
        cursor.setPosition(cell.lastPosition(), QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(head)


# --- taking parts out ---


def _blocks(document: QTextDocument) -> list[QTextBlock]:
    blocks = []
    block = document.begin()
    while block.isValid():
        blocks.append(block)
        block = block.next()
    return blocks


def _tables(frame: QTextFrame) -> list[QTextTable]:
    found = []
    for child in frame.childFrames():
        if isinstance(child, QTextTable):
            found.append(child)
        else:
            found.extend(_tables(child))
    return found


def _drop_tables(document: QTextDocument) -> int:
    tables = _tables(document.rootFrame())
    for table in reversed(tables):
        table.removeRows(0, table.rows())
    return len(tables)


def _drop_pictures(document: QTextDocument) -> int:
    """Take out the pictures, and a space beside each where there were two."""
    spans: list[tuple[int, int]] = []
    for block in _blocks(document):
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            it += 1
            if fragment.charFormat().isImageFormat():
                spans.append((fragment.position(), fragment.length()))
    cursor = QTextCursor(document)
    for position, length in reversed(spans):
        start, end = position, position + length
        before = document.characterAt(start - 1) if start > 0 else ""
        if before == " " and document.characterAt(end) in (" ", ".", ",", ""):
            start -= 1
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
    return len(spans)


def _marked(block: QTextBlock) -> bool:
    return block.blockFormat().hasProperty(_DROP)


def _drop_block(block: QTextBlock) -> None:
    """Take ``block`` out with its paragraph break; the blocks around it keep their format
    (and a list item its list)."""
    document = block.document()
    cursor = QTextCursor(document)
    before, after = block.previous(), block.next()
    if before.isValid():
        cursor.setPosition(before.position() + before.length() - 1)
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        return
    if not after.isValid():  # the only block: empty it
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.removeSelectedText()
        cursor.setBlockFormat(QTextBlockFormat())
        return
    form, text_list = after.blockFormat(), after.textList()
    cursor.setPosition(block.position())
    cursor.setPosition(after.position(), QTextCursor.MoveMode.KeepAnchor)
    cursor.removeSelectedText()
    cursor.setBlockFormat(form)
    if text_list is not None and cursor.block().textList() is None:
        text_list.add(cursor.block())
