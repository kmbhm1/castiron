r"""``EmittedFile`` → disk. The one place castiron writes, and where determinism survives.

Emitters deliberately do no file I/O (CI4-D1) precisely so the nondeterminism the
predecessor smuggled in through ``AbstractFileWriter.save()`` (timestamped, uniquified file
names) has exactly one place it could come back. It does not come back here:

- the emitted text is written **byte for byte** — no post-hoc ``ruff``/``black`` pass, no
  generated-on banner, no source URL, no newline normalization;
- ``newline='\n'`` is **mandatory**, not defensive. Without it Python's text layer rewrites
  ``\n`` to ``\r\n`` on Windows, the file stops matching what the emitter produced, and
  CI-021's ``check`` reports permanent, unfixable drift. (``Path.write_text`` grew the
  parameter in 3.10 — castiron's exact floor.)

``--no-overwrite`` is checked all-or-nothing *before* anything is written: a half-generated
output tree is worse than none.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import click

from castiron.emitters import EmittedFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WriteResult:
    """One resolved output file and what happened to it.

    Attributes:
        path: The resolved target path.
        size: The size in bytes of the UTF-8 encoded content (reported identically in
            ``--dry-run``, so both modes agree).
        written: ``False`` under ``--dry-run``, when nothing touched the filesystem.
    """

    path: Path
    size: int
    written: bool


class OutputError(click.ClickException):
    """A generated file could not be written (exit code 1)."""


def resolve_output_path(output_dir: Path, emitted: EmittedFile) -> Path:
    """Resolve an emitted file's relative path under ``output_dir``.

    Args:
        output_dir: The directory ``--output`` selected.
        emitted: The file an emitter produced.

    Returns:
        The target path under ``output_dir``.

    Raises:
        OutputError: ``emitted.path`` is absolute or escapes ``output_dir``. This is
            reachable from user input today — ``--filename`` and the config file both feed
            :attr:`~castiron.emitters.EmitterConfig.output_filename`.
    """
    relative = Path(emitted.path)
    if relative.is_absolute() or '..' in relative.parts:
        raise OutputError(
            f"Refusing to write '{emitted.path}': a generated path must be relative to the output "
            "directory and must not contain '..'."
        )
    return output_dir / relative


def write_emitted_files(
    files: Sequence[EmittedFile],
    output_dir: Path,
    *,
    overwrite: bool = True,
    dry_run: bool = False,
) -> list[WriteResult]:
    """Write emitted files under ``output_dir``, byte for byte.

    Args:
        files: The emitted files, in emitter order.
        output_dir: The directory to write into (created, with parents, when missing).
        overwrite: When ``False``, fail if any target already exists — checked for **every**
            target before **any** is written.
        dry_run: Do everything except touch the filesystem.

    Returns:
        One :class:`WriteResult` per file, in input order.

    Raises:
        OutputError: A target exists and ``overwrite`` is ``False``, two emitted files
            resolve to the same path, a path escapes ``output_dir``, or the write failed.
    """
    targets: list[tuple[Path, EmittedFile]] = []
    seen: set[Path] = set()
    for emitted in files:
        target = resolve_output_path(output_dir, emitted)
        if target in seen:
            raise OutputError(
                f'Two emitted files resolve to the same path {target}; give one of them a distinct '
                'file name (nothing was written).'
            )
        seen.add(target)
        targets.append((target, emitted))

    if not overwrite:
        for target, _ in targets:
            if target.exists():
                raise OutputError(f'{target} already exists and --no-overwrite was given; nothing was written.')

    results: list[WriteResult] = []
    for target, emitted in targets:
        size = len(emitted.content.encode('utf-8'))
        if not dry_run:
            _write(target, emitted.content)
        results.append(WriteResult(path=target, size=size, written=not dry_run))
    return results


def _write(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` verbatim, creating parent directories as needed."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # newline='\n' is load-bearing: see the module docstring.
        target.write_text(content, encoding='utf-8', newline='\n')
    except OSError as exc:
        raise OutputError(f'Could not write {target}: {exc}') from exc
    logger.debug(f'Wrote {target}')
