"""Pure PostgREST OpenAPI (Swagger 2.0) document → the Schema IR's positional row contracts.

Nothing in this module performs I/O: :func:`parse_openapi_document` is a pure function of a
``Mapping``, so every parser test loads a JSON fixture and no test mocks HTTP. The rows it
returns are handed straight to :func:`castiron.ir.build_schema`, which owns all the
fidelity logic (reserved-name aliasing, flag propagation, relationship typing, bridge-table
detection, enum linkage) — this module never constructs an IR node itself.

How PostgREST encodes a schema (verified against the generator, not guessed)
---------------------------------------------------------------------------
References, cite these before changing a rule:

- ``PostgREST/postgrest`` → ``src/library/PostgREST/Response/OpenAPI.hs`` — the generator
  (``makeProperty``, ``makeProcSchema``, ``makeProcPathItem``, ``toSwaggerFormat``).
- ``PostgREST/postgrest`` → ``test/spec/Feature/OpenApi/OpenApiSpec.hs`` — literal expected
  JSON for every case; the committed fixture reproduces these shapes.
- ``PostgREST/postgrest`` → ``src/library/PostgREST/SchemaCache.hs`` (``tablesSqlQuery``,
  ``funcsSqlQuery``) — where the column/function facts come from, and why some are missing.
- ``PostgREST/postgrest`` → ``src/library/PostgREST/SchemaCache/Routine.hs`` — proves a
  function's return type exists internally but is never encoded.

The document is **Swagger 2.0** (root key ``swagger``), describes exactly **one** schema
(selected by ``Accept-Profile``; table names in ``definitions`` are unqualified), and is
filtered by the API role's privileges. Columns live in ``definitions.<t>.properties.<c>``:
``format`` carries the raw pg type name, ``required`` lists exactly the NOT NULL columns,
and keys/relationships exist only as ``<pk/>`` / ``<fk table='..' column='..'/>`` markers
inside a column's ``description``. Functions live at ``paths./rpc/<name>``.

The fidelity floor (what this source structurally cannot see)
-------------------------------------------------------------
Each line is asserted by a test in ``tests/unit/sources/openapi/`` so it cannot silently
move:

- ``smallint`` and ``integer`` both arrive as ``int32`` and are **indistinguishable**;
  ``bigint`` survives as ``int64``. Everything else keeps its real pg type name.
- ``nextval(...)`` defaults are dropped upstream (PostgREST feeds the default text to
  ``JSON.decode``, which fails), so an integer surrogate primary key looks NOT NULL with no
  default and no identity marker. See ``infer_generated_primary_keys``.
- Numeric precision/scale, ``varchar(n)`` typmods (``maxLength`` survives) and domain names
  are lost — ``format_type(atttypid, NULL)`` erases them.
- **UNIQUE, CHECK and EXCLUDE constraints do not exist anywhere in the document.**
- Foreign keys are **single-column only**, carry no schema and no real constraint name
  (castiron synthesizes pg's own default ``<table>_<column>_fkey``); composite FKs are
  invisible and a column in two FKs reports only one.
- Primary-key *membership* is recoverable, composite-key **order** is not.
- Views carry no marker at all, so ``table_type`` is a two-signal heuristic biased toward
  ``BASE TABLE`` (see :func:`classify_table_type`), and PostgREST reports every view column
  as nullable.
- Enum **values** are absent for array columns (``pg_enum`` is keyed on the base type), so
  such a column links only when the same enum also appears on a scalar column.
- A function's **return type** and **set-returning** flag are never encoded; volatility is a
  binary signal (POST-only ⇒ VOLATILE); **overloads collapse** to one arbitrary signature.
- Objects the API role cannot see are simply absent (RLS/privileges shrink the schema).

Point castiron at the database itself (CI-010/CI-011) when those facts matter.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from castiron.ir import Row, TableType
from castiron.sources.errors import SourceParseError

logger = logging.getLogger(__name__)

#: An untyped JSON object as it arrives from :func:`json.loads`.
JsonObject = Mapping[str, Any]

#: Swagger's own numeric formats → the Postgres vocabulary every castiron type map speaks.
#: These two are the *only* tokens ``toSwaggerFormat`` rewrites; everything else is already
#: the raw pg type name, so there is no second type map (decision CI5-D5).
OPENAPI_FORMAT_ALIASES: dict[str, str] = {
    'int32': 'integer',
    'int64': 'bigint',
}

#: Fallback pg type for a property that carries a Swagger ``type`` but no ``format``.
SWAGGER_TYPE_FALLBACKS: dict[str, str] = {
    'string': 'text',
    'integer': 'integer',
    'number': 'numeric',
    'boolean': 'boolean',
    'array': 'array',
    'object': 'jsonb',
}

#: Types eligible for the opt-in surrogate-primary-key inference (post-normalization).
_INTEGER_FAMILY = frozenset({'smallint', 'integer', 'bigint'})

#: The path prefix PostgREST exposes database functions under.
_RPC_PREFIX = '/rpc/'

#: HTTP methods whose presence proves a definition is writable through the API.
_WRITE_METHODS = ('post', 'patch', 'delete')

# Description markers, exactly as ``makeProperty`` builds them.
_PK_MARKER = re.compile(r'<pk\s*/>')
_FK_MARKER = re.compile(r"<fk\s+table='([^']*)'\s+column='([^']*)'\s*/>")
_NOTE_BLOCK = re.compile(
    r'(?:\n\n)?Note:\n'
    r'(?:(?:This is a Primary Key\.<pk/>'
    r"|This is a Foreign Key to `[^`]*`\.<fk table='[^']*' column='[^']*'/>)\n?)+\s*$"
)


@dataclass(frozen=True)
class ColumnMarkers:
    """What a column's ``description`` says, once its ``Note:`` marker block is split off.

    Attributes:
        comment: The human-authored SQL comment, or ``None`` when the description was
            nothing but markers.
        is_primary_key: Whether the column carries a ``<pk/>`` marker.
        foreign_table: The ``table`` attribute of a ``<fk .../>`` marker, if any.
        foreign_column: The ``column`` attribute of a ``<fk .../>`` marker, if any.
    """

    comment: str | None = None
    is_primary_key: bool = False
    foreign_table: str | None = None
    foreign_column: str | None = None


@dataclass(frozen=True)
class OpenApiRows:
    """The positional row contracts parsed out of a PostgREST OpenAPI document.

    Field names and tuple shapes match :func:`castiron.ir.build_schema`'s parameters
    one-for-one; see :mod:`castiron.ir.build` for each contract.
    """

    column_details: tuple[Row, ...] = ()
    fk_details: tuple[Row, ...] = ()
    constraints: tuple[Row, ...] = ()
    enum_types: tuple[Row, ...] = ()
    enum_type_mapping: tuple[Row, ...] = ()
    function_details: tuple[Row, ...] = ()


@dataclass
class _RowAccumulator:
    """Mutable per-parse collector for the six row contracts."""

    columns: list[Row] = field(default_factory=list)
    fks: list[Row] = field(default_factory=list)
    constraints: list[Row] = field(default_factory=list)
    enum_mappings: list[Row] = field(default_factory=list)
    #: ``(namespace, type_name)`` → the enum's labels, de-duplicated across columns.
    enums: dict[tuple[str, str], list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small, pure helpers (exported: tests and future sources reuse them).
# ---------------------------------------------------------------------------


def normalize_format(format_token: str) -> str:
    """Translate a Swagger ``format`` token into the Postgres type vocabulary.

    Only ``int32``/``int64`` are Swagger's own vocabulary; every other token PostgREST
    emits is already the raw pg type name and passes through unchanged.

    Args:
        format_token: The raw ``format`` value (or an array-element token).

    Returns:
        The pg-vocabulary type token.
    """
    return OPENAPI_FORMAT_ALIASES.get(format_token, format_token)


def stringify_default(value: Any) -> str:
    """Render a JSON ``default`` value as the raw default *text* the IR expects.

    A JSON string passes through verbatim (PostgREST already stripped the ``::type`` cast
    and the quotes); every other JSON value is re-rendered with :func:`json.dumps`, so
    ``True`` becomes ``'true'`` and ``42.2`` becomes ``'42.2'``.

    Args:
        value: The decoded JSON ``default``.

    Returns:
        The default as text.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value)


