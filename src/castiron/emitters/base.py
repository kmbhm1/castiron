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


#: Standard-library modules castiron can emit an import for.
#:
#: ⚠ Deliberately an **explicit table** rather than :data:`sys.stdlib_module_names` (``CI94-D8``).
#: That set differs between 3.10 and 3.13, so classifying with it would make the emitted bytes a
#: function of the *running interpreter* -- a Hard Rule #9 violation the committed goldens would
#: surface only as a one-leg-red mystery. An unlisted module falls back to third-party, which is
#: the safe direction: a new stdlib import shows up as a wrongly-grouped line in a golden diff,
#: not as a green test on three legs and a red one on the fourth.
STDLIB_MODULES = frozenset({'datetime', 'decimal', 'enum', 'ipaddress', 'typing'})

#: Import section ranks, in emission order: ``__future__``, standard library, everything else.
_FUTURE_SECTION = 0
_STDLIB_SECTION = 1
_THIRD_PARTY_SECTION = 2


def _import_section(module: str) -> int:
    """Return the isort section rank for ``module`` (its top-level package decides)."""
    if module == '__future__':
        return _FUTURE_SECTION
    return _STDLIB_SECTION if module.partition('.')[0] in STDLIB_MODULES else _THIRD_PARTY_SECTION


def _member_sort_key(name: str) -> tuple[int, str, str]:
    """Return isort's ``order-by-type`` sort key for one imported name.

    CONSTANT (all upper-case) sorts before CLASS (leading upper-case) before everything else,
    then case-insensitively. This is why ``UUID4`` precedes ``BaseModel``; a plain alphabetical
    sort leaves the line ``I001``-dirty.

    Args:
        name: The imported symbol.

    Returns:
        A **total** key -- the final ``name`` component breaks any case-folding tie, so the order
        never depends on the iteration order of the set the names arrived in (Hard Rule #9).
    """
    if name.isupper():
        rank = 0
    elif name[:1].isupper():
        rank = 1
    else:
        rank = 2
    return rank, name.lower(), name


def _split_import(line: str) -> tuple[str, tuple[str, ...]]:
    """Split one import line into ``(module, names)``; ``names`` is empty for ``import X``."""
    if line.startswith('from '):
        module, _, names = line.removeprefix('from ').partition(' import ')
        return module, tuple(name.strip() for name in names.split(','))
    return line.removeprefix('import ').strip(), ()


def render_import_block(imports: Iterable[str]) -> str:
    """Render a deduplicated, isort-compatible block of single-line imports.

    The output matches what ``ruff check --select I`` produces under **default** settings, so a
    generated module does not trip the linter of the project it was just added to (``CI94-Q3``,
    captain's override). Four rules, each derived by running ruff rather than by reading the isort
    documentation:

    1. Sections are ``__future__`` -> standard library -> third party, separated by exactly one
       blank line. :data:`STDLIB_MODULES` decides the middle one.
    2. Within a section, every plain ``import X`` precedes every ``from X import ...``
       (isort's ``force-sort-within-sections = false`` default).
    3. Same-module ``from`` imports are merged onto one line, and the names are ordered
       CONSTANT -> CLASS -> rest (``order-by-type``). See :func:`_member_sort_key`.
    4. Module and name ordering is case-insensitive (``case-sensitive = false``).

    **Determinism (Hard Rule #9).** ``imports`` is routinely a ``set``, whose iteration order
    varies with ``PYTHONHASHSEED``. Grouping replaces one total sort over raw strings with three
    nested orderings, so every one of them is total by construction: sections are distinct ints;
    statements sort on ``(kind, module.lower(), module)``, unique once same-module lines are
    merged; names sort on :func:`_member_sort_key`, whose last component is the name itself. No
    ``dict`` or ``set`` is ever iterated straight into the output -- ``CI-065`` is the precedent
    for what a non-total key costs.

    Args:
        imports: An iterable of single import lines (e.g. ``'import datetime'`` or
            ``'from typing import Annotated, Any'``). Duplicates and repeated modules are fine.

    Returns:
        The rendered block, with no trailing newline. Empty input renders as ``''``.
    """
    plain: dict[int, set[str]] = {}
    grouped: dict[int, dict[str, set[str]]] = {}
    for line in set(imports):
        module, names = _split_import(line)
        section = _import_section(module)
        if names:
            grouped.setdefault(section, {}).setdefault(module, set()).update(names)
        else:
            plain.setdefault(section, set()).add(module)

    blocks: list[str] = []
    for section in (_FUTURE_SECTION, _STDLIB_SECTION, _THIRD_PARTY_SECTION):
        statements: list[tuple[int, str, str, str]] = [
            (0, module.lower(), module, f'import {module}') for module in plain.get(section, set())
        ]
        statements.extend(
            (1, module.lower(), module, f'from {module} import {", ".join(sorted(names, key=_member_sort_key))}')
            for module, names in grouped.get(section, {}).items()
        )
        if statements:
            blocks.append('\n'.join(statement for *_, statement in sorted(statements)))
    return '\n\n'.join(blocks)


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
