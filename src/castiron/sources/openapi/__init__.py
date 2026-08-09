"""The OpenAPI/PostgREST source — a Schema IR from a URL + API key, no DB credentials.

Three modules, one hard split: :mod:`~castiron.sources.openapi.fetch` is the only code
that touches the network, :mod:`~castiron.sources.openapi.parse` is a pure
``document -> rows`` function, and :mod:`~castiron.sources.openapi.source` joins them.
Read ``parse``'s module docstring for the *fidelity floor* — what this coarser source
structurally cannot see.
"""

from castiron.sources.openapi.fetch import (
    DEFAULT_TIMEOUT,
    build_request_headers,
    fetch_openapi_document,
    normalize_postgrest_url,
)
from castiron.sources.openapi.parse import (
    INTEGER_FAMILY,
    MIN_VOLATILITY_SIGNAL_VERSION,
    OPENAPI_FORMAT_ALIASES,
    ColumnMarkers,
    OpenApiRows,
    classify_table_type,
    normalize_format,
    parse_column_description,
    parse_openapi_document,
    stringify_default,
    volatility_is_encoded,
)
from castiron.sources.openapi.source import build_schema_from_document, load_openapi_schema

__all__ = [
    'DEFAULT_TIMEOUT',
    'INTEGER_FAMILY',
    'MIN_VOLATILITY_SIGNAL_VERSION',
    'OPENAPI_FORMAT_ALIASES',
    'ColumnMarkers',
    'OpenApiRows',
    'build_request_headers',
    'build_schema_from_document',
    'classify_table_type',
    'fetch_openapi_document',
    'load_openapi_schema',
    'normalize_format',
    'normalize_postgrest_url',
    'parse_column_description',
    'parse_openapi_document',
    'stringify_default',
    'volatility_is_encoded',
]
