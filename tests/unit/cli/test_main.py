"""Smoke tests for the castiron CLI entrypoint.

The first three moved verbatim from ``tests/unit/test_cli.py`` when ``cli.py`` became the
``cli/`` package; they are the guard that ``castiron.cli:cli`` -- the console entrypoint
declared in ``pyproject.toml`` -- still resolves after the move.
"""

import pytest
from click.testing import CliRunner

from castiron import __version__
from castiron.cli import cli


@pytest.mark.unit
def test_cli_version_reports_package_version() -> None:
    result = CliRunner().invoke(cli, ['--version'])
    assert result.exit_code == 0
    assert __version__ in result.output


@pytest.mark.unit
def test_cli_help_lists_the_group() -> None:
    result = CliRunner().invoke(cli, ['--help'])
    assert result.exit_code == 0
    assert 'schema' in result.output.lower()


@pytest.mark.unit
def test_cli_no_args_shows_usage() -> None:
    result = CliRunner().invoke(cli, [])
    # A click group with no subcommand prints usage (exit code 2 by convention).
    assert 'Usage' in result.output


@pytest.mark.unit
def test_short_version_flag_works() -> None:
    result = CliRunner().invoke(cli, ['-V'])
    assert result.exit_code == 0
    assert result.output.strip() == f'castiron {__version__}'


@pytest.mark.unit
def test_the_console_entrypoint_target_still_resolves() -> None:
    # pyproject.toml declares `castiron = "castiron.cli:cli"`; the package move must not break it.
    module = __import__('castiron.cli', fromlist=['cli'])
    assert callable(module.cli)


@pytest.mark.unit
def test_help_advertises_gen_and_check_and_nothing_else() -> None:
    result = CliRunner().invoke(cli, ['--help'])
    # CI6-D12 said `check` must not be advertised while it was reserved -- "a subcommand that says
    # 'not implemented' is a promise broken in the user's face". CI-021b made it real, so the
    # decision's SPIRIT is what survives: `--help` advertises exactly the commands that work.
    assert set(cli.commands) == {'gen', 'check'}
    for name in ('gen', 'check'):
        assert name in result.output
