"""Unit tests for the tuple-contract → Schema IR builder (castiron.ir.build)."""

import json

import pytest

from castiron.ir import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    ForeignKeyInfo,
    RelationType,
    Schema,
    TableInfo,
    build_schema,
    construct_tables,
)
from castiron.ir.build import (
    UserEnumType,
    UserTypeMapping,
    add_foreign_key_info_to_table_details,
    add_relationships_to_table_details,
    analyze_bridge_tables,
    analyze_table_relationships,
    determine_relationship_type,
    get_enum_types,
    get_unique_columns_from_constraints,
    get_user_type_mappings,
    is_bridge_table,
    normalize_constraint_type,
    parse_constraint_definition_for_fk,
    standardize_column_name,
    update_column_constraint_definitions,
    update_columns_with_constraints,
)

# ---------------------------------------------------------------------------
# Tuple-contract fixtures.
# ---------------------------------------------------------------------------


def _users_orders_columns() -> list[tuple]:
    return [
        (
            'public',
            'users',
            'user_id',
            'uuid_generate_v4()',
            'NO',
            'uuid',
            None,
            'BASE TABLE',
            None,
            'uuid',
            None,
            None,
        ),
        ('public', 'users', 'email', None, 'YES', 'text', 255, 'BASE TABLE', None, 'text', None, None),
        (
            'public',
            'users',
            'seq',
            "nextval('users_seq'::regclass)",
            'NO',
            'integer',
            None,
            'BASE TABLE',
            None,
            'int4',
            None,
            None,
        ),
        ('public', 'orders', 'order_id', None, 'NO', 'integer', None, 'BASE TABLE', 'ALWAYS', 'int4', None, None),
        ('public', 'orders', 'user_id', None, 'YES', 'uuid', None, 'BASE TABLE', None, 'uuid', None, None),
        ('public', 'orders', 'age', None, 'YES', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
    ]


def _users_orders_fks() -> list[tuple]:
    return [('public', 'orders', 'user_id', 'public', 'users', 'user_id', 'fk_orders_user')]


def _users_orders_constraints() -> list[tuple]:
    return [
        ('pk_users', 'users', ['user_id'], 'p', 'PRIMARY KEY (user_id)'),
        ('uniq_email', 'users', ['email'], 'u', 'UNIQUE (email)'),
        ('pk_orders', 'public.orders', ['order_id'], 'p', 'PRIMARY KEY (order_id)'),
        ('fk_orders_user', 'orders', ['user_id'], 'f', 'FOREIGN KEY (user_id) REFERENCES users(user_id)'),
        ('age_check', 'orders', ['age'], 'c', 'CHECK (age >= 0)'),
    ]


def _build_users_orders() -> Schema:
    return build_schema(_users_orders_columns(), _users_orders_fks(), _users_orders_constraints(), [], [])


def _table(schema: Schema, name: str) -> TableInfo:
    return next(t for t in schema.tables if t.name == name)


def _col(table: TableInfo, name: str) -> ColumnInfo:
    return next(c for c in table.columns if c.name == name)


# ---------------------------------------------------------------------------
# Full-pipeline structural fills.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_schema_keys_tables_and_columns() -> None:
    schema = _build_users_orders()
    names = {t.name for t in schema.tables}
    assert names == {'users', 'orders'}
    assert len(_table(schema, 'users').columns) == 3
    assert len(_table(schema, 'orders').columns) == 3


@pytest.mark.unit
def test_build_schema_sets_column_flags_from_constraints() -> None:
    schema = _build_users_orders()
    users = _table(schema, 'users')
    orders = _table(schema, 'orders')

    assert _col(users, 'user_id').primary is True
    assert _col(users, 'email').is_unique is True
    assert _col(users, 'email').unique_partners == ['email']
    assert _col(orders, 'user_id').is_foreign_key is True
    assert _col(orders, 'order_id').primary is True
    # CHECK constraint definition copied onto the column.
    assert _col(orders, 'age').constraint_definition == 'CHECK (age >= 0)'


@pytest.mark.unit
def test_build_schema_identity_and_generated_flags() -> None:
    schema = _build_users_orders()
    users = _table(schema, 'users')
    orders = _table(schema, 'orders')

    # Identity column (identity_generation is non-None).
    assert _col(orders, 'order_id').is_identity is True
    assert _col(orders, 'order_id').is_generated is True
    # Serial via nextval default → generated but not identity.
    assert _col(users, 'seq').is_identity is False
    assert _col(users, 'seq').is_generated is True
    # Plain default → not generated.
    assert _col(users, 'user_id').is_generated is False


@pytest.mark.unit
def test_build_schema_strips_schema_prefix_on_constraint_table_name() -> None:
    # pk_orders was declared against 'public.orders'; it must still attach to orders.
    schema = _build_users_orders()
    orders = _table(schema, 'orders')
    assert any(c.constraint_name == 'pk_orders' for c in orders.constraints)


@pytest.mark.unit
def test_build_schema_foreign_key_parsed_and_relation_type() -> None:
    schema = _build_users_orders()
    orders = _table(schema, 'orders')
    users = _table(schema, 'users')

    orders_fk = next(fk for fk in orders.foreign_keys if fk.constraint_name == 'fk_orders_user')
    assert orders_fk.foreign_table_name == 'users'
    assert orders_fk.foreign_column_name == 'user_id'
    # After the double-run analysis: many orders → one user.
    assert orders_fk.relation_type == RelationType.MANY_TO_ONE

    # A reverse FK is synthesized on the users side with the inverse type.
    reverse = next(fk for fk in users.foreign_keys if fk.constraint_name == 'fk_orders_user')
    assert reverse.foreign_table_name == 'orders'
    assert reverse.relation_type == RelationType.ONE_TO_MANY


@pytest.mark.unit
def test_build_schema_skips_fk_to_absent_table() -> None:
    columns = [
        ('public', 'orders', 'order_id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
        ('public', 'orders', 'ghost_id', None, 'YES', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
    ]
    fks = [('public', 'orders', 'ghost_id', 'public', 'ghost', 'id', 'fk_ghost')]
    schema = build_schema(columns, fks, [], [], [])
    orders = _table(schema, 'orders')
    assert orders.foreign_keys == []


@pytest.mark.unit
def test_build_schema_column_name_aliasing() -> None:
    columns = [
        ('public', 't', 'class', None, 'YES', 'text', None, 'BASE TABLE', None, 'text', None, None),
        ('public', 't', 'id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
    ]
    schema = build_schema(columns, [], [], [], [])
    table = _table(schema, 't')
    # 'class' is a python keyword → renamed with an alias preserving the original.
    class_col = _col(table, 'field_class')
    assert class_col.alias == 'class'
    # 'id' is a curated exception → not renamed, no alias.
    assert _col(table, 'id').alias is None
    assert table.aliasing_in_columns() is True


@pytest.mark.unit
def test_build_schema_view_has_no_primary_key() -> None:
    columns = [
        ('public', 'v', 'x', None, 'YES', 'integer', None, 'VIEW', None, 'int4', None, None),
    ]
    constraints = [('pk_v', 'v', ['x'], 'p', 'PRIMARY KEY (x)')]
    schema = build_schema(columns, [], constraints, [], [])
    view = _table(schema, 'v')
    assert view.table_type == 'VIEW'
    assert view.primary_key() == []


@pytest.mark.unit
def test_build_schema_empty_inputs() -> None:
    schema = build_schema([], [], [], [], [])
    assert schema.tables == []
    assert schema.enums == []
    assert schema.as_dict() == {'tables': [], 'enums': []}


# ---------------------------------------------------------------------------
# Determinism & idempotence (Hard Rule #9).
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_schema_is_deterministic() -> None:
    args = (_users_orders_columns(), _users_orders_fks(), _users_orders_constraints(), [], [])
    first = build_schema(*args)
    second = build_schema(*args)
    assert first == second
    assert json.dumps(first.as_dict()) == json.dumps(second.as_dict())


@pytest.mark.unit
def test_relationship_analysis_is_idempotent_after_double_run() -> None:
    tables = construct_tables(_users_orders_columns(), _users_orders_fks(), _users_orders_constraints(), [], [])
    before = Schema(tables=list(tables.values())).as_dict()
    # A third analysis pass must not change anything the ported double-run produced.
    analyze_table_relationships(tables)
    after = Schema(tables=list(tables.values())).as_dict()
    assert before == after


# ---------------------------------------------------------------------------
# Enums.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_schema_direct_enum_attachment() -> None:
    columns = [
        (
            'public',
            'orders',
            'status',
            None,
            'YES',
            'USER-DEFINED',
            None,
            'BASE TABLE',
            None,
            'order_status',
            None,
            None,
        ),
    ]
    enum_types = [('order_status', 'public', 'owner', 'E', True, 'e', ['pending', 'shipped'])]
    enum_mapping = [('status', 'orders', 'public', 'order_status', 'E', 'desc')]

    schema = build_schema(columns, [], [], enum_types, enum_mapping)
    status = _col(_table(schema, 'orders'), 'status')
    assert status.enum_info is not None
    assert status.enum_info.name == 'order_status'
    assert status.enum_info.values == ['pending', 'shipped']
    assert status.user_defined_values == ['pending', 'shipped']

    # Deduped schema-level registry carries the enum exactly once.
    assert len(schema.enums) == 1
    assert schema.enums[0].name == 'order_status'


@pytest.mark.unit
def test_build_schema_array_element_enum_attachment() -> None:
    columns = [
        ('public', 'events', 'moods', None, 'YES', 'ARRAY', None, 'BASE TABLE', None, '_mood', '_mood', None),
    ]
    enum_types = [('mood', 'public', 'owner', 'E', True, 'e', ['happy', 'sad'])]

    schema = build_schema(columns, [], [], enum_types, [])
    moods = _col(_table(schema, 'events'), 'moods')
    # Matched via UserEnumType.matches_type_name normalization ('_mood' → 'mood').
    assert moods.enum_info is not None
    assert moods.enum_info.name == 'mood'
    assert schema.enums[0].name == 'mood'


@pytest.mark.unit
def test_build_schema_array_element_no_enum_match() -> None:
    columns = [
        ('public', 'events', 'nums', None, 'YES', 'ARRAY', None, 'BASE TABLE', None, '_int4', '_int4', None),
    ]
    schema = build_schema(columns, [], [], [], [])
    nums = _col(_table(schema, 'events'), 'nums')
    assert nums.enum_info is None
    assert schema.enums == []


@pytest.mark.unit
def test_build_schema_mapping_to_absent_table_or_column_is_safe() -> None:
    columns = [
        (
            'public',
            'orders',
            'status',
            None,
            'YES',
            'USER-DEFINED',
            None,
            'BASE TABLE',
            None,
            'order_status',
            None,
            None,
        ),
    ]
    enum_types = [('order_status', 'public', 'owner', 'E', True, 'e', ['a'])]
    enum_mapping = [
        ('status', 'ghost_table', 'public', 'order_status', 'E', 'd'),  # table not present
        ('ghost_col', 'orders', 'public', 'order_status', 'E', 'd'),  # column not present
    ]
    # Must not raise; the real column stays unattached.
    schema = build_schema(columns, [], [], enum_types, enum_mapping)
    assert _col(_table(schema, 'orders'), 'status').enum_info is None
    assert schema.enums == []


@pytest.mark.unit
def test_build_schema_mapping_to_non_enum_type_sets_none_values() -> None:
    columns = [
        ('public', 'orders', 'kind', None, 'YES', 'USER-DEFINED', None, 'BASE TABLE', None, 'composite_t', None, None),
    ]
    # A mapping whose type_name is not among the enum rows → enum_info stays None,
    # user_defined_values becomes None (faithful to supabase-pydantic).
    enum_mapping = [('kind', 'orders', 'public', 'composite_t', 'C', 'd')]
    schema = build_schema(columns, [], [], [], enum_mapping)
    kind = _col(_table(schema, 'orders'), 'kind')
    assert kind.enum_info is None
    assert kind.user_defined_values is None


# ---------------------------------------------------------------------------
# Bridge tables.
# ---------------------------------------------------------------------------


def _bridge_inputs() -> tuple[list[tuple], list[tuple], list[tuple]]:
    columns = [
        ('public', 'student', 'id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
        ('public', 'course', 'id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
        ('public', 'enrollment', 'student_id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
        ('public', 'enrollment', 'course_id', None, 'NO', 'integer', None, 'BASE TABLE', None, 'int4', None, None),
    ]
    fks = [
        ('public', 'enrollment', 'student_id', 'public', 'student', 'id', 'fk_enr_student'),
        ('public', 'enrollment', 'course_id', 'public', 'course', 'id', 'fk_enr_course'),
    ]
    constraints = [
        ('pk_student', 'student', ['id'], 'p', 'PRIMARY KEY (id)'),
        ('pk_course', 'course', ['id'], 'p', 'PRIMARY KEY (id)'),
        ('pk_enrollment', 'enrollment', ['student_id', 'course_id'], 'p', 'PRIMARY KEY (student_id, course_id)'),
        ('fk_enr_student', 'enrollment', ['student_id'], 'f', 'FOREIGN KEY (student_id) REFERENCES student(id)'),
        ('fk_enr_course', 'enrollment', ['course_id'], 'f', 'FOREIGN KEY (course_id) REFERENCES course(id)'),
    ]
    return columns, fks, constraints


@pytest.mark.unit
def test_build_schema_detects_bridge_table() -> None:
    columns, fks, constraints = _bridge_inputs()
    schema = build_schema(columns, fks, constraints, [], [])
    assert _table(schema, 'enrollment').is_bridge is True
    assert _table(schema, 'student').is_bridge is False


@pytest.mark.unit
def test_analyze_bridge_tables_forces_many_to_many() -> None:
    columns, fks, constraints = _bridge_inputs()
    tables = construct_tables(columns, fks, constraints, [], [])
    # Re-run the bridge pass in isolation to observe the MANY_TO_MANY forcing directly
    # (in the full pipeline analyze_table_relationships later refines FK relation types).
    analyze_bridge_tables(tables)
    enrollment = tables[('public', 'enrollment')]
    assert enrollment.is_bridge is True
    assert all(fk.relation_type == RelationType.MANY_TO_MANY for fk in enrollment.foreign_keys)


@pytest.mark.unit
def test_add_relationships_one_to_one_single_fk() -> None:
    # A single FK whose source and target columns are both unique/primary → ONE_TO_ONE.
    table1 = TableInfo(name='t1', columns=[ColumnInfo(name='id', raw_type='int', primary=True)])
    table2 = TableInfo(name='t2', columns=[ColumnInfo(name='id', raw_type='int', primary=True)])
    table1.add_foreign_key(
        ForeignKeyInfo(constraint_name='fk', column_name='id', foreign_table_name='t2', foreign_column_name='id')
    )
    tables = {('public', 't1'): table1, ('public', 't2'): table2}
    add_relationships_to_table_details(tables, [('public', 't1', 'id', 'public', 't2', 'id', 'fk')])
    assert any(
        r.related_table_name == 't2' and r.relation_type == RelationType.ONE_TO_ONE for r in table1.relationships
    )


@pytest.mark.unit
def test_add_relationships_many_to_many_multiple_fks() -> None:
    # Two FKs from t1 to t2 → MANY_TO_MANY on the derived relationships.
    table1 = TableInfo(name='t1')
    table2 = TableInfo(name='t2')
    table1.add_foreign_key(
        ForeignKeyInfo(constraint_name='fk1', column_name='a', foreign_table_name='t2', foreign_column_name='id')
    )
    table1.add_foreign_key(
        ForeignKeyInfo(constraint_name='fk2', column_name='b', foreign_table_name='t2', foreign_column_name='id')
    )
    tables = {('public', 't1'): table1, ('public', 't2'): table2}
    add_relationships_to_table_details(tables, [('public', 't1', 'a', 'public', 't2', 'id', 'fk1')])
    assert any(r.relation_type == RelationType.MANY_TO_MANY for r in table1.relationships)


@pytest.mark.unit
def test_is_bridge_table_requires_composite_primary_foreign_keys() -> None:
    # Two FKs but neither column is primary → not a bridge (guarded before the PK check).
    table = TableInfo(
        name='log',
        columns=[ColumnInfo(name='a', raw_type='int'), ColumnInfo(name='b', raw_type='int')],
        foreign_keys=[
            ForeignKeyInfo(constraint_name='f1', column_name='a', foreign_table_name='x', foreign_column_name='id'),
            ForeignKeyInfo(constraint_name='f2', column_name='b', foreign_table_name='y', foreign_column_name='id'),
        ],
    )
    assert is_bridge_table(table) is False


@pytest.mark.unit
def test_add_relationships_bridge_branch() -> None:
    table1 = TableInfo(name='table1')
    table2 = TableInfo(name='table2')
    bridge = TableInfo(name='bridge', is_bridge=True)
    bridge.add_foreign_key(
        ForeignKeyInfo(
            constraint_name='fk1', column_name='t1_id', foreign_table_name='table1', foreign_column_name='id'
        )
    )
    bridge.add_foreign_key(
        ForeignKeyInfo(
            constraint_name='fk2', column_name='t2_id', foreign_table_name='table2', foreign_column_name='id'
        )
    )
    tables = {
        ('public', 'table1'): table1,
        ('public', 'table2'): table2,
        ('public', 'bridge'): bridge,
    }
    fk_details = [
        ('public', 'bridge', 't1_id', 'public', 'table1', 'id', 'fk1'),
        ('public', 'bridge', 't2_id', 'public', 'table2', 'id', 'fk2'),
    ]
    add_relationships_to_table_details(tables, fk_details)

    assert any(
        r.related_table_name == 'table2' and r.relation_type == RelationType.MANY_TO_MANY for r in table1.relationships
    )
    assert any(
        r.related_table_name == 'table1' and r.relation_type == RelationType.MANY_TO_MANY for r in table2.relationships
    )


# ---------------------------------------------------------------------------
# add_foreign_key_info_to_table_details — first-pass relation inference.
# ---------------------------------------------------------------------------


def _bare(name: str) -> TableInfo:
    return TableInfo(name=name)


@pytest.mark.unit
def test_add_fk_info_missing_source_and_target() -> None:
    tables = {('public', 'table1'): _bare('table1')}
    # Missing target table.
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 't2_id', 'public', 'table2', 'id', 'fk1')])
    assert tables[('public', 'table1')].foreign_keys == []
    # Missing source table.
    add_foreign_key_info_to_table_details(tables, [('public', 'table3', 't1_id', 'public', 'table1', 'id', 'fk2')])
    assert tables[('public', 'table1')].foreign_keys == []


@pytest.mark.unit
def test_add_fk_info_one_to_one() -> None:
    table1, table2 = _bare('table1'), _bare('table2')
    table1.add_constraint(ConstraintInfo(constraint_name='pk1', type=ConstraintType.PRIMARY_KEY, columns=['id']))
    table2.add_constraint(ConstraintInfo(constraint_name='pk2', type=ConstraintType.PRIMARY_KEY, columns=['id']))
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'id', 'public', 'table2', 'id', 'fk1')])
    assert table1.foreign_keys[0].relation_type == RelationType.ONE_TO_ONE


@pytest.mark.unit
def test_add_fk_info_composite_key_is_many_to_one() -> None:
    table1, table2 = _bare('table1'), _bare('table2')
    table1.add_constraint(
        ConstraintInfo(constraint_name='pk1', type=ConstraintType.PRIMARY_KEY, columns=['id', 'other_id'])
    )
    table2.add_constraint(ConstraintInfo(constraint_name='pk2', type=ConstraintType.PRIMARY_KEY, columns=['id']))
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'other_id', 'public', 'table2', 'id', 'fk1')])
    assert table1.foreign_keys[0].relation_type == RelationType.MANY_TO_ONE


