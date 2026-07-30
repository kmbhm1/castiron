"""The emitter abstraction: the ``Emitter`` base, ``EmittedFile``, and render helpers.

Every emitter (Pydantic here; SQLAlchemy in CI-012; msgspec/TypedDict/attrs and TS/Zod
later) implements :meth:`Emitter.emit`, turning a :class:`castiron.ir.Schema` into a list
of in-memory :class:`EmittedFile` values. There is **no** file I/O and **no**
nondeterminism (no timestamped filenames -- supabase-pydantic's ``AbstractFileWriter.save``
is intentionally not ported): writing to disk is CI-006's job, and byte-stable output is
load-bearing for the ``check`` drift-guard (Hard Rule #9).
"""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass

from castiron.ir import Schema


@dataclass(frozen=True)
class EmittedFile:
    """One generated file: a relative path and its full, byte-stable text.

    Attributes:
        path: The relative output path (the CLI in CI-006 resolves it to disk).
        content: The complete file text.
    """

    path: str
    content: str


class Emitter(ABC):
    """Base class for every code emitter.

    An emitter is a pure function of a :class:`castiron.ir.Schema`: same schema in,
    byte-identical files out, with no I/O and no nondeterministic ordering.
    """

    @abstractmethod
    def emit(self, schema: Schema) -> list[EmittedFile]:
        """Render the schema to one or more in-memory files.

        Args:
            schema: The schema to render (treated as read-only).

        Returns:
            The generated files, in a deterministic order.
        """
        raise NotImplementedError


def render_import_block(imports: Iterable[str]) -> str:
    """Render a deduplicated, sorted block of single-line imports.

    Args:
        imports: An iterable of single import lines (e.g. ``'import datetime'``).

    Returns:
        The imports joined by newlines, deduplicated and sorted for determinism.
    """
    return '\n'.join(sorted(set(imports)))


def _chunk_text(text: str, width: int = 78) -> list[str]:
    """Wrap ``text`` into lines no wider than ``width`` characters (word-preserving)."""
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        if current and sum(len(w) for w in current) + len(word) + len(current) > width:
            lines.append(' '.join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(' '.join(current))
    return lines


def section_comment(title: str, notes: list[str] | None = None) -> str:
    """Build an upper-cased ``# TITLE`` section header with optional wrapped notes.

    Args:
        title: The section title (rendered upper-case).
        notes: Optional description lines, word-wrapped under the header.

    Returns:
        The rendered comment block.
    """
    lines = [f'# {title.upper()}']
    for note in notes or []:
        lines.extend(f'# {chunk}' for chunk in _chunk_text(note))
    return '\n'.join(lines)
