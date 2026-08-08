"""Canonical Schema IR nodes — the single typed data model every source produces.

This module is a faithful port of supabase-pydantic's fidelity dataclasses onto a
source-neutral, dependency-free footing (stdlib only). The nodes are **mutable**
plain ``@dataclass`` objects (captain decision D1): the builder in
:mod:`castiron.ir.build` assembles them in place, exactly as supabase-pydantic's
pipeline does. Determinism (Hard Rule #9) is guaranteed by the builder's stable
construction order plus :meth:`Schema.as_dict`'s stable, sorted serialization —
not by immutability.

Deliberate deviations from supabase-pydantic (per the CI-003 spec):

- ``post_gres_datatype`` is renamed to the source-neutral ``raw_type``; the resolved
  Python ``datatype`` is *omitted* (type maps are CI-004).
- ``ConstraintInfo`` carries a normalized :class:`ConstraintType` (raw pg codes are
  mapped in the build layer); the raw code is retained on ``raw_constraint_type``.
- ``is_generated`` is a build-time bool field rather than a runtime property.
- ``TableInfo.generated_data`` (Faker seed output) and the pg-specific
  ``is_user_defined_type`` helper are dropped.
- ``EnumInfo`` Python-naming helpers move to the CI-004 emitter.
- ``TableInfo.description`` (a table's ``COMMENT ON TABLE``) is a castiron **addition**,
  not a port: supabase-pydantic had no table-comment field, so the comment was dropped
  before it could reach an emitter (CI-009).
"""

from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from typing import Any, Literal

TableType = Literal['BASE TABLE', 'VIEW']


class RelationType(str, Enum):
    """The direction/cardinality of a relationship between two tables."""

    ONE_TO_ONE = 'One-to-One'
    ONE_TO_MANY = 'One-to-Many'
    MANY_TO_MANY = 'Many-to-Many'
    MANY_TO_ONE = 'Many-to-One'


class ConstraintType(str, Enum):
    """A normalized, source-neutral constraint category.

    Raw Postgres ``pg_constraint.contype`` codes (``p``/``f``/``u``/``c``/``x``) are
    mapped to these members in the build layer, keeping the pg-specific vocabulary out
    of the canonical IR.
    """

    PRIMARY_KEY = 'PRIMARY KEY'
    FOREIGN_KEY = 'FOREIGN KEY'
    UNIQUE = 'UNIQUE'
    CHECK = 'CHECK'
    EXCLUDE = 'EXCLUDE'
    OTHER = 'OTHER'


class FunctionVolatility(str, Enum):
    """Normalized Postgres function volatility (``pg_proc.provolatile``).

    Raw source codes (``'v'``/``'s'``/``'i'``) are mapped to these members in the build
    layer, keeping the pg-specific vocabulary out of the canonical IR (decision D3).
    """

    VOLATILE = 'VOLATILE'
    STABLE = 'STABLE'
    IMMUTABLE = 'IMMUTABLE'


class ParameterMode(str, Enum):
    """Normalized function-parameter mode (``pg_proc.proargmodes``).

    Raw source codes (``'i'``/``'o'``/``'b'``/``'v'``/``'t'``) are mapped to these members
    in the build layer, exactly as :class:`ConstraintType` handles ``contype``.
    """

    IN = 'IN'
    OUT = 'OUT'
    INOUT = 'INOUT'
    VARIADIC = 'VARIADIC'
    TABLE = 'TABLE'


@dataclass
class EnumInfo:
    """A database enum type: its name, values, and owning schema.

    Source-neutral by design — Python-identifier naming (PascalCase class names,
    member names) is an emitter concern and lives in CI-004, not here.
    """

    name: str
    values: list[str] = field(default_factory=list)
    schema: str = 'public'


