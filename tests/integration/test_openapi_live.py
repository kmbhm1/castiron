"""The OpenAPI/PostgREST source against a **real** PostgREST, one class per fidelity claim.

Every assertion here was measured against the ``castiron-testbed`` apparatus (see
``tests/integration/README.md``), not reasoned about. That distinction is the whole point of the
suite: the seed schema exists to *falsify* castiron's documented model of PostgREST, and it
already has — the corrected expectations below deliberately contradict several claims in the
CI-008 design spec, which was written before the apparatus existed.

Reading guide for the three kinds of assertion in this file:

- **A fidelity-floor fact.** "This source structurally cannot see X." Asserted positively (the
  coarse value *is* what arrives) with a comment naming what was lost. Not a bug; changing it
  requires the live-DB source (CI-010/011).
- **A characterization.** castiron's current behaviour on a shape where the *right* behaviour is
  genuinely undecided. Pinned so a future row has to choose deliberately, with the open question
  stated in the comment.
- **An ``xfail``.** A known defect in shipped code. The reason names the row; ``strict=True``
  unless a fix is already in flight, so the marker deletes itself the day the defect is fixed.
"""

import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from types import ModuleType
from typing import Any

import pytest

from castiron.emitters import EmitterConfig, PydanticEmitter
from castiron.ir import ColumnInfo, ConstraintType, FunctionInfo, FunctionVolatility, ParameterMode, Schema, TableInfo
from castiron.sources.openapi import build_schema_from_document
from tests.integration.conftest import DocumentLoader

pytestmark = pytest.mark.integration

#: The relationship marker ``makeProperty`` writes into a column description. Spelled out here
#: rather than imported from ``parse`` on purpose: a test that reused the parser's own pattern
#: would pass even if that pattern stopped matching what PostgREST actually emits.
_FK_MARKER = re.compile(r"<fk table='([^']*)' column='([^']*)'/>")

#: The primary-key marker, same rationale as :data:`_FK_MARKER` and with the same deliberate
#: strictness: the parser accepts the laxer ``<pk\s*/>``, so spelling that here would hide a change
#: in what PostgREST actually writes behind castiron's tolerance for it.
_PK_MARKER = re.compile(r'<pk/>')

#: One row of the key-constraint parity table: name, type, columns, definition.
_KeyRow = tuple[str, ConstraintType, tuple[str, ...], str | None]


def _table(schema: Schema, name: str) -> TableInfo:
    """Return the named table, or fail listing what the schema actually contains."""
    for table in schema.tables:
        if table.name == name:
            return table
    raise AssertionError(f'{name!r} is absent from the schema. Present: {[t.name for t in schema.tables]}')


def _column(schema: Schema, table: str, name: str) -> ColumnInfo:
    """Return the named column, or fail listing the table's actual columns."""
    found = _table(schema, table)
    for column in found.columns:
        if column.name == name:
            return column
    raise AssertionError(f'{table}.{name!r} is absent. Present: {[c.name for c in found.columns]}')


def _function(schema: Schema, name: str) -> FunctionInfo:
    """Return the named function, or fail listing the schema's actual functions."""
    for function in schema.functions:
        if function.name == name:
            return function
    raise AssertionError(f'{name!r} is absent. Present: {[f.name for f in schema.functions]}')


def _pk_marked_columns(document: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return every definition's ``<pk/>``-marked columns, read from the **document**.

    Reading the raw document rather than the IR is the whole point: it gives an expectation an
    input castiron did not produce. An expectation computed from ``Schema`` alone can only restate
    whatever castiron did, and a hand-written roster of relation names goes stale the first time
    the seed or the classification changes -- which is exactly how ``CI-135`` happened.

    Args:
        document: The PostgREST OpenAPI document.

    Returns:
        ``{definition name: marked column names}`` in the document's own property order (which is
        the order ``_parse_definition`` collects them in), for definitions carrying ≥1 marker.
    """
    marked: dict[str, tuple[str, ...]] = {}
    for name, definition in document['definitions'].items():
        columns = tuple(
            column
            for column, prop in definition.get('properties', {}).items()
            if _PK_MARKER.search(prop.get('description') or '')
        )
        if columns:
            marked[name] = columns
    return marked


def _emit(schema: Schema) -> str:
    """Emit ``schema`` with default options and return the single generated module's text."""
    files = PydanticEmitter(EmitterConfig()).emit(schema)
    assert [f.path for f in files] == ['schema.py']
    return files[0].content


@contextmanager
def _executed(schema: Schema) -> Iterator[ModuleType]:
    """Execute ``schema``'s emitted module the way importing the written file does.

    ⚠ **The** ``sys.modules`` **registration is load-bearing, not bookkeeping.** The emitted module
    opens with ``from __future__ import annotations``, so every annotation is a string that pydantic
    resolves lazily — by looking up ``cls.__module__`` in ``sys.modules``. Executing into a bare
    ``dict`` (which is what this suite did while the proof ran on ``inventory``) leaves nothing to
    look up, so the first model carrying a deferred name fails at **instantiation**::

        PydanticUserError: `CustomersInsert` is not fully defined; you should define `UUID4`,
        then call `CustomersInsert.model_rebuild()`

    That is an artifact of the harness, not a defect in the output — a user writes ``schema.py`` to
    disk and imports it, which registers it — but it is why the bare-dict harness could not be
    pointed at ``public`` unchanged. It passed on ``inventory`` only because two small tables
    happen to use no type that defers (measured: ``UUID4`` is imported by the module and still
    unresolvable without the registration). Mirrors ``tests/unit/cli/test_gen.py``'s exec harness.

    Args:
        schema: The IR to emit and execute.

    Yields:
        The executed module, with the generated classes as attributes.
    """
    module = ModuleType('castiron_live_generated')
    sys.modules[module.__name__] = module
    try:
        exec(compile(_emit(schema), 'schema.py', 'exec'), module.__dict__)  # noqa: S102 - the proof
        yield module
    finally:
        del sys.modules[module.__name__]


class TestEnvelope:
    """The document castiron receives, and the schema-selection contract around it."""

    def test_document_is_postgrest_swagger_2(self, live_public_document: Mapping[str, Any]) -> None:
        assert live_public_document['swagger'] == '2.0'
        assert 'definitions' in live_public_document
        assert 'paths' in live_public_document

    def test_public_schema_contains_exactly_the_seed_objects(self, live_public_schema: Schema) -> None:
        # Enumerated rather than counted (standing lesson CI-072): a count of 26 would survive one
        # object being renamed into another's place. `private_ledger` and `secret_op` are absent by
        # privilege, which is asserted in TestPrivilegeFloor.
        assert [t.name for t in live_public_schema.tables] == [
            'active_customers',
            'all_nullable_readonly',
            'audit_links',
            'awkward_names',
            'bookings',
            'customer_tags',
            'customers',
            'employee_profiles',
            'employees',
            'ledger_refs',
            'ledger_summary',
            'mv_customer_spend',
            'order_lines',
            'order_report',
            'orders',
            'partially_visible',
            'people',
            'person_notes',
            'products',
            'rls_locked_notes',
            'rls_open_notes',
            'series',
            'series_entries',
            'tags',
            'type_menagerie',
            'writable_customer_view',
        ]

    def test_accept_profile_reaches_a_different_smaller_schema(
        self, live_public_schema: Schema, live_inventory_schema: Schema
    ) -> None:
        assert [t.name for t in live_inventory_schema.tables] == ['currencies', 'regions']
        public_names = {t.name for t in live_public_schema.tables}
        assert public_names.isdisjoint({t.name for t in live_inventory_schema.tables})
        assert [f.name for f in live_inventory_schema.functions] == ['lookup_region']

    def test_an_unexposed_schema_is_invisible_in_every_exposed_document(
        self, live_public_schema: Schema, live_inventory_schema: Schema, live_edge_schema: Schema
    ) -> None:
        # `audit` is deliberately kept out of `db-schemas`. Enumerating all three exposed documents
        # rather than only `public` is standing lesson CI6-Q7: an "invisible everywhere" claim has
        # to be checked on every path that could see it.
        for schema in (live_public_schema, live_inventory_schema, live_edge_schema):
            assert 'event_log' not in {t.name for t in schema.tables}

    def test_the_edge_schema_is_the_quarantine_seed(self, live_edge_schema: Schema) -> None:
        assert [t.name for t in live_edge_schema.tables] == [
            'dual_fk_child',
            'geese',
            'goslings',
            'identifier_torture',
            'parent_a',
            'parent_b',
        ]


