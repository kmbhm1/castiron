"""Smoke tests for the castiron CLI entrypoint."""

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