@dataclass
class ColumnInfo:
    """A single column, carrying raw type signal plus resolved schema flags."""

    name: str
    raw_type: str
    alias: str | None = None
    default: str | None = None
    max_length: int | None = None
    is_nullable: bool | None = True
    primary: bool = False
    is_unique: bool = False
    is_foreign_key: bool = False
    is_identity: bool = False
    is_generated: bool = False
    unique_partners: list[str] | None = field(default_factory=list)
    enum_info: EnumInfo | None = None
    array_element_type: str | None = None
    description: str | None = None
    constraint_definition: str | None = None
    user_defined_values: list[str] | None = field(default_factory=list)

    def __str__(self) -> str:
        """Return a short, human-readable representation of the column."""
        return f'ColumnInfo({self.name}, {self.raw_type})'

    @property
    def has_default(self) -> bool:
        """Whether the column declares a default value."""
        return self.default is not None

    def nullable(self) -> bool:
        """Whether the column is nullable, treating an unknown (``None``) as ``False``."""
        return self.is_nullable if self.is_nullable is not None else False


@dataclass
class ConstraintInfo:
    """A table constraint, normalized to a :class:`ConstraintType`.

    ``raw_constraint_type`` retains the original source code (e.g. the pg ``contype``)
    for round-trip fidelity; it is ``None`` for sources that do not expose one.

    ``name_is_synthesized`` is ``True`` when ``constraint_name`` was **manufactured by the
    source** from a naming template rather than read from the database. It defaults to
    ``False``, so every existing constructor and every source that reports a real name is
    unaffected. It exists because the fact is otherwise **unrecoverable**: a synthesized
    ``orders_pkey`` is byte-identical to the name Postgres gives a genuinely default-named
    constraint, so no downstream heuristic can tell them apart. Two consumers are specified
    to read it -- ``castiron check`` (CI-021) compares constraint names only when **both**
    sides report ``False``, and the SQLAlchemy/DDL emitters (CI-030/CI-031) omit ``name=``
    entirely when it is ``True``, letting Postgres apply its own default. Both remove a
    false drift positive by construction rather than by convention.
    """

    constraint_name: str
    type: ConstraintType
    columns: list[str] = field(default_factory=list)
    constraint_definition: str | None = None
    raw_constraint_type: str | None = None
    name_is_synthesized: bool = False

    def __str__(self) -> str:
        """Return a short, human-readable representation of the constraint."""
        return f'ConstraintInfo({self.constraint_name}, {self.type.value})'


@dataclass
class ForeignKeyInfo:
    """A foreign-key edge from a column to a column on a foreign table.

    ``name_is_synthesized`` carries the same provenance fact as
    :attr:`ConstraintInfo.name_is_synthesized`, for the same two consumers, and defaults to
    ``False`` for the same reason: a source that reports the database's own
    ``pg_constraint.conname`` says nothing and is unaffected. A **reverse** edge synthesized
    by :func:`castiron.ir.build.analyze_table_relationships` inherits the flag from the
    forward edge it mirrors, because it deliberately reuses that edge's ``constraint_name``.
    """

    constraint_name: str
    column_name: str
    foreign_table_name: str
    foreign_column_name: str
    foreign_table_schema: str = 'public'
    relation_type: RelationType | None = None
    name_is_synthesized: bool = False


@dataclass(unsafe_hash=True)
class RelationshipInfo:
    """A derived relationship descriptor between two tables.

    ``unsafe_hash`` makes the node hashable (so relationships can be de-duplicated via
    a set) while staying mutable per D1. This is safe because a ``RelationshipInfo`` is
    written once at construction and never mutated afterwards.
    """

    table_name: str
    related_table_name: str
    relation_type: RelationType | None = None


@dataclass
class SortedColumns:
    """The return shape of :meth:`TableInfo.sort_and_separate_columns`.

    A transient view used by emitters for field ordering — not a stored IR node.
    """

    primary_keys: list[ColumnInfo]
    nullable: list[ColumnInfo]
    non_nullable: list[ColumnInfo]
    remaining: list[ColumnInfo]


