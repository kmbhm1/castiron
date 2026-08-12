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
from pathlib import Path, PurePath

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


def is_unsafe_output_path(relative: PurePath) -> bool:
    """Whether ``relative`` would escape the output directory when joined onto it.

    Takes a :class:`~pathlib.PurePath` rather than a :class:`~pathlib.Path` so the rule can
    be exercised with ``PureWindowsPath`` on a POSIX test runner — which is not academic.
    On Windows ``PureWindowsPath('/evil.py').is_absolute()`` is **False** (root, no drive)
    and so is ``PureWindowsPath('C:evil.py')`` (drive, no root), yet ``/`` **discards the
    left operand** when the right carries either::

        PureWindowsPath('out') / '/evil.py'   ->  WindowsPath('/evil.py')
        PureWindowsPath('out') / 'C:evil.py'  ->  WindowsPath('C:evil.py')

    So an ``is_absolute()``-only guard lets ``--filename /schema.py`` — or a committed
    ``[tool.castiron] filename = "/schema.py"``, which does it to everyone who runs castiron
    in that repo — write to the drive root. Testing ``drive`` and ``root`` too is what makes
    the guard hold on the platform it protects.

    Args:
        relative: The path an emitter produced, in either path flavour.

    Returns:
        ``True`` when the path is absolute, anchored, drive-qualified, or contains ``..``.
    """
    return bool(relative.is_absolute() or relative.drive or relative.root or '..' in relative.parts)


def resolve_output_path(output_dir: Path, emitted: EmittedFile) -> Path:
    """Resolve an emitted file's relative path under ``output_dir``.

    Args:
        output_dir: The directory ``--output`` selected.
        emitted: The file an emitter produced.

    Returns:
        The target path under ``output_dir``.

    Raises:
        OutputError: ``emitted.path`` escapes ``output_dir`` (see
            :func:`is_unsafe_output_path`) or names no file at all. Both are reachable from
            user input today — ``--filename`` and the config file both feed
            :attr:`~castiron.emitters.EmitterConfig.output_filename`.
    """
    relative = Path(emitted.path)
    if is_unsafe_output_path(relative):
        raise OutputError(
            f"Refusing to write '{emitted.path}': a generated path must be relative to the output "
            "directory, must not be drive- or root-anchored, and must not contain '..'."
        )
    if not relative.name:
        # `.` and `` both normalize away, so `output_dir / relative` is `output_dir` itself and
        # the write would replace the output directory with a regular file (exit 0, silently).
        raise OutputError(
            f"Refusing to write '{emitted.path}': it names no file, so castiron would write over the "
            'output directory itself. Pass --filename <name.py>.'
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
    except (OSError, ValueError) as exc:
        # ValueError too: the path layer raises it, not OSError, for an embedded NUL --
        # reachable from a config-file `filename` -- and that is bad user input (exit 1),
        # not a castiron bug (exit 70, "please report it"). CI6-D9.
        raise OutputError(f'Could not write {target}: {exc}') from exc
    logger.debug(f'Wrote {target}')


def display_path(path: Path) -> str:
    """Render a resolved output path the shortest honest way.

    A config-file ``output`` is anchored to the config file's directory (CI6-D5a), so it
    arrives absolute. Printing it relative to the cwd keeps the common case reading
    ``wrote out/schema.py``, while a run from a subdirectory — where the file genuinely
    lands somewhere else — still shows the full path rather than a misleading short one.

    It lives here rather than in :mod:`castiron.cli.gen` because ``castiron check`` names the
    same resolved paths in its drift report and must name them identically: a drift report that
    spelled a path differently from the ``gen`` line that wrote it would read like two files.

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