def parse_column_description(description: str | None) -> ColumnMarkers:
    r"""Split a column ``description`` into its SQL comment and its key markers.

    ``makeProperty`` renders ``[<comment>\n\n]Note:\n<marker lines>``; markers are
    detected position-independently and the ``Note:`` block is stripped to recover the
    comment. An unrecognized or malformed marker is left alone — the description is
    preserved verbatim rather than mangled.

    Args:
        description: The property's ``description``, or ``None``.

    Returns:
        The parsed :class:`ColumnMarkers`.
    """
    if description is None:
        return ColumnMarkers()

    fk_match = _FK_MARKER.search(description)
    comment = _NOTE_BLOCK.sub('', description).strip() or None
    return ColumnMarkers(
        comment=comment,
        is_primary_key=_PK_MARKER.search(description) is not None,
        foreign_table=fk_match.group(1) if fk_match else None,
        foreign_column=fk_match.group(2) if fk_match else None,
    )


def classify_table_type(name: str, definition: JsonObject, paths: JsonObject) -> TableType:
    """Classify a ``definitions`` entry as a ``VIEW`` or a ``BASE TABLE``.

    The document carries **no** view marker (PostgREST computes ``relkind IN ('v','m')``
    internally and never emits it), so this is a two-signal heuristic deliberately biased
    toward ``BASE TABLE``: ``VIEW`` only when the entry is not writable through the API
    **and** declares no NOT NULL column. Misreading a table as a view would empty its
    primary key (a visible regression); misreading a view as a table is nearly harmless.

    Args:
        name: The ``definitions`` key.
        definition: The definition object.
        paths: The document's ``paths`` object.

    Returns:
        ``'VIEW'`` or ``'BASE TABLE'``.
    """
    path_item = _as_object(paths.get(f'/{name}')) or {}
    is_writable = any(method in path_item for method in _WRITE_METHODS)
    required = definition.get('required')
    has_required = isinstance(required, list) and len(required) > 0

    table_type: TableType = 'BASE TABLE' if (is_writable or has_required) else 'VIEW'
    logger.debug(f'Classified {name} as {table_type} (writable={is_writable}, has_required={has_required})')
    return table_type


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def parse_openapi_document(
    document: JsonObject,
    *,
    schema: str = 'public',
    infer_generated_primary_keys: bool = False,
) -> OpenApiRows:
    """Parse a PostgREST OpenAPI (Swagger 2.0) document into the IR's row contracts.

    Pure: no I/O, no network, no global state. Ordering is fixed for the ``check``
    drift-guard (Hard Rule #9) — ``definitions`` and ``/rpc/*`` keys are **sorted** (both
    are built from a Haskell hash map upstream, so their document order is not
    contractual), while ``properties`` order is **preserved** for both columns and function
    parameters (that order is real: pg ordinal / argument position).

    Args:
        document: The decoded OpenAPI document.
        schema: The schema the document describes (the document never states it; it is
            selected by ``Accept-Profile``).
        infer_generated_primary_keys: When ``True``, a sole NOT NULL integer-family primary
            key with no default is reported as ``BY DEFAULT`` identity, so an emitter makes
            it optional on insert. Off by default: PostgREST drops ``nextval(...)`` defaults
            upstream, so the fact is genuinely unknown and a natural integer key would be
            guessed wrong.

    Returns:
        The six row contracts, ready for :func:`castiron.ir.build_schema`.

    Raises:
        SourceParseError: The document is not PostgREST Swagger 2.0 output, exposes no
            tables or views, or contains a property with no usable type.
    """
    definitions = _validate_envelope(document, schema)
    paths = _as_object(document.get('paths')) or {}

    rows = _RowAccumulator()
    for table_name in sorted(definitions):
        definition = _as_object(definitions[table_name])
        if definition is None:
            logger.debug(f'Skipping definition {table_name}: not a JSON object')
            continue
        _parse_definition(table_name, definition, paths, schema, infer_generated_primary_keys, rows)

    if not rows.columns:
        raise SourceParseError(
            f'The OpenAPI document exposes no readable columns for schema {schema!r}. '
            f'Check the API key and the role privileges (PostgREST hides objects the role cannot access).'
        )

    return OpenApiRows(
        column_details=tuple(rows.columns),
        fk_details=tuple(rows.fks),
        constraints=tuple(rows.constraints),
        enum_types=tuple(
            (type_name, namespace, '', 'E', True, 'e', values)
            for (namespace, type_name), values in sorted(rows.enums.items())
        ),
        enum_type_mapping=tuple(rows.enum_mappings),
        function_details=_parse_functions(paths, schema),
    )


