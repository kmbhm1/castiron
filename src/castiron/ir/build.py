"""Tuple-contract → Schema IR builder.

A faithful port of supabase-pydantic's ``construct_table_info`` pipeline and its
marshalers (schema / relationships / constraints / column). It turns the six
positional-tuple row contracts a source produces into a fully-populated
:class:`~castiron.ir.models.Schema` — filling columns, PK/unique/FK/check flags,
foreign keys, constraints, relationships, bridge-table detection, and per-column
enum linkage — **without** resolving Python types (that is CI-004) and **without**
any database or network access.

Positional tuple contracts (the source ↔ builder boundary)
----------------------------------------------------------
- **column row (12-tuple):** ``(schema, table_name, column_name, default, is_nullable,
  data_type, max_length, table_type, identity_generation, udt_name,
  array_element_type, description)`` — ``is_nullable`` is ``'YES'``/``'NO'``;
  ``table_type`` is ``'BASE TABLE'``/``'VIEW'``; ``identity_generation`` is
  non-``None`` for identity columns.
- **fk row (7-tuple):** ``(table_schema, table_name, column_name,
  foreign_table_schema, foreign_table_name, foreign_column_name, constraint_name)``.
- **constraint row (5-tuple):** ``(constraint_name, table_name, columns,
  raw_constraint_type, constraint_definition)`` — ``raw_constraint_type`` is the pg
  code ``'p'|'f'|'u'|'c'|'x'``.
- **enum-type row (7-tuple):** ``(type_name, namespace, owner, category, is_defined,
  typtype, enum_values)`` — only ``typtype == 'e'`` rows are enums.
- **enum-type-mapping row (6-tuple):** ``(column_name, table_name, namespace,
  type_name, type_category, type_description)``.
- **function row (8-tuple):** ``(schema, function_name, description, return_type,
  returns_set, raw_volatility, is_read_only, parameters)`` -- ``returns_set`` and
  ``is_read_only`` are ``bool | None`` (``None`` == unknown); ``raw_volatility`` is the
  source's raw code (pg ``provolatile``: ``'v'|'s'|'i'``) or ``None``; ``parameters`` is a
  list of **parameter 5-tuples**.
- **parameter row (5-tuple):** ``(name, raw_type, raw_mode, has_default,
  array_element_type)`` -- ``raw_mode`` is the source's raw code (pg ``proargmodes``:
  ``'i'|'o'|'b'|'v'|'t'``) or ``None`` (which normalizes to ``ParameterMode.IN``).
- **table row (3-tuple):** ``(schema, table_name, description)`` -- the table's own SQL
  comment (``COMMENT ON TABLE``), or ``None``. Rows naming a table that has no column rows
  are ignored: a table exists in the IR only because a source reported columns for it, so
  attaching a comment must never bring a table (and therefore a generated class) into
  being. This is exactly the shape a ``pg_class`` introspection query produces per row --
  ``(n.nspname, c.relname, obj_description(c.oid, 'pg_class'))`` -- so CI-010's live-DB
  path fills ``TableInfo.description`` by emitting these rows, with no signature change and
  no redesign. ⚠ Widening this tuple later would be breaking, exactly as widening the
  column 12-tuple would be; a source wanting more table-level facts appends a **new**
  contract, not a fourth element.

  ⚠ **Duplicate rows for one table: the LAST one wins.**
  :func:`add_table_descriptions_to_table_details` assigns unconditionally rather than
  skipping an already-set description, so the final row for a ``(schema, table)`` key is the
  one that survives. That is deliberate and pinned by a test -- it keeps the rule stateless
  and therefore order-deterministic (Hard Rule #9) instead of depending on whether a
  previous row happened to be ``None``. It is unreachable from the OpenAPI source, which
  emits exactly one row per sorted ``definitions`` key, but **CI-010 will emit these rows
  from a query**, and a ``LEFT JOIN`` that fans a table out twice would deliver two rows for
  one table. A source that can produce duplicates owns making them consistent -- or must
  order them so the row it wants is last.

A nested list inside a positional row is already the house style (the enum-type row's 7th
field is a ``list[str]``; the constraint row's 3rd is a ``list[str]``), and a live-DB
source will naturally ``array_agg`` a function's arguments the same way.

⚠ **Ordering caveat:** ``function_details`` and ``table_details`` are the *last* two
parameters of :func:`build_schema`, not the sixth and seventh -- inserting either after
``enum_type_mapping`` would silently break any caller that passes ``schema`` positionally.
``table_details`` is appended after ``function_details`` for the same reason. The cosmetic
cost is that the row contracts are no longer adjacent in the signature.
"""

import builtins
import keyword
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from castiron.ir.models import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    EnumInfo,
    ForeignKeyInfo,
    FunctionInfo,
    FunctionVolatility,
    ParameterInfo,
    ParameterMode,
    RelationshipInfo,
    RelationType,
    Schema,
    TableInfo,
    TableType,
)

logger = logging.getLogger(__name__)

# A single heterogeneous positional row from a source (see the module docstring).
Row = tuple[Any, ...]

# Maps raw Postgres ``pg_constraint.contype`` codes to normalized IR constraint types.
# Lives in the build layer (not the model) so the pg-specific vocabulary stays out of
# the canonical IR (spec decision D3).
CONSTRAINT_TYPE_MAP: dict[str, ConstraintType] = {
    'p': ConstraintType.PRIMARY_KEY,
    'f': ConstraintType.FOREIGN_KEY,
    'u': ConstraintType.UNIQUE,
    'c': ConstraintType.CHECK,
    'x': ConstraintType.EXCLUDE,
}


# Maps raw Postgres ``pg_proc.provolatile`` codes to normalized IR volatility members.
VOLATILITY_MAP: dict[str, FunctionVolatility] = {
    'v': FunctionVolatility.VOLATILE,
    's': FunctionVolatility.STABLE,
    'i': FunctionVolatility.IMMUTABLE,
}

# Maps raw Postgres ``pg_proc.proargmodes`` codes to normalized IR parameter modes.
PARAMETER_MODE_MAP: dict[str, ParameterMode] = {
    'i': ParameterMode.IN,
    'o': ParameterMode.OUT,
    'b': ParameterMode.INOUT,
    'v': ParameterMode.VARIADIC,
    't': ParameterMode.TABLE,
}


