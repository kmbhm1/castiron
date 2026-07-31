"""The ``castiron`` command group.

One command today (``gen``); CI-021 adds ``check`` next to it. Nothing is advertised in
``--help`` before it works — a subcommand that prints "not implemented" is a promise broken
in the user's face.
"""

import click

from castiron import __version__
from castiron.cli.gen import gen


@click.group()
@click.version_option(__version__, '-V', '--version', prog_name='castiron', message='%(prog)s %(version)s')
def cli() -> None:
    """A schema→typed-code compiler for Python."""


cli.add_command(gen)