@dataclass
class TableInfo:
    """A table (or view): its columns, constraints, foreign keys, and relationships.

    ``description`` is the table's own SQL comment (``COMMENT ON TABLE``), or ``None`` when
    the source does not report one. It is **never** a guess: a source that cannot see
    comments leaves it ``None``, and an empty or whitespace-only comment normalizes to
    ``None`` too, so "no comment" has exactly one representation. The OpenAPI source fills
    it from ``definitions.<t>.description`` (CI-009); CI-010's live-DB path fills it from
    ``obj_description(c.oid, 'pg_class')`` — an *enrichment* of the same field, not a
    redesign. The field is appended last so positional ``TableInfo(...)`` construction
    stays valid (the ``Schema.functions`` precedent).
    """

    name: str
    schema: str = 'public'
    table_type: TableType = 'BASE TABLE'
    is_bridge: bool = False
    columns: list[ColumnInfo] = field(default_factory=list)
    foreign_keys: list[ForeignKeyInfo] = field(default_factory=list)
    constraints: list[ConstraintInfo] = field(default_factory=list)
    relationships: list[RelationshipInfo] = field(default_factory=list)
    description: str | None = None

    def __str__(self) -> str:
        """Return a short, human-readable representation of the table."""
        return f'TableInfo({self.schema}.{self.name})'

    def add_column(self, column: ColumnInfo) -> None:
        """Append a column to the table (build-layer helper)."""
        self.columns.append(column)

    def add_foreign_key(self, fk: ForeignKeyInfo) -> None:
        """Append a foreign key to the table (build-layer helper)."""
        self.foreign_keys.append(fk)

    def add_constraint(self, constraint: ConstraintInfo) -> None:
        """Append a constraint to the table (build-layer helper)."""
        self.constraints.append(constraint)

    def aliasing_in_columns(self) -> bool:
        """Whether any column within the table carries an alias."""
        return any(bool(c.alias is not None) for c in self.columns)

    def table_dependencies(self) -> set[str]:
        """Return the set of foreign table names this table depends on."""
        return {fk.foreign_table_name for fk in self.foreign_keys}

    def primary_key(self) -> list[str]:
        """Return the primary-key column names (empty for a VIEW or when absent)."""
        if self.table_type == 'BASE TABLE':
            for constraint in self.constraints:
                if constraint.type == ConstraintType.PRIMARY_KEY:
                    return constraint.columns
        return []

    def primary_is_composite(self) -> bool:
        """Whether the primary key spans more than one column."""
        return len(self.primary_key()) > 1

    def get_primary_columns(self, sort_results: bool = False) -> list[ColumnInfo]:
        """Return the columns that make up the primary key."""
        return self._get_columns(is_primary=True, sort_results=sort_results)

    def get_secondary_columns(self, sort_results: bool = False) -> list[ColumnInfo]:
        """Return the columns that are not part of the primary key."""
        return self._get_columns(is_primary=False, sort_results=sort_results)

    def _get_columns(self, is_primary: bool = True, sort_results: bool = False) -> list[ColumnInfo]:
        """Return primary or secondary columns, optionally sorted by name."""
        if is_primary:
            res = [c for c in self.columns if c.name in self.primary_key()]
        else:
            res = [c for c in self.columns if c.name not in self.primary_key()]

        if sort_results:
            res.sort(key=lambda x: x.name)

        return res

    def sort_and_separate_columns(
        self, separate_nullable: bool = False, separate_primary_key: bool = False
    ) -> SortedColumns:
        """Sort columns by name and optionally bucket them by key/nullability.

        Args:
            separate_nullable: Whether to split nullable and non-nullable columns.
            separate_primary_key: Whether to split primary-key and secondary columns.

        Returns:
            A :class:`SortedColumns` with ``primary_keys``, ``nullable``,
            ``non_nullable``, and ``remaining`` populated per the flags.
        """
        result = SortedColumns([], [], [], [])
        if separate_primary_key:
            result.primary_keys = self.get_primary_columns(sort_results=True)
            result.remaining = self.get_secondary_columns(sort_results=True)
        else:
            result.remaining = sorted(self.columns, key=lambda x: x.name)

        if separate_nullable:
            nullable_columns = [column for column in result.remaining if column.is_nullable]
            non_nullable_columns = [column for column in result.remaining if not column.is_nullable]
            result.nullable = nullable_columns
            result.non_nullable = non_nullable_columns
            result.remaining = []

        return result

    def has_unique_constraint(self) -> bool:
        """Whether the table declares at least one UNIQUE constraint."""
        return any(c.type == ConstraintType.UNIQUE for c in self.constraints)