class TestTypeFidelity:
    """``type_menagerie`` — one column per interesting Postgres type."""

    def test_column_order_is_the_declaration_order(self, live_public_schema: Schema) -> None:
        # `properties` is insertion-ordered from array_agg(... ORDER BY attnum), and castiron
        # preserves it. This is the assertion that would catch a parser that sorted columns.
        assert [c.name for c in _table(live_public_schema, 'type_menagerie').columns] == [
            'id',
            'c_smallint',
            'c_integer',
            'c_bigint',
            'c_numeric',
            'c_numeric_plain',
            'c_decimal',
            'c_real',
            'c_double',
            'c_money',
            'c_text',
            'c_varchar',
            'c_varchar_unbounded',
            'c_char',
            'c_boolean',
            'c_uuid',
            'c_json',
            'c_jsonb',
            'c_date',
            'c_time',
            'c_timetz',
            'c_timestamp',
            'c_timestamptz',
            'c_interval',
            'c_bytea',
            'c_inet',
            'c_cidr',
            'c_macaddr',
            'c_bit',
            'c_varbit',
            'c_tsvector',
            'c_xml',
            'c_point',
            'c_polygon',
            'c_int_array',
            'c_text_array',
            'c_timestamptz_array',
            'c_status',
            'c_status_array',
            'c_audit_status',
            'c_email',
            'c_percentage',
        ]

    def test_every_raw_type_is_the_measured_one(self, live_public_schema: Schema) -> None:
        # Enumerated, not sampled (CI-072). Every lossy mapping in the fidelity floor is visible in
        # this one table: the int32 collapse, the typmod erasure, the domain collapse, and the
        # schema-qualification rule for user-defined types.
        table = _table(live_public_schema, 'type_menagerie')
        assert {c.name: c.raw_type for c in table.columns} == {
            'id': 'bigint',
            'c_smallint': 'integer',  # smallint is INDISTINGUISHABLE from integer here
            'c_integer': 'integer',
            'c_bigint': 'bigint',
            'c_numeric': 'numeric',  # numeric(12,4) -- precision and scale erased
            'c_numeric_plain': 'numeric',
            'c_decimal': 'numeric',  # decimal(6,2) -- the pg alias resolves to numeric too
            'c_real': 'real',
            'c_double': 'double precision',
            'c_money': 'money',
            'c_text': 'text',
            'c_varchar': 'character varying',  # the length survives on max_length, not here
            'c_varchar_unbounded': 'character varying',
            'c_char': 'character',
            'c_boolean': 'boolean',
            'c_uuid': 'uuid',
            'c_json': 'json',
            'c_jsonb': 'jsonb',
            'c_date': 'date',
            'c_time': 'time without time zone',
            'c_timetz': 'time with time zone',
            'c_timestamp': 'timestamp without time zone',
            'c_timestamptz': 'timestamp with time zone',
            'c_interval': 'interval',
            'c_bytea': 'bytea',
            'c_inet': 'inet',
            'c_cidr': 'cidr',
            'c_macaddr': 'macaddr',
            'c_bit': 'bit',
            'c_varbit': 'bit varying',
            'c_tsvector': 'tsvector',
            'c_xml': 'xml',
            'c_point': 'point',
            'c_polygon': 'polygon',
            'c_int_array': 'integer[]',
            'c_text_array': 'text[]',
            'c_timestamptz_array': 'timestamp with time zone[]',
            'c_status': 'public.status',  # SEED-Q1: user-defined types are ALWAYS qualified
            'c_status_array': 'public.status[]',
            'c_audit_status': 'audit.status',
            'c_email': 'text',  # DOMAIN email_address over text -- the domain name is gone
            'c_percentage': 'numeric',  # DOMAIN percentage over numeric(5,2) -- name AND typmod
        }

    def test_the_integer_width_collapse_is_an_equality_not_a_coincidence(self, live_public_schema: Schema) -> None:
        smallint = _column(live_public_schema, 'type_menagerie', 'c_smallint')
        integer = _column(live_public_schema, 'type_menagerie', 'c_integer')
        bigint = _column(live_public_schema, 'type_menagerie', 'c_bigint')
        assert smallint.raw_type == integer.raw_type
        assert bigint.raw_type != integer.raw_type

    def test_max_length_survives_where_the_type_name_does_not(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'type_menagerie', 'c_varchar').max_length == 50
        assert _column(live_public_schema, 'type_menagerie', 'c_varchar_unbounded').max_length is None
        assert _column(live_public_schema, 'type_menagerie', 'c_char').max_length == 4
        assert _column(live_public_schema, 'customers', 'display_name').max_length == 120
        assert _column(live_public_schema, 'products', 'region').max_length == 2

    def test_array_element_types_come_from_the_format_token(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'type_menagerie', 'c_int_array').array_element_type == 'integer'
        assert _column(live_public_schema, 'type_menagerie', 'c_text_array').array_element_type == 'text'
        assert (
            _column(live_public_schema, 'type_menagerie', 'c_timestamptz_array').array_element_type
            == 'timestamp with time zone'
        )

    def test_a_domain_collapses_to_its_base_type_wherever_it_is_used(self, live_public_schema: Schema) -> None:
        # SEED-Q5 -- the CI-008 spec's designated STOP condition. It did not fire: `parse.py`'s
        # module docstring ("domain names are lost") is CORRECT. Checked on all three domain
        # columns in the seed, not just the one in type_menagerie (CI6-Q7).
        assert _column(live_public_schema, 'type_menagerie', 'c_email').raw_type == 'text'
        assert _column(live_public_schema, 'type_menagerie', 'c_percentage').raw_type == 'numeric'
        assert _column(live_public_schema, 'customers', 'email').raw_type == 'text'
        assert _column(live_public_schema, 'employees', 'work_email').raw_type == 'text'


class TestEnumCollision:
    """The CI5-Q7 regression, live: two same-named enums in two schemas, in one document."""

    def test_both_enums_are_registered_with_their_own_labels(self, live_public_schema: Schema) -> None:
        assert [(e.schema, e.name, e.values) for e in live_public_schema.enums] == [
            ('audit', 'status', ['ok', 'warn', 'error']),
            ('public', 'order_status', ['pending', 'paid', 'shipped', 'cancelled']),
            ('public', 'status', ['active', 'inactive', 'archived']),
        ]

    def test_each_column_links_to_its_own_enum(self, live_public_schema: Schema) -> None:
        # The bug this pins rejected a valid value and accepted an invalid one, silently, by
        # handing `audit.status`'s members to the `public.status` class.
        public_status = _column(live_public_schema, 'type_menagerie', 'c_status').enum_info
        audit_status = _column(live_public_schema, 'type_menagerie', 'c_audit_status').enum_info
        assert public_status is not None and audit_status is not None
        assert (public_status.schema, public_status.values) == ('public', ['active', 'inactive', 'archived'])
        assert (audit_status.schema, audit_status.values) == ('audit', ['ok', 'warn', 'error'])
        assert set(public_status.values).isdisjoint(audit_status.values)

    def test_every_enum_column_in_the_schema_links_to_the_right_type(self, live_public_schema: Schema) -> None:
        # "Every enum column" stated as an invariant, so every path is enumerated (CI6-Q7): scalar,
        # array, a second table, a view, and a matview-adjacent view column.
        linked = {
            (t.name, c.name): (c.enum_info.schema, c.enum_info.name)
            for t in live_public_schema.tables
            for c in t.columns
            if c.enum_info is not None
        }
        assert linked == {
            ('active_customers', 'signup_status'): ('public', 'status'),
            ('customers', 'signup_status'): ('public', 'status'),
            ('order_report', 'order_status'): ('public', 'order_status'),
            ('orders', 'order_status'): ('public', 'order_status'),
            ('orders', 'tags'): ('public', 'order_status'),
            ('people', 'favorite_status'): ('public', 'status'),
            ('type_menagerie', 'c_audit_status'): ('audit', 'status'),
            ('type_menagerie', 'c_status'): ('public', 'status'),
            ('type_menagerie', 'c_status_array'): ('public', 'status'),
        }

    def test_a_bare_format_token_on_a_function_argument_still_resolves(self, live_public_schema: Schema) -> None:
        # One type, two spellings, one document (CI-078): `public.status` on a column but a BARE
        # `order_status` on a function argument. The registry lookup has to handle both.
        parameter = next(p for p in _function(live_public_schema, 'create_order').parameters if p.name == 'p_status')
        assert parameter.raw_type == 'order_status'
        assert parameter.enum_info is not None
        assert (parameter.enum_info.schema, parameter.enum_info.values) == (
            'public',
            ['pending', 'paid', 'shipped', 'cancelled'],
        )


