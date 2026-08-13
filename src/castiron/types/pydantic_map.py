"""The Postgres-vocabulary -> Pydantic v2 type map (the type moat).

A faithful port of supabase-pydantic's ``PYDANTIC_TYPE_MAP`` (only the Pydantic map;
the two SQLAlchemy maps land with the SQLAlchemy emitter in CI-032). Each entry's
newline-joined import blob is split into a tuple of single import lines so an emitter
can collect one flat, deduplicated import set. *Rendering* that set is the emitter's
job and is no longer a plain sort -- ``emitters.base.render_import_block`` groups it into
isort's sections and merges same-module lines (CI-094).

The keys span **both** Postgres vocabularies -- the internal forms (``int4``,
``timestamptz``, ``varchar``) and the ``information_schema`` / ``format_type`` forms
(``integer``, ``timestamp with time zone``, ``character varying``). This redundancy is
what makes array-element resolution via ``array_element_type`` type-equivalent to
supabase-pydantic's ``udt_name`` resolution (see the CI4-Q3 parity test).

supabase-pydantic's fidelity choices are ported verbatim -- ``float`` -> ``Decimal``,
``json``/``jsonb`` -> the ``dict | list[dict] | list[Any] | Json`` union, ``uuid`` ->
``UUID4``, other geometrics -> ``Any``. Those quirks were carried, not "fixed", in CI-004.

⚠ **Two entries are deliberate divergences from upstream's resolution string, and they are the
only two** (CI-092, an enumerated sweep of all 65 entries): ``point`` and ``cidr``. Both were
putting a ruff finding into the user's repository -- a deprecated ``typing`` alias and an import
of a name the resolution never uses. Neither changes the *shape* castiron reports, only its
spelling; see each entry's comment. castiron promises its output is clean under **F**, **UP** and
**I** at ruff's defaults (``CI94-Q3(c)``), and a type map that ships ``F401``/``UP035`` cannot
keep that promise.

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
    # ``character`` is what ``char(n)`` reports through information_schema.data_type,
    # format_type(), and PostgREST's ``format`` -- a gap in supabase-pydantic's map
    # (``char``, ``char(n)`` and ``character(n)`` were all present) filled in CI-005.
    'character': TypeResolution('str'),
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
    # ⚠ CI-092: upstream resolves this to ``Tuple[float, float]`` + ``from typing import Tuple``,
    # which puts ``UP035`` (deprecated alias) and three ``UP006`` findings into every user's
    # repository. The PEP 585 builtin is a valid *runtime* expression on Python 3.9+, so it needs
    # **no import and no** ``from __future__ import annotations`` -- which matters, because
    # castiron emits that future import only conditionally (``emitter.py``, on foreign keys). The
    # emitted module already requires >=3.10 to execute regardless: every nullable field is
    # ``X | None`` and ``json`` resolves to ``dict | list[dict] | list[Any] | Json``, both bare
    # PEP 604/585 forms. Same shape, current spelling.
    'point': TypeResolution('tuple[float, float]'),
    'line': TypeResolution('Any', _ANY),
    'lseg': TypeResolution('Any', _ANY),
    'box': TypeResolution('Any', _ANY),
    'path': TypeResolution('Any', _ANY),
    'polygon': TypeResolution('Any', _ANY),
    'circle': TypeResolution('Any', _ANY),
    # Network address types
    # ⚠ CI-092: the import used to name ``IPv6Network`` as well, which the resolution never
    # references -- a live ``F401`` in the emitted module. Narrowed to what is used. Widening the
    # *resolution* to ``IPv4Network | IPv6Network`` was the other way to close it and is NOT what
    # this row does: that would change the reported type, which is a fidelity decision, not a lint
    # fix. (``inet`` below imports both names and **uses both** -- it is correct as it stands.)
    'cidr': TypeResolution('IPv4Network', ('from ipaddress import IPv4Network',)),
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
