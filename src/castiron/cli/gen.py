"""``castiron gen`` — the front door: a schema source in, typed files on disk out.

This is where the three merged libraries become one command. The option surface is
deliberately broad (every :class:`~castiron.emitters.EmitterConfig` toggle is reachable
from a flag *and* a config key), and every boolean is declared as a ``--x/--no-x`` pair so a
command-line invocation can override a ``[tool.castiron]`` value in **both** directions. It is
declared once, in :mod:`castiron.cli.options`, and shared with ``check``.

Everything up to the write lives in :mod:`castiron.cli.pipeline`, so ``gen`` is
``run_pipeline`` + ``write_emitted_files`` + :func:`echo_summary` and ``castiron check`` is
``run_pipeline`` + compare + report. That is what makes *"check is gen minus the write"* a
structural fact rather than an aspiration.

Failure is loud. supabase-pydantic's ``gen`` logged a connection error and ``return``ed,
exiting **0**; every failure here carries a documented exit code (see
:mod:`castiron.cli.errors`).
"""

import logging
from functools import partial
from pathlib import Path

import click

from castiron.cli.errors import cli_error_handling, redact
from castiron.cli.options import emitter_options, source_options, verbosity_options, write_options
from castiron.cli.output import WriteResult, display_path, format_size, write_emitted_files
from castiron.cli.pipeline import PipelineResult, describe, hint_for, run_pipeline
from castiron.utils.logging import configure_logging

logger = logging.getLogger(__name__)

#: ``gen``'s ``--help`` body. Passed to ``@click.command(help=...)`` rather than left as the
#: function docstring so the example block can carry click's ``\b`` no-rewrap marker: a
#: docstring holding a backslash would have to be a raw string, in which case ``\b`` stops
#: being the backspace character click looks for.
GEN_HELP = """Generate typed code from a schema source.

Reads a schema from a source (a Supabase/PostgREST URL, or a local OpenAPI JSON
document), lowers it into castiron's Schema IR, and writes one file per emitter.
No database connection is required.

\b
Examples:
  castiron gen --from https://abcdefgh.supabase.co --emit pydantic
  castiron gen --from ./openapi.json --emit pydantic --output src/myapp/models
"""


@click.command(help=GEN_HELP)
@source_options
@write_options
@emitter_options
@verbosity_options
def gen(
    config_path: Path | None,
    source: str | None,
    key: str | None,
    emit: tuple[str, ...],
    output: Path,
    filename: str | None,
    schema: str,
    timeout: float,
    overwrite: bool,
    dry_run: bool,
    infer_generated_primary_keys: bool,
    crud_models: bool,
    enums: bool,
    foreign_keys: bool,
    null_parent_classes: bool,
    singular_names: bool,
    model_prefix_protection: bool,
    verbose: int,
    quiet: bool,
    debug: bool,
) -> None:
    """Generate typed code from a schema source.

    The ``--help`` body is :data:`GEN_HELP`; the twenty parameters are the option surface
    declared in :mod:`castiron.cli.options`, each also readable from ``[tool.castiron]``
    (except ``key``, ``config`` and the three per-invocation verbosity flags).
    """
    # The redactor is not optional: `fetch` logs the normalized target at DEBUG, and
    # `normalize_postgrest_url` preserves the query string, so -vv/--debug would otherwise
    # print `?apikey=...` -- the very output the internal-error message asks users to paste
    # into an issue.
    configure_logging(verbose=verbose, debug=debug, redactor=partial(redact, key=key))
    if config_path is not None:
        # INFO, not DEBUG: discovery walks up from the cwd implicitly, so "which file did you
        # read" is exactly what -v is for (spec §3.2 note 3).
        logger.info(f'Reading [tool.castiron] from {config_path}')

    hint = partial(hint_for, key=key, schema=schema, source=source)
    with cli_error_handling(debug=debug, key=key, hint=hint):
        result = run_pipeline(
            source=source,
            key=key,
            emit=emit,
            filename=filename,
            schema=schema,
            timeout=timeout,
            infer_generated_primary_keys=infer_generated_primary_keys,
            crud_models=crud_models,
            enums=enums,
            foreign_keys=foreign_keys,
            null_parent_classes=null_parent_classes,
            singular_names=singular_names,
            model_prefix_protection=model_prefix_protection,
        )
        writes = write_emitted_files(result.files, output, overwrite=overwrite, dry_run=dry_run)
        if not quiet:
            echo_summary(result, writes, dry_run=dry_run)


def echo_summary(result: PipelineResult, writes: list[WriteResult], *, dry_run: bool) -> None:
    """Print the two-line run summary on stdout.

    Args:
        result: What the pipeline read and emitted.
        writes: What was (or would be) written.
        dry_run: Whether nothing was actually written.
    """
    click.echo(describe(result))
    verb = 'would write' if dry_run else 'wrote'
    suffix = ' [dry run, nothing written]' if dry_run else ''
    for entry in writes:
        click.echo(f'castiron: {verb} {display_path(entry.path)} ({format_size(entry.size)}){suffix}')