class TestEnumArrays:
    """An enum array carries no labels of its own — it links only through a scalar sibling."""

    def test_the_document_carries_no_enum_key_on_an_array_property(
        self, live_public_document: Mapping[str, Any]
    ) -> None:
        definitions = live_public_document['definitions']
        for definition, column in (('type_menagerie', 'c_status_array'), ('orders', 'tags')):
            prop = definitions[definition]['properties'][column]
            assert 'enum' not in prop
            assert prop['format'].endswith('[]')
        # ...while the scalar sibling does carry one. That is the only reason the link is possible.
        assert 'enum' in definitions['type_menagerie']['properties']['c_status']

    def test_an_array_column_still_links_via_its_scalar_sibling(self, live_public_schema: Schema) -> None:
        array_column = _column(live_public_schema, 'type_menagerie', 'c_status_array')
        assert array_column.array_element_type == 'public.status'
        assert array_column.enum_info is not None
        assert array_column.enum_info.values == ['active', 'inactive', 'archived']

        tags = _column(live_public_schema, 'orders', 'tags')
        assert tags.array_element_type == 'public.order_status'
        assert tags.enum_info is not None
        assert tags.enum_info.values == ['pending', 'paid', 'shipped', 'cancelled']


class TestKeysAndRelations:
    """Primary keys, foreign keys, and the relationship shapes derived from them."""

    def test_primary_key_membership_survives_but_key_order_does_not(self, live_public_schema: Schema) -> None:
        assert _table(live_public_schema, 'customers').primary_key() == ['id']
        assert _table(live_public_schema, 'orders').primary_key() == ['id']
        # `products` declares its columns (sku, region) and its key as PRIMARY KEY (region, sku).
        # The document reports document order only, so castiron says (sku, region) -- provably the
        # wrong order and unfixable from this source. Asserted as the fidelity floor, not a bug.
        assert _table(live_public_schema, 'products').primary_key() == ['sku', 'region']

    def test_a_composite_foreign_key_is_invisible(self, live_public_schema: Schema) -> None:
        order_lines = _table(live_public_schema, 'order_lines')
        # Declared: one single-column FK to orders, one COMPOSITE FK to products(sku, region).
        assert [(fk.column_name, fk.foreign_table_name) for fk in order_lines.foreign_keys] == [
            ('order_id', 'orders'),
        ]
        assert not _column(live_public_schema, 'order_lines', 'product_sku').is_foreign_key
        assert not _column(live_public_schema, 'order_lines', 'product_region').is_foreign_key

    def test_bridge_detection_diverges_from_the_database(self, live_public_schema: Schema) -> None:
        # `order_lines` IS a bridge in the catalog (3 PK columns, all of them FK columns), but only
        # one of its FKs is expressible here, so this source cannot see it. `customer_tags`, whose
        # FKs are both single-column, is detected correctly.
        assert _table(live_public_schema, 'order_lines').is_bridge is False
        assert _table(live_public_schema, 'customer_tags').is_bridge is True

    def test_a_self_referencing_foreign_key_survives(self, live_public_schema: Schema) -> None:
        employees = _table(live_public_schema, 'employees')
        outbound = [fk for fk in employees.foreign_keys if fk.column_name == 'manager_id']
        assert [(fk.foreign_table_name, fk.foreign_column_name) for fk in outbound] == [('employees', 'id')]

    def test_a_shared_primary_and_foreign_key_is_one_to_one(self, live_public_schema: Schema) -> None:
        profiles = _table(live_public_schema, 'employee_profiles')
        edge = next(fk for fk in profiles.foreign_keys if fk.column_name == 'employee_id')
        assert edge.relation_type is not None
        assert edge.relation_type.value == 'One-to-One'

    def test_a_cross_schema_foreign_key_is_dropped_not_misattributed(self, live_public_schema: Schema) -> None:
        # The document describes ONE schema and its <fk/> marker carries no schema, so a
        # cross-schema edge could plausibly have arrived mis-attributed to `public`. Measured: the
        # marker is not emitted at all, which is the favourable outcome -- nothing to un-guess.
        event_id = _column(live_public_schema, 'audit_links', 'event_id')
        assert event_id.is_foreign_key is False
        assert _table(live_public_schema, 'audit_links').foreign_keys == []

    def test_foreign_key_constraint_names_are_synthesized(self, live_public_schema: Schema) -> None:
        # The document carries no constraint name, so castiron synthesizes pg's own default
        # spelling. That happens to match here; it would NOT match a hand-named constraint --
        # `order_lines_order_fk` in this very seed is the counter-example. CI-090: castiron now
        # DECLARES the synthesis, so a consumer never has to guess which of the two it is holding.
        orders = _table(live_public_schema, 'orders')
        outbound = next(fk for fk in orders.foreign_keys if fk.column_name == 'customer_id')
        assert outbound.constraint_name == 'orders_customer_id_fkey'
        assert outbound.foreign_table_schema == 'public'
        assert outbound.name_is_synthesized is True


class TestConstraintFloor:
    """UNIQUE, CHECK and EXCLUDE do not exist anywhere in a PostgREST document."""

    def test_no_base_table_carries_a_constraint_other_than_pk_or_fk(self, live_public_schema: Schema) -> None:
        # The seed declares five UNIQUE constraints, three CHECKs, two domain CHECKs and one
        # EXCLUDE. Enumerating every base table (CI6-Q7) rather than spot-checking `customers` is
        # what makes "absent entirely" a real claim.
        found = {
            (t.name, c.constraint_name, c.type)
            for t in live_public_schema.tables
            if t.table_type == 'BASE TABLE'
            for c in t.constraints
            if c.type not in (ConstraintType.PRIMARY_KEY, ConstraintType.FOREIGN_KEY)
        }
        assert found == set()

    def test_the_only_unique_constraints_are_downgraded_view_keys(
        self, live_public_schema: Schema, live_public_document: Mapping[str, Any]
    ) -> None:
        """No UNIQUE reaches the IR except a view key castiron itself downgraded (CI5-D14a).

        ⚠ **This assertion was a hard-coded roster of two views, and it was wrong for six days.**
        It was written on 2026-08-02 against the behaviour of the day; ``CI-075`` landed on
        2026-08-03 and stopped views misclassifying as base tables, which *correctly* grew the set
        from two to five. Nothing caught it because nothing had ever run this suite. The
        expectation is therefore computed now -- from the document's own ``<pk/>`` markers and
        castiron's ``table_type`` -- so that the next correct classification change moves the
        expectation with the behaviour instead of staling it. ``CI-135``.

        What a derived expectation gives up, stated precisely because it was **measured** rather
        than reasoned about (by replaying the real pre-``CI-075`` output against all three tests):
        a *partial* classification regression is invisible here. Read three of the five views as
        base tables again and both sides shrink together, so this stays green -- and so does
        :meth:`TestViewKeyDowngrade.test_a_key_marker_becomes_a_pk_on_a_table_and_a_unique_on_a_view`.
        ``TestViewClassification.test_every_view_classifies_as_a_view`` goes red, which is why its
        enumeration of the seed's five views is deliberate and belongs there: that is the one claim
        a seed change *should* force someone to edit. (A *total* collapse -- no view classified as
        one at all -- is caught right here, by the emptiness guard below.)
        """
        marked = _pk_marked_columns(live_public_document)
        expected = {
            (t.name, f'{t.name}_{"_".join(marked[t.name])}_key', marked[t.name])
            for t in live_public_schema.tables
            if t.table_type == 'VIEW' and t.name in marked
        }
        assert expected, 'no VIEW carries a <pk/> marker, so this test would assert nothing'
        unique = {
            (t.name, c.constraint_name, tuple(c.columns))
            for t in live_public_schema.tables
            for c in t.constraints
            if c.type is ConstraintType.UNIQUE
        }
        assert unique == expected

    def test_a_length_check_never_reaches_the_column(self, live_public_schema: Schema) -> None:
        # `customers.bio` carries CHECK (length(bio) BETWEEN 3 AND 500). Through the live-DB source
        # that becomes Annotated[str, StringConstraints(...)]; here the field is a bare `str`.
        bio = _column(live_public_schema, 'customers', 'bio')
        assert bio.constraint_definition is None
        assert bio.raw_type == 'text'

    def test_a_unique_column_is_not_reported_as_unique(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'customers', 'email').is_unique is False
        assert _column(live_public_schema, 'tags', 'label').is_unique is False
        assert _column(live_public_schema, 'employees', 'work_email').is_unique is False