def normalize_constraint_type(raw_constraint_type: str) -> ConstraintType:
    """Map a raw source constraint code to a normalized :class:`ConstraintType`."""
    return CONSTRAINT_TYPE_MAP.get(raw_constraint_type.lower(), ConstraintType.OTHER)


def normalize_volatility(raw_volatility: str | None) -> FunctionVolatility | None:
    """Map a raw source volatility code to a :class:`FunctionVolatility`, or ``None``.

    Args:
        raw_volatility: The source's raw code (pg ``provolatile``), or ``None`` when the
            source cannot tell (the OpenAPI source only knows *volatile* vs *not*).

    Returns:
        The normalized member, or ``None`` for an absent or unrecognized code.
    """
    if raw_volatility is None:
        return None
    return VOLATILITY_MAP.get(raw_volatility.lower())


def normalize_parameter_mode(raw_mode: str | None) -> ParameterMode:
    """Map a raw source parameter-mode code to a :class:`ParameterMode`.

    Args:
        raw_mode: The source's raw code (pg ``proargmodes``), or ``None``.

    Returns:
        The normalized member; ``ParameterMode.IN`` for an absent or unrecognized code
        (pg itself leaves ``proargmodes`` NULL when every argument is ``IN``).
    """
    if raw_mode is None:
        return ParameterMode.IN
    return PARAMETER_MODE_MAP.get(raw_mode.lower(), ParameterMode.IN)


def split_type_name(type_name: str) -> tuple[str | None, str]:
    """Split a raw type token into its optional schema qualifier and its bare name.

    Removes pg decoration first -- leading underscores (the array-type naming ``_int4``),
    a trailing ``[]`` (``test_status[]``), and surrounding double quotes
    (``"FourthType"``) -- then splits on the last ``.``. Case is preserved; callers
    compare case-insensitively.

    The namespace matters: a schema-qualified token is the *only* thing that
    distinguishes ``public.status`` from ``audit.status``, and two enums may legitimately
    share a bare name across schemas. Dropping the qualifier silently resolves one of
    them to the other's values.

    Args:
        type_name: The raw type token.

    Returns:
        A ``(namespace, bare_name)`` pair; ``namespace`` is ``None`` for an unqualified
        token.
    """
    clean_name = type_name
    while clean_name.startswith('_'):
        clean_name = clean_name[1:]

    if clean_name.endswith('[]'):
        clean_name = clean_name[:-2]

    if clean_name.startswith('"') and clean_name.endswith('"'):
        clean_name = clean_name[1:-1]

    namespace, separator, bare_name = clean_name.rpartition('.')
    return (namespace if separator else None), bare_name


# ---------------------------------------------------------------------------
# Build-layer DTOs (source/pg-catalog raw rows — NOT part of the canonical IR).
# ---------------------------------------------------------------------------


@dataclass
class UserEnumType:
    """A raw enum-type row from the source catalog, used only during construction."""

    type_name: str
    namespace: str
    owner: str
    category: str
    is_defined: bool
    type: str
    enum_values: list[str] = field(default_factory=list)

    def matches_type_name(self, type_name: str) -> bool:
        """Whether ``type_name`` names this enum, tolerating pg array-naming quirks.

        Decoration handling is delegated to :func:`split_type_name` so every caller that
        compares a raw type token to an enum shares one implementation. A **schema-
        qualified** token must also match this enum's namespace: ``audit.status`` is not
        ``public.status``, and treating them as the same silently gives a column the wrong
        member list. An unqualified token matches on name alone, as before.
        """
        if not type_name:
            return False
        namespace, bare_name = split_type_name(type_name)
        if namespace is not None and self.namespace.lower() != namespace.lower():
            return False
        return self.type_name.lower() == bare_name.lower()


@dataclass
class UserTypeMapping:
    """A raw column→type mapping row from the source catalog."""

    column_name: str
    table_name: str
    namespace: str
    type_name: str
    type_category: str
    type_description: str


# ---------------------------------------------------------------------------
# Column-name hygiene (source-agnostic Python-identifier protection).
# ---------------------------------------------------------------------------


def string_is_reserved(value: str) -> bool:
    """Whether ``value`` collides with a Python builtin or keyword."""
    return value in dir(builtins) or value in keyword.kwlist


def column_name_is_reserved(column_name: str, disable_model_prefix_protection: bool = False) -> bool:
    """Whether the column name is reserved (or ``model_``-prefixed, unless disabled)."""
    if disable_model_prefix_protection:
        return string_is_reserved(column_name)
    return string_is_reserved(column_name) or column_name.startswith('model_')


def column_name_reserved_exceptions(column_name: str) -> bool:
    """Whether the column name is a curated exception that need not be renamed."""
    exceptions = ['id', 'credits', 'copyright', 'license', 'help', 'property', 'sum']
    return column_name.lower() in exceptions


def standardize_column_name(column_name: str, disable_model_prefix_protection: bool = False) -> str | None:
    """Return a safe column identifier, prefixing reserved names with ``field_``."""
    return (
        f'field_{column_name}'
        if column_name_is_reserved(column_name, disable_model_prefix_protection)
        and not column_name_reserved_exceptions(column_name)
        else column_name
    )


def get_alias(column_name: str, disable_model_prefix_protection: bool = False) -> str | None:
    """Return the original column name as an alias when it had to be renamed, else ``None``."""
    return (
        column_name
        if column_name_is_reserved(column_name, disable_model_prefix_protection)
        and not column_name_reserved_exceptions(column_name)
        else None
    )


# ---------------------------------------------------------------------------
# Columns → tables.
# ---------------------------------------------------------------------------


