r"""``castiron check`` — re-emit in memory, compare against what is committed, fail CI on drift.

This is the command castiron's published pitch is about: *generation that runs in CI without
database credentials*. Every hook it needs was driven into the codebase ahead of it — ``EXIT_DRIFT``
in :mod:`castiron.cli.errors`, ``newline='\n'`` made mandatory in :mod:`castiron.cli.output`
(*"otherwise CI-021's check reports permanent, unfixable drift"*), the emitter registry placed in
:mod:`castiron.emitters` *"because check needs the same lookup"*, and the provenance header the
report reads back. This module assembles parts that were cut to fit it.

**It compares bytes, not IR** (captain's ruling). Byte comparison answers the question users
actually ask — *"is my committed file what castiron would write today?"* — where an IR comparison
answers the narrower *"is my committed file's schema current?"* and reports clean when the emitter
itself has moved.

**It never writes.** No ``mkdir``, no ``write_text``, not even to create ``--output``. There is no
``--fix``: ``gen`` is the fix.

Three rules decide the exit codes, and the second is the one that looks arbitrary until it is
stated:

1. Every file matches → **0**.
2. The comparison **ran** and the answer is "not identical" → **3**. That includes a target that
   does not exist: it is an answer ("the committed state does not match the schema") and the user's
   next action is the same as for any other drift. It also includes a version-only difference —
   the file genuinely is not what ``gen`` produces, and a ``check`` that reported clean there would
   be lying about the file's currency. What the recorded version changes is the **message**.
3. castiron could not perform the comparison at all (unreadable file, unreachable source, bad
   config) → **1**, through the same boundary ``gen`` uses.

The drift report goes to **stdout**, because it is the command's *result* rather than an error
about the command — the same reading ``git diff``, ``ruff check`` and ``mypy`` take, and those are
the tools ``check`` will sit beside in a ``.pre-commit-config.yaml``.
"""

import logging
import sys
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Literal

import click

from castiron import __version__
from castiron.cli.errors import EXIT_DRIFT, cli_error_handling, redact
from castiron.cli.options import emitter_options, source_options, verbosity_options
from castiron.cli.output import display_path, resolve_output_path
from castiron.cli.pipeline import describe, hint_for, run_pipeline
from castiron.emitters import EmittedFile
from castiron.emitters.base import parse_header_version
from castiron.utils.logging import configure_logging
from castiron.utils.textdiff import changed_line_counts, sha256_text, unified_hunks, whitespace_only_lines

logger = logging.getLogger(__name__)

#: The first castiron release whose emitted modules carry a provenance header (CI-021a). Named in
#: the "records no castiron version" message so a user can tell "older than the header" from
#: "hand-written" without reading a changelog.
HEADER_SINCE = '0.5.0'

#: What the report calls the two sides. The file on disk is the reference; the emitted bytes are
#: what the schema says it should be. Spelled once so the ``---``/``+++`` labels, the ``size:``
#: row and the ``sha256:`` row can never drift apart.
ON_DISK = 'on disk'
FROM_SCHEMA = 'from the schema'

#: ``check``'s ``--help`` body. A module constant rather than the function docstring for the same
#: reason as :data:`castiron.cli.gen.GEN_HELP`: the example block needs click's ``\b`` marker.
CHECK_HELP = """Fail if the committed generated code no longer matches the schema.

Reads the schema exactly as `castiron gen` would, re-emits every file in memory, and
compares it against the files already under --output. Nothing is written, ever.

Exits 0 when every file is up to date and 3 when any of them is not -- including
when a file castiron would write is missing. No database connection is required.

\b
Examples:
  castiron check --from ./openapi.json --output src/myapp/models
  castiron check   # with from/emit/output in [tool.castiron]
"""

#: What one comparison concluded. ``'missing'`` is kept distinct from ``'differs'`` even though
#: both are drift, because the two need different sentences: one says "run gen", the other says
#: "run gen, and check --output points where you think it does".
ComparisonStatus = Literal['match', 'differs', 'missing']


