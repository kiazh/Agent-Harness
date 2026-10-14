"""Shared logger identity for the provider package.

Every module logs under the historical ``machina.core.provider`` name so log
filters (tests/test_e1_prompt_cache.py, tests/test_fm2_provider.py) and user
logging configuration keep working after the package split.
"""

import logging

logger = logging.getLogger("machina.core.provider")
