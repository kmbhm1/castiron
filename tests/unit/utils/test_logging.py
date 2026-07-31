"""castiron's stdlib logging configuration (CI6-D11: stdlib, not loguru)."""

import io
import logging
import sys
from collections.abc import Iterator

import pytest

from castiron.utils.logging import DEBUG_FORMAT, DEFAULT_FORMAT, LOGGER_NAME, RedactingFilter, configure_logging

#: A URL shaped like the one `normalize_postgrest_url` keeps the query string of.
LEAKY_URL = 'https://x.supabase.co/rest/v1/?apikey=SUPERSECRET'


def _redact(text: str) -> str:
    """Stand in for :func:`castiron.cli.errors.redact` without importing the CLI."""
    return text.replace('SUPERSECRET', '***')


@pytest.fixture(autouse=True)
def restore_castiron_logging() -> Iterator[None]:
    logger = logging.getLogger(LOGGER_NAME)
    handlers, level, propagate = list(logger.handlers), logger.level, logger.propagate
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for handler in handlers:
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate


@pytest.mark.unit
class TestConfigureLogging:
    @pytest.mark.parametrize(
        ('kwargs', 'expected'),
        [
            ({}, logging.WARNING),
            ({'verbose': 0}, logging.WARNING),
            ({'verbose': 1}, logging.INFO),
            ({'verbose': 2}, logging.DEBUG),
            ({'verbose': 5}, logging.DEBUG),
            ({'debug': True}, logging.DEBUG),
        ],
    )
    def test_the_level_follows_the_flags(self, kwargs: dict[str, object], expected: int) -> None:
        configure_logging(**kwargs)  # type: ignore[arg-type] - the table is intentionally heterogeneous
        assert logging.getLogger(LOGGER_NAME).level == expected

    def test_the_format_switches_at_debug(self) -> None:
        configure_logging(verbose=1)
        info_handler = logging.getLogger(LOGGER_NAME).handlers[0]
        configure_logging(debug=True)
        debug_handler = logging.getLogger(LOGGER_NAME).handlers[0]
        assert info_handler.formatter is not None
        assert debug_handler.formatter is not None
        assert info_handler.formatter._fmt == DEFAULT_FORMAT
        assert debug_handler.formatter._fmt == DEBUG_FORMAT

    def test_it_is_idempotent(self) -> None:
        # A repeated CliRunner invocation must not stack handlers, or every log line doubles.
        for _ in range(4):
            configure_logging(verbose=1)
        assert len(logging.getLogger(LOGGER_NAME).handlers) == 1

    def test_it_never_touches_the_root_logger(self) -> None:
        # castiron is importable as a library; hijacking root logging in a consuming
        # application is bad manners.
        before = list(logging.getLogger().handlers)
        configure_logging(debug=True)
        assert logging.getLogger().handlers == before

    def test_it_stops_propagation_so_the_prefix_is_not_duplicated(self) -> None:
        configure_logging()
        assert logging.getLogger(LOGGER_NAME).propagate is False


