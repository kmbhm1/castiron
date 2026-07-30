"""Command-line entrypoint for castiron.

The ``castiron`` console command. Subcommands (``gen``, ``check``, ...) land as
the compiler pipeline is ported in — see the roadmap. For now this exposes the
group and ``--version`` so the package is installable and wired end to end.
"""

import click

from castiron import __version__


@click.group()
@click.version_option(version=__version__, prog_name='castiron')
def cli() -> None:
    """A schema→typed-code compiler for Python."""
