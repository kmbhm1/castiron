"""castiron's logging configuration — stdlib :mod:`logging`, configured only by the CLI.

Decision **CI6-D11**: stdlib, **not** ``loguru``. castiron is importable as a library, and a
library that installs handlers (let alone hijacks the *root* logger) is bad manners — so
every module does the plain ``logger = logging.getLogger(__name__)`` and nothing else, and
this one entry point attaches the single stderr handler when the ``castiron`` command runs.
loguru's real value (async sinks, rotation, structured records) is irrelevant to a one-shot
CLI that prints a two-line summary; keeping the configuration behind one function means a
later swap stays contained.
"""

import logging
import sys

#: The logger every castiron module hangs off (``castiron.*`` propagates into it).
LOGGER_NAME = 'castiron'

#: User-facing format: the same ``castiron: `` prefix the summary lines carry.
DEFAULT_FORMAT = 'castiron: %(message)s'

#: Debug format: enough provenance to locate the emitting module.
DEBUG_FORMAT = '%(levelname)s %(name)s: %(message)s'


def configure_logging(*, verbose: int = 0, debug: bool = False) -> None:
    """Configure castiron's stderr logging from the CLI's verbosity flags.

    Idempotent: existing handlers on the ``castiron`` logger are removed first, so repeated
    invocations in one process (a ``CliRunner`` test suite, or a programmatic caller) never
    stack duplicate handlers. The root logger is never touched.

    Args:
        verbose: The ``-v`` count: 0 = warnings only, 1 = info, 2+ = debug.
        debug: Force debug level (and, at the call site, full tracebacks).
    """
    if debug or verbose >= 2:
        level, fmt = logging.DEBUG, DEBUG_FORMAT
    elif verbose >= 1:
        level, fmt = logging.INFO, DEFAULT_FORMAT
    else:
        level, fmt = logging.WARNING, DEFAULT_FORMAT

    logger = logging.getLogger(LOGGER_NAME)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