@dataclass(frozen=True)
class FileComparison:
    """One emitted file measured against what is on disk at its resolved path.

    Attributes:
        path: The resolved target path — the same path ``gen`` would write, resolved by the same
            :func:`~castiron.cli.output.resolve_output_path`.
        status: Whether the file matches, differs, or is absent.
        expected: The text on disk, decoded in universal-newline mode. ``None`` — and only —
            when ``status`` is ``'missing'``.
        actual: The text castiron produced from the schema.
        recorded_version: The castiron version the file on disk records in its provenance header,
            or ``None`` for a headerless, hand-written, or pre-header file. ``None`` is expected,
            not exceptional.
    """

    path: Path
    status: ComparisonStatus
    expected: str | None
    actual: str
    recorded_version: str | None


@click.command(help=CHECK_HELP)
@source_options
@emitter_options
@verbosity_options
def check(
    config_path: Path | None,
    source: str | None,
    key: str | None,
    emit: tuple[str, ...],
    output: Path,
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
    verbose: int,
    quiet: bool,
    debug: bool,
) -> None:
    """Compare the committed generated files against a freshly read schema.

    The ``--help`` body is :data:`CHECK_HELP`. The eighteen parameters are ``gen``'s twenty minus
    ``--overwrite`` and ``--dry-run``, applied from the same decorator stacks in
    :mod:`castiron.cli.options` — so the two commands cannot drift apart, and passing a write-only
    flag here is a click usage error (exit 2) rather than a silently accepted no-op.
    """
    configure_logging(verbose=verbose, debug=debug, redactor=partial(redact, key=key))
    if config_path is not None:
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
        comparisons = compare_emitted_files(result.files, output)
        report = render_report(comparisons, output_dir=output, running_version=__version__, quiet=quiet)
        if not quiet:
            click.echo(describe(result))
        if report:
            click.echo(report)

    # Outside the boundary on purpose. `SystemExit` derives from `BaseException`, so it would pass
    # through `except Exception` untouched anyway -- but raising it here makes the ordering
    # obvious: the report is the command's result, and the exit code is how a CI job reads it.
    if any(comparison.status != 'match' for comparison in comparisons):
        sys.exit(EXIT_DRIFT)


def compare_emitted_files(files: list[EmittedFile], output_dir: Path) -> list[FileComparison]:
    r"""Compare every emitted file against the file at its resolved output path.

    ⚠ **Every file is checked; the loop never short-circuits.** Reporting only the first drifted
    file would cost a second CI round trip to discover the next one — the same argument
    ``--no-overwrite``'s all-or-nothing pre-check makes on the write side.

    ⚠ **The on-disk text is decoded in universal-newline mode**, so ``\r\n`` and lone ``\r``
    become ``\n`` before the comparison. castiron writes LF unconditionally, which stops *Python*
    translating on write but cannot stop *git* translating on checkout: a contributor with
    ``core.autocrlf=true`` would otherwise get permanent, unfixable drift on every CI run — the
    exact failure ``newline='\n'`` was introduced to prevent, arriving through a different door.
    The accepted cost is a false *negative* confined to line endings: a genuinely CRLF-ified file
    reports clean while ``gen`` would rewrite it to LF. A UTF-8 BOM is **not** normalized away —
    it is real drift, and the whitespace renderer makes it visible.

    Args:
        files: The emitted files, in emitter order.
        output_dir: The directory ``--output`` selected.

    Returns:
        One :class:`FileComparison` per emitted file, in input order.

    Raises:
        OutputError: An emitted path escapes ``output_dir`` or names no file. castiron cannot
            perform the comparison at all, so it is exit 1 rather than drift.
        click.ClickException: A target exists but cannot be read or decoded — also exit 1, for
            the same reason. A permission error is not an answer to "has this drifted?".
    """
    comparisons: list[FileComparison] = []
    for emitted in files:
        target = resolve_output_path(output_dir, emitted)
        if not target.exists():
            comparisons.append(
                FileComparison(
                    path=target, status='missing', expected=None, actual=emitted.content, recorded_version=None
                )
            )
            continue
        try:
            on_disk = target.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            raise click.ClickException(f'Could not read {target}: {exc}') from exc
        comparisons.append(
            FileComparison(
                path=target,
                status='match' if on_disk == emitted.content else 'differs',
                expected=on_disk,
                actual=emitted.content,
                recorded_version=parse_header_version(on_disk),
            )
        )
    return comparisons