def get_table_details_from_columns(
    column_details: Sequence[Row],
    disable_model_prefix_protection: bool = False,
) -> dict[tuple[str, str], TableInfo]:
    """Parse column rows into :class:`ColumnInfo` grouped into tables by ``(schema, name)``.

    Unlike supabase-pydantic, no Python type is resolved here: the raw ``data_type`` is
    recorded on ``ColumnInfo.raw_type`` and the array-element token is preserved.
    ``is_generated`` is computed at build time (identity, or a ``nextval`` default).

    Args:
        column_details: The column rows (12-tuple contract).
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.

    Returns:
        A mapping of ``(schema, table_name)`` to its :class:`TableInfo`.
    """
    tables: dict[tuple[str, str], TableInfo] = {}
    for row in column_details:
        (
            schema,
            table_name,
            column_name,
            default,
            is_nullable,
            data_type,
            max_length,
            table_type,
            identity_generation,
            _udt_name,
            array_element_type,
            description,
        ) = row
        table_key: tuple[str, str] = (schema, table_name)
        if table_key not in tables:
            normalized_table_type: TableType = 'VIEW' if table_type == 'VIEW' else 'BASE TABLE'
            tables[table_key] = TableInfo(name=table_name, schema=schema, table_type=normalized_table_type)

        is_identity = identity_generation is not None
        is_generated = is_identity or (default is not None and 'nextval' in str(default).lower())

        column_info = ColumnInfo(
            name=standardize_column_name(column_name, disable_model_prefix_protection) or column_name,
            raw_type=data_type,
            alias=get_alias(column_name, disable_model_prefix_protection),
            default=default,
            is_nullable=is_nullable == 'YES',
            max_length=max_length,
            is_identity=is_identity,
            is_generated=is_generated,
            array_element_type=array_element_type,
            description=description,
        )
        tables[table_key].add_column(column_info)

    return tables


# ---------------------------------------------------------------------------
# Table-level SQL comments.
# ---------------------------------------------------------------------------


def _normalize_description(value: Any) -> str | None:
    r"""Normalize a raw source comment: CRLF→LF, trimmed, empty → ``None``.

    Normalization lives here rather than in a source so that every source inherits one
    rule instead of re-deriving (and re-breaking) it. Three decisions are load-bearing:

    - **CRLF→LF first.** A comment authored in a Windows editor arrives with ``\\r\\n``. A
      lone ``\r`` in generated output is a byte-stability hazard across platforms and pure
      diff noise (Hard Rule #9). This is the CI-063 lesson applied at the IR boundary:
      normalize the encoding the input actually arrives in, not the canonical one.
    - **Empty → ``None``.** ``COMMENT ON TABLE t IS ''`` stores an empty string and
      PostgREST reports ``"description": ""``. Two IRs that both mean *no comment* must be
      indistinguishable, or ``as_dict()`` reports drift where none exists.
    - **``isinstance`` rather than ``str(value)``.** Rows are ``tuple[Any, ...]``; a
      non-string is *unknown*, and castiron's standing posture is that unknown is ``None``,
      never a guess. It also keeps this function ``mypy --strict``-clean without leaking
      ``Any``.

    Args:
        value: The raw value from a table row's third element.

    Returns:
        The normalized comment, or ``None`` when there is nothing to record.
    """
    if not isinstance(value, str):
        return None
    text = value.replace('\r\n', '\n').replace('\r', '\n').strip()
    return text or None


def add_table_descriptions_to_table_details(
    tables: dict[tuple[str, str], TableInfo],
    table_details: Sequence[Row],
) -> None:
    """Attach each table's SQL comment to its :class:`TableInfo`.

    A row naming a table that has no column rows is skipped, **never created**: a table
    exists in the IR only because a source reported columns for it, and inventing one here
    would add a class to every emitter's output.

    Assignment is **unconditional**, so when two rows name the same ``(schema, table)`` the
    last one wins -- see the ``table row (3-tuple)`` contract in the module docstring for why
    that is deliberate and which source can actually hit it.

    Args:
        tables: The tables built from the column rows, keyed by ``(schema, name)``.
        table_details: Table rows (3-tuple contract).
    """
    for row in table_details:
        (schema, table_name, description) = row
        table_key: tuple[str, str] = (schema, table_name)
        if table_key not in tables:
            logger.debug(f'Skipping table comment for {schema}.{table_name} - no columns were reported for it')
            continue
        tables[table_key].description = _normalize_description(description)


# ---------------------------------------------------------------------------
# Foreign keys.
# ---------------------------------------------------------------------------


def add_foreign_key_info_to_table_details(
    tables: dict[tuple[str, str], TableInfo],
    fk_details: Sequence[Row],
    disable_model_prefix_protection: bool = False,
) -> None:
    """Parse FK rows into :class:`ForeignKeyInfo`, skipping edges to absent tables.

    Column names are standardized (reserved-name protection). A first-pass relationship
    type is inferred from primary-key shape; :func:`analyze_table_relationships` refines
    it afterwards.

    Args:
        tables: The tables built from the column rows, keyed by ``(schema, name)``.
        fk_details: Foreign-key rows (7-tuple contract).
        disable_model_prefix_protection: Must match the value used to build the columns.
            A mismatch renames a column here but not there (or vice versa), so the FK
            silently stops matching any column and ``is_foreign_key`` is never set.
    """
    for row in fk_details:
        (
            table_schema,
            table_name,
            column_name,
            foreign_table_schema,
            foreign_table_name,
            foreign_column_name,
            constraint_name,
        ) = row
        table_key = (table_schema, table_name)
        foreign_table_key = (foreign_table_schema, foreign_table_name)

        if table_key not in tables or foreign_table_key not in tables:
            missing_source = table_key not in tables
            missing_target = foreign_table_key not in tables
            if missing_target and not missing_source:
                logger.debug(
                    f'Foreign key {constraint_name} references table {foreign_table_schema}.{foreign_table_name} '
                    f'which is not in the current analysis.'
                )
            else:
                logger.debug(
                    f'Skipping foreign key {constraint_name} - missing source table {table_schema}.{table_name}'
                )
            continue

        relation_type = _infer_first_pass_relation_type(
            tables, table_key, foreign_table_key, column_name, foreign_column_name, foreign_table_name
        )

        fk_info = ForeignKeyInfo(
            constraint_name=constraint_name,
            column_name=standardize_column_name(column_name, disable_model_prefix_protection) or column_name,
            foreign_table_name=foreign_table_name,
            foreign_column_name=(
                standardize_column_name(foreign_column_name, disable_model_prefix_protection) or foreign_column_name
            ),
            relation_type=relation_type,
            foreign_table_schema=foreign_table_schema,
        )
        tables[table_key].add_foreign_key(fk_info)


