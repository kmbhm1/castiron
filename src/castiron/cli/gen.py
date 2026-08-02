"""``castiron gen`` — the front door: a schema source in, typed files on disk out.

This is where the three merged libraries become one command. The option surface is
deliberately broad (every :class:`~castiron.emitters.EmitterConfig` toggle is reachable
from a flag *and* a config key), and every boolean is declared as a ``--x/--no-x`` pair so a
command-line invocation can override a ``[tool.castiron]`` value in **both** directions.

Failure is loud. supabase-pydantic's ``gen`` logged a connection error and ``return``ed,
exiting **0**; every failure here carries a documented exit code (see
:mod:`castiron.cli.errors`).
"""

import json
import logging
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

import click

from castiron.cli.config import config_option_callback, looks_like_url
from castiron.cli.errors import (
    cli_error_handling,
    key_option_callback,
    redact,
    redact_source,
    source_error_hint,
    source_option_callback,
)
from castiron.cli.notices import report as report_notices
from castiron.cli.output import WriteResult, write_emitted_files
from castiron.emitters import EMITTERS, EmittedFile, EmitterConfig, get_emitter_spec
from castiron.ir import Schema
from castiron.sources import (
    SourceError,
    SourceFetchError,
    build_schema_from_document,
    load_openapi_schema,
    normalize_postgrest_url,
)
from castiron.sources.openapi import DEFAULT_TIMEOUT
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
@click.option(
    '--config',
    'config_path',
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    envvar='CASTIRON_CONFIG',
    show_envvar=True,
    is_eager=True,
    callback=config_option_callback,
    help="Read settings from this TOML file's [tool.castiron] table (default: the nearest pyproject.toml).",
)
@click.option(
    '-f',
    '--from',
    'source',
    envvar=['CASTIRON_FROM', 'SUPABASE_URL'],
    show_envvar=True,
    callback=source_option_callback,
    help='The schema source: a Supabase project or PostgREST URL, or a path to an OpenAPI JSON document.',
)
@click.option(
    '-k',
    '--key',
    envvar=['CASTIRON_KEY', 'SUPABASE_KEY'],
    show_envvar=True,
    callback=key_option_callback,
    help=(
        'API key for the source. Prefer the environment variable -- a key on the command line lands '
        'in your shell history.'
    ),
)
@click.option(
    '-e',
    '--emit',
    type=click.Choice(sorted(EMITTERS)),
    multiple=True,
    default=('pydantic',),
    show_default=True,
    help='Emitter to run. Repeat the flag for more than one.',
)
@click.option(
    '-o',
    '--output',
    type=click.Path(file_okay=False, path_type=Path),
    default=Path('.'),
    show_default='.',
    help='Directory to write generated files into (created if missing).',
)
@click.option(
    '--filename',
    help="Override the generated file name (single-emitter runs only; default: the emitter's own).",
)
@click.option(
    '-s',
    '--schema',
    default='public',
    show_default=True,
    help='Database schema to read (sent to PostgREST as Accept-Profile).',
)
@click.option(
    '--timeout',
    type=float,
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help='Seconds to wait for the source URL.',
)
@click.option(
    '--overwrite/--no-overwrite',
    default=True,
    show_default=True,
    help='Overwrite existing generated files. --no-overwrite fails if any target already exists.',
)
@click.option(
    '--dry-run',
    is_flag=True,
    default=False,
    help='Do everything except write files; report what would be written.',
)
@click.option(
    '--infer-generated-primary-keys/--no-infer-generated-primary-keys',
    default=False,
    show_default=True,
    help=(
        'Treat a sole NOT NULL integer primary key with no visible default as identity, so it is '
        'optional on Insert models. PostgREST hides nextval() defaults, so this is an inference.'
    ),
)
@click.option(
    '--crud-models/--no-crud-models',
    default=True,
    show_default=True,
    help='Emit Insert/Update model variants alongside the Row models.',
)
@click.option(
    '--enums/--no-enums',
    default=True,
    show_default=True,
    help='Emit Enum classes for enum columns.',
)
@click.option(
    '--foreign-keys/--no-foreign-keys',
    default=True,
    show_default=True,
    help='Emit nested foreign-key relationship fields.',
)
@click.option(
    '--null-parent-classes/--no-null-parent-classes',
    default=False,
    show_default=True,
    help='Also emit an all-nullable parent class per table.',
)
@click.option(
    '--singular-names/--no-singular-names',
    default=False,
    show_default=True,
    help='Singularize generated class names (Product, not Products).',
)
@click.option(
    '--model-prefix-protection/--no-model-prefix-protection',
    default=True,
    show_default=True,
    help=(
        "Rename columns starting with 'model_' (Pydantic's protected namespace). --no- emits "
        'ConfigDict(protected_namespaces=()) instead.'
    ),
)
@click.option('-v', '--verbose', count=True, help='Increase log verbosity: -v = info, -vv = debug.')
@click.option('-q', '--quiet', is_flag=True, default=False, help='Suppress the summary output (errors still print).')
@click.option(
    '--debug',
    is_flag=True,
    default=False,
    help='Log at debug level and show full tracebacks on unexpected errors.',
)
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
    declared above, each also readable from ``[tool.castiron]`` (except ``key``, ``config``
    and the three per-invocation verbosity flags).
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

    hint = partial(_hint_for, key=key, schema=schema, source=source)
    with cli_error_handling(debug=debug, key=key, hint=hint):
        if not source:
            raise click.UsageError(
                'No schema source. Pass --from <url|path>, set CASTIRON_FROM, '
                'or add `from = "..."` under [tool.castiron] in pyproject.toml.'
            )
        if not emit:
            raise click.UsageError('No emitters selected; pass --emit <name>.')
        specs = [get_emitter_spec(name) for name in emit]
        if filename and len(specs) > 1:
            raise click.UsageError('--filename applies to a single-emitter run; drop it or pass one --emit.')

        schema_ir, origin = load_schema(
            source,
            key=key,
            schema=schema,
            timeout=timeout,
            infer_generated_primary_keys=infer_generated_primary_keys,
            disable_model_prefix_protection=not model_prefix_protection,
        )
        report_notices(
            schema_ir,
            infer_generated_primary_keys=infer_generated_primary_keys,
            from_openapi=True,
        )

        base = EmitterConfig(
            generate_crud_models=crud_models,
            generate_enums=enums,
            add_null_parent_classes=null_parent_classes,
            disable_model_prefix_protection=not model_prefix_protection,
            singular_names=singular_names,
            include_foreign_keys=foreign_keys,
        )
        files: list[EmittedFile] = [
            emitted
            for spec in specs
            for emitted in spec.build(replace(base, output_filename=filename or spec.default_filename)).emit(schema_ir)
        ]
        results = write_emitted_files(files, output, overwrite=overwrite, dry_run=dry_run)
        if not quiet:
            echo_summary(schema_ir, origin, results, dry_run=dry_run)


