"""castiron's Schema IR — the single canonical data model (the spine).

Pluggable sources build a :class:`Schema` (via :func:`build_schema`) and pluggable
emitters consume it. This package is stdlib-only by design; nothing here imports a
third-party runtime dependency.
"""

from castiron.ir.build import Row, build_schema, construct_functions, construct_tables
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
    ParameterOrder,
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
    'FunctionInfo',
    'FunctionVolatility',
    'ParameterInfo',
    'ParameterMode',
    'ParameterOrder',
    'RelationType',
    'RelationshipInfo',
    'Row',
    'Schema',
    'SortedColumns',
    'TableInfo',
    'TableType',
    'build_schema',
    'construct_functions',
    'construct_tables',
]