@pytest.mark.unit
def test_add_fk_info_second_fk_to_same_table_is_many_to_one() -> None:
    # Faithful to supabase-pydantic: the FK under analysis is not yet in the list, so a
    # single pre-existing FK to the target yields MANY_TO_ONE (not MANY_TO_MANY).
    table1, table2 = _bare('table1'), _bare('table2')
    table1.add_foreign_key(
        ForeignKeyInfo(
            constraint_name='fk1', column_name='ref1_id', foreign_table_name='table2', foreign_column_name='id'
        )
    )
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'ref2_id', 'public', 'table2', 'id', 'fk2')])
    assert len(table1.foreign_keys) == 2
    assert table1.foreign_keys[1].relation_type == RelationType.MANY_TO_ONE


@pytest.mark.unit
def test_add_fk_info_third_fk_to_same_table_is_many_to_many() -> None:
    # Two FKs already point at table2, so a third is inferred as MANY_TO_MANY first-pass.
    table1, table2 = _bare('table1'), _bare('table2')
    for i in (1, 2):
        table1.add_foreign_key(
            ForeignKeyInfo(
                constraint_name=f'fk{i}',
                column_name=f'ref{i}_id',
                foreign_table_name='table2',
                foreign_column_name='id',
            )
        )
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'ref3_id', 'public', 'table2', 'id', 'fk3')])
    assert table1.foreign_keys[2].relation_type == RelationType.MANY_TO_MANY


