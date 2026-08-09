"""The Pydantic v2 emitter -- supabase-pydantic's trust moat, ported onto the Schema IR.

``PydanticEmitter(config).emit(schema)`` renders a :class:`castiron.ir.Schema` into a
single Pydantic v2 module: enum classes, custom base classes, Base/Insert/Update ("Row"
+ CRUD) models, and operational classes that carry nested foreign-key relationship
fields (CI4-D-scope). Fidelity ported from supabase-pydantic's
``PydanticFastAPIClassWriter`` / ``PydanticFastAPIWriter``:

- per-column type resolution (jsonb, arrays, datetime, uuid, ...) via
  :func:`castiron.types.resolve_column_type`, with enum columns overlaid from
  ``col.enum_info``;
- ``text`` + ``length()`` CHECK constraints -> ``Annotated[str, StringConstraints(...)]``
  (constraints are parsed out of the single-column CHECK ``constraint_definition``, not
  ``max_length``);
- identity columns omitted from Insert/Update; defaults/nullable -> optional; Update ->
  all optional;
- ``model_``-prefix handling via ``ConfigDict(protected_namespaces=())``;
- foreign-key / relationship fields with singular vs pluralized names and self-ref
  handling.

The emitter treats the ``Schema`` as read-only (it never mutates ``fk.relation_type`` --
the self-ref effective type is computed in a local; supabase-pydantic mutates it) and its
output is deterministic (Hard Rule #9): tables in ``Schema.tables`` order, enums from the
IR's sorted ``schema.enums``, fields via ``sort_and_separate_columns``, imports grouped and
ordered exactly as ruff's isort would under default settings
(:func:`castiron.emitters.base.render_import_block`), ``from __future__ import annotations``
when relationship fields need forward references. castiron owns this output shape; it is not
byte-identical to supabase-pydantic.
"""

import json
import re
from collections.abc import Mapping, Sequence
from enum import Enum

from castiron import __version__
from castiron.emitters.base import (
    EmittedFile,
    Emitter,
    _split_import,
    render_header,
    render_import_block,
    section_comment,
)
from castiron.emitters.config import EmitterConfig
from castiron.ir import ColumnInfo, RelationType, Schema, SortedColumns, TableInfo
from castiron.ir.build import standardize_column_name
from castiron.types import PYDANTIC_TYPE_MAP, resolve_column_type
from castiron.utils.naming import (
    ClassStem,
    EnumClass,
    class_stem_reason,
    enum_class_reason,
    pluralize,
    python_class_name,
    python_class_names,
    python_class_stem,
    python_class_stems,
    python_member_names,
    singularize,
)

#: Indentation unit for generated code (4 spaces -- clean, PEP 8-style output).
IND = '    '
#: Name of the shared custom base model.
CUSTOM_MODEL_NAME = 'CustomModel'
#: Length-constraint pattern: ``length(col) <op> N`` inside a CHECK definition.
_LENGTH_PATTERN = r'length\((\w+)\)\s*([=<>]+)\s*(\d+)'
#: The one spelling of a ``Field`` call this emitter produces. :meth:`PydanticEmitter._imports`
#: searches the rendered body for it to decide whether ``Field`` is imported at all (``CI94-D9``).
#:
#: ⚠ **Every site that renders a ``Field`` call goes through :func:`_field_call`, so this literal
#: and the emitted text cannot drift apart.** They were separate literals in three places; a
#: refactor touching one and not this one would emit a module that uses ``Field`` without
#: importing it. The corpus ruff sweep does catch that (as ``F821``), but a constant that is
#: *structurally* the same string is better than a constant a test happens to notice.
_FIELD_CALL = '= Field('

#: Names the import block binds unconditionally on its own account, independent of the type map.
#: Every ``imports.add`` / initialiser site in :meth:`PydanticEmitter._imports` contributes exactly
#: one of these.
_FIXED_IMPORT_BOUND_NAMES = frozenset(
    {'annotations', 'Annotated', 'BaseModel', 'ConfigDict', 'Enum', 'Field', 'StringConstraints'}
)


def _type_map_bound_names() -> frozenset[str]:
    """Every name the per-column type-map imports can bind (``datetime``, ``Decimal``, ``UUID4``...).

    ⚠ **Derived from** :data:`~castiron.types.PYDANTIC_TYPE_MAP` **rather than hand-listed, so it
    cannot rot when the map grows.** This is the eighth contributor to the import block and the
    easiest to forget: it is a single ``imports.update`` in a loop rather than a literal
    ``imports.add``, and a hand-written constant that omitted it would make
    :data:`_IMPORT_BOUND_NAMES` quietly untrue for any schema with a ``numeric``, ``uuid``,
    ``jsonb`` or ``inet`` column.

    Returns:
        The bound names, parsed from the map's import lines with the same splitter
        :func:`~castiron.emitters.base.render_import_block` renders them with.
    """
    names: set[str] = set()
    for resolution in PYDANTIC_TYPE_MAP.values():
        for line in resolution.imports:
            module, imported = _split_import(line)
            names.update(imported if imported else (module,))
    return frozenset(names)


#: **Every** top-level name the emitted module's import block can bind, over every configuration.
#:
#: 🔴 Seeded into :meth:`PydanticEmitter._reserved_class_names` so an enum class can never shadow an
#: import. The degenerate enum ``EnumInfo(name='', schema='')`` is named ``Enum`` by
#: :func:`~castiron.utils.naming.python_class_name` and would otherwise rebind
#: ``from enum import Enum`` -- taking the base class of every other enum in the module with it.
#:
#: ⚠ **A restatement, cross-checked against reality in a test** (the ``CI94-D8`` pattern):
#: ``test_the_import_bound_names_constant_covers_a_maximal_emission`` AST-parses the import block of
#: a maximally-configured emission and asserts every bound name appears here, so the constant fails
#: loudly rather than silently going stale.
_IMPORT_BOUND_NAMES = _FIXED_IMPORT_BOUND_NAMES | _type_map_bound_names()


def _field_call(arguments: str) -> str:
    """Render a ``= Field(...)`` suffix from its already-formatted argument text.

    Args:
        arguments: The comma-joined keyword arguments, e.g. ``'default=None'``.

    Returns:
        The rendered call, always beginning with :data:`_FIELD_CALL`.
    """
    return f'{_FIELD_CALL}{arguments})'


class _ClassVariant(Enum):
    """The variant of a generated model class."""

    BASE = 'base'
    BASE_WITH_PARENT = 'base_with_parent'
    PARENT = 'parent'
    INSERT = 'insert'
    UPDATE = 'update'