@pytest.mark.unit
class TestRedactor:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
        stream = io.StringIO()
        monkeypatch.setattr(sys, 'stderr', stream)
        return stream

    @pytest.mark.parametrize('child', ['castiron.sources.openapi.fetch', 'castiron.cli.gen', 'castiron'])
    def test_it_redacts_records_from_every_castiron_logger(self, monkeypatch: pytest.MonkeyPatch, child: str) -> None:
        # The load-bearing case is a CHILD logger: `Logger.handle` applies only the
        # originating logger's filters, then `callHandlers` walks the ancestors applying each
        # HANDLER's filters. A filter on the `castiron` logger would therefore never see
        # `castiron.sources.openapi.fetch`'s DEBUG line -- which is the one that leaks.
        stream = self._capture(monkeypatch)
        configure_logging(debug=True, redactor=lambda text: text.replace('SUPERSECRET', '***'))
        logging.getLogger(child).debug('target %s', 'https://x.supabase.co/rest/v1/?apikey=SUPERSECRET')
        assert 'SUPERSECRET' not in stream.getvalue()
        assert '***' in stream.getvalue()

    def test_it_applies_at_every_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = self._capture(monkeypatch)
        configure_logging(debug=True, redactor=lambda text: text.replace('SUPERSECRET', '***'))
        child = logging.getLogger('castiron.sources.openapi.fetch')
        child.info('info SUPERSECRET')
        child.warning('warning SUPERSECRET')
        child.error('error SUPERSECRET')
        assert 'SUPERSECRET' not in stream.getvalue()
        assert stream.getvalue().count('***') == 3

    def test_lazy_percent_args_are_interpolated_before_masking(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The leak arrives as `logger.debug('... %s', url)`, so masking record.msg alone would
        # miss it; the filter must interpolate first and then clear args.
        stream = self._capture(monkeypatch)
        configure_logging(debug=True, redactor=lambda text: text.replace('SUPERSECRET', '***'))
        logging.getLogger('castiron.cli.gen').debug('%s and %s', 'SUPERSECRET', 'plain')
        assert stream.getvalue().strip().endswith('*** and plain')

    def test_without_a_redactor_the_message_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = self._capture(monkeypatch)
        configure_logging(debug=True)
        logging.getLogger('castiron.cli.gen').debug('SUPERSECRET')
        assert 'SUPERSECRET' in stream.getvalue()

    def test_reconfiguring_does_not_stack_redactors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = self._capture(monkeypatch)
        for _ in range(3):
            configure_logging(debug=True, redactor=lambda text: text.replace('a', 'b'))
        logging.getLogger('castiron.cli.gen').debug('aaa')
        assert stream.getvalue().strip().endswith('bbb')


# ⚠ The message is not the only thing a handler prints. `logging.Formatter.format` appends
# `record.exc_text` (the exc_info traceback) and `record.stack_info` to it, and a
# `logger.exception(...)` logs at **ERROR** -- above the default WARNING threshold, so no
# `--debug` is needed to see it. castiron writes no `logger.exception` today; the first
# source adapter that does must not reopen the leak CI-006 closed.
@pytest.mark.unit
class TestRedactingExceptionText:
    @staticmethod
    def _capture(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
        stream = io.StringIO()
        monkeypatch.setattr(sys, 'stderr', stream)
        return stream

    @staticmethod
    def _log_an_exception(logger_name: str = 'castiron.sources.openapi.fetch') -> None:
        try:
            raise RuntimeError(f'GET {LEAKY_URL} failed')
        except RuntimeError:
            logging.getLogger(logger_name).exception('the source call failed')

    def test_the_traceback_is_redacted_at_default_verbosity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Default verbosity: no -v, no --debug. logger.exception is ERROR, so it prints anyway.
        stream = self._capture(monkeypatch)
        configure_logging(redactor=_redact)
        self._log_an_exception()
        printed = stream.getvalue()
        # Not vacuous: the traceback really was printed, and it really did carry the URL.
        assert 'Traceback (most recent call last)' in printed
        assert 'RuntimeError: GET https://x.supabase.co/rest/v1/?apikey=***' in printed
        assert 'SUPERSECRET' not in printed

    def test_it_is_redacted_at_every_verbosity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for kwargs in ({}, {'verbose': 1}, {'verbose': 2}, {'debug': True}):
            stream = self._capture(monkeypatch)
            configure_logging(redactor=_redact, **kwargs)  # type: ignore[arg-type] - the table is heterogeneous
            self._log_an_exception()
            assert 'SUPERSECRET' not in stream.getvalue()

    def test_an_already_formatted_exc_text_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A record can arrive with exc_text already cached (another handler formatted it
        # first), in which case exc_info is never re-rendered -- that path must mask too.
        record = logging.LogRecord('castiron.x', logging.ERROR, __file__, 1, 'boom', None, None)
        record.exc_text = f'Traceback (most recent call last):\nRuntimeError: {LEAKY_URL}'
        assert RedactingFilter(_redact).filter(record) is True
        assert record.exc_text is not None
        assert 'SUPERSECRET' not in record.exc_text

    def test_exc_info_is_cleared_so_nothing_downstream_can_re_render_it(self) -> None:
        try:
            raise RuntimeError(f'GET {LEAKY_URL} failed')
        except RuntimeError:
            record = logging.LogRecord('castiron.x', logging.ERROR, __file__, 1, 'boom', None, sys.exc_info())
        RedactingFilter(_redact).filter(record)
        # The live exception is the unredacted original; keeping it lets a formatter that
        # ignores exc_text print the secret anyway.
        assert record.exc_info is None
        assert record.exc_text is not None
        assert 'SUPERSECRET' not in record.exc_text

    def test_stack_info_is_redacted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # stack_info renders the *calling source lines* through linecache, so a literal in
        # the caller leaks even with no exception in play.
        stream = self._capture(monkeypatch)
        configure_logging(redactor=_redact)
        logging.getLogger('castiron.cli.gen').error('boom SUPERSECRET', stack_info=True)
        printed = stream.getvalue()
        assert 'Stack (most recent call last)' in printed
        assert 'SUPERSECRET' not in printed

    def test_a_record_without_an_exception_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stream = self._capture(monkeypatch)
        configure_logging(redactor=_redact)
        logging.getLogger('castiron.cli.gen').warning('plain')
        assert stream.getvalue() == 'castiron: plain\n'

    def test_without_a_redactor_the_traceback_is_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The library default: no filter, no masking -- proof the assertions above are the
        # filter's doing and not something logging does for free.
        stream = self._capture(monkeypatch)
        configure_logging()
        self._log_an_exception()
        assert 'SUPERSECRET' in stream.getvalue()
