"""The emitter abstraction: the ``Emitter`` base, ``EmittedFile``, and render helpers.

Every emitter (Pydantic here; SQLAlchemy in CI-012; msgspec/TypedDict/attrs and TS/Zod
later) implements :meth:`Emitter.emit`, turning a :class:`castiron.ir.Schema` into a list
of in-memory :class:`EmittedFile` values. There is **no** file I/O and **no**
nondeterminism (no timestamped filenames -- supabase-pydantic's ``AbstractFileWriter.save``
is intentionally not ported): writing to disk is CI-006's job, and byte-stable output is
load-bearing for the ``check`` drift-guard (Hard Rule #9).
"""

import re
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
#:
#: 🔴 **Adding an emitter? Extend this table.** This module is shared with every emitter (CI-012
#: SQLAlchemy, CI-030 the typed client, ...), and this list covers only what the **Pydantic**
#: emitter can currently reach. A new emitter's ``import uuid`` or
#: ``from collections.abc import Sequence`` would land in the third-party block and **silently
#: re-open I001** for its users. It is deliberately not pre-widened: an entry no importer reaches
#: is unfalsifiable, and speculative entries are how a table stops meaning anything.
#: ``tests/unit/emitters/test_base.py::test_every_emittable_import_names_a_classified_module``
#: enumerates the literals of **every** module under ``castiron/emitters/`` plus the whole type
#: map, so a new emitter's unclassified import fails that test rather than a user's linter.
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


def _natural_key(text: str) -> tuple[tuple[int, int, str], ...]:
    """Split ``text`` into digit and non-digit runs so ``Item2`` sorts before ``Item10``.

    isort compares names *naturally*, not lexicographically -- measured against ruff, which
    orders ``ITEM2, ITEM10`` where ``sorted()`` gives ``ITEM10, ITEM2``.

    Args:
        text: The string to key.

    Returns:
        A tuple of ``(is_text, numeric_value, text_value)`` triples -- **uniformly shaped**, so
        any two keys are comparable. The obvious ``tuple[int | str, ...]`` spelling of this trick
        raises ``TypeError`` the moment a digit run is compared against a text run, which is not
        a total order and would surface as a crash on some ``PYTHONHASHSEED`` values and not
        others (Hard Rule #9).
    """
    return tuple((0, int(run), '') if run.isdigit() else (1, 0, run) for run in re.split(r'(\d+)', text) if run)


def _member_sort_key(name: str) -> tuple[int, tuple[tuple[int, int, str], ...], str]:
    """Return isort's ``order-by-type`` sort key for one imported name.

    CONSTANT sorts before CLASS before everything else, then case-insensitively and *naturally*.
    This is why ``UUID4`` precedes ``BaseModel``; a plain alphabetical sort leaves the line
    ``I001``-dirty.

    ⚠ CONSTANT requires **more than one character**: ruff ranks a single-character name as a
    variable, so ``T`` sorts *last*, not first. Measured, not read -- ``from typing import T, Ab,
    ITEM2`` orders ``ITEM2, Ab, T``.

    Args:
        name: The imported symbol.

    Returns:
        A **total** key -- the final ``name`` component breaks any case-folding tie, so the order
        never depends on the iteration order of the set the names arrived in (Hard Rule #9).
    """
    if len(name) > 1 and name.isupper():
        rank = 0
    elif name[:1].isupper():
        rank = 1
    else:
        rank = 2
    return rank, _natural_key(name.lower()), name


def _split_import(line: str) -> tuple[str, tuple[str, ...]]:
    """Split one import line into ``(module, names)``; ``names`` is empty for ``import X``."""
    if line.startswith('from '):
        module, _, names = line.removeprefix('from ').partition(' import ')
        return module, tuple(name.strip() for name in names.split(','))
    return line.removeprefix('import ').strip(), ()