#: The suffix each variant appends to a table's class stem, in one place so
#: :meth:`PydanticEmitter._write_name` and :data:`_STEM_SUFFIXES` cannot disagree about what a stem
#: binds. ``BASE`` and ``BASE_WITH_PARENT`` deliberately share a suffix: they differ only in which
#: class they inherit from.
_VARIANT_SUFFIX: Mapping[_ClassVariant, str] = {
    _ClassVariant.BASE: 'BaseSchema',
    _ClassVariant.BASE_WITH_PARENT: 'BaseSchema',
    _ClassVariant.PARENT: 'Parent',
    _ClassVariant.INSERT: 'Insert',
    _ClassVariant.UPDATE: 'Update',
}

#: 🔴 **Every top-level class name ONE table stem binds.** ``''`` is the operational class, which
#: carries the bare stem (``class Orders(OrdersBaseSchema):``).
#:
#: This is what makes the table allocator a *stem* allocator rather than a name allocator: a stem is
#: free only when all five derived names are. Measured on ``main``, the tables ``order`` and
#: ``order_insert`` have obviously distinct stems and still emit **two** ``class OrderInsert``
#: definitions -- the insert model of one and the operational class of the other -- with the second
#: silently winning the binding.
#:
#: ⚠ Derived from :data:`_VARIANT_SUFFIX` rather than hand-listed (``dict.fromkeys`` dedupes the
#: shared ``BaseSchema`` and preserves literal order, so it is deterministic), and cross-checked
#: against :meth:`PydanticEmitter._write_name` by a test -- the ``CI94-D8`` pattern: restate the
#: rule in ``src/``, falsify it against reality in a test that runs on all four legs.
_STEM_SUFFIXES: tuple[str, ...] = ('', *dict.fromkeys(_VARIANT_SUFFIX.values()))


def _parse_length_constraint(constraint_def: str | None) -> dict[str, int] | None:
    """Parse ``length(col) <op> N`` clauses from a CHECK definition.

    Args:
        constraint_def: The single-column CHECK constraint definition, or ``None``.

    Returns:
        A dict with ``min_length`` and/or ``max_length``, or ``None`` if none are found.
    """
    if not constraint_def:
        return None

    matches = re.findall(_LENGTH_PATTERN, constraint_def)
    if not matches:
        return None

    result: dict[str, int] = {}
    for _, operator, raw_value in matches:
        value = int(raw_value)
        if operator == '=':
            result['min_length'] = value
            result['max_length'] = value
        elif operator == '>=':
            result['min_length'] = value
        elif operator == '<=':
            result['max_length'] = value

    return result or None


def _py_string(value: str) -> str:
    r"""Render ``value`` as a double-quoted, single-line Python string literal.

    JSON's escape alphabet is a strict subset of Python's, so ``json.dumps`` produces a
    literal Python accepts verbatim -- including for newlines, carriage returns, tabs and
    C0 control characters. The previous hand-rolled escaping covered only ``\`` and ``"``
    and emitted the rest raw, so a multi-line ``COMMENT ON COLUMN`` reached
    ``Field(description=...)`` as an unterminated string literal and the generated module
    did not parse. ``ensure_ascii=False`` keeps non-ASCII text readable; the CLI writes the
    file as UTF-8 with LF endings (``cli/output.py``).

    Note this is not a strict no-op against the old helper: a TAB now renders as ``\\t``
    where it used to be emitted raw (both parse; the escaped form is the correct one). No
    committed golden contains a tab, a newline or a control character, so no golden moves.
    """
    return json.dumps(value, ensure_ascii=False)


def _docstring_text(description: str | None) -> str | None:
    r"""Render a table's SQL comment as an indented, escaped docstring paragraph.

    A SQL comment is arbitrary user text being injected into a Python source file, so this
    has to be **total**, not merely correct for well-behaved input (the standing CI-063
    lesson). Each rule earns its place:

    - **CRLF and lone CR are folded to LF first.** The builder already normalizes what it
      stores, but this keeps the *renderer* total for a hand-built ``TableInfo``, and a CR
      in generated output is a byte-stability hazard across platforms (Hard Rule #9).
    - **Backslashes are doubled before anything else.** Otherwise ``C:\temp\new`` renders a
      real tab and newline inside the docstring value, ``\d+`` raises
      ``SyntaxWarning: invalid escape sequence`` on 3.12+, and a *trailing* backslash
      escapes the closing delimiter.
    - **Every** ``"`` **is escaped, unconditionally.** A comment containing ``\"\"\"`` would
      otherwise terminate the docstring early -- an injection that breaks the generated
      module. Escaping all of them makes ``\"\"\"``, ``\"\"\"\"`` and a trailing quote safe with
      no position-dependent cleverness. The cost is source prettiness: the *file* reads
      ``\\\"app\\\"`` while the runtime ``__doc__`` is exactly ``"app"``, which is what
      mkdocstrings/Sphinx/IDE hover show.
    - ⚠ **Split on** ``'\n'``, **never** :meth:`str.splitlines`. ``splitlines`` also breaks on
      ``\x0b``, ``\x0c``, ``\x1c``-``\x1e``, ``\x85``, ``\u2028`` and ``\u2029``, every one of
      which is legal inside a Postgres comment. Using it would silently re-indent such a
      comment -- nondeterministic-looking output from valid input. Do not "simplify" this.
    - **Blank lines render truly empty** (no indent), so generated code carries no trailing
      whitespace (``W291`` for the user, and pure diff noise).
    - **Content lines are indented and right-stripped, never left-stripped**, so the
      comment's own relative indentation (lists, code blocks) survives.

    - ⚠ **NUL is removed, and it is the only character that is.** ``U+0000`` is the one
      code point that no amount of escaping saves: a raw NUL anywhere in a module makes
      CPython raise ``SyntaxError: source code string cannot contain null bytes`` at import,
      so castiron would *write* the file successfully and the user's import would fail --
      the exact failure shape the ``_py_string`` fix exists to prevent. It is stripped
      rather than rendered as a visible ``\x00`` (which would inject four characters the
      user never wrote) or as a real NUL escape (which would merely relocate the NUL into
      every consumer of ``__doc__``). **Stripping is also the only option that preserves
      decision D6:** it lets a NUL-only comment collapse to "no comment", so it stays
      indistinguishable from an absent one. Nothing is lost from the system of record --
      the builder does not strip it, so ``TableInfo.description`` and ``Schema.as_dict()``
      still carry the NUL.

    Every other control character and all non-ASCII are deliberately *not* escaped: they
    compile inside a triple-quoted literal and round-trip to the right ``__doc__``, and the
    CLI writes UTF-8 with LF (``cli/output.py``).

    ⚠ Postgres text cannot contain NUL, so a PostgREST document never carries one -- but
    **that is a property of one source, not of the input**. The OpenAPI source accepts any
    JSON document via ``--from``, a NUL is perfectly expressible as the JSON escape
    ``\u0000``, and a future source may be less disciplined. The renderer must be total over
    its actual input domain, not over the domain its best-behaved caller happens to supply.

    Args:
        description: The table's SQL comment, or ``None``.

    Returns:
        The indented, escaped paragraph, or ``None`` when there is nothing to render -- so
        an absent, empty, whitespace-only or NUL-only comment all produce byte-identical
        output.
    """
    if description is None:
        return None
    # NUL removal precedes `.strip()` so a NUL-only or NUL-padded comment collapses to
    # "no comment" (decision D6) rather than to a body of invisible characters.
    text = description.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '').strip()
    if not text:
        return None
    escaped = text.replace('\\', '\\\\').replace('"', '\\"')
    return '\n'.join(f'{IND}{line.rstrip()}' if line.strip() else '' for line in escaped.split('\n'))


