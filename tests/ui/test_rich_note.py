"""A note rendered for its page (#553, DESIGN.md §12): collapsed, its main text only (no
headings, pictures, tables, or code blocks), with Show more whenever something is left
out; expanded, all of it, styled from the palette, at most half the window's height."""

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextTable,
)
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from pytestqt.qtbot import QtBot

from tagalot.ui.markdown_doc import (
    HEADING_SPACE,
    QUOTE_INDENT,
    Hidden,
    collapsed,
    fit_pictures,
    render,
    shade,
)
from tagalot.ui.note_view import NoteView

pytestmark = pytest.mark.gui

NOTE = """Painted skies and **dusk** light, *since* 2009; see [the prices](Prices/list.txt).
And ![a sketch](sketch.png) beside the text.

# Prices

| Size | Price |
|------|------:|
| Small | $10 |

1. First
2. Second

> Quoted.

```
line one
line two
```

---

![Wide](wide.png)

Last paragraph.
"""


def _palette() -> QPalette:
    return QApplication.palette()


def _blocks(document: QTextDocument) -> list[str]:
    found = []
    block = document.begin()
    while block.isValid():
        found.append(block.text())
        block = block.next()
    return found


def _block_at(document: QTextDocument, text: str) -> QTextCursor:
    cursor = QTextCursor(document)
    cursor.setPosition(document.toPlainText().index(text) + 1)
    return cursor


def _char(document: QTextDocument, text: str) -> QTextCharFormat:
    return _block_at(document, text).charFormat()


def _tables(document: QTextDocument) -> list[QTextTable]:
    return [f for f in document.rootFrame().childFrames() if isinstance(f, QTextTable)]


def test_collapsed_keeps_the_main_text(qtbot: QtBot) -> None:
    full = render(NOTE, _palette())
    short, hidden = collapsed(full, _palette())
    assert hidden == Hidden(headings=1, pictures=2, tables=1, code=1, rules=1)
    assert _blocks(short) == [
        "Painted skies and dusk light, since 2009; see the prices. And beside the text.",
        "First",
        "Second",
        "Quoted.",
        "Last paragraph.",
    ]
    assert not _tables(short)
    # The list is still a numbered list of two.
    text_list = _block_at(short, "First").block().textList()
    assert text_list is not None
    assert text_list.count() == 2
    # Inline formatting and links stay.
    assert _char(short, "dusk").fontWeight() == QFont.Weight.Bold
    assert _char(short, "since").fontItalic()
    assert _char(short, "the prices").anchorHref() == "Prices/list.txt"
    # The full document is untouched.
    assert "line two" in full.toPlainText()
    assert len(_tables(full)) == 1


def test_a_note_starting_with_a_heading_keeps_its_list(qtbot: QtBot) -> None:
    full = render("# About\n\n- one\n- two\n", _palette())
    short, hidden = collapsed(full, _palette())
    assert hidden == Hidden(headings=1)
    assert _blocks(short) == ["one", "two"]
    first = short.begin()
    assert first.textList() is not None
    assert first.blockFormat().headingLevel() == 0


def test_nothing_hidden(qtbot: QtBot) -> None:
    full = render("Just *words*.\n\n- and a list\n", _palette())
    short, hidden = collapsed(full, _palette())
    assert not hidden
    assert hidden.summary() == ""
    assert _blocks(short) == ["Just words.", "and a list"]


def test_a_note_with_only_bulky_parts_says_what_it_holds(qtbot: QtBot) -> None:
    full = render("| A |\n|---|\n| 1 |\n\n![x](x.png) ![y](y.png)\n", _palette())
    short, hidden = collapsed(full, _palette())
    assert hidden == Hidden(pictures=2, tables=1)
    assert short.toPlainText() == "A table and 2 pictures"
    assert _char(short, "table").fontItalic()


@pytest.mark.parametrize(
    ("hidden", "summary"),
    [
        (Hidden(tables=1), "A table"),
        (Hidden(pictures=3), "3 pictures"),
        (Hidden(tables=2, code=1), "2 tables and a code block"),
        (
            Hidden(headings=2, pictures=1, tables=1, code=1),
            "A table, a picture, a code block and 2 headings",
        ),
        (Hidden(rules=2), ""),
    ],
)
def test_the_summary(hidden: Hidden, summary: str) -> None:
    assert hidden.summary() == summary
    assert bool(hidden) is bool(summary)


