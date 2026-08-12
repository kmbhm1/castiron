"""Everything ``gen`` does up to the write — so ``check`` is *gen minus the write*, structurally.

``castiron check`` (CI-021b) has to reproduce ``gen``'s answer exactly: the same source dispatch,
the same IR, the same emitter construction, the same notices, the same emitted bytes. If those
steps lived in :mod:`castiron.cli.gen` and ``check`` imported them from there, a module named
``gen`` would own ``check``'s pipeline and ~15 lines of the emitter-build block would still have to
be duplicated — two derivations of one thing, which is the defect the emitter registry already paid
for once.

So the shared half lives here and both commands are thin:

* ``gen``   = :func:`run_pipeline` + ``write_emitted_files`` + ``echo_summary``
* ``check`` = :func:`run_pipeline` + compare + report

Nothing in this module writes, and nothing in it is CLI-shaped beyond raising ``click.UsageError``
for the three input combinations both commands reject identically.
"""

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import click

from castiron.cli.config import looks_like_url
from castiron.cli.errors import redact, redact_source, source_error_hint
from castiron.cli.notices import report as report_notices
from castiron.emitters import EmittedFile, EmitterConfig, PydanticEmitter, get_emitter_spec
from castiron.ir import Schema
from castiron.sources import (
    SourceError,
    SourceFetchError,
    build_schema_from_document,
    load_openapi_schema,
    normalize_postgrest_url,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineResult:
    """What a source-to-bytes run produced, before anything touches the filesystem.

    Attributes:
        schema: The Schema IR that was read.
        origin: A human-readable, already-redacted description of where it came from.
        files: The emitted files, in emitter order. ``gen`` writes them; ``check`` compares
            them against what is already on disk.
    """

    schema: Schema
    origin: str
    files: list[EmittedFile]


def run_pipeline(
    *,
    source: str | None,
    key: str | None,
    emit: tuple[str, ...],
    filename: str | None,
    schema: str,
    timeout: float,
    infer_generated_primary_keys: bool,
    crud_models: bool,
    enums: bool,
    foreign_keys: bool,
    null_parent_classes: bool,
    singular_names: bool,
    model_prefix_protection: bool,
) -> PipelineResult:
    """Read the schema and emit every selected emitter's files, in memory.

    Keyword arguments rather than an options dataclass, deliberately: ``tests/unit/corpus/cases.py``
    carries a note that when a real source-options type lands in ``src/`` the corpus one must be
    **replaced** by it. Introducing a second shape here would be the Hard Rule #6 violation that
    note exists to prevent.

    Args:
        source: The ``--from`` value: a URL, or a path to an OpenAPI document.
        key: The API key, used only on the URL path.
        emit: The selected emitter names, in ``--emit`` order.
        filename: Overrides the emitter's default output file name (single-emitter runs only).
        schema: The database schema to read.
        timeout: Seconds to wait for the source URL.
        infer_generated_primary_keys: Report a sole NOT NULL integer primary key as identity.
        crud_models: Emit Insert/Update variants.
        enums: Emit Enum classes.
        foreign_keys: Emit nested foreign-key relationship fields.
        null_parent_classes: Also emit an all-nullable parent class per table.
        singular_names: Singularize generated class names.
        model_prefix_protection: Rename ``model_``-prefixed columns.

    Returns:
        The schema, its redacted origin, and the emitted files.

    Raises:
        click.UsageError: No source, no emitters, or ``--filename`` with more than one emitter.
        SourceError: The source could not be read or parsed (the caller's error boundary maps it).
    """
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
    base = EmitterConfig(
        generate_crud_models=crud_models,
        generate_enums=enums,
        add_null_parent_classes=null_parent_classes,
        disable_model_prefix_protection=not model_prefix_protection,
        singular_names=singular_names,
        include_foreign_keys=foreign_keys,
    )
    # Built before the notices, not after: the class-name notices must report the names the
    # emitter will actually write, and asking the emitter is the only way to do that without a
    # second derivation (CI-114's defect). `isinstance` rather than a name string because the
    # Pydantic emitter is currently the only one that binds Python class names -- a future
    # emitter with the same property joins this test, it does not get its own notice.
    built = [spec.build(replace(base, output_filename=filename or spec.default_filename)) for spec in specs]
    pydantic = next((emitter for emitter in built if isinstance(emitter, PydanticEmitter)), None)
    report_notices(
        schema_ir,
        infer_generated_primary_keys=infer_generated_primary_keys,
        from_openapi=True,
        disable_model_prefix_protection=not model_prefix_protection,
        enum_classes=pydantic.enum_classes(schema_ir) if pydantic is not None else (),
        class_stems=pydantic.class_stems(schema_ir) if pydantic is not None else (),
    )

    files: list[EmittedFile] = [emitted for emitter in built for emitted in emitter.emit(schema_ir)]
    return PipelineResult(schema=schema_ir, origin=origin, files=files)


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

    ⚠ That claim was **false until CI-089** and this is the load-bearing half of the fix. It
    catches :class:`SourceError` only, while ``normalize_postgrest_url`` used to let ``urlsplit``'s
    bare ``ValueError`` through on a malformed URL. Because this runs inside
    :func:`~castiron.cli.errors.cli_error_handling`'s own ``except SourceError`` block, raising
    here does not re-enter the boundary -- it escapes the ``try`` statement entirely and prints an
    unredacted traceback. Narrowing what ``normalize_postgrest_url`` can raise is what makes the
    sentence above true, rather than widening the ``except`` here.

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
    except SourceError:
        # Reachable since CI-089: a malformed URL (`http://[::1`) now raises SourceFetchError out
        # of `normalize_postgrest_url` instead of a bare ValueError, and this runs from the hint
        # path *inside* the error boundary's `except SourceError` -- where a raise would escape the
        # boundary entirely and print an unredacted traceback. Echo the raw value, redacted.
        return redact(source, key)


def hint_for(exc: SourceError, *, key: str | None, schema: str, source: str | None) -> str | None:
    """Build the ``Hint:`` line for a source failure, resolving the origin lazily.

    Args:
        exc: The source failure the error boundary caught.
        key: The API key in play.
        schema: The ``--schema`` value, which several hints name.
        source: The ``--from`` value, or ``None`` when the run never got one.

    Returns:
        The hint text, or ``None`` when no hint applies to that failure.
    """
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


def describe(result: PipelineResult) -> str:
    """Render the one-line "what castiron read" summary both commands print.

    The counts are the cheap way a user notices that RLS is hiding half their tables: "read 2
    tables" when you expected 20 is the whole signal, and it is worth exactly as much to ``check``
    as it is to ``gen`` -- so it is rendered once, here, rather than twice in two commands.

    Args:
        result: The pipeline result to describe.

    Returns:
        The summary line, without a trailing newline.
    """
    counts = ', '.join(
        [
            pluralize(len(result.schema.tables), 'table'),
            pluralize(len(result.schema.enums), 'enum'),
        ]
    )
    return f'castiron: read {counts} and {pluralize(len(result.schema.functions), "function")} from {result.origin}'


def pluralize(count: int, noun: str) -> str:
    """Render ``count`` with ``noun``, pluralized by simple suffixing.

    Args:
        count: The number of things.
        noun: The singular noun.

    Returns:
        ``'1 table'`` / ``'2 tables'``.
    """
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'