@pytest.mark.unit
def test_add_fk_info_target_sole_pk_is_one_to_one() -> None:
    # Source has no matching PK; the target's sole primary key drives ONE_TO_ONE.
    table1, table2 = _bare('table1'), _bare('table2')
    table2.add_constraint(ConstraintInfo(constraint_name='pk2', type=ConstraintType.PRIMARY_KEY, columns=['id']))
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'x', 'public', 'table2', 'id', 'fk1')])
    assert table1.foreign_keys[0].relation_type == RelationType.ONE_TO_ONE


@pytest.mark.unit
def test_add_fk_info_target_composite_key_is_many_to_one() -> None:
    # Exercises the composite-key branch on the *target* table.
    table1, table2 = _bare('table1'), _bare('table2')
    table2.add_constraint(
        ConstraintInfo(constraint_name='pk2', type=ConstraintType.PRIMARY_KEY, columns=['id', 'other'])
    )
    tables = {('public', 'table1'): table1, ('public', 'table2'): table2}
    add_foreign_key_info_to_table_details(tables, [('public', 'table1', 'x', 'public', 'table2', 'id', 'fk1')])
    assert table1.foreign_keys[0].relation_type == RelationType.MANY_TO_ONE


# ---------------------------------------------------------------------------
# determine_relationship_type — all four branches.
# ---------------------------------------------------------------------------