def _infer_first_pass_relation_type(
    tables: dict[tuple[str, str], TableInfo],
    table_key: tuple[str, str],
    foreign_table_key: tuple[str, str],
    column_name: str,
    foreign_column_name: str,
    foreign_table_name: str,
) -> RelationType:
    """Infer an initial relation type for a foreign key from primary-key shape.

    A sole-primary-key column on either side implies ONE_TO_ONE; a composite key forces
    MANY_TO_ONE; multiple FKs to the same target imply MANY_TO_MANY; otherwise the
    default is MANY_TO_ONE (from the FK-holder's perspective).
    """
    is_one_to_one = False
    found_composite_key = False

    for constraint in tables[table_key].constraints:
        if constraint.type == ConstraintType.PRIMARY_KEY and column_name in constraint.columns:
            if len(constraint.columns) > 1:
                found_composite_key = True
                break
            is_one_to_one = True

    if found_composite_key:
        is_one_to_one = False
    elif not is_one_to_one:
        for constraint in tables[foreign_table_key].constraints:
            if constraint.type == ConstraintType.PRIMARY_KEY and foreign_column_name in constraint.columns:
                if len(constraint.columns) > 1:
                    found_composite_key = True
                    break
                is_one_to_one = True

    if found_composite_key:
        is_one_to_one = False

    if is_one_to_one:
        return RelationType.ONE_TO_ONE

    fk_columns = [fk for fk in tables[table_key].foreign_keys if fk.foreign_table_name == foreign_table_name]
    if len(fk_columns) > 1:
        return RelationType.MANY_TO_MANY
    return RelationType.MANY_TO_ONE


# ---------------------------------------------------------------------------
# Constraints.
# ---------------------------------------------------------------------------


def parse_constraint_definition_for_fk(constraint_definition: str) -> tuple[str, str, str] | None:
    """Parse ``FOREIGN KEY (col) REFERENCES table(col)`` into its parts, or ``None``."""
    match = re.match(r'FOREIGN KEY \(([^)]+)\) REFERENCES (\S+)\(([^)]+)\)', constraint_definition)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def add_constraints_to_table_details(
    tables: dict[tuple[str, str], TableInfo],
    schema: str,
    constraints: Sequence[Row],
    disable_model_prefix_protection: bool = False,
) -> None:
    """Parse constraint rows into :class:`ConstraintInfo` and attach them to their table.

    A leading ``{schema}.`` on the table name is stripped, and the raw constraint code is
    normalized to a :class:`ConstraintType` (the raw code is retained for fidelity).

    Args:
        tables: The tables built from the column rows, keyed by ``(schema, name)``.
        schema: The schema these constraint rows belong to.
        constraints: Constraint rows (5-tuple contract).
        disable_model_prefix_protection: Must match the value used to build the columns.
            A mismatch makes ``ConstraintInfo.columns`` name a column that does not
            exist, so ``primary``/``is_unique``/``is_foreign_key`` are never set and
            ``TableInfo.primary_key()`` returns a phantom name.
    """
    for row in constraints:
        (constraint_name, table_name, columns, raw_constraint_type, constraint_definition) = row

        if table_name.startswith(f'{schema}.'):
            table_name = table_name[len(schema) + 1 :]
        table_name = table_name.lstrip('.')
        table_key = (schema, table_name)

        if table_key in tables:
            constraint = ConstraintInfo(
                constraint_name=constraint_name,
                type=normalize_constraint_type(raw_constraint_type),
                columns=[standardize_column_name(c, disable_model_prefix_protection) or str(c) for c in columns],
                constraint_definition=constraint_definition,
                raw_constraint_type=raw_constraint_type,
            )
            tables[table_key].add_constraint(constraint)


def get_unique_columns_from_constraints(constraint: ConstraintInfo) -> list[str]:
    """Return the column names named by a ``UNIQUE (...)`` constraint definition."""
    unique_columns: list[str] = []
    if constraint.type == ConstraintType.UNIQUE and constraint.constraint_definition is not None:
        match = re.match(r'UNIQUE \(([^)]+)\)', constraint.constraint_definition)
        if match:
            unique_columns = [c.strip() for c in match.group(1).split(',')]
    return unique_columns


def update_columns_with_constraints(tables: dict[tuple[str, str], TableInfo]) -> None:
    """Set ``primary``/``is_unique``/``is_foreign_key``/``constraint_definition`` on columns."""
    for table in tables.values():
        if not table.columns or not table.constraints:
            continue

        for column in table.columns:
            for constraint in table.constraints:
                for col in constraint.columns:
                    if column.name == col:
                        if constraint.type == ConstraintType.PRIMARY_KEY:
                            column.primary = True
                        if constraint.type == ConstraintType.UNIQUE:
                            column.is_unique = True
                            column.unique_partners = get_unique_columns_from_constraints(constraint)
                        if constraint.type == ConstraintType.FOREIGN_KEY:
                            column.is_foreign_key = True
                        if constraint.type == ConstraintType.CHECK and len(constraint.columns) == 1:
                            column.constraint_definition = constraint.constraint_definition


def update_column_constraint_definitions(tables: dict[tuple[str, str], TableInfo]) -> None:
    """Copy single-column CHECK constraint definitions onto their columns."""
    for table in tables.values():
        if not table.columns or not table.constraints:
            continue

        for column in table.columns:
            for constraint in table.constraints:
                if constraint.type == ConstraintType.CHECK and len(constraint.columns) == 1:
                    if column.name == constraint.columns[0]:
                        column.constraint_definition = constraint.constraint_definition


# ---------------------------------------------------------------------------
# Relationships & bridge tables.
# ---------------------------------------------------------------------------


