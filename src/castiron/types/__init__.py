"""Shared, source-neutral type resolution reused by every emitter.

An emitter resolves a column's ``raw_type`` (and ``array_element_type`` / ``enum_info``)
into a target Python type + the imports it needs, via :func:`resolve_column_type` and a
target :data:`TypeMap`. The Pydantic map lives here; CI-012 adds a SQLAlchemy map that
reuses the same resolver. This package stays free of ``pydantic``/``inflection`` imports.
"""

from castiron.types.pydantic_map import PYDANTIC_TYPE_MAP
from castiron.types.resolution import (
    DEFAULT_RESOLUTION,
    TypeMap,
    TypeResolution,
    resolve,
    resolve_column_type,
)

__all__ = [
    'DEFAULT_RESOLUTION',
    'PYDANTIC_TYPE_MAP',
    'TypeMap',
    'TypeResolution',
    'resolve',
    'resolve_column_type',
]