def _fk() -> ForeignKeyInfo:
    return ForeignKeyInfo(constraint_name='fk', column_name='a_id', foreign_table_name='b', foreign_column_name='id')


@pytest.mark.unit
def test_determine_relationship_type_one_to_one() -> None:
    source = TableInfo(
        name='a', constraints=[ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['a_id'])]
    )
    target = TableInfo(
        name='b', constraints=[ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['id'])]
    )
    assert determine_relationship_type(source, target, _fk()) == (RelationType.ONE_TO_ONE, RelationType.ONE_TO_ONE)


@pytest.mark.unit
def test_determine_relationship_type_many_to_one() -> None:
    source = TableInfo(name='a')
    target = TableInfo(
        name='b', constraints=[ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['id'])]
    )
    assert determine_relationship_type(source, target, _fk()) == (RelationType.MANY_TO_ONE, RelationType.ONE_TO_MANY)


@pytest.mark.unit
def test_determine_relationship_type_one_to_many() -> None:
    source = TableInfo(name='a', columns=[ColumnInfo(name='a_id', raw_type='int', is_unique=True)])
    target = TableInfo(name='b')
    assert determine_relationship_type(source, target, _fk()) == (RelationType.ONE_TO_MANY, RelationType.MANY_TO_ONE)


@pytest.mark.unit
def test_determine_relationship_type_many_to_many() -> None:
    assert determine_relationship_type(TableInfo(name='a'), TableInfo(name='b'), _fk()) == (
        RelationType.MANY_TO_MANY,
        RelationType.MANY_TO_MANY,
    )


