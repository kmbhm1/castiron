"""The option surface `gen` and `check` share, declared exactly once.

``castiron check`` takes **18 of ``gen``'s 20 options** — everything except ``--overwrite`` and
``--dry-run``, which are write-path only. Copying eighteen ``@click.option`` decorators into a
second module would create two surfaces that drift the first time one of them gains a flag, so the
stacks live here and both commands apply them.

⚠ **The order these stacks are applied in is a published contract.** ``docs/reference/cli.md``
renders the command tree with ``mkdocs-click``, and click lists options in decorator order — so
reordering a stack silently rewrites the CLI reference on the docs site. ``gen`` is
``source`` + ``write`` + ``emitter`` + ``verbosity``; ``check`` is the same list with ``write``
removed, which is what makes ``check.params == gen.params - {overwrite, dry_run}`` true by
construction. ``tests/unit/cli/test_options.py`` pins both lists to literals.

The decorators are typed with a ``TypeVar`` bound to ``Callable`` rather than returning ``Any``:
under ``mypy --strict`` an ``Any``-returning decorator erases the command function's type, and the
whole point of declaring the surface once is that both callers keep being checked against it.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import click

from castiron.cli.config import config_option_callback
from castiron.cli.errors import key_option_callback, source_option_callback
from castiron.emitters import EMITTERS
from castiron.sources.openapi import DEFAULT_TIMEOUT

#: A decorated command function. Bound to ``Callable`` so an option stack returns the *same*
#: callable type it was handed, instead of collapsing to ``Any``.
FC = TypeVar('FC', bound=Callable[..., Any])


def source_options(func: FC) -> FC:
    """Apply the options that name a schema source and where its output lives.

    ``--config``, ``--from``, ``--key``, ``--emit``, ``--output``, ``--filename``, ``--schema``
    and ``--timeout``: the eight that decide *what schema* castiron reads and *which files* it
    compares against or writes.

    Args:
        func: The click command function to decorate.

    Returns:
        ``func``, with the eight options attached.
    """
    decorators = (
        click.option(
            '--config',
            'config_path',
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
            envvar='CASTIRON_CONFIG',
            show_envvar=True,
            is_eager=True,
            callback=config_option_callback,
            help="Read settings from this TOML file's [tool.castiron] table (default: the nearest pyproject.toml).",
        ),
        click.option(
            '-f',
            '--from',
            'source',
            envvar=['CASTIRON_FROM', 'SUPABASE_URL'],
            show_envvar=True,
            callback=source_option_callback,
            help='The schema source: a Supabase project or PostgREST URL, or a path to an OpenAPI JSON document.',
        ),
        click.option(
            '-k',
            '--key',
            envvar=['CASTIRON_KEY', 'SUPABASE_KEY'],
            show_envvar=True,
            callback=key_option_callback,
            help=(
                'API key for the source. Prefer the environment variable -- a key on the command line lands '
                'in your shell history.'
            ),
        ),
        click.option(
            '-e',
            '--emit',
            type=click.Choice(sorted(EMITTERS)),
            multiple=True,
            default=('pydantic',),
            show_default=True,
            help='Emitter to run. Repeat the flag for more than one.',
        ),
        click.option(
            '-o',
            '--output',
            type=click.Path(file_okay=False, path_type=Path),
            default=Path('.'),
            show_default='.',
            help='Directory to write generated files into (created if missing).',
        ),
        click.option(
            '--filename',
            help="Override the generated file name (single-emitter runs only; default: the emitter's own).",
        ),
        click.option(
            '-s',
            '--schema',
            default='public',
            show_default=True,
            help='Database schema to read (sent to PostgREST as Accept-Profile).',
        ),
        click.option(
            '--timeout',
            type=float,
            default=DEFAULT_TIMEOUT,
            show_default=True,
            help='Seconds to wait for the source URL.',
        ),
    )
    return _apply(func, decorators)


def write_options(func: FC) -> FC:
    """Apply the two options that only make sense when castiron writes.

    ``check`` never writes, so it never carries these: passing ``--overwrite`` or ``--dry-run`` to
    it is a click usage error (exit 2), which is the correct answer and costs no code.

    Args:
        func: The click command function to decorate.

    Returns:
        ``func``, with ``--overwrite/--no-overwrite`` and ``--dry-run`` attached.
    """
    decorators = (
        click.option(
            '--overwrite/--no-overwrite',
            default=True,
            show_default=True,
            help='Overwrite existing generated files. --no-overwrite fails if any target already exists.',
        ),
        click.option(
            '--dry-run',
            is_flag=True,
            default=False,
            help='Do everything except write files; report what would be written.',
        ),
    )
    return _apply(func, decorators)


def emitter_options(func: FC) -> FC:
    """Apply the options that shape the emitted bytes.

    One source-side inference (``--infer-generated-primary-keys``) plus the six
    :class:`~castiron.emitters.EmitterConfig` toggles. ``check`` needs every one of them: it must
    re-emit with the *same* settings the committed files were generated with, or it would report
    drift that is really a flag mismatch.

    Args:
        func: The click command function to decorate.

    Returns:
        ``func``, with the seven options attached.
    """
    decorators = (
        click.option(
            '--infer-generated-primary-keys/--no-infer-generated-primary-keys',
            default=False,
            show_default=True,
            help=(
                'Treat a sole NOT NULL integer primary key with no visible default as identity, so it is '
                'optional on Insert models. PostgREST hides nextval() defaults, so this is an inference.'
            ),
        ),
        click.option(
            '--crud-models/--no-crud-models',
            default=True,
            show_default=True,
            help='Emit Insert/Update model variants alongside the Row models.',
        ),
        click.option(
            '--enums/--no-enums',
            default=True,
            show_default=True,
            help='Emit Enum classes for enum columns.',
        ),
        click.option(
            '--foreign-keys/--no-foreign-keys',
            default=True,
            show_default=True,
            help='Emit nested foreign-key relationship fields.',
        ),
        click.option(
            '--null-parent-classes/--no-null-parent-classes',
            default=False,
            show_default=True,
            help='Also emit an all-nullable parent class per table.',
        ),
        click.option(
            '--singular-names/--no-singular-names',
            default=False,
            show_default=True,
            help='Singularize generated class names (Product, not Products).',
        ),
        click.option(
            '--model-prefix-protection/--no-model-prefix-protection',
            default=True,
            show_default=True,
            help=(
                "Rename columns starting with 'model_' (Pydantic's protected namespace). --no- emits "
                'ConfigDict(protected_namespaces=()) instead.'
            ),
        ),
    )
    return _apply(func, decorators)


def verbosity_options(func: FC) -> FC:
    """Apply the three per-invocation output knobs (``-v``, ``-q``, ``--debug``).

    Args:
        func: The click command function to decorate.

    Returns:
        ``func``, with the three options attached.
    """
    decorators = (
        click.option('-v', '--verbose', count=True, help='Increase log verbosity: -v = info, -vv = debug.'),
        click.option(
            '-q',
            '--quiet',
            is_flag=True,
            default=False,
            help='Suppress the summary output (errors still print).',
        ),
        click.option(
            '--debug',
            is_flag=True,
            default=False,
            help='Log at debug level and show full tracebacks on unexpected errors.',
        ),
    )
    return _apply(func, decorators)


def _apply(func: FC, decorators: tuple[Callable[[FC], FC], ...]) -> FC:
    """Apply ``decorators`` so the resulting ``--help`` lists them top to bottom.

    click collects options onto ``__click_params__`` as each decorator runs and reverses the list
    when the command is built, so a stack written top-to-bottom must be applied **bottom-up** to
    read in that order — exactly what stacked ``@click.option`` lines do, and what this reproduces
    for a tuple.

    Args:
        func: The command function.
        decorators: The option decorators, in the order they should appear in ``--help``.

    Returns:
        ``func`` with every decorator applied.
    """
    for decorator in reversed(decorators):
        func = decorator(func)
    return func
