"""Shared fixtures for the Pydantic emitter tests.

Column tuples follow the IR builder's 12-tuple contract:
``(schema, table, column, default, is_nullable, data_type, max_length, table_type,
identity_generation, udt_name, array_element_type, description)``.
"""

from collections.abc import Callable

import pytest

from castiron.ir import (
    ColumnInfo,
    ForeignKeyInfo,
    RelationshipInfo,
    RelationType,
    Schema,
    TableInfo,
    build_schema,
)

Row = tuple[object, ...]


def col(
    table: str,
    name: str,
    data_type: str,
    *,
    schema: str = 'public',
    default: object = None,
    nullable: bool = False,
    table_type: str = 'BASE TABLE',
    identity: bool = False,
    udt_name: str | None = None,
    array_element_type: str | None = None,
    description: str | None = None,
) -> Row:
    """Build a 12-tuple column row for the IR builder."""
    return (
        schema,
        table,
        name,
        default,
        'YES' if nullable else 'NO',
        data_type,
        None,
        table_type,
        'a' if identity else None,
        udt_name,
        array_element_type,
        description,
    )


@pytest.fixture
def representative_schema() -> Schema:
    """A schema exercising the full fidelity surface (the golden anchor)."""
    columns = [
        col('user', 'id', 'integer', identity=True),
        col('user', 'company_id', 'uuid'),
        col('user', 'email', 'character varying', nullable=True, description='User email'),
        col('user', 'sku', 'text'),
        col('user', 'bio', 'text', nullable=True),
        col('user', 'metadata', 'jsonb', nullable=True),
        col('user', 'roles', 'ARRAY', nullable=True, array_element_type='text'),
        col('user', 'status', 'USER-DEFINED', udt_name='user_status'),
        col('user', 'flags', 'ARRAY', nullable=True, udt_name='_user_status', array_element_type='user_status[]'),
        col('user', 'created_at', 'timestamp with time zone'),
        col('company', 'id', 'uuid'),
        col('company', 'name', 'text'),
    ]
    fks = [('public', 'user', 'company_id', 'public', 'company', 'id', 'user_company_id_fkey')]
    constraints = [
        ('user_pkey', 'user', ['id'], 'p', 'PRIMARY KEY (id)'),
        ('company_pkey', 'company', ['id'], 'p', 'PRIMARY KEY (id)'),
        ('user_company_id_fkey', 'user', ['company_id'], 'f', 'FOREIGN KEY (company_id) REFERENCES company(id)'),
        ('user_sku_len', 'user', ['sku'], 'c', 'CHECK (length(sku) = 10)'),
        ('user_bio_len', 'user', ['bio'], 'c', 'CHECK (length(bio) <= 500)'),
    ]
    enum_types = [('user_status', 'public', 'owner', 'E', True, 'e', ['active', 'pending', 'import'])]
    enum_mapping = [('status', 'user', 'public', 'user_status', 'E', '')]
    return build_schema(columns, fks, constraints, enum_types, enum_mapping)


@pytest.fixture
def build_columns() -> Callable[..., Row]:
    """Expose the ``col`` helper to tests that build their own schemas."""
    return col


@pytest.fixture
def relationship_tables() -> list[TableInfo]:
    """Hand-built tables covering every relationship type (ported from supabase-pydantic)."""
    post = TableInfo(
        name='Post',
        columns=[
            ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True),
            ColumnInfo(name='title', raw_type='varchar', is_nullable=False),
            ColumnInfo(name='author_id', raw_type='integer', is_nullable=False, is_foreign_key=True),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                constraint_name='Post_author_id_fkey',
                column_name='author_id',
                foreign_table_name='User',
                foreign_column_name='id',
                relation_type=RelationType.ONE_TO_ONE,
            ),
        ],
        relationships=[
            RelationshipInfo(table_name='Post', related_table_name='Tag', relation_type=RelationType.MANY_TO_MANY),
        ],
    )
    user = TableInfo(
        name='User',
        columns=[
            ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True),
            ColumnInfo(name='name', raw_type='varchar', is_nullable=False),
        ],
        relationships=[
            RelationshipInfo(table_name='User', related_table_name='Post', relation_type=RelationType.ONE_TO_MANY),
        ],
    )
    tag = TableInfo(
        name='Tag',
        columns=[
            ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True),
            ColumnInfo(name='name', raw_type='varchar', is_nullable=False),
        ],
        relationships=[
            RelationshipInfo(table_name='Tag', related_table_name='Post', relation_type=RelationType.MANY_TO_MANY),
        ],
    )
    return [post, user, tag]