# ---------------------------------------------------------------------------
# analyze_table_relationships — reverse-FK handling & skips.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_analyze_table_relationships_synthesizes_reverse_fk() -> None:
    users = TableInfo(
        name='users',
        columns=[ColumnInfo(name='id', raw_type='int', is_unique=True, primary=True)],
        constraints=[ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['id'])],
    )
    orders = TableInfo(name='orders')
    orders.add_foreign_key(
        ForeignKeyInfo(
            constraint_name='fk', column_name='user_id', foreign_table_name='users', foreign_column_name='id'
        )
    )
    tables = {('public', 'users'): users, ('public', 'orders'): orders}

    analyze_table_relationships(tables)
    assert orders.foreign_keys[0].relation_type == RelationType.MANY_TO_ONE
    # Reverse FK synthesized on the users side.
    assert len(users.foreign_keys) == 1
    assert users.foreign_keys[0].relation_type == RelationType.ONE_TO_MANY


@pytest.mark.unit
def test_analyze_table_relationships_skips_fk_to_unknown_foreign_table() -> None:
    orders = TableInfo(name='orders')
    orders.add_foreign_key(
        ForeignKeyInfo(constraint_name='fk', column_name='x', foreign_table_name='nope', foreign_column_name='id')
    )
    tables = {('public', 'orders'): orders}
    analyze_table_relationships(tables)
    # No foreign table → relation type stays unset and no reverse FK is added.
    assert orders.foreign_keys[0].relation_type is None
    assert len(orders.foreign_keys) == 1


