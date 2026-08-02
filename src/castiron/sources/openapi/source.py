"""The OpenAPI/PostgREST source entrypoint: a URL (or a document) → a :class:`Schema`.

This is the surface the CLI (CI-006) calls. It is deliberately split in two:

- :func:`build_schema_from_document` is **pure** — hand it a decoded document and it
  returns the IR. That gives the CLI a free ``--from ./openapi.json`` offline path and
  gives ``check`` (CI-021) a network-free round trip.
- :func:`load_openapi_schema` is the one-liner that fetches first.

Neither builds an IR node directly: the parser emits the documented positional rows and
:func:`castiron.ir.build_schema` owns every fidelity rule (Hard Rule #6).
"""

from collections.abc import Mapping
from typing import Any

from castiron.ir import Schema, build_schema
from castiron.sources.openapi.fetch import DEFAULT_TIMEOUT, fetch_openapi_document
from castiron.sources.openapi.parse import parse_openapi_document


def build_schema_from_document(
    document: Mapping[str, Any],
    *,
    schema: str = 'public',
    disable_model_prefix_protection: bool = False,
    infer_generated_primary_keys: bool = False,
) -> Schema:
    """Build the Schema IR from an already-loaded PostgREST OpenAPI document (no I/O).

    Args:
        document: The decoded OpenAPI (Swagger 2.0) document.
        schema: The schema the document describes (it never states its own name).
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.
        infer_generated_primary_keys: Report a sole NOT NULL integer primary key with no
            default as identity (see :func:`castiron.sources.openapi.parse_openapi_document`).

    Returns:
        The populated, deterministic :class:`~castiron.ir.Schema`.

    Raises:
        SourceParseError: The document is not a schema castiron can read.
    """
    rows = parse_openapi_document(
        document,
        schema=schema,
        infer_generated_primary_keys=infer_generated_primary_keys,
    )
    return build_schema(
        rows.column_details,
        rows.fk_details,
        rows.constraints,
        rows.enum_types,
        rows.enum_type_mapping,
        schema,
        disable_model_prefix_protection,
        function_details=rows.function_details,
        table_details=rows.table_details,
    )


def load_openapi_schema(
    url: str,
    *,
    key: str | None = None,
    schema: str = 'public',
    timeout: float = DEFAULT_TIMEOUT,
    disable_model_prefix_protection: bool = False,
    infer_generated_primary_keys: bool = False,
) -> Schema:
    """Fetch a PostgREST OpenAPI document and build the Schema IR from it.

    Args:
        url: A PostgREST API root or a Supabase project URL.
        key: The API key to authenticate with, if any.
        schema: The database schema to request (sent as ``Accept-Profile``).
        timeout: Seconds to wait for the response.
        disable_model_prefix_protection: If ``True``, do not rename ``model_`` columns.
        infer_generated_primary_keys: Report a sole NOT NULL integer primary key with no
            default as identity.

    Returns:
        The populated, deterministic :class:`~castiron.ir.Schema`.

    Raises:
        SourceFetchError: The document could not be retrieved or was not JSON.
        SourceParseError: The document is not a schema castiron can read.
    """
    document = fetch_openapi_document(url, key=key, schema=schema, timeout=timeout)
    return build_schema_from_document(
        document,
        schema=schema,
        disable_model_prefix_protection=disable_model_prefix_protection,
        infer_generated_primary_keys=infer_generated_primary_keys,
    )
