"""castiron's stdlib logging configuration (CI6-D11: stdlib, not loguru)."""

import logging
from collections.abc import Iterator

import pytest

from castiron.utils.logging import DEBUG_FORMAT, DEFAULT_FORMAT, LOGGER_NAME, configure_logging


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
