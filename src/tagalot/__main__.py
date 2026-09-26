"""Entry point for the ``tagalot`` console script and ``python -m tagalot``."""

import logging
import sys

logger = logging.getLogger(__name__)


def main() -> int:
    """Start Tagalot and return the process exit code."""
    logging.basicConfig(level=logging.INFO)
    logger.info("Tagalot starting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