class TestDefaultsAndIdentity:
    """Which defaults survive PostgREST's ``JSON.decode``, and which identity signals do not."""

    def test_json_decodable_defaults_survive(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'customers', 'id').default == 'gen_random_uuid()'
        assert _column(live_public_schema, 'customers', 'id').has_default is True
        assert _column(live_public_schema, 'customers', 'created_at').default == 'now()'
        assert _column(live_public_schema, 'customers', 'lifetime_value').default == '0'
        assert _column(live_public_schema, 'customers', 'is_active').default == 'true'
        assert _column(live_public_schema, 'customers', 'signup_status').default == 'active'
        assert _column(live_public_schema, 'orders', 'order_status').default == 'pending'

    def test_a_serial_primary_key_loses_its_default_entirely(self, live_public_schema: Schema) -> None:
        # PostgREST feeds `nextval('orders_id_seq'::regclass)` to JSON.decode, which fails, so the
        # `default` key is silently omitted. The column arrives NOT NULL / no default / not
        # identity -- a REQUIRED field on the Insert model. This is the wedge's worst-looking
        # defect and the reason --infer-generated-primary-keys exists.
        column = _column(live_public_schema, 'orders', 'id')
        assert column.default is None
        assert column.has_default is False
        assert column.is_identity is False
        assert column.is_generated is False
        assert column.is_nullable is False

    def test_no_identity_or_generated_signal_survives(self, live_public_schema: Schema) -> None:
        # Four distinct pg mechanisms, none of which is encoded: BY DEFAULT identity, ALWAYS
        # identity, and two GENERATED ALWAYS AS (...) STORED columns. Enumerated per CI6-Q7.
        for table, column in (
            ('type_menagerie', 'id'),  # GENERATED BY DEFAULT AS IDENTITY
            ('tags', 'id'),  # GENERATED ALWAYS AS IDENTITY
            ('orders', 'total'),  # GENERATED ALWAYS AS (subtotal + tax) STORED
            ('tags', 'slug'),  # GENERATED ALWAYS AS (lower(label)) STORED
        ):
            found = _column(live_public_schema, table, column)
            assert found.is_identity is False, f'{table}.{column}'
            assert found.is_generated is False, f'{table}.{column}'
            assert found.default is None, f'{table}.{column}'

    def test_infer_generated_primary_keys_touches_only_sole_integer_keys(
        self, live_public_schema_inferred: Schema
    ) -> None:
        for table in ('orders', 'tags', 'type_menagerie'):
            inferred = _column(live_public_schema_inferred, table, 'id')
            assert inferred.is_identity is True, table
            assert inferred.is_generated is True, table
        # A composite key is left alone, and so is a uuid key that already has a default.
        assert _column(live_public_schema_inferred, 'products', 'sku').is_identity is False
        assert _column(live_public_schema_inferred, 'customers', 'id').is_identity is False


class TestViewClassification:
    """``table_type`` is inferred, and the real apparatus is what showed the old inference wrong.

    ⚠ The CI-008 spec's version of this class is **wrong** and must not be restored from it. The
    spec predicted that ``active_customers``/``ledger_summary`` classify correctly and that
    ``all_nullable_readonly`` misclassifies. Measured on PostgREST v14.14 (and reproduced on a
    pinned v12.2.3 by the testbed dispatch), the truth was the opposite on all three counts.

    **CI-075 is now fixed** and this class records the result: the verb signal is gone, all five
    views classify as VIEW, and the single residual (``all_nullable_readonly``) is the known,
    ruled, accepted cost of ``CI94-Q2``.
    """

    def test_path_verbs_track_auto_updatability_not_write_privileges(
        self, live_public_document: Mapping[str, Any]
    ) -> None:
        # The mechanism behind CI-075, asserted as a fact about PostgREST so it cannot be confused
        # with a castiron defect. Every relation below is granted SELECT only for `anon`; the ones
        # that are auto-updatable get write verbs anyway, and the ones that are not (a JOIN view, a
        # materialized view) do not.
        verbs = {path: sorted(item) for path, item in live_public_document['paths'].items()}
        assert verbs['/active_customers'] == ['delete', 'get', 'patch', 'post']
        assert verbs['/ledger_summary'] == ['delete', 'get', 'patch', 'post']
        assert verbs['/writable_customer_view'] == ['delete', 'get', 'patch', 'post']
        assert verbs['/rls_locked_notes'] == ['delete', 'get', 'patch', 'post']
        assert verbs['/all_nullable_readonly'] == ['delete', 'get', 'patch', 'post']
        assert verbs['/order_report'] == ['get']
        assert verbs['/mv_customer_spend'] == ['get']

    @pytest.mark.parametrize(
        'view_name',
        [
            # Not auto-updatable: a JOIN view and a materialized view. These classified correctly
            # even under the old verb heuristic, which is what made them its counter-witness.
            'order_report',
            'mv_customer_spend',
            # ⚠ AUTO-UPDATABLE, and therefore writable through the API. These three carried a
            # strict `xfail` naming CI-075 until CI-094: the verb signal read them as base tables.
            # The marker is deleted, not relaxed, and so is the reason constant it shared.
            'active_customers',
            'ledger_summary',
            'writable_customer_view',
        ],
    )
    def test_every_view_classifies_as_a_view(self, live_public_schema: Schema, view_name: str) -> None:
        assert _table(live_public_schema, view_name).table_type == 'VIEW'

    def test_a_read_only_all_nullable_base_table_is_read_as_a_view(self, live_public_schema: Schema) -> None:
        """⚠ The one KNOWN, RULED, ACCEPTED miss -- pinned so it stays a decision, not a surprise.

        ``all_nullable_readonly`` is a BASE TABLE with no NOT NULL column, which is genuinely
        indistinguishable from a view in this document: PostgREST emits write verbs for it (it is
        auto-updatable) and an empty ``required``. ``CI5-D6`` biased that cell toward BASE TABLE;
        ``CI94-Q2`` reversed the bias, because ``CI5-D6``'s justification -- "misreading a table as
        a view empties its primary key" -- is void here. A base table lands in this cell only if it
        has no NOT NULL column, and a Postgres PRIMARY KEY column *is* NOT NULL, so it has no
        primary key to empty. Asserted below rather than argued.

        The trade: 3 real views fixed for 1 inert miss, 23/26 -> 25/26 on this capture.
        """
        table = _table(live_public_schema, 'all_nullable_readonly')
        assert table.table_type == 'VIEW'
        assert table.primary_key() == [], 'the miss is only inert while there is no key to lose'
        assert all(c.primary is False for c in table.columns)

    def test_real_base_tables_stay_base_tables(self, live_public_schema: Schema) -> None:
        for name in ('customers', 'orders', 'products', 'rls_locked_notes', 'partially_visible'):
            assert _table(live_public_schema, name).table_type == 'BASE TABLE', name