def load_schema(
    source: str,
    *,
    key: str | None,
    schema: str,
    timeout: float,
    infer_generated_primary_keys: bool,
    disable_model_prefix_protection: bool,
) -> tuple[Schema, str]:
    """Load the Schema IR from a URL or a local OpenAPI JSON document.

    The dispatch lives here rather than in :mod:`castiron.sources` deliberately: reading a
    path off the filesystem is a CLI-input concern, and CI-005 keeps ``pathlib`` out of the
    source's parser entirely.

    Args:
        source: The ``--from`` value: an ``http(s)`` URL, or a path to an OpenAPI document.
        key: The API key, used only for the URL path.
        schema: The database schema to read.
        timeout: Seconds to wait for the source URL.
        infer_generated_primary_keys: Report a sole NOT NULL integer primary key with no
            default as identity.
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.

    Returns:
        The schema and a human-readable, redacted description of where it came from.

    Raises:
        SourceFetchError: The document could not be retrieved or read.
        SourceParseError: The document is not a schema castiron can read.
        click.UsageError: ``source`` is neither a URL nor an existing file.
    """
    if looks_like_url(source):
        origin = source_origin(source, key)
        logger.debug(f'Reading the schema from {origin}')
        schema_ir = load_openapi_schema(
            source,
            key=key,
            schema=schema,
            timeout=timeout,
            disable_model_prefix_protection=disable_model_prefix_protection,
            infer_generated_primary_keys=infer_generated_primary_keys,
        )
        return schema_ir, origin

    path = Path(source)
    if not path.is_file():
        raise click.UsageError(
            # `redact_source`, not `redact`: this echoes the raw --from value back, and a
            # scheme-less `postgres:user:password@host` (the shape psql connection strings
            # circulate in) has no `://` for redact's userinfo anchor to see. CI-068.
            f"--from '{redact_source(source, key)}' is neither a URL nor an existing file. Pass a "
            'Supabase/PostgREST URL (https://...) or a path to an OpenAPI JSON document.'
        )
    logger.debug(f'Reading the schema from {path}')
    document = read_json_document(path)
    schema_ir = build_schema_from_document(
        document,
        schema=schema,
        disable_model_prefix_protection=disable_model_prefix_protection,
        infer_generated_primary_keys=infer_generated_primary_keys,
    )
    return schema_ir, redact(str(path), key)