def test_the_full_note_is_styled_from_the_palette(qtbot: QtBot) -> None:
    palette = QPalette(_palette())
    palette.setColor(QPalette.ColorRole.Link, QColor("#123456"))
    full = render(NOTE, palette)
    (table,) = _tables(full)
    form = table.format()
    assert form.border() == 1
    assert form.borderCollapse()
    assert table.cellAt(0, 0).firstCursorPosition().charFormat().fontWeight() == QFont.Weight.Bold
    assert _char(full, "the prices").foreground().color() == QColor("#123456")
    code = _block_at(full, "line one").blockFormat()
    assert code.hasProperty(QTextFormat.Property.BlockCodeLanguage)
    assert code.background().color() == shade(palette)
    heading = _block_at(full, "Prices").blockFormat()  # "the prices" is lower case
    assert heading.headingLevel() == 1
    assert heading.topMargin() == HEADING_SPACE
    quote = _block_at(full, "Quoted.").blockFormat()
    assert quote.leftMargin() == QUOTE_INDENT
    assert quote.rightMargin() == 0


def test_pictures_are_scaled_down_to_the_width(qtbot: QtBot) -> None:
    full = render("![Wide](wide.png) ![Small](<a sketch.png>)", _palette())
    pictures = {
        "wide.png": QImage(900, 300, QImage.Format.Format_RGB32),
        "a sketch.png": QImage(20, 10, QImage.Format.Format_RGB32),
    }
    fit_pictures(full, 300, pictures)
    sizes = {}
    it = full.begin().begin()
    while not it.atEnd():
        form = it.fragment().charFormat()
        if form.isImageFormat():
            image = form.toImageFormat()
            sizes[image.name()] = (image.width(), image.height())
        it += 1
    assert sizes == {"wide.png": (300, 100), "a sketch.png": (20, 10)}


# --- the note on the page ---


@pytest.fixture
def window(qtbot: QtBot) -> QWidget:
    window = QWidget()
    QVBoxLayout(window)
    qtbot.addWidget(window)
    window.resize(600, 800)
    return window


def _note(window: QWidget, text: str) -> NoteView:
    note = NoteView()
    layout = window.layout()
    assert isinstance(layout, QVBoxLayout)
    layout.addWidget(note)
    layout.addStretch(1)
    note.show_note(text, None, {}, has_file=False, editable=True)
    note.set_cap(80)
    window.show()
    return note


def test_a_short_note_with_a_table_has_show_more(qtbot: QtBot, window: QWidget) -> None:
    note = _note(window, "A line.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n")
    qtbot.waitUntil(note.more.isVisible, timeout=5000)
    assert note.body.toPlainText() == "A line."
    assert note.body.height() < note.cap
    note.more.click()
    assert note.more.text() == "Show less"
    assert note.body.document() is note.full
    assert len(_tables(note.body.document())) == 1
    note.more.click()
    assert note.body.document() is note.short


def test_a_short_plain_note_has_no_show_more(qtbot: QtBot, window: QWidget) -> None:
    note = _note(window, "Just a line, *nothing* more.")
    qtbot.waitUntil(note.body.isVisible, timeout=5000)
    assert not note.more.isVisible()
    assert note.body.document() is note.full


def test_expanded_at_most_half_the_window(qtbot: QtBot, window: QWidget) -> None:
    note = _note(window, "\n\n".join(f"Paragraph {n}." for n in range(1, 80)))
    qtbot.waitUntil(note.more.isVisible, timeout=5000)
    assert note.body.height() == note.cap
    note.more.click()
    assert note.body.height() == window.height() // 2
    assert note.body.verticalScrollBarPolicy().name == "ScrollBarAsNeeded"
    window.resize(600, 1000)
    qtbot.waitUntil(lambda: note.body.height() == 500, timeout=5000)
    note.more.click()
    assert note.body.height() == note.cap
    assert note.body.verticalScrollBarPolicy().name == "ScrollBarAlwaysOff"


def test_new_colors_style_the_note_again(qtbot: QtBot, window: QWidget) -> None:
    note = _note(window, "A [link](x.txt).\n\n# Heading\n")
    qtbot.waitUntil(note.body.isVisible, timeout=5000)
    palette = QPalette(note.palette())
    palette.setColor(QPalette.ColorRole.Link, QColor("#ab1234"))
    note.setPalette(palette)
    QApplication.sendEvent(note, QEvent(QEvent.Type.PaletteChange))
    assert _char(note.full, "link").foreground().color() == QColor("#ab1234")