class TestViewKeyDowngrade:
    """A view's ``<pk/>`` marker is retained as a UNIQUE constraint, not a primary key (CI5-D14a)."""

    def test_a_key_marker_becomes_a_pk_on_a_table_and_a_unique_on_a_view(
        self, live_public_schema: Schema, live_public_document: Mapping[str, Any]
    ) -> None:
        """The downgrade is one rule with two arms, checked over **every** marked relation.

        The tests below spot-check two views, which cannot see the regression that would cost the
        most on the other arm: a downgrade applied to base tables as well would silently empty
        twenty primary keys, and every one of those spot-checks would still pass. Derived from the
        document's markers (CI-135), so a relation added to the seed extends this check instead of
        staling it, and asserted as one whole table so "exactly one key constraint per marked
        relation, and none at all on an unmarked one" is part of the claim rather than an
        assumption -- ``all_nullable_readonly`` is the live instance of the second half.
        """
        marked = _pk_marked_columns(live_public_document)
        arms = {_table(live_public_schema, name).table_type for name in marked}
        assert arms == {'BASE TABLE', 'VIEW'}, f'both arms of the rule must be exercised; saw {sorted(arms)}'

        expected: dict[str, list[_KeyRow]] = {}
        for name, columns in marked.items():
            if _table(live_public_schema, name).table_type == 'VIEW':
                unique = f'{name}_{"_".join(columns)}_key'
                expected[name] = [(unique, ConstraintType.UNIQUE, columns, f'UNIQUE ({", ".join(columns)})')]
            else:
                expected[name] = [(f'{name}_pkey', ConstraintType.PRIMARY_KEY, columns, None)]

        actual: dict[str, list[_KeyRow]] = {}
        for table in live_public_schema.tables:
            for c in table.constraints:
                if c.type in (ConstraintType.PRIMARY_KEY, ConstraintType.UNIQUE):
                    actual.setdefault(table.name, []).append(
                        (c.constraint_name, c.type, tuple(c.columns), c.constraint_definition)
                    )
        assert actual == expected

        # The column flags have to agree with the constraint row, on both arms. This is CI5-D14a's
        # entire reason for the downgrade: `TableInfo.primary_key()` is *defined* to be empty for a
        # VIEW, so recording a PK row there would leave `ColumnInfo.primary` and `primary_key()`
        # contradicting each other, and different emitters read the two differently.
        for name, columns in marked.items():
            table = _table(live_public_schema, name)
            flags = (table.primary_key(), [c.name for c in table.columns if c.primary])
            unique_columns = [c.name for c in table.columns if c.is_unique]
            if table.table_type == 'VIEW':
                assert (flags, unique_columns) == (([], []), list(columns)), name
            else:
                assert (flags, unique_columns) == ((list(columns), list(columns)), []), name

    def test_a_joined_view_keeps_its_key_as_uniqueness_evidence(self, live_public_schema: Schema) -> None:
        report = _table(live_public_schema, 'order_report')
        assert report.table_type == 'VIEW'
        assert report.primary_key() == []
        assert all(c.primary is False for c in report.columns)
        assert _column(live_public_schema, 'order_report', 'order_id').is_unique is True

    def test_markers_propagate_through_a_materialized_view(self, live_public_schema: Schema) -> None:
        # SEED-Q3: the conservative assumption was that they do NOT propagate through a matview.
        # They do -- so more fidelity is available here than castiron was designed for.
        matview = _table(live_public_schema, 'mv_customer_spend')
        assert matview.table_type == 'VIEW'
        assert [(c.constraint_name, c.type, c.columns) for c in matview.constraints] == [
            ('mv_customer_spend_customer_id_key', ConstraintType.UNIQUE, ['customer_id']),
        ]

    def test_a_foreign_key_on_a_view_is_kept_unchanged(self, live_public_schema: Schema) -> None:
        report = _table(live_public_schema, 'order_report')
        assert [(fk.column_name, fk.foreign_table_name, fk.foreign_column_name) for fk in report.foreign_keys] == [
            ('customer_id', 'customers', 'id'),
        ]

    def test_a_view_is_never_a_foreign_key_target(self, live_public_document: Mapping[str, Any]) -> None:
        # SEED-F1, confirmed under the most favourable conditions the seed can build: the base
        # table `private_ledger` is privilege-filtered and the view `ledger_summary` over it is
        # visible, yet the marker still names the invisible TABLE. CI5-D14a's follow-up ("extend
        # the fixture so a view is an FK target") is therefore unclosable from this source, and
        # the committed unit fixture's `<fk table='active_users_view'/>` is a shape PostgREST
        # cannot emit (CI-076).
        #
        # Asserted on the MARKERS, not on TableInfo.foreign_keys: the IR also carries the reverse
        # edge of every relationship, so `order_report` legitimately appears as the
        # `foreign_table_name` of the inbound edge castiron adds to `customers`. The claim under
        # test is about what PostgREST writes into a description.
        markers = {
            (name, column, match.group(1))
            for name, definition in live_public_document['definitions'].items()
            for column, prop in definition.get('properties', {}).items()
            for match in _FK_MARKER.finditer(prop.get('description') or '')
        }
        assert markers == {
            ('customer_tags', 'customer_id', 'customers'),
            ('customer_tags', 'tag_id', 'tags'),
            ('employee_profiles', 'employee_id', 'employees'),
            ('employees', 'manager_id', 'employees'),
            ('ledger_refs', 'ledger_id', 'private_ledger'),  # the INVISIBLE base table, not the view
            ('order_lines', 'order_id', 'orders'),
            ('order_report', 'customer_id', 'customers'),
            ('orders', 'customer_id', 'customers'),
            ('person_notes', 'person_id', 'people'),
            ('series_entries', 'series_id', 'series'),
        }
        views = {'active_customers', 'ledger_summary', 'mv_customer_spend', 'order_report', 'writable_customer_view'}
        assert {target for _, _, target in markers}.isdisjoint(views)


class TestPrivilegeFloor:
    """What the API role cannot see — and, more importantly, what it can see but cannot read."""

    def test_an_ungranted_table_and_function_are_absent(self, live_public_schema: Schema) -> None:
        assert 'private_ledger' not in {t.name for t in live_public_schema.tables}
        assert 'secret_op' not in {f.name for f in live_public_schema.functions}

    def test_rls_without_a_policy_is_visible_but_returns_nothing(self, live_public_schema: Schema) -> None:
        # Privileges, not RLS, drive the document filter: `rls_locked_notes` has RLS enabled and NO
        # policy, so castiron generates a model whose every query returns zero rows. A common
        # real-world trap, and distinct from "invisible".
        assert _table(live_public_schema, 'rls_locked_notes').table_type == 'BASE TABLE'
        assert [c.name for c in _table(live_public_schema, 'rls_locked_notes').columns] == ['id', 'owner', 'content']

    def test_a_column_level_revoke_is_invisible_to_this_source(self, live_public_schema: Schema) -> None:
        # SEED-Q6 / CI-077, and the sign is the OPPOSITE of what the spec assumed. `anon` holds
        # SELECT on (id, title) only, yet `secret_body` is in `properties` -- and it is there under
        # `openapi-mode = ignore-privileges` too, so PostgREST is not filtering on column
        # privileges at all. castiron therefore emits a field that always errors at query time.
        # This is a fidelity-floor fact about the source, not a castiron defect.
        assert [c.name for c in _table(live_public_schema, 'partially_visible').columns] == [
            'id',
            'title',
            'secret_body',
        ]

    def test_a_dangling_fk_marker_leaves_the_column_unflagged(self, live_public_schema: Schema) -> None:
        # CI-084 (SEED-F2), RULED and fixed. The choice this test used to force has been made:
        # `is_foreign_key` is True **iff** a resolved forward ForeignKeyInfo names the column, so a
        # marker naming a table the role cannot see leaves the flag unset and the edge list empty.
        # The FOREIGN KEY constraint is retained (see below and `TestConstraintFloor`), which is
        # what keeps the evidence that the database really does have one.
        ledger_id = _column(live_public_schema, 'ledger_refs', 'ledger_id')
        assert ledger_id.is_foreign_key is False
        assert [fk.column_name for fk in _table(live_public_schema, 'ledger_refs').foreign_keys] == []
        definitions = [c.constraint_definition for c in _table(live_public_schema, 'ledger_refs').constraints]
        assert 'FOREIGN KEY (ledger_id) REFERENCES private_ledger(id)' in definitions

    def test_the_marker_names_the_invisible_table_in_the_raw_document(
        self, live_public_document: Mapping[str, Any]
    ) -> None:
        description = live_public_document['definitions']['ledger_refs']['properties']['ledger_id']['description']
        assert "<fk table='private_ledger' column='id'/>" in description
        assert 'private_ledger' not in live_public_document['definitions']


