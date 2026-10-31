"""Type to find in the results (#633, DESIGN.md §12): typing goes to the next item whose
title starts with what was typed, as Qt's own keyboard search does, but over every
result's title (rows not loaded yet included) and with a longer pause between keys.

```
typed  B        Bo       Bob
       ▸ Babel  ▸ Bonk   ▸ Bobcat
```

- Keys less than :data:`TYPING_PAUSE_MS` apart add to the text; a longer pause starts again.
- A new search starts after the current item, and wraps around; more letters keep the
  current item if it still matches.
- One letter repeated (``bbb``) steps through the items starting with it.
- Matching ignores case.
"""

from collections.abc import Callable

import shiboken6
from PySide6.QtCore import QElapsedTimer, QModelIndex
from PySide6.QtWidgets import QAbstractItemView

from tagalot.ui.models.results import ResultsModel

TYPING_PAUSE_MS = 1000
"""How long a pause between keys starts a new search. Qt's 400 ms is shorter than
ordinary typing; the app sets this for every list and tree too (``app.run``)."""


class TypeAhead:
    """Type to find for one view. ``source`` gives the view's :class:`ResultsModel` (or
    ``None``, when the view shows something else, which leaves the search to Qt);
    ``index`` turns a result row into the view's index; ``row`` turns the view's current
    index into its result row (-1 for none)."""

    def __init__(
        self,
        view: QAbstractItemView,
        source: Callable[[], ResultsModel | None],
        index: Callable[[int, QModelIndex], QModelIndex],
        row: Callable[[QModelIndex], int],
    ) -> None:
        self.view = view
        self.source = source
        self.index = index
        self.row = row
        self.typed = ""
        self._timer = QElapsedTimer()
        self._from_next = False
        self._model: ResultsModel | None = None
        self._waiting = False
        """Typed while the titles load: the search runs when they arrive."""

    def search(self, text: str) -> bool:
        """Add ``text`` (one key's) to the search and go to the first match. False when the
        view doesn't show a :class:`ResultsModel` (the caller lets Qt search)."""
        model = self.source()
        if model is None:
            return False
        self._follow(model)
        if not text:
            self.reset()
            return True
        current = self.view.currentIndex()
        if not self._timer.isValid() or self._timer.restart() > TYPING_PAUSE_MS:
            self._timer.start()
            self.typed = text
            self._from_next = current.isValid()
        else:
            self.typed += text
            # While the titles load nothing has moved: keep the first key's start.
            self._from_next = self._waiting and self._from_next
        if len(self.typed) > 1 and len(set(self.typed.casefold())) == 1:
            self._from_next = True  # bbb: the next item starting with b
        self._find(model)
        return True

    def reset(self) -> None:
        """Forget what was typed (a new search's results)."""
        self.typed = ""
        self._timer.invalidate()
        self._waiting = False

    def _follow(self, model: ResultsModel) -> None:
        """Listen to ``model``: another search forgets what was typed."""
        if model is self._model:
            return
        old, self._model = self._model, model
        if old is not None and shiboken6.isValid(old):
            old.modelReset.disconnect(self.reset)
            old.titles_loaded.disconnect(self._titles_loaded)
        self.reset()
        model.modelReset.connect(self.reset)
        model.titles_loaded.connect(self._titles_loaded)

    def _find(self, model: ResultsModel) -> None:
        titles = model.titles()
        self._waiting = titles is None
        if titles is None:
            return
        typed = self.typed.casefold()
        prefix = typed[0] if len(set(typed)) == 1 else typed
        total = min(len(titles), model.rowCount())
        if not total:
            return
        current = self.view.currentIndex()
        start = self.row(current) if current.isValid() else -1
        start = start + 1 if self._from_next or start < 0 else start
        for step in range(total):
            found = (start + step) % total
            if titles[found].casefold().startswith(prefix):
                target = self.index(found, current)
                if target.isValid() and target != current:
                    self.view.setCurrentIndex(target)
                return

    def _titles_loaded(self) -> None:
        if self._waiting and self._model is not None and self._model is self.source():
            self._find(self._model)