def add_relationships_to_table_details(tables: dict[tuple[str, str], TableInfo], fk_details: Sequence[Row]) -> None:
    """Derive :class:`RelationshipInfo` edges from foreign keys.

    Bridge tables produce MANY_TO_MANY edges between the tables they connect; other FKs
    yield ONE_TO_ONE / ONE_TO_MANY / MANY_TO_MANY based on column uniqueness.
    """
    for row in fk_details:
        (
            table_schema,
            table_name,
            column_name,
            foreign_table_schema,
            foreign_table_name,
            foreign_column_name,
            _constraint_name,
        ) = row
        table_key = (table_schema, table_name)
        foreign_table_key = (foreign_table_schema, foreign_table_name)

        if table_key not in tables or foreign_table_key not in tables:
            continue

        table = tables[table_key]
        foreign_table = tables[foreign_table_key]

        if table.is_bridge:
            bridge_fks = table.foreign_keys
            for i, fk1 in enumerate(bridge_fks):
                for fk2 in bridge_fks[i + 1 :]:
                    table1_key = (table.schema, fk1.foreign_table_name)
                    table2_key = (table.schema, fk2.foreign_table_name)
                    if table1_key in tables and table2_key in tables:
                        tables[table1_key].relationships.append(
                            RelationshipInfo(
                                table_name=table1_key[1],
                                related_table_name=fk2.foreign_table_name,
                                relation_type=RelationType.MANY_TO_MANY,
                            )
                        )
                        tables[table2_key].relationships.append(
                            RelationshipInfo(
                                table_name=table2_key[1],
                                related_table_name=fk1.foreign_table_name,
                                relation_type=RelationType.MANY_TO_MANY,
                            )
                        )

        fk_columns = [fk for fk in table.foreign_keys if fk.foreign_table_name == foreign_table_name]
        if len(fk_columns) == 1:
            # ⚠ These two flags are ALWAYS False here, and that is load-bearing rather than a
            # bug: `construct_tables` runs this step *before*
            # `update_columns_with_constraints`, and no source populates `primary`/`is_unique`
            # on a column row (the 12-tuple column contract carries neither), so the
            # ONE_TO_ONE branch below is dead in the real pipeline -- every edge lands on
            # ONE_TO_MANY or MANY_TO_MANY. That is why this site is NOT length-checked the way
            # `determine_relationship_type` is: there is no uniqueness signal here to
            # length-check. Do not "fix" it by moving the step later without re-deriving the
            # composite-key guard, because `RelationshipInfo.relation_type` DOES reach emitted
            # output -- the pydantic emitter prefers it over the FK's own type for a
            # self-referential foreign key.
            is_source_unique = any(col.name == column_name and (col.is_unique or col.primary) for col in table.columns)
            is_target_unique = any(
                col.name == foreign_column_name and (col.is_unique or col.primary) for col in foreign_table.columns
            )
            if is_source_unique and is_target_unique:
                relation_type = RelationType.ONE_TO_ONE
            else:
                relation_type = RelationType.ONE_TO_MANY
        else:
            relation_type = RelationType.MANY_TO_MANY

        tables[table_key].relationships.append(
            RelationshipInfo(
                table_name=table_key[1],
                related_table_name=foreign_table_key[1],
                relation_type=relation_type,
            )
        )
        tables[foreign_table_key].relationships.append(
            RelationshipInfo(
                table_name=foreign_table_key[1],
                related_table_name=table_key[1],
                relation_type=relation_type,
            )
        )


def _is_singly_unique(table: TableInfo, column_name: str) -> bool:
    """Whether ``column_name`` is unique **on its own** in ``table``.

    The UNIQUE mirror of the sole-primary-key length check. A composite ``UNIQUE (a, b)``
    -- which is exactly how a VIEW's composite ``<pk/>`` marker is recorded (CI5-D14a, see
    :mod:`castiron.sources.openapi.parse`) -- sets ``is_unique`` on **both** members via
    :func:`update_columns_with_constraints`, yet neither column is unique alone. Reading the
    flag by itself would call a foreign key pointing at one of them many-to-*one* and emit a
    singular attribute where a list belongs.

    When no UNIQUE constraint names the column at all, the column flag is still trusted: a
    source may set it without emitting a constraint row.

    Args:
        table: The table the column belongs to.
        column_name: The column to test.

    Returns:
        ``True`` when a single-column UNIQUE (or an unconstrained ``is_unique`` flag) covers
        the column.
    """
    unique_constraints = [c for c in table.constraints if c.type == ConstraintType.UNIQUE and column_name in c.columns]
    if unique_constraints:
        return any(len(c.columns) == 1 for c in unique_constraints)
    return any(col.name == column_name and col.is_unique for col in table.columns)


def determine_relationship_type(
    source_table: TableInfo, target_table: TableInfo, fk: ForeignKeyInfo
) -> tuple[RelationType, RelationType]:
    """Return the ``(forward, reverse)`` relation types for a foreign key.

    Uniqueness is read from sole primary keys and singly-unique columns on each side --
    **both length-checked**, since one member of a composite PRIMARY KEY or UNIQUE does not
    identify a row (see :func:`_is_singly_unique`). Both unique → ONE_TO_ONE; only the
    target unique → MANY_TO_ONE (forward); only the source unique → ONE_TO_MANY; neither →
    MANY_TO_MANY.
    """
    source_primary_constraints = [
        c for c in source_table.constraints if c.type == ConstraintType.PRIMARY_KEY and fk.column_name in c.columns
    ]
    target_primary_constraints = [
        c
        for c in target_table.constraints
        if c.type == ConstraintType.PRIMARY_KEY and fk.foreign_column_name in c.columns
    ]

    is_source_sole_primary = any(len(c.columns) == 1 for c in source_primary_constraints)
    is_target_sole_primary = any(len(c.columns) == 1 for c in target_primary_constraints)

    is_source_unique = is_source_sole_primary or _is_singly_unique(source_table, fk.column_name)
    is_target_unique = is_target_sole_primary or _is_singly_unique(target_table, fk.foreign_column_name)

    if is_source_unique and is_target_unique:
        return RelationType.ONE_TO_ONE, RelationType.ONE_TO_ONE
    elif is_target_unique:
        return RelationType.MANY_TO_ONE, RelationType.ONE_TO_MANY
    elif is_source_unique:
        return RelationType.ONE_TO_MANY, RelationType.MANY_TO_ONE
    else:
        return RelationType.MANY_TO_MANY, RelationType.MANY_TO_MANY