# ---------------------------------------------------------------------------
# Envelope.
# ---------------------------------------------------------------------------


def _validate_envelope(document: JsonObject, schema: str) -> JsonObject:
    """Validate the document envelope and return its ``definitions`` object."""
    if 'swagger' not in document and 'openapi' in document:
        raise SourceParseError(
            f"castiron reads PostgREST's Swagger 2.0 output; got OpenAPI {document['openapi']!r}. "
            f'Point castiron at the PostgREST API root (it serves the document there).'
        )

    definitions = _as_object(document.get('definitions'))
    if definitions is None:
        raise SourceParseError(
            'The document has no "definitions" object, so it is not a PostgREST OpenAPI document. '
            '(A `db-root-spec` override replaces the document with arbitrary JSON.)'
        )

    if not definitions:
        raise SourceParseError(
            f"The OpenAPI document exposes no tables or views for schema {schema!r}; check the API key's role "
            f'privileges (PostgREST hides objects the role cannot access) and the Accept-Profile schema.'
        )

    return definitions


# ---------------------------------------------------------------------------
# Tables, views, columns.
# ---------------------------------------------------------------------------


def _parse_definition(
    table_name: str,
    definition: JsonObject,
    paths: JsonObject,
    schema: str,
    infer_generated_primary_keys: bool,
    rows: _RowAccumulator,
) -> None:
    """Parse one ``definitions`` entry into column, FK, constraint and enum rows."""
    properties = _as_object(definition.get('properties'))
    if not properties:
        logger.debug(f'Skipping definition {table_name}: no "properties" object')
        return

    required = {value for value in _as_list(definition.get('required')) if isinstance(value, str)}
    table_type = classify_table_type(table_name, definition, paths)

    parsed: list[tuple[str, JsonObject, ColumnMarkers]] = []
    for column_name, raw_property in properties.items():
        prop = _as_object(raw_property)
        if prop is None:
            logger.debug(f'Skipping {table_name}.{column_name}: property is not a JSON object')
            continue
        parsed.append((column_name, prop, parse_column_description(_as_str(prop.get('description')))))

    pk_columns = [name for name, _, markers in parsed if markers.is_primary_key]
    sole_pk_column = pk_columns[0] if infer_generated_primary_keys and len(pk_columns) == 1 else None
    fk_constraints: list[Row] = []

    for column_name, prop, markers in parsed:
        data_type = _resolve_type_token(prop, f'{table_name}.{column_name}')
        is_nullable = column_name not in required
        default = stringify_default(prop['default']) if 'default' in prop else None
        is_inferred_identity = (
            column_name == sole_pk_column and not is_nullable and default is None and data_type in _INTEGER_FAMILY
        )

        rows.columns.append(
            (
                schema,
                table_name,
                column_name,
                default,
                'YES' if is_nullable else 'NO',
                data_type,
                _as_int(prop.get('maxLength')),
                table_type,
                'BY DEFAULT' if is_inferred_identity else None,
                None,  # udt_name — never available (and discarded by the builder)
                _array_element_type(data_type),
                markers.comment,
            )
        )

        if markers.foreign_table is not None and markers.foreign_column is not None:
            constraint_name = f'{table_name}_{column_name}_fkey'
            rows.fks.append(
                (
                    schema,
                    table_name,
                    column_name,
                    schema,
                    markers.foreign_table,
                    markers.foreign_column,
                    constraint_name,
                )
            )
            fk_constraints.append(
                (
                    constraint_name,
                    table_name,
                    [column_name],
                    'f',
                    f'FOREIGN KEY ({column_name}) REFERENCES {markers.foreign_table}({markers.foreign_column})',
                )
            )

        _record_enum(table_name, column_name, prop, data_type, schema, rows)

    if pk_columns:
        rows.constraints.append((f'{table_name}_pkey', table_name, pk_columns, 'p', None))
    rows.constraints.extend(fk_constraints)