class TestFunctions:
    """``/rpc/*`` — what a path item can and cannot say about a database function.

    ⚠ The CI-008 spec's argument-order assertions are **wrong** and must not be restored. A
    VOLATILE function's POST body ``properties`` are **alphabetical**, not declaration order.
    """

    def test_only_the_executable_functions_are_present(self, live_public_schema: Schema) -> None:
        assert [f.name for f in live_public_schema.functions] == [
            'bump_counter',
            'create_order',
            'get_customer_stats',
            'list_statuses',
            'monthly_totals',
            'normalize_email',
            'ping',
            'reserved_args',
            'search_products',
            'split_name',
            'tally',
        ]

    def test_no_function_reports_a_return_type_or_set_flag(self, live_public_schema: Schema) -> None:
        # "Never encoded" is an every-function claim, so it is checked on every function (CI6-Q7),
        # including the three set-returning ones (list_statuses, monthly_totals, search_products).
        for function in live_public_schema.functions:
            assert function.return_type is None, function.name
            assert function.returns_set is None, function.name

    def test_volatility_is_a_binary_signal(self, live_public_schema: Schema) -> None:
        # A VOLATILE function gets POST only; STABLE and IMMUTABLE both get GET+POST and are
        # INDISTINGUISHABLE, which is exactly why FunctionInfo carries both `volatility` (None when
        # unknown) and `is_read_only` (knowable either way).
        assert {f.name: (f.volatility, f.is_read_only) for f in live_public_schema.functions} == {
            'bump_counter': (FunctionVolatility.VOLATILE, False),
            'create_order': (FunctionVolatility.VOLATILE, False),
            'ping': (FunctionVolatility.VOLATILE, False),
            'get_customer_stats': (None, True),
            'list_statuses': (None, True),
            'monthly_totals': (None, True),
            'normalize_email': (None, True),  # IMMUTABLE, reported identically to STABLE
            'reserved_args': (None, True),
            'search_products': (None, True),
            'split_name': (None, True),
            'tally': (None, True),
        }

    def test_every_parameter_list_is_the_measured_one(self, live_public_schema: Schema) -> None:
        # Enumerated (CI-072). Three separate losses are visible in this one comparison:
        # OUT parameters are excluded, INOUT ones are included, TABLE columns are invisible, and
        # the ORDER is alphabetical rather than declaration order (see the two tests below).
        #
        # ⚠ **This enumeration pins a DEFECT, and `CI-078` is the row that fixes it.** Every list
        # below is alphabetical, which for a STABLE/IMMUTABLE function is recoverable declaration
        # order that castiron discards -- `search_products` is declared `(p_terms, p_limit)` and
        # appears here as `['p_limit', 'p_terms']`. When `CI-078` lands, the STABLE entries here
        # move and this test goes red **by design**: update it to declaration order, do not
        # re-sort the fixture. `test_a_stable_functions_argument_order_is_present_but_not_used`
        # below is the companion that proves the order is available in the document today.
        assert {f.name: [p.name for p in f.parameters] for f in live_public_schema.functions} == {
            'bump_counter': ['p_value'],  # INOUT -- included
            'create_order': ['p_customer_id', 'p_lines', 'p_status'],
            'get_customer_stats': ['p_customer_id', 'p_since'],
            'list_statuses': [],
            'monthly_totals': ['p_year'],  # RETURNS TABLE(...) columns are invisible
            'normalize_email': ['p_email'],
            'ping': [],
            'reserved_args': ['class', 'def'],
            'search_products': ['p_limit', 'p_terms'],
            'split_name': ['p_full'],  # OUT -- excluded
            'tally': ['p_values'],
        }

    def test_a_volatile_functions_argument_order_is_unrecoverable(self, live_public_schema: Schema) -> None:
        # `create_order(p_customer_id, p_status, p_lines)` is VOLATILE, so PostgREST emits no GET
        # operation and the only argument list in the document is the POST body's `properties` --
        # which is ALPHABETICAL. Declaration order is simply not present anywhere. A generated
        # positional RPC call (CI-012) would therefore be silently wrong; CI-078 tracks it.
        assert [p.name for p in _function(live_public_schema, 'create_order').parameters] == [
            'p_customer_id',
            'p_lines',
            'p_status',
        ]

    def test_a_stable_functions_argument_order_is_present_but_not_used(
        self, live_public_document: Mapping[str, Any]
    ) -> None:
        # A finding this run added to CI-078: for a STABLE/IMMUTABLE function the declaration order
        # IS recoverable -- the GET operation's `parameters` array preserves it -- but castiron
        # builds its parameter list from the alphabetical POST body and drops it. castiron already
        # reads that GET operation (it is where VARIADIC comes from), so the fact is available at
        # the point it is currently discarded.
        item = live_public_document['paths']['/rpc/search_products']
        get_parameters = [p['name'] for p in item['get']['parameters']]
        body = next(p for p in item['post']['parameters'] if 'schema' in p)
        post_properties = list(body['schema']['properties'])
        assert get_parameters == ['p_terms', 'p_limit']  # declaration order, available
        assert post_properties == ['p_limit', 'p_terms']  # alphabetical, and what castiron uses

    def test_has_default_is_recoverable_from_the_required_list(self, live_public_schema: Schema) -> None:
        stats = {p.name: p.has_default for p in _function(live_public_schema, 'get_customer_stats').parameters}
        assert stats == {'p_customer_id': False, 'p_since': True}
        search = {p.name: p.has_default for p in _function(live_public_schema, 'search_products').parameters}
        assert search == {'p_terms': False, 'p_limit': True}

    def test_every_parameter_mode_is_the_measured_one(self, live_public_schema: Schema) -> None:
        # Enumerated over every parameter in the schema, not summarized as a set of the modes
        # present: a set comparison passes just as happily when the wrong parameter is the VARIADIC
        # one. `tally`'s VARIADIC is recovered from the GET operation's `collectionFormat: multi` —
        # the one place PostgREST encodes it, and unavailable for a VOLATILE function.
        assert {(f.name, p.name): p.mode for f in live_public_schema.functions for p in f.parameters} == {
            ('bump_counter', 'p_value'): ParameterMode.IN,
            ('create_order', 'p_customer_id'): ParameterMode.IN,
            ('create_order', 'p_lines'): ParameterMode.IN,
            ('create_order', 'p_status'): ParameterMode.IN,
            ('get_customer_stats', 'p_customer_id'): ParameterMode.IN,
            ('get_customer_stats', 'p_since'): ParameterMode.IN,
            ('monthly_totals', 'p_year'): ParameterMode.IN,
            ('normalize_email', 'p_email'): ParameterMode.IN,
            ('reserved_args', 'class'): ParameterMode.IN,
            ('reserved_args', 'def'): ParameterMode.IN,
            ('search_products', 'p_limit'): ParameterMode.IN,
            ('search_products', 'p_terms'): ParameterMode.IN,
            ('split_name', 'p_full'): ParameterMode.IN,
            ('tally', 'p_values'): ParameterMode.VARIADIC,
        }
        assert _function(live_public_schema, 'tally').parameters[0].raw_type == 'integer[]'

    def test_a_reserved_word_parameter_is_not_renamed(self, live_public_schema: Schema) -> None:
        # Columns get `field_class` + alias='class'; parameters get neither, because no emitter
        # renders a function signature yet. A forward constraint on CI-012, pinned so the gap is
        # discovered by this suite rather than by a user of the generated client.
        assert [p.name for p in _function(live_public_schema, 'reserved_args').parameters] == ['class', 'def']


