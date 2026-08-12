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
inside a column's ``description``. Functions live at ``paths./rpc/<name>``. A definition's
**own** ``description`` is the table's SQL comment (``COMMENT ON TABLE``) — PostgREST
carries it verbatim, and castiron routes it to ``TableInfo.description`` via the table
3-tuple contract (CI-009); it is *not* a fidelity loss.

The fidelity floor (what this source structurally cannot see)
-------------------------------------------------------------
Each line is asserted by a test in ``tests/unit/sources/openapi/`` so it cannot silently
move:

- **Integer widths collapse only on PostgREST >= 14.8**, where ``smallint`` and ``integer`` both
  arrive as ``int32`` and are **indistinguishable** and ``bigint`` arrives as ``int64``. **Below
  14.8** ``format`` carries the pg type name for integers too (``smallint``/``integer``/``bigint``)
  and the three widths stay distinct, so the *older* server is **more** informative here -- the
  opposite direction from the volatility drift below. ``toSwaggerFormat`` gained the Swagger-legal
  spelling in **14.8** (PR #4641, 2026-04-03, "Fix invalid OpenAPI 2.0 format for integer types",
  because OpenAPI 2.0 defines ``int32``/``int64`` as *the* formats for ``type: integer``); a full
  ``v14.14`` vs ``v12.2.3`` document diff for one schema differs in 49 values -- 43 column
  properties and 6 function parameters, and nothing else.
  ⚠ **The floor here is 14.8 and the volatility floor below is 13.0.5 -- two unrelated upstream
  changes.** Neither is a general "minimum PostgREST", and this row deliberately adds no version
  gate: nothing *behaves* differently either side of 14.8, because
  :data:`OPENAPI_FORMAT_ALIASES` maps ``int32``/``int64`` into the same pg vocabulary the type
  maps and :data:`INTEGER_FAMILY` already key ``smallint``/``integer``/``bigint`` on, so a
  sub-14.8 document resolves correctly with no special case. Only what castiron *can know*
  differs. Everything else keeps its real pg type name on either server.
- ``nextval(...)`` defaults are dropped upstream (PostgREST feeds the default text to
  ``JSON.decode``, which fails), so an integer surrogate primary key looks NOT NULL with no
  default and no identity marker. See ``infer_generated_primary_keys``.
- Numeric precision/scale, ``varchar(n)`` typmods (``maxLength`` survives) and domain names
  are lost — ``format_type(atttypid, NULL)`` erases them.
- **UNIQUE, CHECK and EXCLUDE constraints do not exist anywhere in the document.**
- Foreign keys are **single-column only**, carry no schema and no real constraint name;
  composite FKs are invisible and a column in two FKs reports only one.
- **No constraint name of any kind is in the document.** castiron synthesizes pg's own defaults
  -- ``<table>_<column>_fkey``, ``<table>_pkey``, ``<table>_<cols>_key`` -- and every row it
  emits declares that with ``name_is_synthesized=True`` (CI-090), because the manufactured
  spelling is indistinguishable from a real default-named constraint once it has been written.
- A ``<fk/>`` marker may name a table the document does not contain, because privileges filter
  relations: the marker survives as a FOREIGN KEY constraint, but the builder resolves no edge
  and ``ColumnInfo.is_foreign_key`` stays ``False`` (CI-084).
- Primary-key *membership* is recoverable, composite-key **order** is not.
- Views carry no marker at all, so ``table_type`` is inferred from whether the entry declares
  any NOT NULL column (see :func:`classify_table_type`), and PostgREST reports every view column
  as nullable. The inference is exact except for a base table with no NOT NULL column at all,
  which reads as a VIEW -- a cell in which the misreading provably costs nothing (CI-075).
