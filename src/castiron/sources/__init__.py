"""Pluggable schema sources — everything that turns *some* schema into the Schema IR.

A source's job is narrow: produce the positional row contracts documented in
:mod:`castiron.ir.build` and hand them to :func:`castiron.ir.build_schema`. It never
defines its own model shapes (Hard Rule #6) and never resolves a Python type.

Today there is one source, the OpenAPI/PostgREST adapter (a Supabase URL + API key, no
database credentials and no driver). The live-DB (CI-010) and migrations (CI-020) sources
land later and will want materially different constructor shapes, so there is deliberately
**no** ``Source`` ABC yet — only the shared error contract in
:mod:`castiron.sources.errors` is hoisted here.
"""

from castiron.sources.errors import SourceError, SourceFetchError, SourceParseError
from castiron.sources.openapi import (
    OpenApiRows,
    build_schema_from_document,
    fetch_openapi_document,
    load_openapi_schema,
    normalize_postgrest_url,
    parse_openapi_document,
)

__all__ = [
    'OpenApiRows',
    'SourceError',
    'SourceFetchError',
    'SourceParseError',
    'build_schema_from_document',
    'fetch_openapi_document',
    'load_openapi_schema',
    'normalize_postgrest_url',
    'parse_openapi_document',
]
