"""The Postgres-vocabulary -> Pydantic v2 type map (the type moat).

A faithful port of supabase-pydantic's ``PYDANTIC_TYPE_MAP`` (only the Pydantic map;
the two SQLAlchemy maps land with the SQLAlchemy emitter in CI-012). Each entry's
newline-joined import blob is split into a tuple of single import lines so an emitter
can build one flat, sorted import set.

The keys span **both** Postgres vocabularies -- the internal forms (``int4``,
``timestamptz``, ``varchar``) and the ``information_schema`` / ``format_type`` forms
(``integer``, ``timestamp with time zone``, ``character varying``). This redundancy is
what makes array-element resolution via ``array_element_type`` type-equivalent to
supabase-pydantic's ``udt_name`` resolution (see the CI4-Q3 parity test).

supabase-pydantic's fidelity choices are ported verbatim -- ``float`` -> ``Decimal``,
``json``/``jsonb`` -> the ``dict | list[dict] | list[Any] | Json`` union, ``uuid`` ->
``UUID4``, ``point`` -> ``Tuple[float, float]``, other geometrics -> ``Any``. Quirks are
carried, not "fixed", in this row.

This module defines type *strings* only; it does not import ``pydantic``.
"""

from castiron.types.resolution import TypeMap, TypeResolution

_DECIMAL = ('from decimal import Decimal',)
_DATETIME = ('import datetime',)
_ANY = ('from typing import Any',)
_JSON = ('from typing import Any', 'from pydantic import Json')

PYDANTIC_TYPE_MAP: TypeMap = {
    # Integer types
    'integer': TypeResolution('int'),
    'int': TypeResolution('int'),
    'bigint': TypeResolution('int'),
    'smallint': TypeResolution('int'),
    'int2': TypeResolution('int'),
    'int4': TypeResolution('int'),
    'int8': TypeResolution('int'),
    # Decimal / numeric types
    'numeric': TypeResolution('Decimal', _DECIMAL),
    'decimal': TypeResolution('Decimal', _DECIMAL),
    # Floating point types
    'real': TypeResolution('float'),
    'float4': TypeResolution('float'),
    'double precision': TypeResolution('float'),
    'float': TypeResolution('Decimal', _DECIMAL),
    'float8': TypeResolution('float'),
    # Serial types
    'serial': TypeResolution('int'),
    'bigserial': TypeResolution('int'),
    'smallserial': TypeResolution('int'),
    'serial2': TypeResolution('int'),
    'serial4': TypeResolution('int'),
    'serial8': TypeResolution('int'),
    # Money type
    'money': TypeResolution('Decimal', _DECIMAL),
    # Character types
    'character varying': TypeResolution('str'),
    'varchar': TypeResolution('str'),
    'character varying(n)': TypeResolution('str'),
    'varchar(n)': TypeResolution('str'),
    'character(n)': TypeResolution('str'),
    'char(n)': TypeResolution('str'),
    'char': TypeResolution('str'),
    'text': TypeResolution('str'),
    # Binary type
    'bytea': TypeResolution('bytes'),
    # Date / time types
    'timestamp': TypeResolution('datetime.datetime', _DATETIME),
    'timestamp with time zone': TypeResolution('datetime.datetime', _DATETIME),
    'timestamptz': TypeResolution('datetime.datetime', _DATETIME),
    'timestamp without time zone': TypeResolution('datetime.datetime', _DATETIME),
    'date': TypeResolution('datetime.date', _DATETIME),
    'time': TypeResolution('datetime.time', _DATETIME),
    'time with time zone': TypeResolution('datetime.time', _DATETIME),
    'time without time zone': TypeResolution('datetime.time', _DATETIME),
    'timetz': TypeResolution('datetime.time', _DATETIME),
    'interval': TypeResolution('datetime.timedelta', _DATETIME),
    # Boolean type
    'boolean': TypeResolution('bool'),
    'bool': TypeResolution('bool'),
    # Enum sentinel (columns overlay their enum class from ColumnInfo.enum_info)
    'enum': TypeResolution('str'),
    # Geometric types
    'point': TypeResolution('Tuple[float, float]', ('from typing import Tuple',)),
    'line': TypeResolution('Any', _ANY),
    'lseg': TypeResolution('Any', _ANY),
    'box': TypeResolution('Any', _ANY),
    'path': TypeResolution('Any', _ANY),
    'polygon': TypeResolution('Any', _ANY),
    'circle': TypeResolution('Any', _ANY),
    # Network address types
    'cidr': TypeResolution('IPv4Network', ('from ipaddress import IPv4Network, IPv6Network',)),
    'inet': TypeResolution('IPv4Address | IPv6Address', ('from ipaddress import IPv4Address, IPv6Address',)),
    'macaddr': TypeResolution('str'),
    'macaddr8': TypeResolution('str'),
    # Bit string types
    'bit': TypeResolution('str'),
    'bit varying': TypeResolution('str'),
    'varbit': TypeResolution('str'),
    # Text search types
    'tsvector': TypeResolution('str'),
    'tsquery': TypeResolution('str'),
    # UUID type
    'uuid': TypeResolution('UUID4', ('from pydantic import UUID4',)),
    # XML type
    'xml': TypeResolution('str'),
    # JSON types
    'json': TypeResolution('dict | list[dict] | list[Any] | Json', _JSON),
    'jsonb': TypeResolution('dict | list[dict] | list[Any] | Json', _JSON),
    # Array sentinel (element type is resolved from ColumnInfo.array_element_type)
    'array': TypeResolution('list'),
}