def analyze_table_relationships(tables: dict[tuple[str, str], TableInfo]) -> None:
    """Set forward/reverse ``relation_type`` on every FK, synthesizing reverse FKs.

    Ported verbatim (including behavior) from supabase-pydantic. The builder runs this
    twice (see :func:`build_schema`); a single further run is idempotent.
    """
    processed_constraints: set[str] = set()

    for table in tables.values():
        for fk in table.foreign_keys:
            if fk.constraint_name in processed_constraints:
                continue

            foreign_table = next(
                (t for t in tables.values() if t.name == fk.foreign_table_name and t.schema == fk.foreign_table_schema),
                None,
            )
            if not foreign_table:
                continue

            forward_type, reverse_type = determine_relationship_type(table, foreign_table, fk)
            fk.relation_type = forward_type

            existing_fk = next((f for f in foreign_table.foreign_keys if f.constraint_name == fk.constraint_name), None)
            if existing_fk:
                existing_fk.relation_type = reverse_type
            else:
                reverse_fk = ForeignKeyInfo(
                    constraint_name=fk.constraint_name,
                    column_name=fk.foreign_column_name,
                    foreign_table_name=table.name,
                    foreign_column_name=fk.column_name,
                    relation_type=reverse_type,
                )
                foreign_table.foreign_keys.append(reverse_fk)

            processed_constraints.add(fk.constraint_name)


def is_bridge_table(table: TableInfo) -> bool:
    """Whether the table is a pure bridge (its whole PK is composed of FK columns)."""
    if len(table.foreign_keys) < 2:
        return False

    primary_foreign_keys = [
        col.name
        for col in table.columns
        if col.primary and any(fk.column_name == col.name for fk in table.foreign_keys)
    ]
    if len(primary_foreign_keys) < 2:
        return False

    primary_keys = [col.name for col in table.columns if col.primary]
    return len(primary_foreign_keys) == len(primary_keys)


def analyze_bridge_tables(tables: dict[tuple[str, str], TableInfo]) -> None:
    """Flag bridge tables and force their foreign keys to MANY_TO_MANY."""
    for table in tables.values():
        table.is_bridge = is_bridge_table(table)
        if table.is_bridge:
            for fk in table.foreign_keys:
                fk.relation_type = RelationType.MANY_TO_MANY


# ---------------------------------------------------------------------------
# Enums / user-defined types.
# ---------------------------------------------------------------------------


def get_enum_types(enum_types: Sequence[Row], schema: str | None = None) -> list[UserEnumType]:
    """Parse enum-type rows into :class:`UserEnumType` DTOs (only ``typtype == 'e'``)."""
    enums: list[UserEnumType] = []
    for row in enum_types:
        (type_name, namespace, owner, category, is_defined, typtype, enum_values) = row
        if schema is not None and namespace != schema:
            continue
        if typtype == 'e':
            enums.append(UserEnumType(type_name, namespace, owner, category, is_defined, typtype, enum_values))
    return enums


def get_user_type_mappings(enum_type_mapping: Sequence[Row], schema: str | None = None) -> list[UserTypeMapping]:
    """Parse enum-type-mapping rows into :class:`UserTypeMapping` DTOs."""
    mappings: list[UserTypeMapping] = []
    for row in enum_type_mapping:
        (column_name, table_name, namespace, type_name, type_category, type_description) = row
        if schema is not None and namespace != schema:
            continue
        mappings.append(UserTypeMapping(column_name, table_name, namespace, type_name, type_category, type_description))
    return mappings


def _rank_enum_candidates(
    namespaces: Sequence[str],
    token_namespace: str | None,
    default_schema: str,
    allow_any_schema: bool,
) -> list[int]:
    """Return the indexes of ``namespaces`` to try, best first, for one type token.

    This is the single statement of castiron's enum-resolution order; every matching site
    calls it so the rule cannot be re-derived (and re-broken) per call site. ``namespaces``
    holds the owning schema of each *name-matching* candidate, in registry order.

    The order is:

    1. the token's own namespace, when it is schema-qualified;
    2. otherwise ``default_schema`` -- **a bare token means the schema under
       construction.** PostgREST omits the prefix exactly when the type is in
       ``search_path``, so ``status`` is ``public.status``, not "whichever namespace sorts
       first". Enum rows arrive sorted by ``(namespace, type_name)``, so without this rule
       a bare token deterministically binds to the alphabetically-first schema;
    3. any remaining namespace, in registry order — **always** for a bare token, and for a
       qualified one only when ``allow_any_schema``.

    Read step 3's guard literally (``if token_namespace is not None and not
    allow_any_schema``): ``allow_any_schema=False`` restricts a *qualified* token only, so a
    bare token falls through to every namespace regardless of it. No caller exercises that
    corner — all three pass ``allow_any_schema=True`` whenever the token is bare — but the
    flag does not mean "never leave the preferred namespace", and a reader who assumes it
    does will mis-predict this function.

    Step 3 is disabled for a *qualified* token because naming a schema castiron has no enum
    for is a statement, not a gap, and silently binding to a same-named enum elsewhere is the
    entire bug class this function exists to prevent. **One caller opts out of that
    protection on purpose:** :func:`_find_enum_type` passes ``allow_any_schema=True`` even
    though its namespace is always supplied, because that namespace comes from a source's
    own column→type mapping row rather than from a user-written token -- see its docstring.
    Every other site passes ``allow_any_schema=<the token was bare>``.

    Qualification is decided by ``token_namespace is not None``, never by truthiness: a
    degenerate leading-dot token (``.status``) splits to an **empty** namespace, and reading
    that as "bare" made one document resolve the same token differently at each call site.

    Args:
        namespaces: The owning schema of each name-matching candidate, in registry order.
        token_namespace: The token's schema qualifier, or ``None`` when it is bare.
        default_schema: The schema currently being built.
        allow_any_schema: Whether an unrelated namespace may be used as a last resort for a
            **qualified** token (a bare token always may — see step 3).

    Returns:
        Candidate indexes in preference order; empty when nothing is acceptable.
    """
    wanted = default_schema.lower() if token_namespace is None else token_namespace.lower()
    preferred = [i for i, ns in enumerate(namespaces) if ns.lower() == wanted]
    if token_namespace is not None and not allow_any_schema:
        return preferred
    return preferred + [i for i, ns in enumerate(namespaces) if ns.lower() != wanted]