def _resolve_type_token(prop: JsonObject, location: str) -> str:
    """Return a property's pg type token from ``format``, falling back to ``type``.

    Args:
        prop: The property (or function-parameter) object.
        location: A ``table.column`` / ``function(parameter)`` label for the error message.

    Returns:
        The pg-vocabulary type token.

    Raises:
        SourceParseError: The property declares neither ``format`` nor a usable ``type``.
    """
    format_token = _as_str(prop.get('format'))
    if format_token is not None:
        return normalize_format(format_token)

    swagger_type = _as_str(prop.get('type'))
    if swagger_type is not None and swagger_type in SWAGGER_TYPE_FALLBACKS:
        return SWAGGER_TYPE_FALLBACKS[swagger_type]

    raise SourceParseError(
        f'{location} declares neither a "format" nor a recognized "type", so castiron cannot tell what it is.'
    )


def _array_element_type(data_type: str) -> str | None:
    """Return the array element's pg type token, or ``None`` when it is not recoverable.

    PostgREST encodes an array's element type only inside the ``format`` token (``text[]``);
    ``items`` carries the element's *Swagger* type, which is too coarse to map back to pg.
    So an array with no ``format`` has a genuinely unknown element type — ``None``, never a
    guess.
    """
    if data_type.endswith('[]'):
        return normalize_format(data_type[:-2])
    return None


