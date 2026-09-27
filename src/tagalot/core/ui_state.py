"""``ui_state.json`` in the keep folder: view toggles and layout (DESIGN.md §4).

It is a convenience, not data: a missing or unreadable file means defaults, and a problem is
logged rather than raised. Writes are atomic.
"""

import json
import logging
from pathlib import Path
from typing import Any

from tagalot.core.tomlio import write_atomic

logger = logging.getLogger(__name__)


def load_ui_state(path: Path) -> dict[str, Any]:
    """The saved UI state, or ``{}`` if there is none or it can't be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable UI state %s: %s", path, e)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring UI state %s: not a JSON object", path)
        return {}
    return data


def save_ui_state(path: Path, state: dict[str, Any]) -> None:
    """Write the UI state atomically; failures are logged, never raised."""
    try:
        write_atomic(path, json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))
    except OSError as e:
        logger.warning("Could not save UI state %s: %s", path, e)
