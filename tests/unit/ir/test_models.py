"""Unit tests for the canonical Schema IR nodes (castiron.ir.models)."""

import dataclasses
import json

import pytest

from castiron.ir import (
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
    SortedColumns,
    TableInfo,
)


@pytest.mark.unit
def test_column_info_defaults_and_helpers() -> None:
    col = ColumnInfo(name='id', raw_type='uuid')
    assert col.alias is None
    assert col.is_nullable is True
    assert col.has_default is False
    assert col.nullable() is True
    assert str(col) == 'ColumnInfo(id, uuid)'

    col_with_default = ColumnInfo(name='n', raw_type='int', default='0', is_nullable=None)
    assert col_with_default.has_default is True
    # is_nullable=None is treated as not-nullable by nullable().
    assert col_with_default.nullable() is False


@pytest.mark.unit
def test_column_info_is_generated_is_a_stored_field() -> None:
    # is_generated is a build-time bool field, not a runtime property.
    col = ColumnInfo(name='id', raw_type='int', is_generated=True)
    assert col.is_generated is True
    assert ColumnInfo(name='x', raw_type='text').is_generated is False


@pytest.mark.unit
def test_constraint_info_type_and_str() -> None:
    pk = ConstraintInfo(
        constraint_name='pk',
        type=ConstraintType.PRIMARY_KEY,
        columns=['id'],
        constraint_definition='PRIMARY KEY (id)',
        raw_constraint_type='p',
    )
    assert pk.type is ConstraintType.PRIMARY_KEY
    assert str(pk) == 'ConstraintInfo(pk, PRIMARY KEY)'
    assert pk.raw_constraint_type == 'p'


@pytest.mark.unit
def test_relationship_info_equality_and_hashability() -> None:
    a = RelationshipInfo(table_name='orders', related_table_name='users', relation_type=RelationType.MANY_TO_ONE)
    b = RelationshipInfo(table_name='orders', related_table_name='users', relation_type=RelationType.MANY_TO_ONE)
    c = RelationshipInfo(table_name='orders', related_table_name='items', relation_type=RelationType.MANY_TO_ONE)

    assert a == b
    assert a != c
    # Hashable → set de-dup works (a and b collapse to one member).
    assert len({a, b, c}) == 2


@pytest.mark.unit
def test_enum_info_construction() -> None:
    enum = EnumInfo(name='order_status', values=['pending', 'shipped'], schema='public')
    assert enum.name == 'order_status'
    assert enum.values == ['pending', 'shipped']
    assert enum.schema == 'public'
    # Default schema and empty values.
    assert EnumInfo(name='x').schema == 'public'
    assert EnumInfo(name='x').values == []


@pytest.mark.unit
def test_foreign_key_info_defaults() -> None:
    fk = ForeignKeyInfo(
        constraint_name='fk', column_name='user_id', foreign_table_name='users', foreign_column_name='id'
    )
    assert fk.foreign_table_schema == 'public'
    assert fk.relation_type is None


def _sample_table() -> TableInfo:
    """Mirror supabase-pydantic's test_TableInfo_methods fixture, on the castiron IR."""
    return TableInfo(
        name='test',
        schema='public',
        table_type='BASE TABLE',
        is_bridge=False,
        columns=[
            ColumnInfo(
                name='id',
                raw_type='uuid',
                alias='foo',
                default='uuid_generate_v4()',
                is_nullable=False,
                primary=True,
                is_unique=True,
                is_foreign_key=True,
            ),
            ColumnInfo(name='name', raw_type='text', alias='bar', max_length=255, is_nullable=False),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                constraint_name='test', column_name='foo', foreign_table_name='bar', foreign_column_name='baz'
            ),
        ],
        constraints=[
            ConstraintInfo(
                constraint_name='primary key',
                type=ConstraintType.PRIMARY_KEY,
                columns=['id'],
                constraint_definition='PRIMARY KEY (id)',
                raw_constraint_type='p',
            ),
            ConstraintInfo(
                constraint_name='other',
                type=ConstraintType.OTHER,
                columns=['bar'],
                constraint_definition='FOREIGN KEY (bar) REFERENCES barz(baz)',
                raw_constraint_type='t',
            ),
        ],
    )