def _record_enum(
    table_name: str,
    column_name: str,
    prop: JsonObject,
    data_type: str,
    schema: str,
    rows: _RowAccumulator,
) -> None:
    """Record the enum type + column mapping for a **scalar** enum column.

    Array columns are deliberately skipped: ``SchemaCache.hs`` resolves labels from
    ``pg_enum WHERE enumtypid = base_type``, and an ``my_enum[]`` column's base type is the
    *array* type, so the ``enum`` key is absent and the labels are unknown. Such a column
    still records ``array_element_type``, which the builder links **iff** the same enum
    appears on a scalar column somewhere in the document.
    """
    values = prop.get('enum')
    if not isinstance(values, list) or data_type.endswith('[]'):
        return

    labels = [value for value in values if isinstance(value, str)]
    namespace, _, type_name = data_type.rpartition('.')
    namespace = namespace or schema

    rows.enums.setdefault((namespace, type_name), labels)
    rows.enum_mappings.append((column_name, table_name, namespace, type_name, 'E', ''))


# ---------------------------------------------------------------------------
# Functions / RPCs.
# ---------------------------------------------------------------------------


def _parse_functions(paths: JsonObject, schema: str) -> tuple[Row, ...]:
    """Parse every ``/rpc/<name>`` path item into a function 8-tuple, sorted by name."""
    functions: list[Row] = []
    for path_key in sorted(key for key in paths if key.startswith(_RPC_PREFIX)):
        name = path_key[len(_RPC_PREFIX) :]
        path_item = _as_object(paths[path_key])
        if not name or path_item is None:
            logger.debug(f'Skipping RPC path {path_key!r}: no function name or not a JSON object')
            continue

        post_op = _as_object(path_item.get('post'))
        get_op = _as_object(path_item.get('get'))
        if post_op is None and get_op is None:
            logger.debug(f'Skipping RPC path {path_key!r}: neither a "post" nor a "get" operation')
            continue

        body_schema = _find_body_schema(post_op) if post_op is not None else None
        parameters = (
            _parse_body_parameters(name, body_schema, get_op)
            if body_schema is not None
            else _parse_query_parameters(name, get_op)
        )

        functions.append(
            (
                schema,
                name,
                _function_description(body_schema, post_op or get_op),
                None,  # return_type — PostgREST encodes only `"200": {"description": "OK"}`
                None,  # returns_set — `produces` is a constant, so there is no signal
                'v' if get_op is None else None,
                get_op is not None,
                parameters,
            )
        )
    return tuple(functions)


def _find_body_schema(post_op: JsonObject) -> JsonObject | None:
    """Return the ``in: body`` parameter's schema from a POST operation, if present."""
    for raw_parameter in _as_list(post_op.get('parameters')):
        parameter = _as_object(raw_parameter)
        if parameter is not None and parameter.get('in') == 'body':
            return _as_object(parameter.get('schema'))
    return None


