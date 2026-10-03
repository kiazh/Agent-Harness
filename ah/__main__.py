"""Entry point for `python -m ah`."""

import logging

from ah.cli import app

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    app()