def _find_enum_type(
    enums: Sequence[UserEnumType],
    namespace: str,
    type_name: str,
    default_schema: str = 'public',
) -> UserEnumType | None:
    """Return the enum named ``namespace.type_name``, or ``None``.

    An exact ``(namespace, type_name)`` match wins; failing that **any** namespace's
    same-named enum is accepted, in registry order. That second tier is the one deliberate
    exception to :func:`_rank_enum_candidates`'s qualified-token rule (``allow_any_schema``
    is passed ``True`` here): ``namespace`` arrives from a source's own column→type mapping
    row, not from a token a user wrote, so a source that reports the owning schema
    inconsistently would otherwise lose the enum entirely. It never fires when the
    namespaces line up.

    ``default_schema`` is inert at this site -- ``namespace`` is always supplied, so the
    bare-token tier cannot be reached from here. It is accepted and forwarded anyway so the
    resolution order stays stated in exactly one place.
    """
    matching = [e for e in enums if e.type_name == type_name]
    ranked = _rank_enum_candidates([e.namespace for e in matching], namespace, default_schema, allow_any_schema=True)
    return matching[ranked[0]] if ranked else None


def _find_enum_type_for_token(
    enums: Sequence[UserEnumType],
    type_token: str,
    default_schema: str,
) -> UserEnumType | None:
    """Return the enum a raw type token names, honoring its namespace (or the default)."""
    token_namespace, bare_name = split_type_name(type_token)
    if not bare_name:
        return None
    matching = [e for e in enums if e.matches_type_name(type_token)]
    ranked = _rank_enum_candidates(
        [e.namespace for e in matching],
        token_namespace,
        default_schema,
        allow_any_schema=token_namespace is None,
    )
    return matching[ranked[0]] if ranked else None


def add_user_defined_types_to_tables(
    tables: dict[tuple[str, str], TableInfo],
    schema: str,
    enum_types: Sequence[Row],
    enum_type_mapping: Sequence[Row],
) -> None:
    """Attach :class:`EnumInfo` to columns from enum-type + mapping rows.

    Handles both direct column→enum mappings and array columns whose element type is an
    enum. The array branch guards on the *raw* array signal (``raw_type`` is ``'array'``
    or ends with ``'[]'`` and an element type is present), since castiron does not
    resolve a Python type; enum-name normalization is delegated to
    :meth:`UserEnumType.matches_type_name`.

    Both branches are **namespace-aware** via :func:`_rank_enum_candidates`: two schemas
    may define a same-named enum, and matching on the bare name alone gives one of the
    columns the other's member list -- silently wrong generated code, with no warning.
    An unqualified token resolves against ``schema``, the schema under construction.
    """
    enums = get_enum_types(enum_types)
    mappings = get_user_type_mappings(enum_type_mapping)

    # Direct column→enum mappings.
    for mapping in mappings:
        table_key = (schema, mapping.table_name)
        enum_info = _find_enum_type(enums, mapping.namespace, mapping.type_name, schema)
        enum_values = enum_info.enum_values if enum_info else None
        if table_key in tables:
            for col in tables[table_key].columns:
                if col.name == mapping.column_name:
                    col.user_defined_values = enum_values
                    col.enum_info = None
                    if enum_info:
                        # The MATCHED enum's namespace, not the mapping's: when the
                        # fallback fires they differ, and recording the mapping's would
                        # name a schema that does not own the type.
                        col.enum_info = EnumInfo(
                            name=enum_info.type_name, values=enum_info.enum_values, schema=enum_info.namespace
                        )
                    break

    # Array columns whose element type is an enum.
    for table in tables.values():
        for col in table.columns:
            if col.enum_info is not None:
                continue
            is_array = col.raw_type.lower() == 'array' or col.raw_type.lower().endswith('[]')
            if not is_array or col.array_element_type is None:
                continue

            matched_enum = _find_enum_type_for_token(enums, col.array_element_type, schema)
            if matched_enum:
                col.enum_info = EnumInfo(
                    name=matched_enum.type_name, values=matched_enum.enum_values, schema=matched_enum.namespace
                )
            else:
                logger.debug(f'No enum matched array element type {col.array_element_type} on {table.name}.{col.name}')


def _collect_enum_registry(tables: dict[tuple[str, str], TableInfo]) -> list[EnumInfo]:
    """Return the de-duplicated, sorted set of enums referenced by any column.

    De-duplication is keyed on ``(schema, name)``; the result is sorted for
    deterministic emitter output.
    """
    registry: dict[tuple[str, str], EnumInfo] = {}
    for table in tables.values():
        for col in table.columns:
            if col.enum_info is not None:
                key = (col.enum_info.schema, col.enum_info.name)
                if key not in registry:
                    registry[key] = col.enum_info
    return [registry[key] for key in sorted(registry)]


# ---------------------------------------------------------------------------
# Functions / RPCs.
# ---------------------------------------------------------------------------


def _match_enum(
    type_token: str | None,
    enums: Sequence[EnumInfo],
    default_schema: str = 'public',
) -> EnumInfo | None:
    """Return the registered enum named by ``type_token``, or ``None``.

    Decoration is stripped by :func:`split_type_name`, then :func:`_rank_enum_candidates`
    applies castiron's single enum-resolution order: a qualified token must match its own
    schema, and a bare token resolves against ``default_schema`` before anything else.
    """
    if not type_token:
        return None
    namespace, bare_name = split_type_name(type_token)
    if not bare_name:
        return None

    lowered = bare_name.lower()
    matching = [e for e in enums if e.name.lower() == lowered]
    ranked = _rank_enum_candidates(
        [e.schema for e in matching],
        namespace,
        default_schema,
        allow_any_schema=namespace is None,
    )
    return matching[ranked[0]] if ranked else None


def construct_parameters(
    parameter_rows: Sequence[Row],
    enums: Sequence[EnumInfo] = (),
    schema: str = 'public',
) -> list[ParameterInfo]:
    """Parse parameter rows (5-tuple contract) into :class:`ParameterInfo` nodes.

    Args:
        parameter_rows: The parameter rows, in the source's (pg argument) order.
        enums: The schema's enum registry, used to link a parameter whose type names one.
        schema: The schema under construction -- an unqualified parameter type token
            resolves against it before any other namespace.

    Returns:
        The parameters, in row order.
    """
    parameters: list[ParameterInfo] = []
    for row in parameter_rows:
        (name, raw_type, raw_mode, has_default, array_element_type) = row
        parameters.append(
            ParameterInfo(
                name=name,
                raw_type=raw_type,
                mode=normalize_parameter_mode(raw_mode),
                has_default=bool(has_default),
                array_element_type=array_element_type,
                enum_info=_match_enum(array_element_type or raw_type, enums, schema),
            )
        )
    return parameters


