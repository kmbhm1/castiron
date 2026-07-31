"""castiron's logging configuration — stdlib :mod:`logging`, configured only by the CLI.

Decision **CI6-D11**: stdlib, **not** ``loguru``. castiron is importable as a library, and a
library that installs handlers (let alone hijacks the *root* logger) is bad manners — so
every module does the plain ``logger = logging.getLogger(__name__)`` and nothing else, and
this one entry point attaches the single stderr handler when the ``castiron`` command runs.
loguru's real value (async sinks, rotation, structured records) is irrelevant to a one-shot
CLI that prints a two-line summary; keeping the configuration behind one function means a
later swap stays contained.

⚠ **Log records carry secrets too.** ``normalize_postgrest_url`` preserves the query string,
so a DEBUG line naming the fetch target leaks ``?apikey=...`` on ``-vv``/``--debug`` -- the
exact verbosity castiron's own internal-error message tells users to rerun with and paste
into an issue. :class:`RedactingFilter` closes that.

⚠ It is installed on the **handler**, not on the ``castiron`` logger, and that is
load-bearing rather than stylistic: ``Logger.handle`` applies only the *originating*
logger's filters, then ``callHandlers`` walks the ancestors applying each **handler's**
filters. Every castiron log line is emitted on a child logger (``castiron.sources.…``,
``castiron.cli.…``) and reaches this handler by propagation, so a filter on the ``castiron``
logger would never run for any of them -- verified before writing this.
"""

import logging
import sys
from collections.abc import Callable

#: The logger every castiron module hangs off (``castiron.*`` propagates into it).
LOGGER_NAME = 'castiron'

#: User-facing format: the same ``castiron: `` prefix the summary lines carry.
DEFAULT_FORMAT = 'castiron: %(message)s'

#: Debug format: enough provenance to locate the emitting module.
DEBUG_FORMAT = '%(levelname)s %(name)s: %(message)s'

#: Renders ``record.exc_info`` exactly as the handler's own formatter would, so redacting it
#: early changes only the secret and not the layout (``Formatter.formatException`` strips the
#: trailing newline; ``traceback.format_exception`` does not).
_EXCEPTION_RENDERER = logging.Formatter()


class RedactingFilter(logging.Filter):
    """Rewrite a record through a redactor before the handler formats it.

    Attached to castiron's stderr handler (see the module docstring for why a logger-level
    filter would not fire). ``record.args`` is cleared because the message is interpolated
    during redaction; re-interpolating it downstream would raise.

    ⚠ **The message is not the only thing a handler prints.**
    ``logging.Formatter.format`` concatenates the interpolated message with
    ``record.exc_text`` (the ``exc_info`` traceback) and ``record.stack_info``, so masking
    ``msg`` alone leaves a ``logger.exception('…')`` printing ``?apikey=…`` verbatim -- and at
    **ERROR**, which the default WARNING threshold shows without ``--debug``. castiron writes
    no ``logger.exception`` today; the first source adapter that does must not reopen the leak
    CI-006 closed, so all three are redacted here rather than in a later fix round.
    """

    def __init__(self, redactor: Callable[[str], str]) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact every string ``record`` can print, in place, and keep it.

        ``exc_info`` is rendered here and then cleared: leaving it set would let a formatter
        that ignores ``exc_text`` re-derive the unredacted traceback from the live exception.

        Args:
            record: The record about to be handled (mutated in place).

        Returns:
            Always ``True`` — this filter masks, it never drops.
        """
        record.msg = self._redactor(record.getMessage())
        record.args = ()
        if record.exc_info is not None and not record.exc_text:
            record.exc_text = _EXCEPTION_RENDERER.formatException(record.exc_info)
        record.exc_info = None
        if record.exc_text:
            record.exc_text = self._redactor(record.exc_text)
        if record.stack_info:
            record.stack_info = self._redactor(record.stack_info)
        return True


def configure_logging(
    *,
    verbose: int = 0,
    debug: bool = False,
    redactor: Callable[[str], str] | None = None,
) -> None:
    """Configure castiron's stderr logging from the CLI's verbosity flags.

    Idempotent: existing handlers on the ``castiron`` logger are removed first, so repeated
    invocations in one process (a ``CliRunner`` test suite, or a programmatic caller) never
    stack duplicates. The root logger is never touched.

    Args:
        verbose: The ``-v`` count: 0 = warnings only, 1 = info, 2+ = debug.
        debug: Force debug level (and, at the call site, full tracebacks).
        redactor: Applied to every string this handler can print -- the interpolated message,
            the ``exc_info``/``exc_text`` traceback, and ``stack_info`` (see
            :class:`RedactingFilter`) -- before the record is formatted. The CLI always passes
            :func:`castiron.cli.errors.redact` bound to the API key in play; the parameter
            keeps this module free of any dependency on the CLI's secret policy.
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
    if redactor is not None:
        handler.addFilter(RedactingFilter(redactor))
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