@pytest.mark.unit
def test_table_info_str() -> None:
    assert str(TableInfo(name='users', schema='public')) == 'TableInfo(public.users)'


@pytest.mark.unit
def test_table_info_mutating_helpers() -> None:
    table = _sample_table()
    assert len(table.columns) == 2
    table.add_column(ColumnInfo(name='test', raw_type='text', alias='bar', max_length=255))
    assert len(table.columns) == 3

    assert len(table.foreign_keys) == 1
    table.add_foreign_key(
        ForeignKeyInfo(constraint_name='t', column_name='foo', foreign_table_name='bar', foreign_column_name='baz')
    )
    assert len(table.foreign_keys) == 2

    assert len(table.constraints) == 2
    table.add_constraint(ConstraintInfo(constraint_name='t', type=ConstraintType.OTHER, columns=['foo', 'bar']))
    assert len(table.constraints) == 3


@pytest.mark.unit
def test_table_info_derived_methods() -> None:
    table = _sample_table()
    table.add_column(ColumnInfo(name='test', raw_type='text', alias='bar', max_length=255, is_nullable=True))

    assert table.aliasing_in_columns() is True
    assert table.table_dependencies() == {'bar'}

    assert table.primary_key() == ['id']
    table.table_type = 'VIEW'
    assert table.primary_key() == []  # VIEW → no primary key
    table.table_type = 'BASE TABLE'

    assert table.primary_is_composite() is False
    assert len(table.get_primary_columns()) == 1
    assert table.get_primary_columns()[0].name == 'id'
    assert len(table.get_secondary_columns()) == 2

    secondary_sorted = table.get_secondary_columns(sort_results=True)
    assert [c.name for c in secondary_sorted] == ['name', 'test']


@pytest.mark.unit
def test_table_info_sort_and_separate_columns() -> None:
    table = _sample_table()
    table.add_column(ColumnInfo(name='test', raw_type='text', alias='bar', max_length=255, is_nullable=True))

    separated = table.sort_and_separate_columns(separate_nullable=True, separate_primary_key=True)
    assert len(separated.primary_keys) == 1
    assert len(separated.nullable) == 1
    assert len(separated.non_nullable) == 1
    assert len(separated.remaining) == 0

    flat = table.sort_and_separate_columns()
    assert len(flat.primary_keys) == 0
    assert len(flat.nullable) == 0
    assert len(flat.non_nullable) == 0
    assert [c.name for c in flat.remaining] == ['id', 'name', 'test']


@pytest.mark.unit
def test_table_info_has_unique_constraint() -> None:
    assert _sample_table().has_unique_constraint() is False
    table = _sample_table()
    table.add_constraint(ConstraintInfo(constraint_name='u', type=ConstraintType.UNIQUE, columns=['name']))
    assert table.has_unique_constraint() is True


@pytest.mark.unit
def test_table_info_composite_primary_key() -> None:
    table = TableInfo(
        name='bridge',
        columns=[
            ColumnInfo(name='a', raw_type='int', primary=True),
            ColumnInfo(name='b', raw_type='int', primary=True),
        ],
        constraints=[ConstraintInfo(constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['a', 'b'])],
    )
    assert table.primary_key() == ['a', 'b']
    assert table.primary_is_composite() is True


@pytest.mark.unit
def test_sorted_columns_shape() -> None:
    sc = SortedColumns([], [], [], [])
    assert sc.primary_keys == []
    assert sc.nullable == []
    assert sc.non_nullable == []
    assert sc.remaining == []


@pytest.mark.unit
def test_schema_as_dict_is_stable_and_json_serializable() -> None:
    schema = Schema(
        tables=[
            TableInfo(
                name='orders',
                columns=[ColumnInfo(name='id', raw_type='uuid', primary=True)],
                constraints=[
                    ConstraintInfo(
                        constraint_name='pk', type=ConstraintType.PRIMARY_KEY, columns=['id'], raw_constraint_type='p'
                    ),
                ],
                relationships=[
                    RelationshipInfo(
                        table_name='orders', related_table_name='users', relation_type=RelationType.MANY_TO_ONE
                    ),
                ],
            )
        ],
        enums=[EnumInfo(name='status', values=['a', 'b'])],
    )
    as_dict = schema.as_dict()

    # Enums render as their string values (plain builtins, not Enum instances).
    assert as_dict['tables'][0]['constraints'][0]['type'] == 'PRIMARY KEY'
    assert as_dict['tables'][0]['relationships'][0]['relation_type'] == 'Many-to-One'
    assert as_dict['enums'][0] == {'name': 'status', 'values': ['a', 'b'], 'schema': 'public'}

    # Fully JSON-serializable and byte-stable across repeated calls.
    assert json.dumps(as_dict) == json.dumps(schema.as_dict())


