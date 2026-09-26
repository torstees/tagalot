"""Entry point for the ``tagalot`` console script and ``python -m tagalot``."""

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> int:
    """Start Tagalot and return the process exit code."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Tagalot starting")

    # Imported here so that importing this module does not load Qt.
    from tagalot.ui import app

    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
