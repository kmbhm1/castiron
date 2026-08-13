"""The ``castiron`` command group.

Two commands: ``gen`` writes, ``check`` compares and writes nothing. Nothing is advertised in
``--help`` before it works — a subcommand that prints "not implemented" is a promise broken
in the user's face, which is why ``check`` was absent here until CI-021b made it real.
"""

import click

from castiron import __version__
from castiron.cli.check import check
from castiron.cli.gen import gen


@click.group()
@click.version_option(__version__, '-V', '--version', prog_name='castiron', message='%(prog)s %(version)s')
def cli() -> None:
    """A schema→typed-code compiler for Python."""


cli.add_command(gen)
cli.add_command(check)