def _class_docstring(summary: str, description: str | None, trailer: str | None = None) -> str:
    """Assemble a class docstring from its summary, optional description and optional trailer.

    The description is inserted as the **first body paragraph, after the summary line**; it
    never replaces the summary. The summary is castiron's statement about the *class*
    (``UsersBaseSchema`` vs ``UsersInsert`` vs ``UsersUpdate``), while the comment is the
    user's statement about the *table* -- replacing it would give every variant an identical
    docstring and destroy the only line saying which one you are reading. Keeping it also
    makes the golden diff pure insertion, so a reviewer can verify "nothing was rewritten"
    from the deletion count alone.

    Args:
        summary: The one-line summary (already ending in a period).
        description: The table's SQL comment, or ``None``.
        trailer: A trailing boilerplate paragraph (already indented), or ``None``.

    Returns:
        The complete, indented docstring including its delimiters.
    """
    paragraphs = [p for p in (_docstring_text(description), trailer) if p]
    if not paragraphs:
        return f'{IND}"""{summary}"""'
    body = '\n\n'.join(paragraphs)
    return f'{IND}"""{summary}\n\n{body}\n{IND}"""'


class PydanticEmitter(Emitter):
    """Emit Pydantic v2 models from a :class:`castiron.ir.Schema`."""

    def __init__(self, config: EmitterConfig | None = None, *, tool_version: str = __version__) -> None:
        """Initialize the emitter.

        🔴 ``tool_version`` is a **keyword-only constructor parameter and deliberately not an**
        :class:`EmitterConfig` **field.** ``tests/unit/corpus/cases.py::config_axes()`` derives the
        corpus sweep from ``dataclasses.fields(EmitterConfig)`` and assigns a **bool** to every
        axis, so a new field would widen the sweep from 128 to 256 points per family *and* build
        ``EmitterConfig(tool_version=True)`` -- twice the cost, for nonsense. It is also not a
        behavioural toggle: it changes one token of one comment line, never the code.

        ``EmitterSpec.build: Callable[[EmitterConfig], Emitter]`` still matches, because a
        keyword-only parameter with a default does not change the call signature. **The CLI never
        passes it** -- production always records the installed version. The tests do, so no
        committed golden embeds a version ``python-semantic-release`` rewrites.

        Args:
            config: Behavioral toggles; defaults to :class:`EmitterConfig` defaults.
            tool_version: The castiron version recorded in the emitted module's provenance header.
                Defaults to the installed :data:`castiron.__version__`.
        """
        self.config = config if config is not None else EmitterConfig()
        self.tool_version = tool_version

    def emit(self, schema: Schema) -> list[EmittedFile]:
        """Render ``schema`` to a single in-memory :class:`EmittedFile`.

        Args:
            schema: The schema to render (read-only).

        Returns:
            A one-element list: the emitted module at ``config.output_filename``.
        """
        return [EmittedFile(self.config.output_filename, self._write(schema))]

    # ------------------------------------------------------------------ file assembly

    def _write(self, schema: Schema) -> str:
        """Assemble the full module text from its sections.

        The **body is rendered first** and the import block is computed from it (``CI94-D9``), so
        a conditional import is exact rather than re-derived by a predicate that would drift from
        the renderer that made it necessary.

        ⚠ The two-line provenance header (:func:`~castiron.emitters.base.render_header`) precedes
        ``from __future__ import annotations``, separated from it by one blank line. That is legal
        and not a near-miss: ``__future__`` must be the first *statement*, and a comment is not a
        statement. It is also why the header is a comment rather than a module docstring -- a
        docstring there would both become the user's ``__doc__`` and displace the future import.

        ⚠ The import block is joined to the body with **one** blank line, while body sections are
        joined with two. That asymmetry is not a typo: the section after the imports always begins
        with a ``#`` comment, and ruff's isort accepts exactly one blank line before a comment and
        exactly two before code. Emitting two here put ``I001`` in every module castiron has ever
        written. ``ruff format`` agrees with the one-blank form, so there is no check-vs-format
        conflict to trade off.

        🔴 **The class stems and the enum class names are resolved exactly ONCE here and threaded
        to every consumer.** A table's stem reaches five class headers, every ``Inherits from …``
        docstring, and the type annotation of every relationship field pointing at that table; the
        enum class name reaches its header and every column annotation. The moment a collision rule
        can allocate ``OrderLines_2`` or ``…Enum_2``, two independent derivations of one name is a
        module that does not import. That is ``CI-114`` exactly, and the fix is the same shape: one
        derivation, read many times.

        ⚠ Both maps are **locals**, not per-render state on ``self``. The emitter holds only
        ``config`` and treats the ``Schema`` as read-only; giving it mutable per-render state would
        make a reused emitter instance order-dependent.
        """
        stems: Mapping[str, ClassStem] = self._resolved_stems(schema)
        enum_classes: Sequence[EnumClass] = self.enum_classes(schema)
        enum_names: Mapping[tuple[str, str], str] = {
            (entry.enum.schema, entry.enum.name): entry.name for entry in enum_classes
        }

        sections: list[str] = []
        enum_section = self._enum_section(enum_classes)
        if enum_section:
            sections.append(enum_section)
        sections.append(self._custom_section())
        base_section = self._base_section(schema, enum_names, stems)
        if base_section:
            sections.append(base_section)
        operational_section = self._operational_section(schema, stems)
        if operational_section:
            sections.append(operational_section)
        body = '\n\n\n'.join(sections)
        header = render_header(self.tool_version)
        return f'{header}\n\n{self._imports(schema, body)}\n\n{body}\n'

    def _imports(self, schema: Schema, body: str) -> str:
        """Build the grouped, deduplicated import block for the module.

        Args:
            schema: The schema being emitted.
            body: The already-rendered module body. ``Field`` is imported iff the body actually
                calls it, which is what keeps ``castiron gen --no-crud-models
                --no-null-parent-classes`` on an all-NOT-NULL schema from shipping an ``F401``
                (measured: 32 of the 512 reachable emissions). Reading the rendered text rather
                than re-deriving ``_render_column``'s conditions is deliberate (``CI94-D9``): one
                source of truth, and it generalizes to any future conditional import. Its single
                failure mode is **conservative** -- a column comment containing the literal
                ``= Field(`` imports ``Field`` unnecessarily, which costs a lint finding and
                never a broken module.

        Returns:
            The rendered import block.
        """
        imports = {'from pydantic import BaseModel'}

        if _FIELD_CALL in body:
            imports.add('from pydantic import Field')

        if self.config.include_foreign_keys and any(t.foreign_keys or t.relationships for t in schema.tables):
            imports.add('from __future__ import annotations')

        if self._emits_enums(schema):
            imports.add('from enum import Enum')

        if self.config.disable_model_prefix_protection and any(
            self._has_model_prefix_columns(t) for t in schema.tables
        ):
            imports.add('from pydantic import ConfigDict')

        if self._needs_string_constraints(schema):
            imports.add('from typing import Annotated')
            imports.add('from pydantic import StringConstraints')

        for table in schema.tables:
            for column in table.columns:
                if column.enum_info is not None:
                    continue
                imports.update(resolve_column_type(column, PYDANTIC_TYPE_MAP).imports)

        return render_import_block(imports)

    def _emits_enums(self, schema: Schema) -> bool:
        """Whether the module will carry enum classes -- the ONE condition, read by both sites.

        ⚠ :meth:`_imports` and :meth:`_enum_section` used to test different things: the import
        block gated ``from enum import Enum`` on a **column** carrying ``enum_info``, while the
        section renders from ``schema.enums``. An enum reachable only through the registry
        therefore emitted its class with no import -- ``NameError: name 'Enum' is not defined`` at
        import, with ``castiron gen`` exiting 0 (``CI-114``). Two sources of truth for one import
        is the defect; one predicate is the fix.

        Args:
            schema: The schema being emitted.

        Returns:
            ``True`` when :meth:`_enum_section` will render at least one enum class.
        """
        return bool(self.config.generate_enums and schema.enums)

    def _needs_string_constraints(self, schema: Schema) -> bool:
        """Whether any ``text`` column carries a ``length()`` CHECK constraint."""
        return any(
            column.raw_type.lower() == 'text'
            and column.constraint_definition is not None
            and 'length(' in column.constraint_definition.lower()
            for table in schema.tables
            for column in table.columns
        )

    # ------------------------------------------------------------------ class-name allocation

    def _module_reserved_names(self) -> set[str]:
        """Every top-level name the module binds independently of any table or enum.

        The import block plus the ``CustomModel`` bases. Seeded into **both** allocators, so a table
        named ``custom_model`` or ``field`` cannot rebind a name the module already needs. Measured
        on ``main``, a table called ``custom_model`` emits ``class CustomModelInsert(CustomModelInsert)``
        and rebinds every one of the three shared bases.

        Returns:
            The reserved names. Consumed by membership only -- never iterated into the output, so it
            cannot reach the emitted bytes (Hard Rule #9).
        """
        return set(_IMPORT_BOUND_NAMES) | {
            CUSTOM_MODEL_NAME,
            f'{CUSTOM_MODEL_NAME}Insert',
            f'{CUSTOM_MODEL_NAME}Update',
        }

    def class_stems(self, schema: Schema) -> list[ClassStem]:
        """Resolve the class stem for every table this module will emit -- the **single authority**.

        Public for the same reason :meth:`enum_classes` is: the CLI's fidelity notices
        (:mod:`castiron.cli.notices`) tell the user which tables were renamed, and they must report
        the stems actually written rather than re-deriving them (``CI-114``).

        ⚠ **Deduplicated on the raw table name, in ``Schema.tables`` order.** The emitter's whole
        relationship layer is name-keyed and schema-blind (``rel.related_table_name``,
        ``fk.foreign_table_name``, the self-reference test), so allocating per ``(schema, name)``
        would hand the second of two same-named tables a stem nothing could ever point at.
        Same-named tables in two Postgres schemas therefore still share one set of model classes --
        **unchanged from ``main``**, unreachable through the OpenAPI source (``build_schema`` reads
        one schema), and reported rather than fixed here.

        Args:
            schema: The schema being emitted (treated as read-only).

        Returns:
            One :class:`~castiron.utils.naming.ClassStem` per distinct table name.
        """
        return python_class_stems(
            list(dict.fromkeys(table.name for table in schema.tables)),
            singular_names=self.config.singular_names,
            suffixes=_STEM_SUFFIXES,
            reserved=self._module_reserved_names(),
        )

    def _resolved_stems(self, schema: Schema) -> dict[str, ClassStem]:
        """Index :meth:`class_stems` by table name, for the lookups the render path does."""
        return {entry.source: entry for entry in self.class_stems(schema)}

    # ------------------------------------------------------------------ enum classes

    def enum_classes(self, schema: Schema) -> list[EnumClass]:
        """Resolve the class name for every enum this module will emit -- the **single authority**.

        Public because it has a second consumer outside the emitter: the CLI's fidelity notices
        (:mod:`castiron.cli.notices`) tell the user which enum types were renamed, and they must
        report the names actually written rather than re-deriving them. One derivation, two
        readers -- re-deriving is the defect ``CI-114`` was.

        Empty exactly when :meth:`_enum_section` renders nothing, because it is gated on the same
        :meth:`_emits_enums` predicate: one condition, read everywhere (``CI-114``'s fix, preserved).

        Args:
            schema: The schema being emitted (treated as read-only).

        Returns:
            One :class:`~castiron.utils.naming.EnumClass` per registry entry, positionally aligned
            with ``schema.enums``, or an empty list when no enum classes will be emitted.
        """
        if not self._emits_enums(schema):
            return []
        return python_class_names(schema.enums, self._reserved_class_names(schema))

    def _reserved_class_names(self, schema: Schema) -> set[str]:
        """Every other top-level name the emitted module binds, so an enum class cannot shadow one.

        The enum namespace is not private: an enum class name can equal a **table model** class name
        (``EnumInfo('status', schema='order')`` and ``TableInfo('order_status_enum')`` both want
        ``OrderStatusEnum``), and it can equal an **import** (``EnumInfo(name='', schema='')`` is
        named ``Enum``). Measured on ``main``, the model won and ``OrderStatusEnum('open')`` returned
        a pydantic model.

        ⚠ **All four table variants are seeded regardless of what this config will actually emit,
        deliberately.** Seeding only the emitted ones would make an enum's class name depend on
        ``--no-crud-models`` / ``--no-null-parent-classes`` -- an unrelated flag silently renaming a
        user's enum class. Over-seeding costs nothing (a name nobody emits simply cannot be taken)
        and buys flag-independence.

        ⚠⚠ **Built by calling :meth:`_class_name` / :meth:`_write_name`, never by re-deriving the
        strings.** That is what made ``CI-130`` (sanitizing table stems) a safe follow-up rather
        than a merge conflict, and it is now load-bearing rather than merely prudent: those two
        methods read the resolved stems, so **``OrderLines_2`` reaches this seed automatically** and
        an enum can neither shadow a renamed model class nor be pushed aside by a stem the module
        never binds. It is also why the seed is computed *after* the stems: table stems are the
        stable anchor a user's imports point at, and enums yield to them.

        Args:
            schema: The schema being emitted (treated as read-only).

        Returns:
            The reserved names. Consumed by membership only -- never iterated into the output, so it
            cannot reach the emitted bytes (Hard Rule #9).
        """
        stems = self._resolved_stems(schema)
        reserved = self._module_reserved_names()
        for table in schema.tables:
            reserved.add(self._class_name(table, stems))
            reserved.update(self._write_name(table, variant, stems) for variant in _ClassVariant)
        return reserved

    def _enum_section(self, enum_classes: Sequence[EnumClass]) -> str | None:
        r"""Render the enum classes from the IR's deduplicated, sorted enum registry.

        The member **name** comes from :func:`~castiron.utils.naming.python_member_names`, which
        takes the whole enum rather than one label at a time -- a collision rule is not
        expressible per value (``CI94-D1``). The member **value** is the label rendered through
        :func:`_py_string` and is always exact, so the name transform is never lossy.

        A comment is emitted only when the name is *not* the straight transform of the label
        (``CI94-D3``): the value literal sits on the same line and already is the label, so
        glossing every member would be bytes in every user's file forever. ⚠ The label in that
        comment goes through :func:`_py_string` too. That is not decoration -- after CI-080 the
        guards fire on the *sanitized* name, so a label whose raw text carries a newline reaches
        them: ``'\\n\\ndoc\\n\\n'`` maps to ``__DOC___`` via the enum-shape guard, and two labels
        that sanitize alike put the newline-bearing one in a collision comment. A raw label would
        split the ``#`` comment across lines and break the module, which is CI-009's standing
        lesson: a renderer injecting user text into generated source must be total over its input
        domain, not over its best-behaved caller.

        🔴 **The class header carries an** ``# original name was …`` **comment too, whenever the
        emitted class name is not the straight transform of the Postgres type name** (``CI-128``).
        Unlike a member line, a class header carries **nothing** of the source: after sanitization
        the Postgres type name is otherwise unrecoverable from the module, so the comment is what
        keeps the transform non-lossy. The same ``_py_string`` escaping applies for the same reason,
        and the reason text (:func:`~castiron.utils.naming.enum_class_reason`) is composed once and
        shared with the CLI notice so the two cannot say different things.

        ⚠ It cannot move a committed golden: the note is ``None`` for every name that is already a
        valid identifier and uncollided, which is every enum in the corpus.

        Args:
            enum_classes: The resolved classes from :meth:`enum_classes` -- **not** ``schema.enums``.
                Empty means no enum section, which is the same condition :meth:`_imports` gates the
                ``from enum import Enum`` line on (``CI-114``: one predicate, read by both sites).

        Returns:
            The rendered section, or ``None`` when there are no enum classes.
        """
        if not enum_classes:
            return None

        classes = []
        for entry in enum_classes:
            lines = []
            reason = enum_class_reason(entry, _py_string)
            if reason is not None:
                source_name = _py_string(f'{entry.enum.schema}.{entry.enum.name}')
                lines.append(f'# original name was {source_name} ({reason})')
            lines.append(f'class {entry.name}(str, Enum):')
            members = python_member_names(entry.enum, entry.name)
            for member in members:
                comment = ''
                if member.note is not None:
                    comment = f'  # original name was {_py_string(member.label)} ({member.note})'
                lines.append(f'{IND}{member.name} = {_py_string(member.label)}{comment}')
            if not members:
                # ⚠ `CREATE TYPE t AS ENUM ()` is legal Postgres and PostgREST reports it as
                # `"enum": []`, which reached here and emitted a class header with NO BODY --
                # an `IndentationError`, at exit 0, taking the whole module with it. Same class
                # of defect as CI-080 and reachable through the real source path.
                #
                # `pass` rather than skipping the class: the column that carries the type still
                # annotates itself with this name, so omitting it would trade an
                # `IndentationError` for a `NameError`. An empty `Enum` subclass is valid and is
                # the honest rendering of an empty Postgres enum.
                lines.append(f'{IND}pass')
            classes.append('\n'.join(lines))

        comment = section_comment('Enum Types', ['These are generated from Postgres user-defined enum types.'])
        return '\n\n\n'.join([comment, *classes])

    # ------------------------------------------------------------------ custom bases

    def _custom_section(self) -> str:
        """Render the shared ``CustomModel`` (+ Insert/Update) base classes."""
        classes = [f'class {CUSTOM_MODEL_NAME}(BaseModel):\n{IND}"""Base model class with common features."""']
        if self.config.generate_crud_models:
            classes.append(
                f'class {CUSTOM_MODEL_NAME}Insert({CUSTOM_MODEL_NAME}):\n'
                f'{IND}"""Base model for insert operations with common features."""'
            )
            classes.append(
                f'class {CUSTOM_MODEL_NAME}Update({CUSTOM_MODEL_NAME}):\n'
                f'{IND}"""Base model for update operations with common features."""'
            )
        comment = section_comment(
            'Custom Classes',
            ['Custom model classes defining common features shared by the generated schemas.'],
        )
        return '\n\n\n'.join([comment, *classes])

    # ------------------------------------------------------------------ base/CRUD classes

    def _base_section(
        self, schema: Schema, enum_names: Mapping[tuple[str, str], str], stems: Mapping[str, ClassStem]
    ) -> str | None:
        """Render the Parent (optional), Base, Insert, and Update class sections."""
        subsections: list[str] = []

        if self.config.add_null_parent_classes:
            parent_classes = [self._render_class(t, _ClassVariant.PARENT, enum_names, stems) for t in schema.tables]
            if parent_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Parent Classes',
                                ['All fields nullable; useful for refining models via inheritance.'],
                            ),
                            *parent_classes,
                        ]
                    )
                )
            base_variant = _ClassVariant.BASE_WITH_PARENT
        else:
            base_variant = _ClassVariant.BASE

        base_classes = [self._render_class(t, base_variant, enum_names, stems) for t in schema.tables]
        if base_classes:
            subsections.append(
                '\n\n\n'.join(
                    [
                        section_comment('Base Classes', ['These are the base Row models that include all fields.']),
                        *base_classes,
                    ]
                )
            )

        if self.config.generate_crud_models:
            insert_classes = [self._render_class(t, _ClassVariant.INSERT, enum_names, stems) for t in schema.tables]
            if insert_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Insert Classes',
                                ['Models for insert operations; auto-generated fields are optional.'],
                            ),
                            *insert_classes,
                        ]
                    )
                )
            update_classes = [self._render_class(t, _ClassVariant.UPDATE, enum_names, stems) for t in schema.tables]
            if update_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Update Classes', ['Models for update operations; all fields are optional.']
                            ),
                            *update_classes,
                        ]
                    )
                )

        return '\n\n\n'.join(subsections) if subsections else None

    def _render_class(
        self,
        table: TableInfo,
        variant: _ClassVariant,
        enum_names: Mapping[tuple[str, str], str],
        stems: Mapping[str, ClassStem],
    ) -> str:
        """Render one model class (header, docstring, optional config, body sections)."""
        null_defaults = variant == _ClassVariant.PARENT
        name = self._write_name(table, variant, stems)
        metaclass = self._metaclass(table, variant, stems)
        header = self._stem_header(f'class {name}({metaclass}):', table, stems)
        docstring = _class_docstring(
            f'{self._class_name(table, stems)} {self._qualifier(variant)} Schema.', table.description
        )

        blocks: list[str] = []
        if self.config.disable_model_prefix_protection and self._has_model_prefix_columns(table):
            blocks.append(f'{IND}model_config = ConfigDict(protected_namespaces=())')
        blocks.extend(self._body_sections(table, variant, null_defaults, enum_names))

        if blocks:
            return f'{header}\n{docstring}\n\n' + '\n\n'.join(blocks)
        return f'{header}\n{docstring}'

    def _body_sections(
        self,
        table: TableInfo,
        variant: _ClassVariant,
        null_defaults: bool,
        enum_names: Mapping[tuple[str, str], str],
    ) -> list[str]:
        """Render the column sections (primary keys, columns, required/optional) for a class."""
        separated: SortedColumns = table.sort_and_separate_columns(separate_primary_key=True)

        if variant in (_ClassVariant.INSERT, _ClassVariant.UPDATE):
            return self._crud_sections(separated, variant, enum_names)

        sections: list[str] = []
        pk = self._column_section(
            'Primary Keys', [self._render_column(c, variant, null_defaults, enum_names) for c in separated.primary_keys]
        )
        if pk:
            sections.append(pk)
        cols = self._column_section(
            'Columns', [self._render_column(c, variant, null_defaults, enum_names) for c in separated.remaining]
        )
        if cols:
            sections.append(cols)
        return sections

    def _crud_sections(
        self, separated: SortedColumns, variant: _ClassVariant, enum_names: Mapping[tuple[str, str], str]
    ) -> list[str]:
        """Render the Insert/Update column sections (identity omitted; optionality bucketed)."""
        sections: list[str] = []

        pk = self._column_section(
            'Primary Keys',
            [self._render_column(c, variant, False, enum_names) for c in separated.primary_keys if not c.is_identity],
        )
        if pk:
            sections.append(pk)

        if variant == _ClassVariant.UPDATE:
            cols = self._column_section(
                'Columns',
                [self._render_column(c, variant, False, enum_names) for c in separated.remaining if not c.is_identity],
            )
            if cols:
                sections.append(cols)
            return sections

        required: list[ColumnInfo] = []
        optional: list[ColumnInfo] = []
        for column in separated.remaining:
            if column.is_identity:
                continue
            if column.has_default or column.is_generated or column.is_nullable:
                optional.append(column)
            else:
                required.append(column)

        req = self._column_section(
            'Required fields', [self._render_column(c, variant, False, enum_names) for c in required]
        )
        if req:
            sections.append(req)
        opt = self._column_section(
            'Optional fields', [self._render_column(c, variant, False, enum_names) for c in optional]
        )
        if opt:
            sections.append(opt)
        return sections

    def _column_section(self, title: str, columns: list[str]) -> str | None:
        """Render a titled, indented block of column lines, or ``None`` when empty."""
        rendered = [c for c in columns if c]
        if not rendered:
            return None
        body = '\n'.join(f'{IND}{c}' for c in rendered)
        return f'{IND}# {title}\n{body}'

    def _render_column(
        self,
        column: ColumnInfo,
        variant: _ClassVariant,
        null_defaults: bool,
        enum_names: Mapping[tuple[str, str], str],
    ) -> str:
        """Render a single field line for a column.

        Identity columns are pre-filtered by the Insert/Update section builder, so they
        never reach this method for a CRUD variant.
        """
        base_type = self._base_type(column, enum_names)

        force_optional = variant == _ClassVariant.UPDATE
        if variant == _ClassVariant.INSERT and (column.has_default or column.is_generated):
            force_optional = True

        type_str = self._apply_constraints(column, base_type)

        nullable = bool(column.is_nullable) or null_defaults or force_optional
        if nullable:
            type_str = f'{type_str} | None'

        field_args: dict[str, str] = {}
        if nullable:
            field_args['default'] = 'None'
        if column.alias is not None:
            field_args['alias'] = _py_string(column.alias)
        if column.description is not None:
            field_args['description'] = _py_string(column.description)

        line = f'{column.name}: {type_str}'
        if field_args:
            line += ' ' + _field_call(', '.join(f'{key}={value}' for key, value in field_args.items()))
        return line

    def _base_type(self, column: ColumnInfo, enum_names: Mapping[tuple[str, str], str]) -> str:
        """Resolve the column's base type string, overlaying an enum class when present.

        Args:
            column: The column to resolve.
            enum_names: The resolved enum class names from :meth:`_write`, keyed on
                ``(schema, name)``. Reading them rather than calling
                :func:`~castiron.utils.naming.python_class_name` again is the point of the whole
                threading: after ``CI-128``'s collision rule the annotation and the class header can
                only agree if they come from **one** derivation.

        Returns:
            The base type string for the column.
        """
        resolution = resolve_column_type(column, PYDANTIC_TYPE_MAP)
        is_array = resolution.python_type.startswith('list[') or column.raw_type.lower().endswith('[]')

        if column.enum_info is None:
            return resolution.python_type

        if self.config.generate_enums:
            # ⚠ The `.get(...)` fallback keeps a column whose enum is NOT in the registry
            # byte-identical to what castiron has always emitted. That case is a known, separately
            # rowed defect (the mirror image of `CI-114`: `enums=[]` plus a column carrying
            # `enum_info` emits an annotation with no class and no import -> `NameError` at exit 0).
            # It is not reachable through the OpenAPI source, it is not this row's to fix, and this
            # line must not make it worse.
            key = (column.enum_info.schema, column.enum_info.name)
            enum_type = enum_names.get(key, python_class_name(column.enum_info))
            return f'list[{enum_type}]' if is_array else enum_type
        return 'list[str]' if is_array else 'str'

    def _apply_constraints(self, column: ColumnInfo, base_type: str) -> str:
        """Wrap a ``str`` base type in ``Annotated[str, StringConstraints(...)]`` when applicable."""
        if base_type != 'str' or column.raw_type.lower() != 'text' or not column.constraint_definition:
            return base_type
        constraints = _parse_length_constraint(column.constraint_definition)
        if not constraints:
            return base_type
        ordered: dict[str, int] = {}
        if 'min_length' in constraints:
            ordered['min_length'] = constraints['min_length']
        if 'max_length' in constraints:
            ordered['max_length'] = constraints['max_length']
        return f'Annotated[str, StringConstraints(**{ordered})]'

    # ------------------------------------------------------------------ operational classes

    def _operational_section(self, schema: Schema, stems: Mapping[str, ClassStem]) -> str | None:
        """Render the operational classes (which carry FK relationship fields)."""
        if not schema.tables:
            return None
        classes = [self._render_operational_class(t, stems) for t in schema.tables]
        comment = section_comment(
            'Operational Classes',
            ['Extend these models to add custom behavior; they carry relationship fields.'],
        )
        return '\n\n\n'.join([comment, *classes])

    def _render_operational_class(self, table: TableInfo, stems: Mapping[str, ClassStem]) -> str:
        """Render one operational class, appending FK relationship fields or ``pass``."""
        name = self._class_name(table, stems)
        # Through `_write_name`, not `f'{name}BaseSchema'`: the base class this inherits from is the
        # same string `_base_section` writes as a header, and the stem allocator reserves it under
        # that one derivation. Two spellings of one name is what `CI-114` was.
        base_name = self._write_name(table, _ClassVariant.BASE, stems)
        header = self._stem_header(f'class {name}({base_name}):', table, stems)
        # The description goes *before* the "Inherits from ..." trailer: the trailer is
        # castiron's boilerplate instruction to the reader and conventionally comes last,
        # while the substantive description belongs directly under the summary.
        docstring = _class_docstring(
            f'{name} Schema for Pydantic.',
            table.description,
            f'{IND}Inherits from {base_name}. Add any customization here.',
        )

        fields = self._foreign_columns(table, stems) if self.config.include_foreign_keys else []
        if fields:
            body = f'{IND}# Foreign Keys\n' + '\n'.join(f'{IND}{f}' for f in fields)
            return f'{header}\n{docstring}\n\n{body}'
        return f'{header}\n{docstring}\n\n{IND}pass'

    def _foreign_columns(self, table: TableInfo, stems: Mapping[str, ClassStem]) -> list[str]:
        """Build the nested relationship field lines for a table's operational class.

        Foreign keys map to a singular field (``author: User``) or a pluralized list
        (``posts: list[Post]``) by relationship type; reverse relationships not already
        covered by a foreign key are synthesized. The effective self-referential relation
        type is computed in a local variable -- the shared IR is never mutated.

        ⚠ **``used`` keeps its shipped "first name wins, later duplicates are skipped" semantics.**
        It already drops a second relationship to the same table (two foreign keys into ``users``
        yield one ``user`` field), and turning it into an ordinal rule would change that shipped
        behaviour for every schema. Sanitization widens it only for two *distinct* table names that
        repair alike, which is strictly better than the module not parsing -- recorded as a
        follow-up rather than absorbed here.
        """
        used: set[str] = set()
        fields: list[str] = []

        for fk in table.foreign_keys:
            field_def = self._foreign_key_field(table, fk.foreign_table_name, fk.column_name, fk.relation_type, stems)
            field_name = field_def.split(':', 1)[0].strip()
            if field_name not in used:
                used.add(field_name)
                fields.append(field_def)

        for rel in table.relationships:
            is_self_ref = rel.related_table_name.lower() == table.name.lower()
            already_covered = any(fk.foreign_table_name == rel.related_table_name for fk in table.foreign_keys)
            if not is_self_ref and already_covered:
                continue
            field_name = self._field_name(pluralize(rel.related_table_name.lower()))
            if field_name not in used:
                used.add(field_name)
                target = self._proper_name(rel.related_table_name, stems)
                fields.append(f'{field_name}: list[{target}] | None {_field_call("default=None")}')

        return fields

    def _foreign_key_field(
        self,
        table: TableInfo,
        foreign_table_name: str,
        column_name: str,
        relation_type: RelationType | None,
        stems: Mapping[str, ClassStem],
    ) -> str:
        """Render one foreign-key relationship field line (name + type by relation type)."""
        target = self._proper_name(foreign_table_name, stems)
        base_field_name = foreign_table_name.lower()
        table_name = table.name.lower()
        we_have_foreign_key = any(c.name == column_name and c.is_foreign_key for c in table.columns)

        effective = relation_type
        if foreign_table_name.lower() == table_name:
            for rel in table.relationships:
                if rel.related_table_name.lower() == table_name:
                    effective = rel.relation_type
                    break

        if effective == RelationType.ONE_TO_ONE:
            type_hint = target
            field_name = singularize(base_field_name)
        elif effective == RelationType.MANY_TO_ONE:
            if we_have_foreign_key:
                type_hint = target
                field_name = singularize(base_field_name)
            else:
                type_hint = f'list[{target}]'
                field_name = pluralize(base_field_name)
        elif effective == RelationType.ONE_TO_MANY:
            if we_have_foreign_key:
                type_hint = target
                field_name = base_field_name
            else:
                type_hint = f'list[{target}]'
                field_name = pluralize(base_field_name)
        else:  # MANY_TO_MANY or unset
            type_hint = f'list[{target}]'
            field_name = pluralize(base_field_name)

        return f'{self._field_name(field_name)}: {type_hint} | None {_field_call("default=None")}'

    # ------------------------------------------------------------------ naming helpers

    def _class_name(self, table: TableInfo, stems: Mapping[str, ClassStem]) -> str:
        """The resolved PascalCase class stem for a table."""
        return self._proper_name(table.name, stems)

    def _proper_name(self, name: str, stems: Mapping[str, ClassStem]) -> str:
        """The resolved PascalCase class stem for a table name, as a relationship annotation uses it.

        🔴 **A lookup, not a second derivation.** This name is what a relationship field is
        *annotated with* (``lines: list[OrderLines] | None``), so if it disagreed with the class
        header the module would carry an annotation naming a class it never defines. That is
        ``CI-114``'s shape, and the collision rule makes it reachable: once ``order_lines`` has been
        allocated ``OrderLines_2``, recomputing ``to_pascal_case`` here would produce ``OrderLines``.

        ⚠ **The fallback is the sanitized per-name transform, deliberately.** A foreign key can name
        a table outside the emitted schema, which the allocator never saw. Such an annotation already
        referenced an undefined class before this row; the fallback keeps that case byte-comparable
        while guaranteeing what this row is about -- that the module **parses** -- rather than
        silently reintroducing ``list[Order lines]``.

        Args:
            name: The table name, verbatim as the FK or relationship row spells it.
            stems: The resolved stems from :meth:`_resolved_stems`.

        Returns:
            The class stem to write.
        """
        entry = stems.get(name)
        return entry.name if entry is not None else python_class_stem(name, self.config.singular_names)

    def _write_name(self, table: TableInfo, variant: _ClassVariant, stems: Mapping[str, ClassStem]) -> str:
        """The class name for a table + variant (e.g. ``UserBaseSchema``, ``UserInsert``)."""
        return f'{self._class_name(table, stems)}{_VARIANT_SUFFIX[variant]}'

    def _field_name(self, name: str) -> str:
        """Repair a relationship field name into one Pydantic v2 will accept.

        🔴 **A different namespace from the class one, under a different rule.** A relationship
        field is a *model field*, so the rule is ``ir/build.py``'s -- the same
        :func:`~castiron.ir.build.standardize_column_name` every column goes through (``CI-085``),
        which additionally handles the two shapes the class rule does not care about: a **leading
        underscore** (``NameError: Fields must not use names with leading underscores`` at import,
        not at compile) and a **reserved word**. Both are reachable from an ordinary table name --
        a table called ``class`` emitted ``class: list[Class] | None`` on ``main``, a
        ``SyntaxError``, and ``"order lines"`` emitted ``order line: Order lines | None``.

        Applied **last**, after ``singularize``/``pluralize``, so inflection still operates on the
        real word rather than on a name with underscores punched through it.

        Args:
            name: The inflected, lowercased table name the relationship field is built from.

        Returns:
            The field name to write. The identity on every ordinary snake_case table name, which is
            the byte-stability property the committed goldens pin.
        """
        return standardize_column_name(name, self.config.disable_model_prefix_protection) or name

    def _stem_header(self, header: str, table: TableInfo, stems: Mapping[str, ClassStem]) -> str:
        """Prefix a class header with ``# original name was …`` when the table's stem was renamed.

        The table-side twin of the comment :meth:`_enum_section` writes, for the same reason: a
        class header carries **nothing** of the source, so once the stem is repaired or suffixed the
        Postgres table name is unrecoverable from the module and the comment is the only thing that
        keeps the transform non-lossy. It is emitted above **every** class the stem produces, not
        only the operational one -- a user importing ``OrderLines_2Insert`` needs the same
        explanation as one importing ``OrderLines_2``.

        ⚠ Both the table name and the holder's name go through :func:`_py_string`. That is not
        decoration: a table name carrying a literal newline is legal Postgres, and a raw one would
        split the ``#`` comment across two lines and break the module (``CI-009``'s standing lesson).

        ⚠ It cannot move a committed golden: the note is ``None`` for every stem that is already a
        valid identifier and uncollided, which is every table in the corpus.

        Args:
            header: The rendered ``class …:`` line.
            table: The table being rendered. Indexed, **not** ``.get``: every rendered table comes
                from ``schema.tables`` and ``stems`` is built from exactly that list, so a miss is a
                broken invariant that should be loud rather than a silently omitted comment.
                (:meth:`_proper_name`'s fallback is a different case -- a *foreign* table name,
                which genuinely can name something outside the emitted schema.)
            stems: The resolved stems from :meth:`_resolved_stems`.

        Returns:
            ``header``, with one comment line above it when the stem was renamed.
        """
        entry = stems[table.name]
        reason = class_stem_reason(entry, _py_string)
        if reason is None:
            return header
        return f'# original name was {_py_string(entry.source)} ({reason})\n{header}'

    def _metaclass(self, table: TableInfo, variant: _ClassVariant, stems: Mapping[str, ClassStem]) -> str:
        """The parent class a variant inherits from."""
        if variant == _ClassVariant.INSERT:
            return f'{CUSTOM_MODEL_NAME}Insert'
        if variant == _ClassVariant.UPDATE:
            return f'{CUSTOM_MODEL_NAME}Update'
        if variant == _ClassVariant.BASE_WITH_PARENT:
            return self._write_name(table, _ClassVariant.PARENT, stems)
        return CUSTOM_MODEL_NAME

    def _qualifier(self, variant: _ClassVariant) -> str:
        """The docstring qualifier for a variant (``Base``, ``Insert``, ...)."""
        if variant == _ClassVariant.INSERT:
            return 'Insert'
        if variant == _ClassVariant.UPDATE:
            return 'Update'
        if variant == _ClassVariant.PARENT:
            return '(Nullable) Parent'
        return 'Base'

    def _has_model_prefix_columns(self, table: TableInfo) -> bool:
        """Whether any column would collide with Pydantic's ``model_`` protected namespace."""
        return any(
            (c.alias is not None and c.alias.lower().startswith('model_'))
            or c.name.lower().startswith('field_model_')
            or c.name.lower().startswith('model')
            for c in table.columns
        )
