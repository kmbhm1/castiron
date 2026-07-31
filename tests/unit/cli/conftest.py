"""Fixtures for the CLI suite: an isolated project, a clean environment, clean logging.

Three pollution traps this closes:

1. **Config discovery walks up from the cwd**, so an un-chdir'd test would find castiron's
   own ``pyproject.toml``. Every test that touches discovery runs inside ``tmp_path``.
2. **The env-var fallbacks are ambient.** A developer with ``SUPABASE_URL`` exported would
   otherwise see different results than CI.
3. **``configure_logging`` binds a handler to the runner's captured stderr**, which is
   closed when the invocation ends. Left in place it would break every later test that logs.
"""

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from castiron.utils.logging import LOGGER_NAME

#: Every environment variable the CLI reads, cleared before each test.
CLI_ENV_VARS = ('CASTIRON_CONFIG', 'CASTIRON_FROM', 'CASTIRON_KEY', 'SUPABASE_URL', 'SUPABASE_KEY')


@pytest.fixture(autouse=True)
def clean_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def restore_castiron_logging() -> Iterator[None]:
    logger = logging.getLogger(LOGGER_NAME)
    handlers = list(logger.handlers)
    level, propagate = logger.level, logger.propagate
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    for handler in handlers:
        logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = propagate


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, openapi_fixture_path: Path) -> Path:
    """An empty project directory, made the cwd, with the OpenAPI fixture as ``openapi.json``."""
    shutil.copy(openapi_fixture_path, tmp_path / 'openapi.json')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_config(directory: Path, body: str, name: str = 'pyproject.toml') -> Path:
    """Write a TOML config file into ``directory`` and return its path."""
    path = directory / name
    path.write_text(body, encoding='utf-8')
    return path