class TestComments:
    """SQL comments, and the ``Note:`` marker block castiron has to split off them."""

    def test_a_column_comment_survives_with_its_marker_block_stripped(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'customers', 'id').description == 'Stable public identifier.'
        assert _column(live_public_schema, 'customers', 'bio').description == 'A short profile blurb.'
        assert _column(live_public_schema, 'orders', 'customer_id').description == 'The customer who placed the order.'

    def test_the_raw_document_really_did_carry_the_marker(self, live_public_document: Mapping[str, Any]) -> None:
        # The anchoring half of the test above: without this, "the marker was stripped" would also
        # pass on a document that never had one.
        raw = live_public_document['definitions']['customers']['properties']['id']['description']
        assert raw == 'Stable public identifier.\n\nNote:\nThis is a Primary Key.<pk/>'

    def test_an_embedded_note_in_the_user_text_is_not_eaten(self, live_public_schema: Schema) -> None:
        # The marker block is anchored to the END of the description, so a comment that merely
        # CONTAINS the word "Note:" keeps it. A greedy strip would silently truncate user prose.
        assert _column(live_public_schema, 'orders', 'tax').description == 'Sales tax.\n\nNote: computed upstream.'

    def test_punctuation_that_matters_to_a_docstring_survives(self, live_public_schema: Schema) -> None:
        assert _column(live_public_schema, 'products', 'sku').description == 'Vendor `SKU` (<= 32 chars).'

    def test_no_description_anywhere_retains_a_marker(self, live_public_schema: Schema) -> None:
        # "Every description" stated as an invariant, so every description is checked (CI6-Q7) --
        # including the ones on views and the matview, where the markers also propagate.
        leaked = [
            (t.name, c.name, c.description)
            for t in live_public_schema.tables
            for c in t.columns
            if c.description and ('<pk' in c.description or '<fk' in c.description)
        ]
        assert leaked == []

    def test_a_table_comment_is_present_in_the_document(self, live_public_document: Mapping[str, Any]) -> None:
        # Asserted at the DOCUMENT level on purpose. CI-009 is adding `TableInfo.description` right
        # now; an assertion that the IR drops the comment would go red the hour that lands, while
        # this one -- the fact that makes CI-009 possible at all -- stays true either way.
        definitions = live_public_document['definitions']
        assert definitions['customers']['description'] == 'People who can place orders.'
        assert definitions['orders']['description'] == 'One row per placed order.'
        assert definitions['type_menagerie']['description'] == 'One column per interesting Postgres type.'


class TestNaming:
    """Identifier hygiene on names that are legal Python but awkward."""

    def test_reserved_and_protected_names_are_renamed_with_an_alias(self, live_public_schema: Schema) -> None:
        assert [(c.name, c.alias) for c in _table(live_public_schema, 'awkward_names').columns] == [
            ('id', None),  # curated exception -- NOT renamed
            ('field_class', 'class'),
            ('field_import', 'import'),
            ('field_lambda', 'lambda'),
            ('field_type', 'type'),
            ('select', None),  # SQL reserved, legal Python -- untouched
            ('order', None),
            ('MixedCase', None),  # case is preserved through the whole pipeline
            ('field_model_name', 'model_name'),
            ('field_model_config', 'model_config'),  # collides with a REAL Pydantic attribute
            ('sum', None),  # curated exception
            ('copyright', None),  # curated exception
        ]

    def test_the_renamed_columns_are_exactly_the_aliased_ones(self, live_public_schema: Schema) -> None:
        table = _table(live_public_schema, 'awkward_names')
        assert {c.name for c in table.columns if c.alias} == {
            'field_class',
            'field_import',
            'field_lambda',
            'field_type',
            'field_model_name',
            'field_model_config',
        }
        assert table.aliasing_in_columns() is True


class TestEmitterEndToEnd:
    """The Phase-0 exit criterion: a real schema in, byte-stable importable Python out."""

    def test_emission_is_byte_stable_for_one_schema_object(self, live_public_schema: Schema) -> None:
        assert _emit(live_public_schema) == _emit(live_public_schema)

    def test_emission_is_byte_stable_across_two_independent_fetches(
        self, live_public_schema: Schema, live_document_refetch: DocumentLoader
    ) -> None:
        # A second real request, a second parse, a second build -- the end-to-end form of Hard Rule
        # #9. Re-emitting from one cached dict would only prove the emitter is a pure function.
        refetched = build_schema_from_document(live_document_refetch('public'), schema='public')
        assert _emit(refetched) == _emit(live_public_schema)

    def test_a_generated_module_executes_and_its_models_instantiate(self, live_public_schema: Schema) -> None:
        # exec() is the point: "the generated module imports and its models instantiate" is the
        # Phase-0 exit criterion, and nothing short of executing the text proves it.
        #
        # ⚠ Runs against `public` (26 tables, 119 classes), NOT `inventory` (2 tables). It used to
        # use `inventory` only because `public` did not compile when this was written -- CI-009
        # fixed that, so the exit criterion is no longer demonstrated on a toy schema (CI-089).
        # The two shapes that make `public` a real exercise are asserted in the two tests below.
        with _executed(live_public_schema) as module:
            # Insert model: only the genuinely required columns, everything defaulted omitted.
            customer = module.CustomersInsert(email='ada@example.com', display_name='Ada')
            assert (customer.email, customer.display_name) == ('ada@example.com', 'Ada')
            assert customer.signup_status is None  # a DB default -> optional on Insert
            # Row model: every NOT NULL column, and the enum column coerces to the enum class
            # rather than staying a bare string.
            row = module.Customers(
                id='0e5b1a3e-1f4d-4a1a-9a3e-2b7c8d9e0f11',
                email='ada@example.com',
                display_name='Ada',
                signup_status='active',
                lifetime_value=0,
                is_active=True,
                created_at='2026-01-01T00:00:00Z',
            )
            assert row.signup_status is module.PublicStatusEnum.ACTIVE

    def test_a_column_that_shadows_a_basemodel_attribute_is_renamed_and_aliased(
        self, live_public_schema: Schema
    ) -> None:
        # Hazard 1 of the exec proof, asserted rather than merely survived (CI6-Q7): a field
        # literally named `model_config` raises PydanticUserError when the class body RUNS -- it
        # compiles clean. `awkward_names` carries `model_config` and `model_name`, so this is the
        # shape that makes `test_..._executes_and_its_models_instantiate` above a real exercise. A
        # regression that dropped the rename would fail there with no explanation; it fails here
        # with one. The alias is what keeps the wire format the real column name.
        with _executed(live_public_schema) as module:
            fields = module.AwkwardNamesInsert.model_fields
            assert {name: fields[name].alias for name in ('field_model_config', 'field_model_name')} == {
                'field_model_config': 'model_config',
                'field_model_name': 'model_name',
            }
            populated = module.AwkwardNamesInsert.model_validate({'id': 1, 'model_config': 'x', 'model_name': 'y'})
            assert (populated.field_model_config, populated.field_model_name) == ('x', 'y')

    def test_the_two_same_named_enums_emit_two_distinct_classes(self, live_public_schema: Schema) -> None:
        # Hazard 2, and the end-to-end form of TestEnumCollision above: `public.status` and
        # `audit.status` share a bare name in one document. If they collapsed to one class, or if a
        # column's annotation named a class that was never emitted, the module would still COMPILE
        # and fail (or silently mistype a column) at exec. This is CI-005's cross-schema
        # `_match_enum` fix holding against the real apparatus -- the bug that once handed
        # `audit.status`'s labels to `public.status`, rejecting a valid value and accepting an
        # invalid one.
        with _executed(live_public_schema) as module:
            assert module.PublicStatusEnum is not module.AuditStatusEnum
            assert [member.value for member in module.PublicStatusEnum] == ['active', 'inactive', 'archived']
            assert [member.value for member in module.AuditStatusEnum] == ['ok', 'warn', 'error']
            # ...and each column's annotation resolves to its OWN class, which is the assertion a
            # bare exec cannot make.
            annotations = module.TypeMenagerie.model_fields
            assert annotations['c_status'].annotation == (module.PublicStatusEnum | None)
            assert annotations['c_audit_status'].annotation == (module.AuditStatusEnum | None)

    def test_the_public_schema_emits_a_module_that_parses(self, live_public_schema: Schema) -> None:
        # Carried `xfail(strict=False)` for CI9-Q1 until CI-089: `_py_string()` did not escape
        # newlines, so a multi-line COMMENT ON COLUMN (orders.tax) emitted an unterminated string
        # literal. CI-009 fixed it and the marker went on XPASSing silently -- `strict=False`
        # announces nothing, ever. Deleted as its own reason line instructed.
        #
        # Note this proves the module PARSES, not that it RUNS: `compile()` does not execute class
        # bodies, so a pydantic field clashing with a BaseModel attribute (PydanticUserError) or an
        # annotation naming an un-emitted enum (NameError) both compile clean and fail at exec.
        # The exec-and-instantiate proof is the separate test above.
        #
        # CI-007 makes the same "it parses" claim OFFLINE, from the committed `testbed-public`
        # capture, so it is no longer only assertable where the apparatus is running.
        compile(_emit(live_public_schema), 'schema.py', 'exec')


