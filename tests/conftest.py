"""Shared pytest configuration."""

import os

# GUI tests must run headless. This runs before pytest-qt creates the QApplication.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
