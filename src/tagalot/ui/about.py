"""Help → About Tagalot (#359): the tower, the version and the formats this build reads,
links to the documentation and the project, the license, and a way to copy what
``tagalot --check`` reports (for a bug report)."""

import platform

import shiboken6
from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import tagalot
from tagalot.core.keep import KEEP_FORMAT_VERSION
from tagalot.resources import tower_file
from tagalot.selfcheck import run_check
from tagalot.themes.api import API_VERSION
from tagalot.ui.workers import run_in_pool

REPOSITORY = "https://github.com/torstees/tagalot"
DOCUMENTATION = "https://torstees.github.io/tagalot/"
LINKS = (
    ("Documentation", DOCUMENTATION),
    ("Source code", REPOSITORY),
    ("Report a problem", f"{REPOSITORY}/issues"),
    ("Releases", f"{REPOSITORY}/releases"),
)
TOWER_SIZE = 128
"""The tower's size on screen (drawn from a 256 px picture, sharp on high-DPI screens)."""


def check_report() -> str:
    """What ``tagalot --check`` reports, as text. Runs in a worker (it loads the themes
    and tries each file reader)."""
    lines: list[str] = []
    run_check(lines.append)
    return "\n".join(lines)


class AboutDialog(QDialog):
    """The About box. **Copy check report** runs the check in a worker and puts it on the
    clipboard."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("About Tagalot")
        tower = QLabel()
        tower.setObjectName("tower")
        picture = QPixmap(str(tower_file()))
        if not picture.isNull():
            ratio = self.devicePixelRatioF()
            picture = picture.scaled(
                round(TOWER_SIZE * ratio),
                round(TOWER_SIZE * ratio),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            picture.setDevicePixelRatio(ratio)
            tower.setPixmap(picture)
        tower.setAlignment(Qt.AlignmentFlag.AlignTop)

        name = QLabel(f"<h2 style='margin: 0'>Tagalot {tagalot.__version__}</h2>")
        tagline = QLabel(
            "Tag, search, and browse your music, movies, books, papers, and art, "
            "without ever changing your files."
        )
        tagline.setWordWrap(True)
        self.versions = QLabel(
            f"Theme API {API_VERSION} · Keep format {KEEP_FORMAT_VERSION}<br>"
            f"Python {platform.python_version()} · Qt {qVersion()}"
        )
        self.versions.setObjectName("versions")
        self.versions.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.links = QLabel(" · ".join(f'<a href="{url}">{label}</a>' for label, url in LINKS))
        self.links.setObjectName("links")
        self.links.setOpenExternalLinks(True)
        license_line = QLabel(
            f'© 2026 Eric Torstenson · <a href="{REPOSITORY}/blob/main/LICENSE">MIT License</a>'
        )
        license_line.setOpenExternalLinks(True)

        text = QVBoxLayout()
        for widget in (name, tagline, self.versions, self.links, license_line):
            text.addWidget(widget)
        text.addStretch(1)
        top = QHBoxLayout()
        top.addWidget(tower)
        top.addSpacing(12)
        top.addLayout(text, 1)

        self.copy_button = QPushButton("Copy check report")
        self.copy_button.setToolTip(
            "What tagalot --check reports about this copy (themes, file readers), for a bug report"
        )
        self.copy_button.clicked.connect(self.copy_report)
        self.status = QLabel()
        self.status.setObjectName("status")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addWidget(self.copy_button)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addSpacing(8)
        layout.addLayout(bottom)
        self.setMinimumWidth(520)

    def copy_report(self) -> None:
        """Run ``tagalot --check`` in a worker and put its report on the clipboard."""
        self.copy_button.setEnabled(False)
        self.status.setText("Checking…")

        def done(report: str) -> None:
            if not shiboken6.isValid(self):
                return
            QApplication.clipboard().setText(report)
            self.copy_button.setEnabled(True)
            self.status.setText("Copied the check report.")

        def failed(error: BaseException) -> None:
            if shiboken6.isValid(self):
                self.copy_button.setEnabled(True)
                self.status.setText(f"The check failed: {error}")

        run_in_pool(check_report, on_done=done, on_error=failed)