class TestEdgeQuarantine:
    """The ``edge`` schema — shapes whose document form is NOT contractual. Never a golden input."""

    def test_overloads_collapse_to_exactly_one_function(self, live_edge_schema: Schema) -> None:
        # PostgREST maps every overload of `f` to the single path key /rpc/f and keeps an arbitrary
        # one. WHICH one survives is deterministic for a fixed build but not contractual, so this
        # asserts the count and the name -- never the surviving signature.
        assert [f.name for f in live_edge_schema.functions] == ['overloaded']

    def test_a_column_in_two_foreign_keys_reports_at_most_one(self, live_edge_schema: Schema) -> None:
        child = _table(live_edge_schema, 'dual_fk_child')
        edges = [fk for fk in child.foreign_keys if fk.column_name == 'ref_id']
        assert len(edges) <= 1
        # Same rule: assert that ONE was chosen, not which. `parent_a` and `parent_b` are
        # interchangeable and a PostgREST upgrade may swap them.
        assert edges[0].foreign_table_name in {'parent_a', 'parent_b'}
        assert _column(live_edge_schema, 'dual_fk_child', 'ref_id').is_foreign_key is True

    def test_hostile_identifiers_are_repaired_and_the_wire_name_is_aliased(self, live_edge_schema: Schema) -> None:
        # ⚠ Renamed from `..._reach_the_ir_verbatim`: "verbatim" is no longer what this
        # demonstrates. CI-085 repairs the identifier and puts the wire name on `alias`, so the
        # thing the live apparatus now proves is that the repair reaches the REAL capture and not
        # just the hand-authored synthetic input.
        columns = _table(live_edge_schema, 'identifier_torture').columns
        assert [c.name for c in columns] == [
            'id',
            'column_with_spaces',
            'field_2fast',
            'Ünïcödé',
            'trailing_underscore_',
        ]
        # The wire name is preserved on exactly the two columns that were renamed, and on no
        # others -- `Ünïcödé` and `trailing_underscore_` are already legal Python and must not
        # acquire an alias (CI94-D2 keeps Unicode; only LEADING underscores are a hazard).
        assert [c.alias for c in columns] == [None, 'column with spaces', '2fast', None, None]

    def test_the_edge_schema_emits_a_module_that_imports(self, live_edge_schema: Schema) -> None:
        # ⚠ Was `@pytest.mark.xfail(strict=True, reason='CI-085: ...')`. CI-085 landed, and a
        # strict xfail FAILS when it passes, so the marker had to go rather than be relaxed.
        #
        # Strengthened from `compile()` to `exec()` while removing it, because `compile()` is not
        # a sufficient oracle for this defect class: a field named `_private` compiles cleanly and
        # raises `NameError: Fields must not use names with leading underscores` only when pydantic
        # builds the class. `tests/unit/ir/test_column_identifiers.py` is the offline version of
        # this assertion; this is the one that runs it against a real PostgREST capture.
        module = _emit(live_edge_schema)
        exec(compile(module, 'schema.py', 'exec'), {})  # noqa: S102 -- executing IS the assertion


class TestCommittedCaptureIsStillFaithful:
    """Does the corpus's committed capture still match the apparatus it was taken from?

    This is the standing check that keeps a committed capture from silently going stale. The
    corpus (``tests/unit/corpus/``) is a set of goldens derived from two documents captured at
    testbed revision ``3150132`` / PostgREST ``14.14``. Those goldens are only *falsifiable*
    while the capture still represents something real — SEED-D2's whole premise is that an
    unreproducible seed makes every golden derived from it unfalsifiable, because you can no
    longer tell "castiron changed" from "the schema changed".

    ⚠ **It cannot be part of the static gate** — it needs the live apparatus, so it is
    ``integration``-marked and skips when ``CASTIRON_TEST_POSTGREST_URL`` is unset. No corpus
    golden is integration-gated; every one of them runs offline.

    **Compared as decoded objects with key order preserved, not as raw bytes.** ``capture.sh``
    pretty-prints with ``indent=2`` while the live fetch does not, so a byte comparison would
    fail on formatting alone. Key *order* is still compared, because the testbed README is
    explicit that ``properties`` order is real information (pg ``attnum``, function argument
    order) and castiron depends on it.
    """

    @pytest.mark.parametrize('family_id', ['testbed-public', 'testbed-inventory'])
    def test_the_committed_capture_matches_the_live_document(
        self, family_id: str, live_document: DocumentLoader
    ) -> None:
        import json

        from tests.unit.corpus.cases import FAMILIES

        family = next(f for f in FAMILIES if f.family_id == family_id)
        committed = json.loads(family.input_path.read_text(encoding='utf-8'))
        live = live_document(family.schema)

        if json.dumps(committed, sort_keys=False) == json.dumps(live, sort_keys=False):
            return
        pytest.fail(_classify_capture_drift(committed, dict(live), family_id), pytrace=False)


def _classify_capture_drift(committed: Mapping[str, Any], live: Mapping[str, Any], family_id: str) -> str:
    """Say WHICH kind of drift this is, so the reader is not left diffing 4 000 lines.

    Three causes need three different responses, and only one of them is a castiron concern:
    a PostgREST upgrade, a seed change, or a content change under an unchanged key set.

    Args:
        committed: The committed capture.
        live: The freshly fetched document.
        family_id: The corpus input family, for the message.

    Returns:
        The classified failure message.
    """
    import json

    header = [
        f'The committed capture {family_id!r} no longer matches the live apparatus.',
        '',
        'Every golden in tests/unit/corpus/ derived from this document is now measuring a schema',
        'that no longer exists. Classify the cause before regenerating anything:',
        '',
    ]

    committed_version = committed.get('info', {}).get('version')
    live_version = live.get('info', {}).get('version')
    if committed_version != live_version:
        header += [
            f'  CAUSE: THE POSTGREST VERSION CHANGED -- {committed_version!r} -> {live_version!r}.',
            '  The apparatus was rebuilt on a different PostgREST. Re-capture, and update BOTH the',
            "  provenance record's postgrest_version and the seed_revision, then regenerate.",
        ]
        return '\n'.join(header)

    for section in ('definitions', 'paths'):
        before, after = set(committed.get(section, {})), set(live.get(section, {}))
        if before != after:
            header += [
                f'  CAUSE: THE SEED SCHEMA CHANGED -- the {section!r} key set differs.',
                f'    added:   {sorted(after - before)}',
                f'    removed: {sorted(before - after)}',
                '  This is a castiron-testbed change, not a castiron change. Re-capture, bump the',
                '  provenance seed_revision, and expect every golden from this input to move.',
            ]
            return '\n'.join(header)

    for section in ('definitions', 'paths'):
        for key in committed.get(section, {}):
            if json.dumps(committed[section][key], sort_keys=False) != json.dumps(live[section][key], sort_keys=False):
                header += [
                    f'  CAUSE: CONTENT CHANGED under an unchanged key set -- first at /{section}/{key}.',
                    '  Same objects, different details (a column, a comment, a marker, or their ORDER --',
                    '  properties order is pg attnum and IS compared). Diff that one object first.',
                ]
                return '\n'.join(header)

    header += ['  CAUSE: the documents differ outside definitions/paths (envelope, host or info).']
    return '\n'.join(header)
