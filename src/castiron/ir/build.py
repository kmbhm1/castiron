"""Tuple-contract → Schema IR builder.

A faithful port of supabase-pydantic's ``construct_table_info`` pipeline and its
marshalers (schema / relationships / constraints / column). It turns the five
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


def normalize_constraint_type(raw_constraint_type: str) -> ConstraintType:
    """Map a raw source constraint code to a normalized :class:`ConstraintType`."""
    return CONSTRAINT_TYPE_MAP.get(raw_constraint_type.lower(), ConstraintType.OTHER)


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

        Handles leading underscores (pg array types), a trailing ``[]``, surrounding
        quotes, schema qualification (``public.Foo``), and case-insensitivity.
        """
        if not type_name:
            return False

        clean_name = type_name
        while clean_name.startswith('_'):
            clean_name = clean_name[1:]

        if clean_name.endswith('[]'):
            clean_name = clean_name[:-2]

        if clean_name.startswith('"') and clean_name.endswith('"'):
            clean_name = clean_name[1:-1]

        if '.' in clean_name:
            clean_name = clean_name.split('.')[-1]

        return self.type_name.lower() == clean_name.lower()


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
# Foreign keys.
# ---------------------------------------------------------------------------


def add_foreign_key_info_to_table_details(tables: dict[tuple[str, str], TableInfo], fk_details: Sequence[Row]) -> None:
    """Parse FK rows into :class:`ForeignKeyInfo`, skipping edges to absent tables.

    Column names are standardized (reserved-name protection). A first-pass relationship
    type is inferred from primary-key shape; :func:`analyze_table_relationships` refines
    it afterwards.
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
            column_name=standardize_column_name(column_name) or column_name,
            foreign_table_name=foreign_table_name,
            foreign_column_name=standardize_column_name(foreign_column_name) or foreign_column_name,
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
    tables: dict[tuple[str, str], TableInfo], schema: str, constraints: Sequence[Row]
) -> None:
    """Parse constraint rows into :class:`ConstraintInfo` and attach them to their table.

    A leading ``{schema}.`` on the table name is stripped, and the raw constraint code is
    normalized to a :class:`ConstraintType` (the raw code is retained for fidelity).
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
                columns=[standardize_column_name(c) or str(c) for c in columns],
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


def determine_relationship_type(
    source_table: TableInfo, target_table: TableInfo, fk: ForeignKeyInfo
) -> tuple[RelationType, RelationType]:
    """Return the ``(forward, reverse)`` relation types for a foreign key.

    Uniqueness is read from sole primary keys and unique columns on each side: both
    unique → ONE_TO_ONE; only the target unique → MANY_TO_ONE (forward); only the source
    unique → ONE_TO_MANY; neither → MANY_TO_MANY.
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

    is_source_unique = is_source_sole_primary or any(
        col.name == fk.column_name and col.is_unique for col in source_table.columns
    )
    is_target_unique = is_target_sole_primary or any(
        col.is_unique and col.name == fk.foreign_column_name for col in target_table.columns
    )

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
    """
    enums = get_enum_types(enum_types)
    mappings = get_user_type_mappings(enum_type_mapping)

    # Direct column→enum mappings.
    for mapping in mappings:
        table_key = (schema, mapping.table_name)
        enum_info = next((e for e in enums if e.type_name == mapping.type_name), None)
        enum_values = enum_info.enum_values if enum_info else None
        if table_key in tables:
            for col in tables[table_key].columns:
                if col.name == mapping.column_name:
                    col.user_defined_values = enum_values
                    col.enum_info = None
                    if enum_info:
                        col.enum_info = EnumInfo(
                            name=enum_info.type_name, values=enum_info.enum_values, schema=mapping.namespace
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

            matched_enum = next((e for e in enums if e.matches_type_name(col.array_element_type)), None)
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
) -> dict[tuple[str, str], TableInfo]:
    """Run the full construction pipeline, returning the tables keyed by ``(schema, name)``.

    Reproduces supabase-pydantic's ``construct_table_info`` step order, including the
    faithful double-run of :func:`analyze_table_relationships` (a known upstream TODO,
    carried for output parity — decision D7).
    """
    tables = get_table_details_from_columns(column_details, disable_model_prefix_protection)
    add_foreign_key_info_to_table_details(tables, fk_details)
    add_constraints_to_table_details(tables, schema, constraints)
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
) -> Schema:
    """Build a :class:`~castiron.ir.models.Schema` from the five tuple contracts.

    This is castiron's canonical construction entrypoint: pluggable sources emit the
    documented positional rows and every emitter consumes the returned ``Schema``. No
    database or network access is involved, and no Python type is resolved.

    Args:
        column_details: Column rows (12-tuple contract).
        fk_details: Foreign-key rows (7-tuple contract).
        constraints: Constraint rows (5-tuple contract).
        enum_types: Enum-type rows (7-tuple contract).
        enum_type_mapping: Enum-type-mapping rows (6-tuple contract).
        schema: The schema name these rows belong to.
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.

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
    )
    return Schema(tables=list(tables.values()), enums=_collect_enum_registry(tables))