- **A view's primary key is recorded as a UNIQUE constraint, not a primary key.** The
  document *does* mark it (PostgREST propagates keys through views), but
  :meth:`castiron.ir.TableInfo.primary_key` is defined to be empty for a VIEW, so carrying
  it as a PK would leave ``ColumnInfo.primary`` and ``primary_key()`` disagreeing. This is
  a **downgrade, not a guess** -- the ``<pk/>`` marker is the document's own statement, and
  it is retained at the strength the IR can represent. Dropping it outright would lose the
  only evidence the key column is unique, which is what tells a foreign key pointing *at*
  the view that it is many-to-one rather than many-to-many. Foreign keys on a view are kept
  unchanged.
- Enum **values** are absent for array columns (``pg_enum`` is keyed on the base type), so
  such a column links only when the same enum also appears on a scalar column.
- A function's **return type** and **set-returning** flag are never encoded; **overloads
  collapse** to one arbitrary signature.
- **Volatility is a binary signal (POST-only ⇒ VOLATILE) — and only on PostgREST >= 13.0.5.**
  ``makeProcPathItem`` gained ``case pdVolatility pd of Volatile -> … & post ?~ postOp`` in
  **13.0.5** (PR #4174, CHANGELOG ``[13.0.5] - 2025-08-24``); at ``v13.0.4``, ``v12.2.12`` and
  ``v12.2.3`` it reads ``pe = (mempty :: PathItem) & get ?~ getOp & post ?~ postOp``, i.e. a
  ``get`` for **every** ``/rpc/`` path. So 13.0.0–13.0.4 are affected too and no 12.x release
  carries the fix. Below the floor castiron reports ``volatility`` **and** ``is_read_only`` as
  ``None`` for every function (see :func:`volatility_is_encoded`) rather than asserting that
  every mutation is read-only, and records the observed ``info.version`` on
  :attr:`castiron.ir.Schema.postgrest_version`.
  ⚠ **Argument order is NOT part of that degradation, and a future reader must not "simplify"
  by ignoring the GET below the floor.** ``makeProcGetParams = fmap makeProcGetParam`` over the
  routine's ordered ``pdParams`` is **byte-identical** at ``v12.2.3``, ``v13.0.4`` and
  ``v13.0.5``, so a sub-floor document's GET ``parameters`` array is still declaration order --
  and because a sub-floor server emits that array for VOLATILE functions too, order is recovered
  in *more* cases there, not fewer. Only the two volatility fields are unsound.
- Objects the API role cannot see are simply absent (RLS/privileges shrink the schema).

Point castiron at the database itself (CI-010/CI-011) when those facts matter.
"""

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from castiron.ir import ParameterOrder, Row, TableType
from castiron.sources.errors import SourceParseError

logger = logging.getLogger(__name__)

#: An untyped JSON object as it arrives from :func:`json.loads`.
JsonObject = Mapping[str, Any]

#: Swagger's own numeric formats → the Postgres vocabulary every castiron type map speaks.
#: These two are the *only* tokens ``toSwaggerFormat`` rewrites, and it only spells integers this
#: way from PostgREST **14.8** (PR #4641); everything else is already the raw pg type name -- as
#: are integers themselves below 14.8, which is why a sub-14.8 document needs no second alias
#: table either. So there is no second type map (decision CI5-D5).
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
#: Public because the CLI's identity-PK notice (CI-006) applies the same rule to the IR;
#: one definition, so the two cannot drift (Hard Rule #6).
INTEGER_FAMILY = frozenset({'smallint', 'integer', 'bigint'})

#: The first PostgREST release whose OpenAPI document encodes function volatility.
#:
#: PR #4174 (`CHANGELOG [13.0.5] - 2025-08-24`, "Fix OpenAPI specification incorrectly exposing
#: GET methods for volatile functions") changed ``makeProcPathItem`` in
#: ``src/PostgREST/Response/OpenAPI.hs`` from an unconditional
#: ``pe = (mempty :: PathItem) & get ?~ getOp & post ?~ postOp`` to
#: ``pe = case pdVolatility pd of Volatile -> … & post ?~ postOp; _ -> … & get ?~ getOp & post ?~ postOp``.
#: Read at the ``v12.2.3``, ``v12.2.12`` and ``v13.0.4`` tags (all unconditional) and at ``v13.0.5``
#: (gated), so the floor is exact: 13.0.0–13.0.4 are affected and no 12.x release carries the fix.
MIN_VOLATILITY_SIGNAL_VERSION: tuple[int, int, int] = (13, 0, 5)

#: The leading numeric run of a PostgREST ``prettyVersion``, which is all a comparison needs.
#:
#: Two eras, both covered: through 13.x ``prettyVersion`` is ``showVersion version`` plus an
#: optional ``' (pre-release)'`` plus an optional ``' (<7-char git hash>)'`` (``12.2.3 (519615d)``,
#: ``1.1 (pre-release)``); from 14.x it is the first **two** dot components plus the optional
#: pre-release marker (``14.14``, ``15 (pre-release)``). Taking the leading ``\d+(\.\d+)*`` run and
#: comparing int tuples is correct across both -- ``(12, 2, 12) < (13, 0, 5) <= (14, 14)`` -- with no
#: normalization, no zero-padding, and no ``packaging`` dependency (castiron has near-zero runtime
#: dependencies; decision D7).
_VERSION_PREFIX = re.compile(r'\d+(?:\.\d+)*')

#: The path prefix PostgREST exposes database functions under.
_RPC_PREFIX = '/rpc/'

#: HTTP methods whose presence proves a definition is writable through the API.
_WRITE_METHODS = ('post', 'patch', 'delete')

# Description markers, exactly as ``makeProperty`` builds them.
#
# The marker *shapes* are declared once and reused by both the extraction patterns and the
# ``Note:`` block pattern. Spelling them out twice let them drift: the block pattern once
# hard-coded single spaces while the extraction pattern allowed ``\s+``, so a marker with
# two spaces was detected but its block was NOT stripped -- and the raw marker text landed
# verbatim in the emitted ``Field(description=...)``.
_PK_MARKER_SHAPE = r'<pk\s*/>'
_FK_MARKER_SHAPE = r"<fk\s+table='{table}'\s+column='{column}'\s*/>"
_ATTRIBUTE = r"[^']*"
_CAPTURED_ATTRIBUTE = r"([^']*)"

_PK_MARKER = re.compile(_PK_MARKER_SHAPE)
_FK_MARKER = re.compile(_FK_MARKER_SHAPE.format(table=_CAPTURED_ATTRIBUTE, column=_CAPTURED_ATTRIBUTE))
_NOTE_BLOCK = re.compile(
    r'(?:\n\n)?Note:\n'
    r'(?:(?:This is a Primary Key\.'
    + _PK_MARKER_SHAPE
    + r'|This is a Foreign Key to `[^`]*`\.'
    + _FK_MARKER_SHAPE.format(table=_ATTRIBUTE, column=_ATTRIBUTE)
    + r')\n?)+\s*$'
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
    one-for-one; see :mod:`castiron.ir.build` for each contract. ``postgrest_version`` is the
    one exception -- it is not a row contract but the document's own provenance, carried through
    to :attr:`castiron.ir.Schema.postgrest_version`. It is appended last because this is a frozen
    dataclass, so field order is the constructor contract.
    """

    column_details: tuple[Row, ...] = ()
    fk_details: tuple[Row, ...] = ()
    constraints: tuple[Row, ...] = ()
    enum_types: tuple[Row, ...] = ()
    enum_type_mapping: tuple[Row, ...] = ()
    function_details: tuple[Row, ...] = ()
    table_details: tuple[Row, ...] = ()
    postgrest_version: str | None = None


@dataclass
class _RowAccumulator:
    """Mutable per-parse collector for the row contracts."""

    columns: list[Row] = field(default_factory=list)
    fks: list[Row] = field(default_factory=list)
    constraints: list[Row] = field(default_factory=list)
    enum_mappings: list[Row] = field(default_factory=list)
    tables: list[Row] = field(default_factory=list)
    #: ``(namespace, type_name)`` → the enum's labels, de-duplicated across columns.
    enums: dict[tuple[str, str], list[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small, pure helpers (exported: tests and future sources reuse them).
# ---------------------------------------------------------------------------


def normalize_format(format_token: str) -> str:
    """Translate a Swagger ``format`` token into the Postgres type vocabulary.

    Only ``int32``/``int64`` are Swagger's own vocabulary; every other token PostgREST
    emits is already the raw pg type name and passes through unchanged. A PostgREST below
    **14.8** spells integers with the pg name too (``smallint``/``integer``/``bigint``), so
    on such a server every token passes through and this function is a no-op.

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
    internally and never emits it), so this is one signal: a non-empty ``required`` array means
    ``BASE TABLE``, and anything else is a ``VIEW``.

    **Why the write verbs are no longer read as evidence (CI-075).** They used to be the first
    signal. Measured against real PostgREST -- **v14.14 and pinned v12.2.3**, in CI-008 -- the
    verbs track Postgres **auto-updatability**, not write privileges and not relation kind: a
    ``GRANT SELECT``-only *simple view* is auto-updatable, so PostgREST emits
    ``post``/``patch``/``delete`` for it, and 24 of the 26 relations in the committed capture
    carry write verbs including **3 of its 5 views**. The signal was noise, and it is what made
    ``active_customers``, ``ledger_summary`` and ``writable_customer_view`` classify as base
    tables and keep a primary key a view cannot have (CI5-D14a's ``<pk/>`` -> UNIQUE downgrade
    never fired). ``is_writable`` is still **computed and logged** below, because "this looked
    like evidence and provably is not" is worth being able to see in a debug trace.

    **The half that is certain.** A view never carries ``required``. PostgREST's ``required`` is
    exactly the NOT NULL set, and a view column's ``pg_attribute.attnotnull`` is false in
    Postgres -- views do not carry NOT NULL. That is a *catalog* fact, not a PostgREST behaviour,
    which is why it outranks anything observed over HTTP. Measured over the capture's 26
    definitions: 20 carry ``required`` and every one of the 20 is a base table, **0 exceptions**.

    **The residual bias, and why it is inert.** The ambiguous cell is "a base table with no NOT
    NULL column at all", which this reads as a ``VIEW``. ``CI5-D6`` biased that cell the other way
    on the argument that misreading a table as a view empties its primary key; **that argument is
    void here** (``CI94-Q2``, ruled -- ``CI5-D6`` is reversed). A base table lands in this cell
    only if PostgREST reports no NOT NULL column, and a Postgres PRIMARY KEY column *is* NOT
    NULL -- so such a table has no primary key and there is nothing to empty. The capture's one
    instance, ``all_nullable_readonly``, carries no ``<pk/>`` marker at all: flipping it changes
    exactly one IR field and **zero** emitted bytes. Net accuracy on the real capture: 23/26 ->
    **25/26**.

    A third signal was considered and rejected: "a ``<pk/>``-marked column outside a non-empty
    ``required`` implies a VIEW" is measured true 6/6 with 0 exceptions, but it is **provably
    equivalent** to the rule above on any document PostgREST can emit -- it could only differ in a
    cell (``required`` non-empty *and* a nullable PK column) Postgres cannot produce. Encoding it
    would be dead logic that reads as live, so it is asserted as a test instead.

    Args:
        name: The ``definitions`` key.
        definition: The definition object.
        paths: The document's ``paths`` object. Not a classification input; retained for the
            debug log (and to keep this exported function's signature stable -- ``CI94-D6``).

    Returns:
        ``'VIEW'`` or ``'BASE TABLE'``.
    """
    path_item = _as_object(paths.get(f'/{name}')) or {}
    is_writable = any(method in path_item for method in _WRITE_METHODS)
    required = definition.get('required')
    has_required = isinstance(required, list) and len(required) > 0

    table_type: TableType = 'BASE TABLE' if has_required else 'VIEW'
    logger.debug(
        f'Classified {name} as {table_type} (has_required={has_required}; writable={is_writable}, '
        f'which tracks auto-updatability and is NOT evidence of relation kind -- CI-075)'
    )
    return table_type


def volatility_is_encoded(postgrest_version: str | None) -> bool:
    """Return whether a document served by ``postgrest_version`` encodes function volatility.

    ``True`` iff the version parses and is at or above :data:`MIN_VOLATILITY_SIGNAL_VERSION`.
    An absent, empty or unparseable version is treated as **below** the floor: the conservative
    answer, because the cost of guessing wrong in the other direction is reporting a mutation as
    read-only, which is what a typed client would turn into a ``GET`` request for an ``INSERT``.

    Args:
        postgrest_version: A PostgREST ``info.version`` string, or ``None``.

    Returns:
        Whether ``volatility`` and ``is_read_only`` can be read out of the document.
    """
    parsed = _parse_postgrest_version(postgrest_version)
    return parsed is not None and parsed >= MIN_VOLATILITY_SIGNAL_VERSION


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
    contractual), while ``properties`` order is **preserved** as the document gives it.

    ⚠ **Preserved is not the same as meaningful, and the two cases differ.** For a table's
    columns the document order is real (pg ordinal). A **function's parameters are handed
    downstream REORDERED**, because the ``properties`` object they are built from is
    alphabetical: ``CI-078`` recovers declaration order from the document's two *ordered*
    encodings — the GET operation's ``parameters`` array and the POST body's ``required``
    array — and every function row states how much of it was established, in element 8
    (:class:`~castiron.ir.models.ParameterOrder`). See :func:`_declaration_order` for the rule.

    ⚠ This paragraph has been wrong in both directions. It first claimed ``properties`` order
    *was* argument position (false — it is alphabetical, measured; that claim is the origin of
    ``CI-078``), then that argument order was therefore unavailable (also false — it is in the
    two arrays above). Do not restate either without measuring against a real PostgREST document.

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
        The row contracts, ready for :func:`castiron.ir.build_schema`.

    Raises:
        SourceParseError: The document is not PostgREST Swagger 2.0 output, exposes no
            tables or views, or contains a property with no usable type.
    """
    definitions = _validate_envelope(document, schema)
    paths = _as_object(document.get('paths')) or {}
    postgrest_version = _postgrest_version(document)
    volatility_encoded = volatility_is_encoded(postgrest_version)
    if not volatility_encoded:
        logger.debug(
            f'PostgREST version {postgrest_version!r} is below {MIN_VOLATILITY_SIGNAL_VERSION} (or unstated), '
            f'so volatility and is_read_only are reported as unknown for every function'
        )

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
        function_details=_parse_functions(paths, schema, volatility_encoded),
        table_details=tuple(rows.tables),
        postgrest_version=postgrest_version,
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


def _postgrest_version(document: JsonObject) -> str | None:
    """Return the document's verbatim ``info.version``, or ``None`` when it states none.

    PostgREST sets it unconditionally in ``postgrestSpec`` (``& info .~ (… & version .~
    prettyVersion …)``). Unlike ``info.title``/``info.description``, which come from the schema's
    SQL comment, ``version`` is always the server's own ``prettyVersion`` -- so it is trustworthy
    provenance rather than user-controlled text.
    """
    info = _as_object(document.get('info'))
    return None if info is None else _as_str(info.get('version'))


def _parse_postgrest_version(value: str | None) -> tuple[int, ...] | None:
    """Return the leading numeric run of a ``prettyVersion`` as an int tuple, or ``None``.

    See :data:`_VERSION_PREFIX` for why the leading run is the whole rule.
    """
    if value is None:
        return None
    match = _VERSION_PREFIX.match(value)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split('.'))


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

    # The definition's own `description` is the table's SQL comment (`COMMENT ON TABLE`).
    # Emitted for every parsed table -- including as `None` -- so `table_details` stays one
    # row per table and the contract is uniform. A definition skipped above (not an object,
    # or no `properties`) contributes no row at all, since it contributes no table.
    # `_as_str` is what keeps a non-string `description` from being mistaken for one; the
    # builder owns the rest of the normalization.
    rows.tables.append((schema, table_name, _as_str(definition.get('description'))))

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
            column_name == sole_pk_column and not is_nullable and default is None and data_type in INTEGER_FAMILY
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

        # Truthiness, not `is not None`: `<fk table='' column=''/>` names nothing, and the
        # builder drops the edge anyway -- but the synthesized constraint row would survive
        # and set `is_foreign_key` on a column that has no relationship.
        if markers.foreign_table and markers.foreign_column:
            # SYNTHESIZED, and declared as such (CI-090). The document carries no constraint name
            # anywhere, so this is pg's own default template, not a name anybody read. It is
            # byte-identical to what Postgres names a genuinely default-named constraint, so the
            # trailing `True` on both rows is the only place the fabrication survives.
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
                    True,
                )
            )
            fk_constraints.append(
                (
                    constraint_name,
                    table_name,
                    [column_name],
                    'f',
                    f'FOREIGN KEY ({column_name}) REFERENCES {markers.foreign_table}({markers.foreign_column})',
                    True,
                )
            )

        _record_enum(table_name, column_name, prop, data_type, schema, rows)

    # A VIEW gets a UNIQUE row rather than a PRIMARY KEY row. PostgREST *does* propagate
    # `<pk/>` markers through views, but ``TableInfo.primary_key()`` is defined to be empty
    # for a VIEW, so a PK row would set ``ColumnInfo.primary = True`` on a table whose
    # ``primary_key()`` says ``[]`` -- an IR that contradicts itself, and that different
    # emitters read differently. Dropping the marker outright is equally wrong: it is the
    # only evidence the key column is unique, and without it every foreign key pointing AT
    # the view degrades to MANY_TO_MANY and is emitted as a plural list. Downgrading to
    # UNIQUE keeps both facts. Foreign keys on a view are carried unchanged.
    #
    # Both names below are SYNTHESIZED from pg's default templates for the same reason as the
    # foreign-key name above, and both rows declare it with a trailing `True` (CI-090). The
    # synthesis is not FK-specific: every constraint name this source produces is manufactured.
    if pk_columns and table_type != 'VIEW':
        rows.constraints.append((f'{table_name}_pkey', table_name, pk_columns, 'p', None, True))
    elif pk_columns:
        logger.debug(f'Recording the primary-key markers on view {table_name} as UNIQUE: a VIEW has no primary key')
        rows.constraints.append(
            (
                f'{table_name}_{"_".join(pk_columns)}_key',
                table_name,
                pk_columns,
                'u',
                f'UNIQUE ({", ".join(pk_columns)})',
                True,
            )
        )
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


def _parse_functions(paths: JsonObject, schema: str, volatility_encoded: bool) -> tuple[Row, ...]:
    """Parse every ``/rpc/<name>`` path item into a function 9-tuple, sorted by name.

    ⚠ **This is the one place that decides a function's parameter ORDER**, for both parameter
    paths, so the GET operation is read once and one rule applies everywhere. See
    :func:`_declaration_order` for the rule and the encodings it reads.

    Args:
        paths: The document's ``paths`` object.
        schema: The schema the document describes.
        volatility_encoded: Whether the serving PostgREST gates the GET operation on volatility
            (:func:`volatility_is_encoded`). When ``False``, elements 5 and 6 -- and **only**
            those two -- degrade to ``None``: the GET is emitted for every function there, so its
            absence proves nothing. Order and ``VARIADIC`` detection still read the same GET,
            because ``makeProcGetParams`` did not change across the boundary (module docstring).
    """
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
        if body_schema is not None:
            parameters = _parse_body_parameters(name, body_schema, get_op)
            order, parameter_order = _declaration_order(
                get_op,
                [value for value in _as_list(body_schema.get('required')) if isinstance(value, str)],
                [parameter[0] for parameter in parameters],
            )
            parameters = _reordered(parameters, order)
        else:
            # The GET query-parameter array IS the parameter list here, and it is an ordered
            # JSON array, so it arrives in declaration order already (decision D8).
            parameters = _parse_query_parameters(name, get_op)
            parameter_order = ParameterOrder.DECLARED

        functions.append(
            (
                schema,
                name,
                _function_description(body_schema, post_op or get_op),
                None,  # return_type — PostgREST encodes only `"200": {"description": "OK"}`
                None,  # returns_set — `produces` is a constant, so there is no signal
                ('v' if get_op is None else None) if volatility_encoded else None,
                (get_op is not None) if volatility_encoded else None,
                parameters,
                parameter_order,
            )
        )
    return tuple(functions)


def _declaration_order(
    get_op: JsonObject | None,
    required: Sequence[str],
    names: Sequence[str],
) -> tuple[list[str], ParameterOrder]:
    """Return the best-known parameter order and how much of it the document established.

    A PostgREST document serializes one parameter list three ways, and **two of them preserve
    order because they are JSON arrays** while the third does not because it is a JSON object:

    ==========================  ========  =============  ==================================
    Encoding                    Shape     Order          Present when
    ==========================  ========  =============  ==================================
    POST body ``properties``    object    alphabetical   always
    GET operation ``parameters``array     declaration    ``STABLE``/``IMMUTABLE`` only,
                                                         **on PostgREST >= 13.0.5**; below
                                                         that, for every function
    POST body ``required``      array     declaration    always (non-defaulted arguments)
    ==========================  ========  =============  ==================================

    ⚠ **The "Present when" column is the only version-dependent cell, and it only ever widens.**
    PostgREST < 13.0.5 emits the GET for VOLATILE functions too (module docstring, PR #4174), and
    ``makeProcGetParams`` is byte-identical across that boundary -- so a sub-floor document
    recovers order in *more* cases, by the same rule, and this function needs no version gate.

    The rule, in priority order:

    1. A GET whose names (filtered to ``names``) are a **permutation** of them -> that order,
       :attr:`~castiron.ir.models.ParameterOrder.DECLARED`. Measured against
       ``pg_proc.proargnames`` with three purpose-built anti-alphabetical probes, which
       falsified the only rival reading ("required first, then alphabetical within group").
    2. ``len(names) <= 1`` -> as given, ``DECLARED``. A list of length =<1 **is** in declaration
       order; that is a fact, not a guess (decision D6).
    3. A non-empty ``required`` -> ``required``, then the remaining names in the order given.
       ``required`` lists exactly the non-defaulted arguments, and Postgres forbids a defaulted
       parameter before a non-defaulted one, so it is the declaration-order **PREFIX** -- never a
       scattered subset, so the result is "correct prefix + unknown tail", never interleaved
       wrongness. All arguments required => the prefix is the whole list => ``DECLARED``;
       otherwise ``DECLARED_PREFIX``. On PostgREST >= 13.0.5 this is the only signal a
       **VOLATILE** function exposes, and a VOLATILE mutation with no defaults recovers its order
       in full.
    4. Otherwise (no usable GET, >=2 arguments, every one defaulted) -> as given, ``UNKNOWN``.

    A GET that is *not* a permutation of the body's names is a shape no capture exhibits, so it
    falls through to rule 3 rather than failing or partially reordering (decision D7).

    ⚠ Hard Rule #9: every returned order comes from a **list** in document order. Sets are used
    for membership only and are never iterated to produce output.

    Args:
        get_op: The path item's GET operation, or ``None`` for a VOLATILE function served by
            PostgREST >= 13.0.5.
        required: The POST body schema's ``required`` array, in document order.
        names: The POST body schema's ``properties`` keys, in document order (alphabetical).

    Returns:
        ``(order, parameter_order)`` -- the names to build the parameter list in, and how much of
        the declaration order that list actually carries.
    """
    known = set(names)
    if get_op is not None:
        query_order = [name for name in _query_parameter_names(get_op) if name in known]
        if sorted(query_order) == sorted(names):  # a permutation -- sorted only to COMPARE
            return query_order, ParameterOrder.DECLARED
        logger.debug(
            f'GET query parameters {query_order} are not a permutation of the POST body names '
            f'{list(names)}; falling back to the `required` array for declaration order'
        )

    if len(names) <= 1:
        return list(names), ParameterOrder.DECLARED

    prefix = [name for name in required if name in known]
    if not prefix:
        return list(names), ParameterOrder.UNKNOWN

    tail = [name for name in names if name not in set(prefix)]
    state = ParameterOrder.DECLARED if not tail else ParameterOrder.DECLARED_PREFIX
    return prefix + tail, state


def _reordered(parameters: Sequence[Row], order: Sequence[str]) -> list[Row]:
    """Return ``parameters`` sorted into ``order`` (the parameter name is row element 0).

    Args:
        parameters: The parameter 5-tuples, in the order they were parsed.
        order: The parameter names, in the order to emit them. A permutation of the rows' names.

    Returns:
        The same rows, reordered. Deterministic: driven by ``order``, which is a list.
    """
    by_name = {parameter[0]: parameter for parameter in parameters}
    return [by_name[name] for name in order]


def _query_parameter_names(get_op: JsonObject) -> list[str]:
    """Return the GET operation's query-parameter names, in document (declaration) order."""
    names: list[str] = []
    for raw_parameter in _as_list(get_op.get('parameters')):
        parameter = _as_object(raw_parameter)
        parameter_name = _as_str(parameter.get('name')) if parameter is not None else None
        if parameter_name is not None:
            names.append(parameter_name)
    return names


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
    """Parse the POST body schema's ``properties`` into parameter 5-tuples, in document order.

    ⚠ **Document order is ALPHABETICAL, not pg argument order**, which is why this function does
    **not** decide the final order — :func:`_parse_functions` reorders these rows through
    :func:`_declaration_order`. This docstring used to claim the opposite — that ``properties`` is
    insertion-ordered from ``pdParams``, so "JSON key order *is* pg argument order". Measured false
    against a real PostgREST (CI-089): ``create_order`` is declared
    ``(p_customer_id, p_status, p_lines)`` and its POST body ``properties`` arrive as
    ``['p_customer_id', 'p_lines', 'p_status']``. **That false claim is why ``CI-078`` exists** —
    someone read this paragraph instead of measuring.

    Declaration order survives in **two** places, and this docstring's successor claim — that it
    survives "in exactly one place, the GET operation" — was also false. The GET's ``parameters``
    array is one (PostgREST emits it only for a STABLE/IMMUTABLE function). The POST body's
    ``required`` array is the other, and it is present regardless of volatility, so it is the only
    order signal a VOLATILE function exposes at all.

    ``required`` is ``idx <= (pronargs - pronargdefaults)``, so a parameter has a default exactly
    when it is absent from ``required``. ⚠ ``CI-078`` measured that the array carries **order as
    well as membership**: it is the declaration-order *prefix*, because Postgres forbids a
    defaulted parameter before a non-defaulted one. The membership claim is unchanged; it is now
    the weaker half of what the array says.
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

    The result needs no reordering: a GET's ``parameters`` is an ordered JSON **array**, so this
    list is already in declaration order and :func:`_parse_functions` reports it ``DECLARED``
    (decision D8).
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

    This is the only place the document betrays a VARIADIC argument, and -- on PostgREST
    >= 13.0.5 -- only for non-volatile functions, since a volatile function has no GET operation
    there. Below that floor every function has one, so VARIADIC is detected for VOLATILE functions
    too; nothing here is version-gated, because the marker means the same thing either way.
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