@dataclass
class ParameterInfo:
    """One function parameter, carrying raw type signal exactly as :class:`ColumnInfo` does.

    The raw type vocabulary is deliberately shared with ``ColumnInfo`` (``raw_type`` /
    ``array_element_type`` / ``enum_info``) so a future emitter resolves a parameter with
    the very same :mod:`castiron.types` machinery it uses for a column.
    """

    name: str
    raw_type: str
    mode: ParameterMode = ParameterMode.IN
    has_default: bool = False
    array_element_type: str | None = None
    enum_info: EnumInfo | None = None

    def __str__(self) -> str:
        """Return a short, human-readable representation of the parameter."""
        return f'ParameterInfo({self.name}, {self.raw_type})'


@dataclass
class FunctionInfo:
    """A database function -- a PostgREST ``/rpc/<name>`` endpoint, or a ``pg_proc`` row.

    Fields a coarse source cannot know are tri-state: ``None`` means *unknown*, never a
    guess. That distinction is the whole point of the node, because the two sources that
    populate it see very different amounts:

    ==========================  =========================================  ==================
    Field                       OpenAPI/PostgREST source (CI-005)          Live DB (CI-011)
    ==========================  =========================================  ==================
    ``name`` / ``schema``       full (path key + the caller's schema)       full
    ``parameters[].name``       full (body-schema ``properties``)           full
    ``parameters[].raw_type``   full, with ``int32``/``int64`` flattening   exact
    ``parameters[].has_default``full (``name not in schema.required``)      full
    ``parameters[].mode``       ``IN``; ``VARIADIC`` from the GET operation full ``proargmodes``
    ``parameters[].enum_info``  only if the enum is on a scalar column too  full
    ``return_type``             **always None** -- never encoded            full
    ``returns_set``             **always None** -- never encoded            ``proretset``
    ``volatility``              ``VOLATILE`` iff POST-only, else ``None``   full
    ``is_read_only``            full (a ``get`` operation exists)           ``provolatile != 'v'``
    ``description``             full (the raw SQL comment)                  ``pg_description``
    overloads                   **collapsed upstream** to one signature     full
    ==========================  =========================================  ==================

    Documented invariant (not runtime-validated -- these are plain mutable dataclasses per
    decision D1): when ``volatility`` is known, ``is_read_only`` equals
    ``volatility is not FunctionVolatility.VOLATILE``. Two fields rather than one because a
    coarse source can honestly assert *"non-volatile"* without knowing *which* -- collapsing
    them would either discard that fact or fabricate ``STABLE``.
    """

    name: str
    schema: str = 'public'
    parameters: list[ParameterInfo] = field(default_factory=list)
    return_type: str | None = None
    returns_set: bool | None = None
    volatility: FunctionVolatility | None = None
    is_read_only: bool | None = None
    description: str | None = None

    def __str__(self) -> str:
        """Return a short, human-readable representation of the function."""
        return f'FunctionInfo({self.schema}.{self.name})'


@dataclass
class Schema:
    """The root IR container: all tables, a de-duplicated enum registry, and functions.

    supabase-pydantic returned a bare ``list[TableInfo]``; castiron wraps that in a
    ``Schema`` so an emitter has one object to consume and a single place to emit each
    enum class exactly once (``enums``), while per-column ``ColumnInfo.enum_info`` keeps
    the linkage.

    ``functions`` is the function/RPC registry (added in CI-005, filling the forward slot
    CI-003 documented). It is populated from a source's function rows; the OpenAPI source
    leaves ``FunctionInfo.return_type`` and ``returns_set`` as ``None`` (PostgREST never
    encodes them) and can only assert ``volatility`` when a function is ``VOLATILE``.
    CI-011's live-DB ``pg_proc`` path *enriches* those fields rather than redesigning the
    node. The field is appended last so positional ``Schema(tables, enums)`` construction
    stays valid.
    """

    tables: list[TableInfo] = field(default_factory=list)
    enums: list[EnumInfo] = field(default_factory=list)
    functions: list[FunctionInfo] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """Return a stable, JSON-serializable projection of the schema.

        Field order follows declaration order and list order is preserved, so the
        result is byte-identical for identical inputs. Enums render as their string
        values; nested dataclasses become plain dicts.
        """
        result = _serialize(self)
        assert isinstance(result, dict)  # Schema is a dataclass, so _serialize returns a dict
        return result


def _serialize(obj: Any) -> Any:
    """Recursively convert IR objects into plain, JSON-serializable builtins."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _serialize(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [_serialize(item) for item in obj]
    return obj