# ---------------------------------------------------------------------------
# Constraint helpers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    'definition, expected',
    [
        ('FOREIGN KEY (user_id) REFERENCES users(id)', ('user_id', 'users', 'id')),
        ('INVALID KEY (x) REFERENCES y(z)', None),
        ('', None),
    ],
)
def test_parse_constraint_definition_for_fk(definition: str, expected: tuple[str, str, str] | None) -> None:
    assert parse_constraint_definition_for_fk(definition) == expected


@pytest.mark.unit
def test_get_unique_columns_from_constraints() -> None:
    unique = ConstraintInfo(
        constraint_name='u', type=ConstraintType.UNIQUE, columns=['a', 'b'], constraint_definition='UNIQUE (a, b)'
    )
    assert get_unique_columns_from_constraints(unique) == ['a', 'b']
    # Non-unique → empty.
    check = ConstraintInfo(
        constraint_name='c', type=ConstraintType.CHECK, columns=['a'], constraint_definition='CHECK (a > 0)'
    )
    assert get_unique_columns_from_constraints(check) == []
    # Unique but no parseable definition → empty.
    unique_no_def = ConstraintInfo(constraint_name='u2', type=ConstraintType.UNIQUE, columns=['a'])
    assert get_unique_columns_from_constraints(unique_no_def) == []