def render_import_block(imports: Iterable[str]) -> str:
    """Render a deduplicated, isort-compatible block of single-line imports.

    🔴 **Scope of the fidelity claim.** For **the import lines castiron's emitters actually
    produce**, the output is byte-identical to what ``ruff check --select I --fix`` writes under
    **default** settings, so a generated module does not trip the linter of the project it was
    just added to (``CI94-Q3``, captain's override). That is verified exhaustively rather than
    argued: the complete power set of the emittable vocabulary renders ``I001``-clean, under both
    ruff 0.6.9 (the pre-commit pin) and 0.16.0. It is **not** a general isort implementation, and
    the difference matters because this module is shared with every future emitter. Four rules,
    each derived by running ruff rather than by reading the isort documentation:

    1. Sections are ``__future__`` -> standard library -> third party, separated by exactly one
       blank line. :data:`STDLIB_MODULES` decides the middle one.
    2. Within a section, every plain ``import X`` precedes every ``from X import ...``
       (isort's ``force-sort-within-sections = false`` default).
    3. Same-module ``from`` imports are merged onto one line, and the names are ordered
       CONSTANT -> CLASS -> rest (``order-by-type``). See :func:`_member_sort_key`.
    4. Module and name ordering is case-insensitive and natural (``case-sensitive = false``).

    ⚠ **Three measured divergences from real isort, none reachable today.** Each is a *design*
    gap rather than a sort-key bug, and each is unreachable because castiron emits no such line
    -- ``test_base.py::TestDivergencesFromRealIsort`` asserts that unreachability rather than
    trusting it, so the guarantee fails loudly the day an emitter reaches one:

    - **An unlisted stdlib module lands in third party.** ``import uuid`` groups with ``pydantic``
      rather than with ``datetime``. See :data:`STDLIB_MODULES` -- extending it is the fix.
    - **Relative imports get no LOCALFOLDER section.** ``from .x import Y`` sorts into third party
      instead of a fourth block after it.
    - **``as`` aliases are merged, where ruff splits them.** ruff writes ``from m import a as b``
      on its own statement; this renders ``from m import a as b, c``.

    **Determinism (Hard Rule #9).** ``imports`` is routinely a ``set``, whose iteration order
    varies with ``PYTHONHASHSEED``. Grouping replaces one total sort over raw strings with three
    nested orderings, so every one of them is total by construction: sections are distinct ints;
    statements sort on the 4-tuple ``(kind, natural_key(module.lower()), module, statement)``,
    whose ``(kind, module)`` prefix is already unique once same-module lines are merged and whose
    trailing ``str`` components are comparable regardless; names sort on :func:`_member_sort_key`,
    whose last component is the name itself. No ``dict`` or ``set`` is ever iterated straight into
    the output -- ``CI-065`` is the precedent for what a non-total key costs.

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
        statements: list[tuple[int, tuple[tuple[int, int, str], ...], str, str]] = [
            (0, _natural_key(module.lower()), module, f'import {module}') for module in plain.get(section, set())
        ]
        statements.extend(
            (
                1,
                _natural_key(module.lower()),
                module,
                f'from {module} import {", ".join(sorted(names, key=_member_sort_key))}',
            )
            for module, names in grouped.get(section, {}).items()
        )
        if statements:
            blocks.append('\n'.join(statement for *_, statement in sorted(statements)))
    return '\n\n'.join(blocks)


#: The repository the header points a reader at. A URL, not a version-bearing link: it must stay
#: valid for a file generated by any castiron and read by any human.
_HEADER_URL = 'https://github.com/kmbhm1/castiron'

#: Line 1 of the provenance header, as a format template. ⚠ **This grammar is a permanent
#: contract.** A module generated by 0.5.0 must still be parseable by a much later
#: :func:`parse_header_version` -- that is the whole point of recording the version at all. The
#: version is the **last whitespace-delimited token of line 1, with no trailing punctuation**, so
#: the parse is a total function of one line and cannot be broken by rewording anything else.
_HEADER_LINE_1 = '{prefix} Generated by castiron {version}'

#: Line 2. Deliberately **not** parsed by anything: it may be reworded freely in any release.
_HEADER_LINE_2 = '{prefix} Do not edit by hand. Regenerate with `castiron gen` -- {url}'

#: The inverse of :data:`_HEADER_LINE_1`, as a pattern tail. Joined to an escaped ``comment_prefix``
#: per call rather than pre-compiled, because the prefix is the caller's.
_HEADER_VERSION_TAIL = r' Generated by castiron (?P<version>\S+)$'


def render_header(version: str, *, comment_prefix: str = '#') -> str:
    r"""Render the two-line provenance header that opens every emitted module.

    The header is the machine-readable record of *which castiron wrote this file*. Its consumer is
    ``castiron check`` (CI-021b), which reads it back with :func:`parse_header_version` to tell
    *"your schema moved"* apart from *"castiron's own output moved"* -- the false positive most
    likely to make a user disable a drift guard.

    **It is emitted bytes, so Hard Rule #9 binds it.** There is no timestamp, no hostname, no
    path, no source URL and no config summary in it: the source URL alone would write
    ``?apikey=...`` into a committed file, which is the exact hazard
    :func:`castiron.cli.errors.redact` exists for. The castiron version is the only varying input,
    and it is **injected** by the caller rather than read ambiently here, so a test can pin it.

    ⚠ **A comment block, never a module docstring.** A docstring would become the user's module
    ``__doc__``, and -- the load-bearing reason -- CPython 3.13 dedents docstrings at compile
    time, which is precisely the defect that shipped CI red on one leg and forced ``make
    validate`` onto the whole interpreter matrix (root ``CLAUDE.md``, CI-082). A comment has no
    such behaviour on any interpreter.

    Args:
        version: The castiron version to record, e.g. ``'0.5.0'``. Rendered verbatim as the last
            token of line 1; it must contain no whitespace for :func:`parse_header_version` to
            recover it.
        comment_prefix: The target language's line-comment marker. ``'#'`` for Python; a future
            TS/Zod emitter passes ``'//'`` rather than growing a second header mechanism.

    Returns:
        The two lines joined by ``\n``, with **no** trailing newline -- the same convention
        :func:`render_import_block` uses, so the caller owns the separator.
    """
    return '\n'.join(
        (
            _HEADER_LINE_1.format(prefix=comment_prefix, version=version),
            _HEADER_LINE_2.format(prefix=comment_prefix, url=_HEADER_URL),
        )
    )


def parse_header_version(text: str, *, comment_prefix: str = '#') -> str | None:
    r"""Recover the castiron version :func:`render_header` recorded, or ``None``.

    The exact inverse of :func:`render_header` over its first line, and **only** its first line:
    a file whose header sits anywhere else is not a castiron header. Line 2 is never read, which
    is what lets any future release reword it without orphaning files already in users' repos.

    ``None`` is a **first-class, expected outcome**, not an error: a hand-written module, a module
    generated before this header existed, and an empty file all return it, and a caller's correct
    response is to say "no recorded provenance", never to raise.

    ⚠ The input is expected to have been decoded in universal-newline mode (which
    :meth:`pathlib.Path.read_text` does), so a CRLF file arrives with ``\n`` line endings. A
    literal ``\r`` left on line 1 makes the parse return ``None`` rather than a version with a
    stray carriage return in it.

    Args:
        text: The full module text, or any prefix of it that includes the first line.
        comment_prefix: The comment marker the header was rendered with.

    Returns:
        The recorded version string, or ``None`` when line 1 is not a castiron header line.
    """
    match = re.match(re.escape(comment_prefix) + _HEADER_VERSION_TAIL, text.partition('\n')[0])
    return match.group('version') if match else None


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