@pytest.mark.unit
def test_schema_defaults_to_empty() -> None:
    schema = Schema()
    assert schema.tables == []
    assert schema.enums == []
    assert schema.functions == []
    # CI-005 amendment: ``_serialize`` walks ``dataclasses.fields``, so adding
    # ``Schema.functions`` necessarily adds exactly one additive key here. Emitted output
    # is unaffected (proved in tests/unit/emitters/pydantic/test_emitter.py).
    assert schema.as_dict() == {'tables': [], 'enums': [], 'functions': []}


@pytest.mark.unit
def test_schema_as_dict_renders_functions_as_plain_builtins() -> None:
    schema = Schema(
        functions=[
            FunctionInfo(
                name='get_user_stats',
                parameters=[
                    ParameterInfo(
                        name='user_id',
                        raw_type='integer',
                        mode=ParameterMode.VARIADIC,
                        has_default=True,
                        enum_info=EnumInfo(name='status', values=['a']),
                    )
                ],
                volatility=FunctionVolatility.STABLE,
                is_read_only=True,
                description='Stats.',
            )
        ]
    )
    as_dict = schema.as_dict()

    assert as_dict['functions'] == [
        {
            'name': 'get_user_stats',
            'schema': 'public',
            'parameters': [
                {
                    'name': 'user_id',
                    'raw_type': 'integer',
                    'mode': 'VARIADIC',
                    'has_default': True,
                    'array_element_type': None,
                    'enum_info': {'name': 'status', 'values': ['a'], 'schema': 'public'},
                }
            ],
            'return_type': None,
            'returns_set': None,
            'volatility': 'STABLE',
            'is_read_only': True,
            'description': 'Stats.',
        }
    ]
    assert json.dumps(as_dict) == json.dumps(schema.as_dict())


@pytest.mark.unit
class TestTableDescription:
    """``TableInfo.description`` — the additive CI-009 field (a table's ``COMMENT ON TABLE``)."""

    def test_defaults_to_none(self) -> None:
        assert TableInfo(name='t').description is None

    def test_is_the_last_declared_field(self) -> None:
        """Declaration order is ``as_dict()`` key order; appending last keeps every existing key put."""
        assert [f.name for f in dataclasses.fields(TableInfo)][-1] == 'description'

    def test_positional_construction_is_unaffected(self) -> None:
        """The field is appended last, so the pre-CI-009 positional shape still works."""
        table = TableInfo('t', 'public', 'VIEW')
        assert (table.name, table.schema, table.table_type) == ('t', 'public', 'VIEW')
        assert table.description is None

    def test_as_dict_gains_exactly_one_key_in_last_position(self) -> None:
        """CI5-Q1/L6: an additive IR field adds an additive ``as_dict()`` key. Wanted, not a regression."""
        schema = Schema(tables=[TableInfo(name='t', description='Application users.')])
        table_dict = schema.as_dict()['tables'][0]

        assert list(table_dict)[-1] == 'description'
        assert table_dict['description'] == 'Application users.'
        assert list(table_dict) == [
            'name',
            'schema',
            'table_type',
            'is_bridge',
            'columns',
            'foreign_keys',
            'constraints',
            'relationships',
            'description',
        ]

    def test_as_dict_stays_json_serializable_and_stable(self) -> None:
        schema = Schema(tables=[TableInfo(name='t', description='Customer orders.')])
        assert json.dumps(schema.as_dict()) == json.dumps(schema.as_dict())

    def test_a_none_description_serializes_as_null(self) -> None:
        schema = Schema(tables=[TableInfo(name='t')])
        assert schema.as_dict()['tables'][0]['description'] is None