@pytest.mark.unit
def test_update_columns_with_constraints_direct() -> None:
    columns = [
        ColumnInfo(name='id', raw_type='uuid'),
        ColumnInfo(name='username', raw_type='text'),
        ColumnInfo(name='order_id', raw_type='uuid'),
    ]
    constraints = [
        ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['id']),
        ConstraintInfo(
            constraint_name='u',
            type=ConstraintType.UNIQUE,
            columns=['username'],
            constraint_definition='UNIQUE (username)',
        ),
        ConstraintInfo(constraint_name='fk', type=ConstraintType.FOREIGN_KEY, columns=['order_id']),
    ]
    table = TableInfo(name='users', columns=columns, constraints=constraints)
    tables = {
        ('public', 'users'): table,
        ('public', 'empty_cols'): TableInfo(name='empty_cols', constraints=constraints),  # skipped: no columns
    }
    update_columns_with_constraints(tables)
    assert columns[0].primary is True
    assert columns[1].is_unique is True
    assert columns[2].is_foreign_key is True


@pytest.mark.unit
def test_update_column_constraint_definitions_uses_last_check() -> None:
    column = ColumnInfo(name='age', raw_type='integer')
    table = TableInfo(
        name='users',
        columns=[column],
        constraints=[
            ConstraintInfo(
                constraint_name='c1',
                type=ConstraintType.CHECK,
                columns=['age'],
                constraint_definition='CHECK (age >= 0)',
            ),
            ConstraintInfo(
                constraint_name='c2',
                type=ConstraintType.CHECK,
                columns=['age'],
                constraint_definition='CHECK (age <= 120)',
            ),
        ],
    )
    update_column_constraint_definitions({('public', 'users'): table})
    assert column.constraint_definition == 'CHECK (age <= 120)'