def _function_description(body_schema: JsonObject | None, operation: JsonObject | None) -> str | None:
    """Return a function's description: the body schema's, else summary + description."""
    if body_schema is not None:
        description = _as_str(body_schema.get('description'))
        if description is not None:
            return description
    if operation is None:  # pragma: no cover - callers always pass an operation
        return None
    parts = [part for part in (_as_str(operation.get('summary')), _as_str(operation.get('description'))) if part]
    return '\n\n'.join(parts) if parts else None


def _parse_body_parameters(name: str, body_schema: JsonObject, get_op: JsonObject | None) -> list[Row]:
    """Parse the POST body schema's ``properties`` into parameter 5-tuples, in order.

    ``properties`` is insertion-ordered from ``pdParams``, which ``funcsSqlQuery`` builds
    with ``array_agg(... ORDER BY idx)`` — so JSON key order *is* pg argument order.
    ``required`` is ``idx <= (pronargs - pronargdefaults)``, so a parameter has a default
    exactly when it is absent from ``required``.
    """
    required = {value for value in _as_list(body_schema.get('required')) if isinstance(value, str)}
    variadic = _variadic_parameter_names(get_op)
    properties = _as_object(body_schema.get('properties')) or {}

    parameters: list[Row] = []
    for parameter_name, raw_property in properties.items():
        prop = _as_object(raw_property)
        if prop is None:
            logger.debug(f'Skipping parameter {name}({parameter_name}): not a JSON object')
            continue
        raw_type = _resolve_type_token(prop, f'{name}({parameter_name})')
        parameters.append(
            (
                parameter_name,
                raw_type,
                'v' if parameter_name in variadic else None,
                parameter_name not in required,
                _array_element_type(raw_type),
            )
        )
    return parameters


def _parse_query_parameters(name: str, get_op: JsonObject | None) -> list[Row]:
    """Parse a GET operation's query parameters into parameter 5-tuples.

    Only reached for a ``/rpc/*`` path item that has no POST body schema, which PostgREST
    should never emit — every RPC gets a POST operation.
    """
    if get_op is None:
        return []

    parameters: list[Row] = []
    for raw_parameter in _as_list(get_op.get('parameters')):
        parameter = _as_object(raw_parameter)
        parameter_name = _as_str(parameter.get('name')) if parameter is not None else None
        if parameter is None or parameter_name is None:
            continue
        raw_type = _resolve_type_token(parameter, f'{name}({parameter_name})')
        parameters.append(
            (
                parameter_name,
                raw_type,
                'v' if parameter.get('collectionFormat') == 'multi' else None,
                parameter.get('required') is not True,
                _array_element_type(raw_type),
            )
        )
    return parameters


def _variadic_parameter_names(get_op: JsonObject | None) -> set[str]:
    """Return the names the GET operation marks ``collectionFormat: multi`` (VARIADIC).

    This is the only place the document betrays a VARIADIC argument, and only for
    non-volatile functions (a volatile function has no GET operation at all).
    """
    if get_op is None:
        return set()

    names: set[str] = set()
    for raw_parameter in _as_list(get_op.get('parameters')):
        parameter = _as_object(raw_parameter)
        if parameter is None or parameter.get('collectionFormat') != 'multi':
            continue
        parameter_name = _as_str(parameter.get('name'))
        if parameter_name is not None:
            names.add(parameter_name)
    return names


# ---------------------------------------------------------------------------
# JSON narrowing (mypy --strict discipline: never index an unguarded ``Any``).
# ---------------------------------------------------------------------------


def _as_object(value: Any) -> JsonObject | None:
    """Return ``value`` when it is a JSON object, else ``None``."""
    if isinstance(value, Mapping):
        narrowed: JsonObject = value
        return narrowed
    return None


def _as_list(value: Any) -> Sequence[Any]:
    """Return ``value`` when it is a JSON array, else an empty sequence."""
    if isinstance(value, list):
        narrowed: list[Any] = value
        return narrowed
    return ()


def _as_str(value: Any) -> str | None:
    """Return ``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


def _as_int(value: Any) -> int | None:
    """Return ``value`` when it is a JSON integer (not a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value