def source_origin(source: str, key: str | None) -> str:
    """Describe where the schema comes (or came) from, redacted and safe to print.

    Never raises: it is called from the error path, where a failure to describe the source
    must not replace the failure the user actually needs to see.

    Args:
        source: The ``--from`` value.
        key: The API key in play, masked out of the result.

    Returns:
        The normalized PostgREST root for a URL, or the path as given — redacted either way.
    """
    if not looks_like_url(source):
        # Redacted like the URL branch, not because a path is a likely place for a key but
        # because "every printed string is redacted" (CI6-D7) is only true if it has no
        # exceptions: `--from './dump.json?apikey=...'` is a path as far as this branch is
        # concerned, and the summary line prints whatever comes back.
        return redact(str(Path(source)), key)
    try:
        return redact(normalize_postgrest_url(source), key)
    except SourceError:  # pragma: no cover - normalize only rejects a blank URL, caught earlier
        return redact(source, key)


def _hint_for(exc: SourceError, *, key: str | None, schema: str, source: str | None) -> str | None:
    """Build the ``Hint:`` line for a source failure, resolving the origin lazily."""
    origin = source_origin(source, key) if source else None
    return source_error_hint(exc, key=key, schema=schema, origin=origin)


def read_json_document(path: Path) -> dict[str, Any]:
    """Read a local OpenAPI JSON document.

    Args:
        path: The file to read.

    Returns:
        The decoded document.

    Raises:
        SourceFetchError: The file is unreadable, is not JSON, or is not a JSON object.
            The shared source contract is reused rather than a parallel CLI error class.
    """
    try:
        raw = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceFetchError(f'Could not read {path}: {exc}') from exc
    try:
        document: Any = json.loads(raw)
    except ValueError as exc:
        raise SourceFetchError(f'{path} is not valid JSON: {exc}') from exc
    if not isinstance(document, dict):
        raise SourceFetchError(
            f'{path} contains a JSON {type(document).__name__}, not an object; expected an OpenAPI document.'
        )
    decoded: dict[str, Any] = document
    return decoded


def echo_summary(schema: Schema, origin: str, results: list[WriteResult], *, dry_run: bool) -> None:
    """Print the two-line run summary on stdout.

    The counts line is the cheap way a user notices that RLS is hiding half their tables:
    "read 2 tables" when you expected 20 is the whole signal.

    Args:
        schema: The schema that was read.
        origin: Where it came from (already redacted).
        results: What was (or would be) written.
        dry_run: Whether nothing was actually written.
    """
    counts = ', '.join(
        [
            _pluralize(len(schema.tables), 'table'),
            _pluralize(len(schema.enums), 'enum'),
        ]
    )
    click.echo(f'castiron: read {counts} and {_pluralize(len(schema.functions), "function")} from {origin}')
    verb = 'would write' if dry_run else 'wrote'
    suffix = ' [dry run, nothing written]' if dry_run else ''
    for result in results:
        click.echo(f'castiron: {verb} {display_path(result.path)} ({format_size(result.size)}){suffix}')


def display_path(path: Path) -> str:
    """Render a resolved output path the shortest honest way.

    A config-file ``output`` is anchored to the config file's directory (CI6-D5a), so it
    arrives absolute. Printing it relative to the cwd keeps the common case reading
    ``wrote out/schema.py``, while a run from a subdirectory — where the file genuinely
    lands somewhere else — still shows the full path rather than a misleading short one.

    Args:
        path: The resolved target path.

    Returns:
        The path relative to the cwd when it is under it, else the path unchanged.
    """
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def format_size(size: int) -> str:
    """Render a byte count the way the summary shows it (``947 B`` / ``14.2 kB``)."""
    if size < 1000:
        return f'{size} B'
    return f'{size / 1000:.1f} kB'


def _pluralize(count: int, noun: str) -> str:
    """Render ``count`` with ``noun``, pluralized by simple suffixing."""
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'