# ---------------------------------------------------------------------------
# Enum DTO parsing & normalization.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normalize_constraint_type() -> None:
    assert normalize_constraint_type('p') == ConstraintType.PRIMARY_KEY
    assert normalize_constraint_type('f') == ConstraintType.FOREIGN_KEY
    assert normalize_constraint_type('u') == ConstraintType.UNIQUE
    assert normalize_constraint_type('c') == ConstraintType.CHECK
    assert normalize_constraint_type('x') == ConstraintType.EXCLUDE
    assert normalize_constraint_type('t') == ConstraintType.OTHER
    # A full human-readable code (not a pg contype) falls through to OTHER.
    assert normalize_constraint_type('PRIMARY KEY') == ConstraintType.OTHER


@pytest.mark.unit
def test_get_enum_types_filters_by_schema_and_typtype() -> None:
    rows = [
        ('type_name', 'public', 'owner', 'c1', True, 'e', ['a', 'b']),
        ('type_name_1', 'public', 'owner', 'c2', True, 'e', ['c', 'd']),
        ('not_enum', 'public', 'owner', 'c3', True, 'c', ['e']),  # typtype != 'e' → excluded
        ('other_schema', 'private', 'owner', 'c4', True, 'e', ['f']),
    ]
    all_enums = get_enum_types(rows)
    assert {e.type_name for e in all_enums} == {'type_name', 'type_name_1', 'other_schema'}
    public_only = get_enum_types(rows, 'public')
    assert {e.type_name for e in public_only} == {'type_name', 'type_name_1'}


@pytest.mark.unit
def test_get_user_type_mappings_filters_by_schema() -> None:
    rows = [
        ('col', 'users', 'public', 'type_name', 'c1', 'd'),
        ('col', 'users', 'private', 'type_name', 'c2', 'd'),
    ]
    assert len(get_user_type_mappings(rows)) == 2
    assert len(get_user_type_mappings(rows, 'public')) == 1


@pytest.mark.unit
@pytest.mark.parametrize(
    'candidate, expected',
    [
        ('mood', True),
        ('_mood', True),
        ('__mood', True),
        ('mood[]', True),
        ('"mood"', True),
        ('public.mood', True),
        ('MOOD', True),
        ('other', False),
        ('', False),
    ],
)
def test_user_enum_type_matches_type_name(candidate: str, expected: bool) -> None:
    enum = UserEnumType('mood', 'public', 'owner', 'E', True, 'e', ['happy'])
    assert enum.matches_type_name(candidate) is expected


@pytest.mark.unit
def test_user_type_mapping_is_a_plain_dto() -> None:
    mapping = UserTypeMapping('col', 'tbl', 'public', 'type', 'cat', 'desc')
    assert mapping.column_name == 'col'
    assert mapping.type_name == 'type'


@pytest.mark.unit
def test_standardize_column_name_variants() -> None:
    # Reserved keyword → prefixed; curated exception → untouched; model_ prefix → prefixed.
    assert standardize_column_name('class') == 'field_class'
    assert standardize_column_name('id') == 'id'
    assert standardize_column_name('model_config') == 'field_model_config'
    # With protection disabled, model_ prefix is left alone.
    assert standardize_column_name('model_config', disable_model_prefix_protection=True) == 'model_config'
    # A plain, non-reserved name is unchanged.
    assert standardize_column_name('email') == 'email'


@pytest.mark.unit
def test_ir_is_importable_with_stdlib_only() -> None:
    # Guards the zero-runtime-dependency contract for the IR package surface.
    import castiron.ir  # noqa: F401  (import side effect is the assertion)

    assert Schema is castiron.ir.Schema
