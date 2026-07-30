"""castiron's Schema IR — the single canonical data model (the spine).

Pluggable sources build a :class:`Schema` (via :func:`build_schema`) and pluggable
emitters consume it. This package is stdlib-only by design; nothing here imports a
third-party runtime dependency.
"""

from castiron.ir.build import build_schema, construct_tables
from castiron.ir.models import (
    ColumnInfo,
    ConstraintInfo,
    ConstraintType,
    EnumInfo,
    ForeignKeyInfo,
    RelationshipInfo,
    RelationType,
    Schema,
    SortedColumns,
    TableInfo,
    TableType,
)

__all__ = [
    'ColumnInfo',
    'ConstraintInfo',
    'ConstraintType',
    'EnumInfo',
    'ForeignKeyInfo',
    'RelationType',
    'RelationshipInfo',
    'Schema',
    'SortedColumns',
    'TableInfo',
    'TableType',
    'build_schema',
    'construct_tables',
]