def render_report(
    comparisons: list[FileComparison],
    *,
    output_dir: Path,
    running_version: str,
    quiet: bool,
) -> str:
    """Render everything ``check`` prints below the "what castiron read" line.

    ``--quiet`` suppresses the up-to-date lines — they are a summary — and never the drift report,
    which is the payload. A ``-q`` run that found drift still prints the diff, because a CI log
    that says only "exit 3" is a log that sends someone back to run the command again.

    Args:
        comparisons: Every comparison, in emitter order.
        output_dir: The ``--output`` value, named in the missing-file message so a typo'd path is
            diagnosable from the report alone.
        running_version: The castiron version doing the checking (:data:`castiron.__version__` in
            production; injected so a test can pin both sides of the diagnostic).
        quiet: Suppress the up-to-date lines.

    Returns:
        The report, without a trailing newline, or ``''`` when there is nothing to say.
    """
    drifted = [comparison for comparison in comparisons if comparison.status != 'match']
    if not drifted:
        if quiet:
            return ''
        return '\n'.join(f'castiron: {display_path(entry.path)} is up to date.' for entry in comparisons)

    lines = [f'castiron: drift detected in {len(drifted)} of {len(comparisons)} generated file(s).']
    for comparison in comparisons:
        if comparison.status == 'match':
            if not quiet:
                lines.extend(['', f'  {display_path(comparison.path)} is up to date.'])
            continue
        lines.append('')
        lines.extend(f'  {line}' for line in _describe_drift(comparison, output_dir, running_version))
    lines.extend(['', 'castiron: run `castiron gen` to regenerate.'])
    return '\n'.join(lines)


def _describe_drift(comparison: FileComparison, output_dir: Path, running_version: str) -> list[str]:
    """Render one drifted file's block, unindented (the caller owns the report indent)."""
    shown = display_path(comparison.path)
    if comparison.expected is None:
        return [
            f'{shown} does not exist.',
            f'castiron would write it here (resolved from --output {display_path(output_dir)}).',
            'Run `castiron gen` to create it, or check that --output points where your generated',
            'files actually live.',
        ]

    expected_lines = comparison.expected.splitlines(keepends=True)
    actual_lines = comparison.actual.splitlines(keepends=True)
    added, removed = changed_line_counts(expected_lines, actual_lines)
    block = [
        f'file:     {shown}',
        f'size:     {len(comparison.expected)} chars {ON_DISK} -> {len(comparison.actual)} chars {FROM_SCHEMA}',
        (
            f'sha256:   {sha256_text(comparison.expected)[:16]} {ON_DISK} -> '
            f'{sha256_text(comparison.actual)[:16]} {FROM_SCHEMA}'
        ),
        f'lines:    +{added} / -{removed}',
    ]
    whitespace = whitespace_only_lines(expected_lines, actual_lines, expected_label=ON_DISK, actual_label=FROM_SCHEMA)
    block.extend(
        whitespace or unified_hunks(expected_lines, actual_lines, fromfile=ON_DISK, tofile=f'produced {FROM_SCHEMA}')
    )
    block.extend(_provenance_verdict(comparison.recorded_version, running_version))
    return block


def _provenance_verdict(recorded: str | None, running: str) -> list[str]:
    """Say what the recorded castiron version does and does not explain about this difference.

    ⚠ **The honest limit, stated here and in the message:** when the recorded version differs,
    castiron knows a version change is in play but **cannot** attribute individual hunks to it —
    it has no way to re-emit as the old version. The message says "some or all", and must keep
    saying something that weak.

    Args:
        recorded: The version the file on disk records, or ``None``.
        running: The version doing the checking.

    Returns:
        The verdict lines, unindented.
    """
    if recorded is None:
        return [
            f'this file records no castiron version (it predates castiron {HEADER_SINCE}, or was',
            'hand-written). Run `castiron gen` to record one -- after that, `check` can tell a',
            'castiron upgrade from a schema change apart.',
        ]
    if recorded == running:
        return [
            f'generated by castiron {running}, and you are running {running} --',
            'this difference is your schema or a hand edit.',
        ]
    return [
        f'generated by castiron {recorded}; you are running {running}. Some or all of this difference',
        "may be castiron's own output changing rather than your schema.",
        'Run `castiron gen` to adopt the current output.',
    ]