def construct_functions(
    function_details: Sequence[Row],
    schema: str = 'public',
    enums: Sequence[EnumInfo] = (),
) -> list[FunctionInfo]:
    """Parse function rows (8-tuple contract) into :class:`FunctionInfo` nodes.

    Raw source codes are normalized here (``raw_volatility`` →
    :class:`~castiron.ir.models.FunctionVolatility`, a parameter's ``raw_mode`` →
    :class:`~castiron.ir.models.ParameterMode`), mirroring how constraint codes are
    handled. Rows are *not* filtered by ``schema`` -- each row carries its own, matching
    how :func:`get_enum_types` is called unfiltered. Function order is row order: ordering
    is the source's responsibility (Hard Rule #9).

    Args:
        function_details: Function rows (8-tuple contract).
        schema: The schema name to fall back on when a row does not carry one.
        enums: The schema's enum registry, used to link parameter enum types.

    Returns:
        The functions, in row order.
    """
    functions: list[FunctionInfo] = []
    for row in function_details:
        (
            row_schema,
            function_name,
            description,
            return_type,
            returns_set,
            raw_volatility,
            is_read_only,
            parameters,
        ) = row
        functions.append(
            FunctionInfo(
                name=function_name,
                schema=row_schema if row_schema else schema,
                # The function's OWN schema resolves its bare parameter tokens: an
                # unqualified type in an `audit` function means `audit.<type>`.
                parameters=construct_parameters(parameters or (), enums, row_schema if row_schema else schema),
                return_type=return_type,
                returns_set=returns_set,
                volatility=normalize_volatility(raw_volatility),
                is_read_only=is_read_only,
                description=description,
            )
        )
    return functions


# ---------------------------------------------------------------------------
# Public entrypoint.
# ---------------------------------------------------------------------------


def construct_tables(
    column_details: Sequence[Row],
    fk_details: Sequence[Row],
    constraints: Sequence[Row],
    enum_types: Sequence[Row],
    enum_type_mapping: Sequence[Row],
    schema: str = 'public',
    disable_model_prefix_protection: bool = False,
    table_details: Sequence[Row] = (),
) -> dict[tuple[str, str], TableInfo]:
    """Run the full construction pipeline, returning the tables keyed by ``(schema, name)``.

    Reproduces supabase-pydantic's ``construct_table_info`` step order, including the
    faithful double-run of :func:`analyze_table_relationships` (a known upstream TODO,
    carried for output parity — decision D7).

    Args:
        column_details: Column rows (12-tuple contract).
        fk_details: Foreign-key rows (7-tuple contract).
        constraints: Constraint rows (5-tuple contract).
        enum_types: Enum-type rows (7-tuple contract).
        enum_type_mapping: Enum-type-mapping rows (6-tuple contract).
        schema: The schema name these rows belong to.
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.
        table_details: Table rows (3-tuple contract). Appended **last** and defaulted, so a
            caller passing ``schema``/``disable_model_prefix_protection`` positionally is
            unaffected. Descriptions are inert for every later step, so this only has to run
            once the tables exist.

    Returns:
        The tables keyed by ``(schema, table_name)``.
    """
    tables = get_table_details_from_columns(column_details, disable_model_prefix_protection)
    add_table_descriptions_to_table_details(tables, table_details)
    add_foreign_key_info_to_table_details(tables, fk_details, disable_model_prefix_protection)
    add_constraints_to_table_details(tables, schema, constraints, disable_model_prefix_protection)
    add_relationships_to_table_details(tables, fk_details)
    add_user_defined_types_to_tables(tables, schema, enum_types, enum_type_mapping)
    update_columns_with_constraints(tables)
    update_column_constraint_definitions(tables)
    analyze_bridge_tables(tables)
    for _ in range(2):
        analyze_table_relationships(tables)
    return tables


def build_schema(
    column_details: Sequence[Row],
    fk_details: Sequence[Row],
    constraints: Sequence[Row],
    enum_types: Sequence[Row],
    enum_type_mapping: Sequence[Row],
    schema: str = 'public',
    disable_model_prefix_protection: bool = False,
    function_details: Sequence[Row] = (),
    table_details: Sequence[Row] = (),
) -> Schema:
    """Build a :class:`~castiron.ir.models.Schema` from the tuple contracts.

    This is castiron's canonical construction entrypoint: pluggable sources emit the
    documented positional rows and every emitter consumes the returned ``Schema``. No
    database or network access is involved, and no Python type is resolved.

    ``function_details`` and ``table_details`` are deliberately the **last** two parameters
    rather than the sixth and seventh, so that a caller passing
    ``schema``/``disable_model_prefix_protection`` positionally is unaffected (see the
    module docstring).

    Args:
        column_details: Column rows (12-tuple contract).
        fk_details: Foreign-key rows (7-tuple contract).
        constraints: Constraint rows (5-tuple contract).
        enum_types: Enum-type rows (7-tuple contract).
        enum_type_mapping: Enum-type-mapping rows (6-tuple contract).
        schema: The schema name these rows belong to.
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.
        function_details: Function rows (8-tuple contract). Defaults to empty, so a source
            that exposes no functions produces the same ``Schema`` it always did.
        table_details: Table rows (3-tuple contract). Defaults to empty, so a source that
            cannot see table comments produces the same ``Schema`` it always did, with
            every ``TableInfo.description`` left ``None``.

    Returns:
        A fully-populated, deterministic :class:`~castiron.ir.models.Schema`.
    """
    tables = construct_tables(
        column_details,
        fk_details,
        constraints,
        enum_types,
        enum_type_mapping,
        schema,
        disable_model_prefix_protection,
        table_details,
    )
    enums = _collect_enum_registry(tables)
    return Schema(
        tables=list(tables.values()),
        enums=enums,
        functions=construct_functions(function_details, schema, enums),
    )
