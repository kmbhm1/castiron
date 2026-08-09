"""Unit tests for the pure PostgREST OpenAPI parser.

Every test here loads (or builds) a plain ``dict`` -- **no test opens a socket and no test
mocks HTTP**, which is the whole point of the fetch/parse split.
"""

import copy
import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from castiron.ir import ParameterOrder, Row
from castiron.sources import parse_openapi_document
from castiron.sources.errors import SourceParseError
from castiron.sources.openapi.parse import (
    OPENAPI_FORMAT_ALIASES,
    ColumnMarkers,
    classify_table_type,
    normalize_format,
    parse_column_description,
    stringify_default,
)

# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

#: The committed corpus documents (real PostgREST captures + the synthetic torture input).
CORPUS_INPUTS = Path(__file__).parents[3] / 'unit' / 'corpus' / 'inputs'

#: CI-005's hand-authored fixture, referenced rather than copied.
FIXTURE_DOCUMENT = Path(__file__).parent / 'fixtures' / 'postgrest_openapi.json'

#: The write verbs PostgREST can expose. ``_all_verb_subsets`` yields the 7 non-empty ones; the
#: parametrization adds the empty (GET-only) case, for **8** in total.
WRITE_VERBS = ('post', 'patch', 'delete')


def _all_verb_subsets() -> list[tuple[str, ...]]:
    """Every non-empty subset of ``{post, patch, delete}``, enumerated (``CI-072``)."""
    return [
        combination
        for size in range(1, len(WRITE_VERBS) + 1)
        for combination in itertools.combinations(WRITE_VERBS, size)
    ]


def column(rows: Any, table: str, name: str) -> Row:
    """Return the 12-tuple column row for ``table.name``."""
    return next(row for row in rows.column_details if row[1] == table and row[2] == name)


def function(rows: Any, name: str) -> Row:
    """Return the 8-tuple function row for ``name``."""
    return next(row for row in rows.function_details if row[1] == name)


def minimal_document(properties: dict[str, Any], **definition: Any) -> dict[str, Any]:
    """Build the smallest valid document holding one table with ``properties``."""
    return {
        'swagger': '2.0',
        'definitions': {'t': {'type': 'object', 'properties': properties, **definition}},
        'paths': {'/t': {'get': {}, 'post': {}}},
    }


def reorder_keys(document: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy whose ``definitions`` and ``paths`` keys are reversed."""
    copied = copy.deepcopy(document)
    copied['definitions'] = dict(reversed(list(copied['definitions'].items())))
    copied['paths'] = dict(reversed(list(copied['paths'].items())))
    return copied


# ---------------------------------------------------------------------------
# Small pure helpers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHelpers:
    def test_normalize_format_translates_only_swagger_tokens(self) -> None:
        assert normalize_format('int32') == 'integer'
        assert normalize_format('int64') == 'bigint'
        # Everything else is already the raw pg type name and passes through untouched.
        for token in ('text', 'character varying', 'character', 'numeric', 'jsonb', 'order_status'):
            assert normalize_format(token) == token

    def test_alias_table_is_exactly_two_entries(self) -> None:
        # CI5-D5: the source normalizes to the pg vocabulary; there is no second type map.
        assert OPENAPI_FORMAT_ALIASES == {'int32': 'integer', 'int64': 'bigint'}

    def test_stringify_default_passes_strings_through_and_json_dumps_the_rest(self) -> None:
        assert stringify_default('now()') == 'now()'
        assert stringify_default('') == ''
        assert stringify_default(True) == 'true'
        assert stringify_default(False) == 'false'
        assert stringify_default(42) == '42'
        assert stringify_default(42.2) == '42.2'
        assert stringify_default(None) == 'null'
        assert stringify_default([1, 2]) == '[1, 2]'


# ---------------------------------------------------------------------------
# Envelope guards.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnvelope:
    def test_openapi3_document_is_refused_by_version(self, openapi3_document: dict[str, Any]) -> None:
        with pytest.raises(SourceParseError) as excinfo:
            parse_openapi_document(openapi3_document)
        assert 'Swagger 2.0' in str(excinfo.value)
        assert '3.0.0' in str(excinfo.value)

    def test_missing_definitions_is_refused(self) -> None:
        with pytest.raises(SourceParseError, match='definitions'):
            parse_openapi_document({'swagger': '2.0', 'paths': {}})

    def test_definitions_that_is_not_an_object_is_refused(self) -> None:
        with pytest.raises(SourceParseError, match='definitions'):
            parse_openapi_document({'swagger': '2.0', 'definitions': [], 'paths': {}})

    def test_empty_definitions_raises_and_names_the_schema(self, empty_definitions_document: dict[str, Any]) -> None:
        # CI5-D10: an anon key + RLS legitimately yields an empty document; emitting an
        # empty models file would be silently wrong, so castiron fails loudly instead.
        with pytest.raises(SourceParseError) as excinfo:
            parse_openapi_document(empty_definitions_document, schema='public')
        message = str(excinfo.value)
        assert "'public'" in message
        assert 'privileges' in message

    def test_definitions_present_but_all_unusable_raises(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'not_an_object': 'nope', 'no_properties': {'type': 'object'}, 'empty': {'properties': {}}},
            'paths': {},
        }
        with pytest.raises(SourceParseError, match='no readable columns'):
            parse_openapi_document(document)


# ---------------------------------------------------------------------------
# Columns -- the 12-tuple, position by position.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestColumns:
    def test_every_tuple_position_for_a_plain_column(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'bio') == (
            'public',  # 1 schema
            'users',  # 2 table_name
            'bio',  # 3 column_name
            None,  # 4 default
            'YES',  # 5 is_nullable
            'text',  # 6 data_type
            None,  # 7 max_length
            'BASE TABLE',  # 8 table_type
            None,  # 9 identity_generation
            None,  # 10 udt_name
            None,  # 11 array_element_type
            'A short profile blurb.',  # 12 description
        )

    def test_smallint_and_integer_both_arrive_as_int32(self) -> None:
        # The fidelity floor: PostgREST's toSwaggerFormat collapses them, so castiron
        # cannot tell them apart and records the wider of the two.
        rows = parse_openapi_document(minimal_document({'a': {'format': 'int32', 'type': 'integer'}}))
        assert column(rows, 't', 'a')[5] == 'integer'

    def test_int64_survives_as_bigint(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'orders', 'id')[5] == 'bigint'
        assert column(rows, 'users', 'id')[5] == 'integer'

    def test_character_and_character_varying_survive_with_max_length(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'orders', 'code')[5] == 'character'
        assert column(rows, 'orders', 'code')[6] == 1
        assert column(rows, 'users', 'email')[5] == 'character varying'
        assert column(rows, 'users', 'email')[6] == 255

    def test_jsonb_without_a_type_key_still_resolves(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'metadata')[5] == 'jsonb'

    def test_array_column_records_its_element_type(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'tags')[5] == 'text[]'
        assert column(rows, 'users', 'tags')[10] == 'text'

    def test_defaults_are_stringified_by_json_type(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'created_at')[3] == 'now()'
        assert column(rows, 'users', 'status')[3] == 'pending'
        assert column(rows, 'users', 'login_count')[3] == '0'
        assert column(rows, 'users', 'is_active')[3] == 'false'
        # PostgREST silently drops nextval(...) defaults, so a surrogate PK has none.
        assert column(rows, 'users', 'id')[3] is None

    def test_is_nullable_comes_from_the_required_array(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'id')[4] == 'NO'
        assert column(rows, 'users', 'email')[4] == 'NO'
        assert column(rows, 'users', 'created_at')[4] == 'YES'

    def test_a_view_has_no_required_array_so_every_column_is_nullable(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        view_rows = [row for row in rows.column_details if row[1] == 'active_users_view']
        assert view_rows
        assert all(row[4] == 'YES' for row in view_rows)

    def test_identity_and_udt_name_are_never_available(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert all(row[8] is None for row in rows.column_details)
        assert all(row[9] is None for row in rows.column_details)

    def test_a_non_list_required_key_is_ignored(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'text'}}, required='id'))
        assert column(rows, 't', 'a')[4] == 'YES'

    def test_a_non_integer_max_length_is_dropped(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'text', 'maxLength': 'ten'}}))
        assert column(rows, 't', 'a')[6] is None

    def test_a_boolean_max_length_is_not_mistaken_for_an_int(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'text', 'maxLength': True}}))
        assert column(rows, 't', 'a')[6] is None

    def test_a_property_that_is_not_an_object_is_skipped(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'text'}, 'b': 'nope'}))
        assert [row[2] for row in rows.column_details] == ['a']

    def test_type_only_properties_fall_back_to_a_pg_token(self) -> None:
        rows = parse_openapi_document(
            minimal_document(
                {
                    'a': {'type': 'string'},
                    'b': {'type': 'integer'},
                    'c': {'type': 'number'},
                    'd': {'type': 'boolean'},
                    'e': {'type': 'array'},
                    'f': {'type': 'object'},
                }
            )
        )
        assert [row[5] for row in rows.column_details] == ['text', 'integer', 'numeric', 'boolean', 'array', 'jsonb']
        # An array with no ``format`` has a genuinely unknown element type.
        assert column(rows, 't', 'e')[10] is None

    def test_a_property_with_neither_format_nor_type_names_the_column(self) -> None:
        with pytest.raises(SourceParseError, match=r't\.a'):
            parse_openapi_document(minimal_document({'a': {'description': 'mystery'}}))

    def test_an_unrecognized_swagger_type_is_also_an_error(self) -> None:
        with pytest.raises(SourceParseError, match=r't\.a'):
            parse_openapi_document(minimal_document({'a': {'type': 'file'}}))

    def test_an_unrecognized_type_token_never_fails(self) -> None:
        # CI5-D10: unknown tokens are recorded verbatim; the emitter's resolver falls back
        # to ``Any`` rather than castiron refusing the schema.
        rows = parse_openapi_document(minimal_document({'a': {'format': 'my_custom_domain', 'type': 'string'}}))
        assert column(rows, 't', 'a')[5] == 'my_custom_domain'

    def test_the_caller_supplies_the_schema_name(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document, schema='api')
        assert all(row[0] == 'api' for row in rows.column_details)
        assert all(row[0] == 'api' and row[3] == 'api' for row in rows.fk_details)
        assert all(row[0] == 'api' for row in rows.function_details)


# ---------------------------------------------------------------------------
# Description markers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestMarkers:
    def test_no_description_yields_empty_markers(self) -> None:
        assert parse_column_description(None) == ColumnMarkers()

    def test_a_plain_comment_is_preserved_whole(self) -> None:
        assert parse_column_description('Just a comment.') == ColumnMarkers(comment='Just a comment.')

    def test_a_note_block_only_leaves_no_comment(self) -> None:
        markers = parse_column_description('Note:\nThis is a Primary Key.<pk/>')
        assert markers == ColumnMarkers(comment=None, is_primary_key=True)

    def test_a_comment_plus_a_note_block_recovers_the_comment(self) -> None:
        markers = parse_column_description(
            "The owner.\n\nNote:\nThis is a Foreign Key to `users.id`.<fk table='users' column='id'/>"
        )
        assert markers == ColumnMarkers(
            comment='The owner.', is_primary_key=False, foreign_table='users', foreign_column='id'
        )

    def test_both_markers_on_one_column(self) -> None:
        markers = parse_column_description(
            "Note:\nThis is a Primary Key.<pk/>\nThis is a Foreign Key to `orders.id`.<fk table='orders' column='id'/>"
        )
        assert markers == ColumnMarkers(comment=None, is_primary_key=True, foreign_table='orders', foreign_column='id')

    def test_a_malformed_marker_is_ignored_and_the_description_preserved(self) -> None:
        markers = parse_column_description('Note:\nThis is something else.<mystery/>')
        assert markers.is_primary_key is False
        assert markers.foreign_table is None
        assert markers.comment == 'Note:\nThis is something else.<mystery/>'

    def test_extra_whitespace_inside_a_marker_still_strips_the_note_block(self) -> None:
        # _FK_MARKER tolerates \s+ between attributes; _NOTE_BLOCK must tolerate exactly
        # the same shapes or the raw marker text leaks into the emitted description.
        markers = parse_column_description(
            "Real comment.\n\nNote:\nThis is a Foreign Key to `users.id`.<fk  table='users'  column='id'/>"
        )
        assert (markers.foreign_table, markers.foreign_column) == ('users', 'id')
        assert markers.comment == 'Real comment.'

    def test_extra_whitespace_in_a_pk_marker_still_strips_the_note_block(self) -> None:
        markers = parse_column_description('Real comment.\n\nNote:\nThis is a Primary Key.<pk />')
        assert markers.is_primary_key is True
        assert markers.comment == 'Real comment.'

    def test_markers_are_detected_position_independently(self) -> None:
        markers = parse_column_description("leading <pk/> and <fk table='t' column='c'/> markers")
        assert markers.is_primary_key is True
        assert (markers.foreign_table, markers.foreign_column) == ('t', 'c')

    def test_the_fk_marker_carries_no_schema_and_no_constraint_name(self, document: dict[str, Any]) -> None:
        # The fidelity floor: castiron must synthesize both.
        rows = parse_openapi_document(document)
        assert ('public', 'orders', 'user_id', 'public', 'users', 'id', 'orders_user_id_fkey', True) in rows.fk_details


# ---------------------------------------------------------------------------
# Constraints -- only two kinds are ever synthesized.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstraints:
    def test_one_primary_key_row_per_table_in_document_order(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        primary_keys = {row[1]: row for row in rows.constraints if row[3] == 'p'}
        # active_users_view is deliberately absent: castiron models a VIEW as having no
        # primary key, so its <pk/> markers are dropped rather than left contradicting
        # TableInfo.primary_key().
        assert set(primary_keys) == {
            'order_items',
            'orders',
            'products',
            'restricted_table',
            'users',
        }
        assert primary_keys['users'] == ('users_pkey', 'users', ['id'], 'p', None, True)
        # Composite membership survives; the key ORDER is document order, not pg key order.
        assert primary_keys['order_items'][2] == ['order_id', 'product_id']

    def test_one_foreign_key_row_per_marker_with_a_synthesized_name(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        foreign_keys = [row for row in rows.constraints if row[3] == 'f']
        assert (
            'orders_user_id_fkey',
            'orders',
            ['user_id'],
            'f',
            'FOREIGN KEY (user_id) REFERENCES users(id)',
            True,
        ) in foreign_keys
        assert len(foreign_keys) == len(rows.fk_details)

    def test_check_and_exclude_constraints_are_never_produced(self, document: dict[str, Any]) -> None:
        # The fidelity floor: the document contains no constraint information at all. The
        # only 'u' rows are a view's `<pk/>` markers downgraded to UNIQUE (CI5-D14a).
        rows = parse_openapi_document(document)
        assert {row[3] for row in rows.constraints} == {'p', 'f', 'u'}
        assert 'c' not in {row[3] for row in rows.constraints}
        assert 'x' not in {row[3] for row in rows.constraints}
        assert [row[1] for row in rows.constraints if row[3] == 'u'] == ['active_users_view']

    def test_a_table_with_no_primary_key_gets_no_primary_key_row(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'text'}}))
        assert rows.constraints == ()

    def test_an_empty_foreign_key_marker_produces_no_rows(self) -> None:
        # `<fk table='' column=''/>` names nothing. The builder drops the edge anyway, so
        # emitting rows for it would leave a bogus constraint that sets is_foreign_key on a
        # column with no relationship.
        rows = parse_openapi_document(
            minimal_document(
                {
                    'a': {
                        'format': 'int32',
                        'description': "Note:\nThis is a Foreign Key to `.`.<fk table='' column=''/>",
                    }
                }
            )
        )
        assert rows.fk_details == ()
        assert rows.constraints == ()

    def test_a_views_key_becomes_a_unique_row_not_a_primary_key_row(self, document: dict[str, Any]) -> None:
        # ``TableInfo.primary_key()`` is empty for a VIEW by definition, so a 'p' row would
        # leave ``col.primary`` and ``primary_key()`` disagreeing -- but dropping the marker
        # outright loses the uniqueness a foreign key pointing at the view needs.
        rows = parse_openapi_document(document)
        view_constraints = [row for row in rows.constraints if row[1] == 'active_users_view']
        assert [row[3] for row in view_constraints] == ['u', 'f']
        assert view_constraints[0] == (
            'active_users_view_id_key',
            'active_users_view',
            ['id'],
            'u',
            'UNIQUE (id)',
            True,
        )


# ---------------------------------------------------------------------------
# Enums.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnums:
    def test_an_unqualified_format_takes_the_callers_schema(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert rows.enum_types == (('order_status', 'public', '', 'E', True, 'e', ['pending', 'shipped', 'cancelled']),)

    def test_a_qualified_format_splits_on_the_last_dot(self) -> None:
        rows = parse_openapi_document(
            minimal_document({'a': {'format': 'test.enum_menagerie_type', 'type': 'string', 'enum': ['foo', 'bar']}})
        )
        assert rows.enum_types == (('enum_menagerie_type', 'test', '', 'E', True, 'e', ['foo', 'bar']),)
        assert rows.enum_type_mapping == (('a', 't', 'test', 'enum_menagerie_type', 'E', ''),)

    def test_two_columns_sharing_an_enum_produce_one_type_row_and_two_mappings(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert len(rows.enum_types) == 1
        assert rows.enum_type_mapping == (
            ('status', 'order_items', 'public', 'order_status', 'E', ''),
            ('status', 'users', 'public', 'order_status', 'E', ''),
        )

    def test_enum_type_rows_are_sorted_by_namespace_then_name(self) -> None:
        rows = parse_openapi_document(
            minimal_document(
                {
                    'c': {'format': 'zeta', 'type': 'string', 'enum': ['z']},
                    'b': {'format': 'b.alpha', 'type': 'string', 'enum': ['a']},
                    'a': {'format': 'alpha', 'type': 'string', 'enum': ['a']},
                }
            )
        )
        assert [(row[1], row[0]) for row in rows.enum_types] == [
            ('b', 'alpha'),
            ('public', 'alpha'),
            ('public', 'zeta'),
        ]

    def test_an_enum_array_column_gets_no_mapping_row(self, document: dict[str, Any]) -> None:
        # PostgREST resolves labels from the *base* type, so an ``order_status[]`` column
        # carries no ``enum`` key -- its values are unknowable from the document.
        rows = parse_openapi_document(document)
        assert all(row[0] != 'labels' for row in rows.enum_type_mapping)
        assert column(rows, 'orders', 'labels')[10] == 'order_status'

    def test_a_non_list_enum_key_is_ignored(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'weird', 'type': 'string', 'enum': 'foo'}}))
        assert rows.enum_types == ()

    def test_non_string_enum_labels_are_dropped(self) -> None:
        rows = parse_openapi_document(minimal_document({'a': {'format': 'e', 'type': 'string', 'enum': ['a', 3]}}))
        assert rows.enum_types[0][6] == ['a']


# ---------------------------------------------------------------------------
# View classification (CI5-D6).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTableClassification:
    """CI-075: ONE signal -- a non-empty ``required`` array. The write verbs are not evidence.

    They used to be the first signal, on the assumption that PostgREST exposes
    ``post``/``patch``/``delete`` only where the API role may write. Measured against real
    PostgREST v14.14 **and** pinned v12.2.3 (CI-008), it exposes them for any **auto-updatable**
    relation regardless of privilege, so a ``GRANT SELECT``-only simple view is writable through
    the API and used to classify as a base table.
    """

    @pytest.mark.parametrize(
        ('definition', 'expected'),
        [
            ({'required': ['id']}, 'BASE TABLE'),
            ({'required': ['id', 'name']}, 'BASE TABLE'),
            ({}, 'VIEW'),
            ({'required': []}, 'VIEW'),
        ],
    )
    def test_a_non_empty_required_array_is_the_whole_decision(self, definition: dict[str, Any], expected: str) -> None:
        assert classify_table_type('t', definition, {'/t': {'get': {}, 'post': {}}}) == expected

    def test_an_empty_required_array_does_not_count_as_a_signal(self) -> None:
        assert classify_table_type('t', {'required': []}, {'/t': {'get': {}}}) == 'VIEW'

    def test_a_missing_path_item_is_treated_as_read_only(self) -> None:
        assert classify_table_type('t', {}, {}) == 'VIEW'

    @pytest.mark.parametrize('verbs', [(), *_all_verb_subsets()], ids=lambda v: '+'.join(v) or 'get-only')
    @pytest.mark.parametrize('required', [None, [], ['id']], ids=['absent', 'empty', 'present'])
    def test_a_write_method_proves_nothing_about_the_relation_kind(
        self, verbs: tuple[str, ...], required: list[str] | None
    ) -> None:
        """⚠ Enumerated, not sampled (``CI-072``): **all 8 verb subsets × 3 `required` states**.

        This test's predecessor was named ``test_any_write_method_proves_a_base_table`` and
        asserted the false premise *in its own name*. A parametrized sample of two would not be
        evidence that the verb signal is gone; the full cross-product is.
        """
        definition: dict[str, Any] = {} if required is None else {'required': required}
        path_item = {'get': {}, **{verb: {} for verb in verbs}}
        expected = 'BASE TABLE' if required else 'VIEW'
        assert classify_table_type('t', definition, {'/t': path_item}) == expected

    def test_the_fixture_classifies_the_view_and_the_read_only_table(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'active_users_view', 'id')[7] == 'VIEW'
        # GET-only, but it declares NOT NULL columns -> a BASE TABLE. Unaffected by CI-075: the
        # deciding signal was already `required` for this one.
        assert column(rows, 'restricted_table', 'id')[7] == 'BASE TABLE'

    @pytest.mark.parametrize(
        ('name', 'expected'),
        [
            # The five real views of the testbed capture. Three of them (the auto-updatable ones)
            # were BASE TABLE before this fix -- that IS CI-075.
            ('active_customers', 'VIEW'),
            ('ledger_summary', 'VIEW'),
            ('writable_customer_view', 'VIEW'),
            ('order_report', 'VIEW'),
            ('mv_customer_spend', 'VIEW'),
            # Real base tables, unchanged.
            ('customers', 'BASE TABLE'),
            ('orders', 'BASE TABLE'),
            ('products', 'BASE TABLE'),
            ('rls_locked_notes', 'BASE TABLE'),
            ('partially_visible', 'BASE TABLE'),
            # ⚠ The one KNOWN, ACCEPTED residual miss (`CI94-Q2`): a base table whose every column
            # is nullable is indistinguishable from a view in this document, and the tie is now
            # broken toward VIEW. It is inert -- no NOT NULL column means no primary key, so
            # `primary_key()` was already `[]` and there is nothing for the VIEW reading to empty.
            # Measured: flipping it changes one IR field and zero emitted bytes.
            ('all_nullable_readonly', 'VIEW'),
        ],
    )
    def test_the_real_capture_classifies_25_of_26_correctly(self, name: str, expected: str) -> None:
        document = json.loads(
            (CORPUS_INPUTS / 'testbed-public.openapi.json').read_text(encoding='utf-8'),
        )
        definition = document['definitions'][name]
        assert classify_table_type(name, definition, document['paths']) == expected


@pytest.mark.unit
class TestThePremiseTheClassifierRestsOn:
    """The fact that licenses reading ``required`` alone, asserted over every committed document.

    A ``<pk/>``-marked column outside a **non-empty** ``required`` implies the relation is a VIEW.
    Measured 6/6 with **0 exceptions** across all four corpus inputs plus the CI-005 fixture. It
    is deliberately **not** a branch in :func:`classify_table_type`: it is *provably equivalent* to
    the one-signal rule on any document PostgREST can emit (it could only differ where ``required``
    is non-empty *and* a PK column is nullable, which Postgres cannot produce), so encoding it
    would be dead logic that reads as live (``CI94-Q2``, ruled). Asserting it here is what keeps
    the justification falsifiable: if a future capture violates it, this goes red and the model's
    basis is gone.
    """

    #: Every committed OpenAPI document, enumerated from the tree rather than listed by hand.
    DOCUMENTS = sorted(CORPUS_INPUTS.glob('*.openapi.json')) + [FIXTURE_DOCUMENT]

    def test_the_sweep_covers_every_committed_document(self) -> None:
        assert len(self.DOCUMENTS) == 4, [p.name for p in self.DOCUMENTS]

    #: Per-document count of relations carrying a ``<pk/>`` outside a non-empty ``required``.
    #: Spelled out per document, because the class claims "**6/6**, 0 exceptions" and a claim
    #: that is never counted is a claim nobody is checking. ``testbed-inventory`` and
    #: ``synthetic-torture`` legitimately have none -- every relation in them is a base table --
    #: and asserting **zero** for those is what makes the other two numbers mean something.
    EXPECTED_WITNESSES = {
        # active_customers, ledger_summary, writable_customer_view, order_report, mv_customer_spend
        'testbed-public.openapi': 5,
        'postgrest_openapi': 1,  # active_users_view
        'testbed-inventory.openapi': 0,
        'synthetic-torture.openapi': 0,
    }

    @staticmethod
    def _witnesses(document: dict[str, Any]) -> list[tuple[str, dict[str, Any], list[str]]]:
        """Relations whose ``<pk/>`` marker falls outside a non-empty ``required``."""
        found = []
        for name, definition in document.get('definitions', {}).items():
            required = definition.get('required') or []
            marked = [
                column
                for column, prop in definition.get('properties', {}).items()
                if isinstance(prop, dict) and '<pk/>' in str(prop.get('description', ''))
            ]
            outside = [column for column in marked if column not in required]
            if outside:
                found.append((name, definition, outside))
        return found

    @pytest.mark.parametrize('path', DOCUMENTS, ids=lambda p: p.stem)
    def test_a_pk_marker_outside_a_non_empty_required_means_view(self, path: Path) -> None:
        document = json.loads(path.read_text(encoding='utf-8'))
        witnesses = self._witnesses(document)

        # ⚠ The count is asserted FIRST. Without it this test passes vacuously on a document with
        # no witnessing relation -- which is two of the four -- and the loop below would be a
        # guard that cannot fail (`CI-072`, `CI-091`).
        assert len(witnesses) == self.EXPECTED_WITNESSES[path.stem], (
            f'{path.name}: expected {self.EXPECTED_WITNESSES[path.stem]} relation(s) with a <pk/> '
            f'outside a non-empty `required`, found {[name for name, _, _ in witnesses]}. The '
            f"premise's evidence has moved; do not adjust the number without re-deriving it."
        )
        for name, definition, outside in witnesses:
            assert classify_table_type(name, definition, document.get('paths', {})) == 'VIEW', (
                f'{path.name}:{name} carries a <pk/> on {outside} outside a non-empty `required`, '
                f'so the premise says VIEW. If a real capture now violates this, the one-signal '
                f'model of CI-075 has lost its justification -- do not "fix" this test.'
            )

    def test_the_premise_totals_six_across_every_committed_document(self) -> None:
        # The number the class docstring claims, asserted as a total rather than left as prose.
        total = sum(len(self._witnesses(json.loads(path.read_text(encoding='utf-8')))) for path in self.DOCUMENTS)
        assert total == 6, total
        assert sum(self.EXPECTED_WITNESSES.values()) == 6

    def test_a_non_empty_required_always_contains_every_pk_marker(self) -> None:
        # The other half, and the stronger claim: over the 26 definitions of the real capture,
        # 20 carry `required` and ALL 20 carry a <pk/> that is inside it -- 0 exceptions.
        document = json.loads((CORPUS_INPUTS / 'testbed-public.openapi.json').read_text(encoding='utf-8'))
        with_required = 0
        for name, definition in document['definitions'].items():
            required = definition.get('required') or []
            if not required:
                continue
            with_required += 1
            marked = [
                column
                for column, prop in definition['properties'].items()
                if isinstance(prop, dict) and '<pk/>' in str(prop.get('description', ''))
            ]
            assert marked, f'{name} declares NOT NULL columns but carries no <pk/> marker'
            assert set(marked) <= set(required), f'{name}: {marked} not all inside {required}'
        assert with_required == 20, with_required
        assert len(document['definitions']) == 26


# ---------------------------------------------------------------------------
# Surrogate primary-key inference (CI5-D7).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGeneratedPrimaryKeyInference:
    def test_it_is_off_by_default(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'users', 'id')[8] is None

    def test_a_sole_not_null_integer_pk_with_no_default_is_inferred(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document, infer_generated_primary_keys=True)
        assert column(rows, 'users', 'id')[8] == 'BY DEFAULT'
        assert column(rows, 'orders', 'id')[8] == 'BY DEFAULT'

    def test_a_composite_primary_key_is_never_inferred(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document, infer_generated_primary_keys=True)
        assert column(rows, 'order_items', 'order_id')[8] is None
        assert column(rows, 'order_items', 'product_id')[8] is None

    def test_a_non_integer_primary_key_is_never_inferred(self) -> None:
        document = minimal_document(
            {'id': {'format': 'uuid', 'type': 'string', 'description': 'Note:\nThis is a Primary Key.<pk/>'}},
            required=['id'],
        )
        rows = parse_openapi_document(document, infer_generated_primary_keys=True)
        assert column(rows, 't', 'id')[8] is None

    def test_a_primary_key_with_a_default_is_never_inferred(self) -> None:
        document = minimal_document(
            {
                'id': {
                    'format': 'int32',
                    'type': 'integer',
                    'default': 7,
                    'description': 'Note:\nThis is a Primary Key.<pk/>',
                }
            },
            required=['id'],
        )
        rows = parse_openapi_document(document, infer_generated_primary_keys=True)
        assert column(rows, 't', 'id')[8] is None

    def test_a_nullable_primary_key_is_never_inferred(self) -> None:
        document = minimal_document(
            {'id': {'format': 'int32', 'type': 'integer', 'description': 'Note:\nThis is a Primary Key.<pk/>'}}
        )
        rows = parse_openapi_document(document, infer_generated_primary_keys=True)
        assert column(rows, 't', 'id')[8] is None


# ---------------------------------------------------------------------------
# Functions / RPCs.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFunctions:
    def test_names_come_from_the_path_key_and_are_sorted(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert [row[1] for row in rows.function_details] == [
            'create_order',
            'get_user_stats',
            'ping',
            'search_products',
        ]

    def test_exactly_one_function_row_per_rpc_path_key(self, document: dict[str, Any]) -> None:
        # Overloads are collapsed upstream: PostgREST maps every overload of ``f`` to the
        # single path key ``/rpc/f`` and the last one wins, so one row per key is all the
        # document can ever express.
        rpc_keys = [key for key in document['paths'] if key.startswith('/rpc/')]
        rows = parse_openapi_document(document)
        assert len(rows.function_details) == len(rpc_keys)
        assert len({row[1] for row in rows.function_details}) == len(rpc_keys)

    def test_return_type_and_set_returning_are_always_none(self, document: dict[str, Any]) -> None:
        # The fidelity floor: PostgREST encodes only ``"200": {"description": "OK"}``.
        rows = parse_openapi_document(document)
        assert all(row[3] is None for row in rows.function_details)
        assert all(row[4] is None for row in rows.function_details)

    def test_post_only_functions_are_volatile(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'create_order')[5] == 'v'
        assert function(rows, 'create_order')[6] is False

    def test_get_plus_post_functions_are_read_only_with_unknown_volatility(self, document: dict[str, Any]) -> None:
        # STABLE vs IMMUTABLE is indistinguishable, so volatility stays unknown.
        rows = parse_openapi_document(document)
        assert function(rows, 'get_user_stats')[5] is None
        assert function(rows, 'get_user_stats')[6] is True

    def test_every_fixture_rpc_body_is_in_alphabetical_order(self, document: dict[str, Any]) -> None:
        # `CI-133`. Not evidence about PostgREST -- the fixture is hand-authored, so it can only
        # ever be evidence about castiron -- but it stops the fixture from drifting BACK to a
        # document shape the generator cannot emit, which is what let `CI-078` survive two
        # corrections. The claim it mirrors is asserted against real bytes in
        # TestRpcParameterOrderInTheRealCaptures below.
        checked = 0
        for key, item in document['paths'].items():
            if not key.startswith('/rpc/'):
                continue
            for parameter in item.get('post', {}).get('parameters', []):
                if parameter.get('in') != 'body':
                    continue
                properties = list(parameter['schema'].get('properties', {}))
                assert properties == sorted(properties), (
                    f'{key} has a POST body in {properties}, which is not alphabetical. A real '
                    f'PostgREST always sorts it; re-sort the fixture rather than the expectation.'
                )
                checked += 1
        # Not vacuous: every /rpc/* path in the fixture has a body, `ping`'s merely empty.
        assert checked == 4, f'expected 4 RPC bodies in the fixture, swept {checked}'

    # ⚠ The three parameter-order expectations below read the hand-authored CI-005 fixture,
    # whose RPC bodies are ALPHABETICAL as of `CI-133` -- the order a real PostgREST emits,
    # measured in TestRpcParameterOrderInTheRealCaptures. They used to pin "castiron preserves
    # whatever order the document gives"; as of `CI-078` that is **no longer what castiron does**.
    # The body is one of THREE serializations of one parameter list, and the other two -- the GET
    # `parameters` array and the POST body's `required` array -- are ordered, so castiron reorders
    # into the best order the document proves (see `_declaration_order`). They are still **not**
    # evidence about PostgREST, because the fixture is hand-authored; they are evidence about
    # castiron's recovery rule, which is what a fixture can honestly carry.
    def test_parameter_order_is_recovered_from_the_get_operation(self, document: dict[str, Any]) -> None:
        # `get_user_stats` is GET-bearing, so the whole order is established: the body arrives
        # `[since, user_id]` and the GET array says `[user_id, since]`.
        rows = parse_openapi_document(document)
        assert function(rows, 'get_user_stats')[7] == [
            ('user_id', 'integer', None, False, None),
            ('since', 'date', None, True, None),
        ]
        assert function(rows, 'get_user_stats')[8] is ParameterOrder.DECLARED

    def test_parameter_types_are_normalized_like_columns(self, document: dict[str, Any]) -> None:
        # POST-only, and `required` is `[user_id, status]` -- two of the three arguments -- so the
        # recovered order is that prefix plus the name-sorted tail, and the row says so.
        rows = parse_openapi_document(document)
        assert function(rows, 'create_order')[7] == [
            ('user_id', 'bigint', None, False, None),
            ('status', 'order_status', None, False, None),
            ('items', 'text[]', None, True, 'text'),
        ]
        assert function(rows, 'create_order')[8] is ParameterOrder.DECLARED_PREFIX

    def test_a_no_argument_function_yields_an_empty_parameter_list(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'ping')[7] == []
        # D6: a list of length <=1 IS in declaration order. `ping` is VOLATILE with no GET, and
        # the claim still costs nothing -- it is a fact about arity, not a guess about a signal.
        assert function(rows, 'ping')[8] is ParameterOrder.DECLARED

    def test_a_variadic_argument_is_only_visible_through_the_get_operation(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'search_products')[7] == [
            ('terms', 'text[]', 'v', False, 'text'),
            ('limit_to', 'integer', None, True, None),
        ]
        assert function(rows, 'search_products')[8] is ParameterOrder.DECLARED

    def test_the_description_comes_from_the_body_schema(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'ping')[2] == 'Health check'
        assert function(rows, 'get_user_stats')[2] == 'Aggregate statistics for one user\n\nReturns lifetime totals.'

    def test_the_description_falls_back_to_summary_plus_description(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'post': {
                        'summary': 'Do a thing',
                        'description': 'At length.',
                        'parameters': [{'name': 'args', 'in': 'body', 'schema': {'properties': {}}}],
                    }
                }
            },
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[2] == 'Do a thing\n\nAt length.'

    def test_a_function_with_no_summary_or_description_has_none(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {'/rpc/f': {'post': {'parameters': [{'name': 'args', 'in': 'body', 'schema': {}}]}}},
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[2] is None

    def test_a_post_only_path_item_without_a_body_schema_falls_back_to_get(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'get': {
                        'summary': 'Query only',
                        'parameters': [
                            {'name': 'a', 'in': 'query', 'required': True, 'type': 'integer', 'format': 'int32'},
                            {
                                'name': 'b',
                                'in': 'query',
                                'type': 'array',
                                'format': 'text[]',
                                'collectionFormat': 'multi',
                            },
                            {'in': 'query', 'type': 'string'},
                            'not-an-object',
                        ],
                    }
                }
            },
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[7] == [
            ('a', 'integer', None, False, None),
            ('b', 'text[]', 'v', True, 'text'),
        ]
        assert function(rows, 'f')[2] == 'Query only'
        # D8: this list IS the GET's `parameters` array, which is an ordered JSON array, so it
        # arrives in declaration order and needs no reordering.
        assert function(rows, 'f')[8] is ParameterOrder.DECLARED

    def test_a_rpc_path_item_with_neither_post_nor_get_is_skipped(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {'/rpc/f': {'head': {}}, '/rpc/g': 'not-an-object', '/rpc/': {'post': {}}},
        }
        assert parse_openapi_document(document).function_details == ()

    def test_a_parameter_that_is_not_an_object_is_skipped(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'post': {
                        'parameters': [
                            'not-an-object',
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {'properties': {'a': {'format': 'text'}, 'b': 'nope'}},
                            },
                        ]
                    }
                }
            },
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[7] == [('a', 'text', None, True, None)]

    def test_a_parameter_with_no_type_names_the_function_and_argument(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'post': {'parameters': [{'name': 'args', 'in': 'body', 'schema': {'properties': {'x': {}}}}]}
                }
            },
        }
        with pytest.raises(SourceParseError, match=r'f\(x\)'):
            parse_openapi_document(document)

    def test_a_body_parameter_whose_schema_is_absent_is_not_treated_as_a_body(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {'/rpc/f': {'post': {'parameters': [{'name': 'args', 'in': 'body'}]}}},
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[7] == []

    def test_a_post_operation_with_no_body_parameter_yields_no_arguments(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {'/rpc/f': {'post': {'parameters': [{'$ref': '#/parameters/preferParams'}]}}},
        }
        rows = parse_openapi_document(document)
        assert function(rows, 'f')[7] == []

    def test_non_rpc_paths_are_ignored(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert all(row[1] not in {'', 'users', 'orders'} for row in rows.function_details)


# ---------------------------------------------------------------------------
# RPC parameter order, against the REAL captures (``CI-133``, pinning ``CI-078``).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRpcParameterOrderInTheRealCaptures:
    """What a real PostgREST emits for function-parameter order, asserted from real bytes.

    ⚠ **Every assertion above this class about parameter order reads the hand-authored CI-005
    fixture, and that fixture cannot settle this question** -- an author who believes a false
    thing about a generator writes the false thing into their fixture, and the test then agrees
    with them forever. That is exactly how ``CI-078`` survived two corrections: the parser
    docstring claimed ``properties`` order was pg argument position, and nothing in the unit
    suite could contradict it. This is the same shape as ``CI-076`` (a fixture carrying a shape
    the real source cannot emit), so the remedy is the one ``CI-076`` established: **assert it
    against a capture.**

    These read ``tests/unit/corpus/inputs/testbed-*.openapi.json`` -- documents produced by a
    real PostgREST against the seeded testbed, committed and key-free. They need no live server,
    and one of them is **self-proving**: ``search_products`` carries the same signature twice, in
    two different orders, in a single document. No outside knowledge is needed to see that at
    most one of them can be the declaration order.

    The live suite (``tests/integration/test_openapi_live.py:762``) pins this too, but only
    under ``RUN_DB_TESTS=1``, so it is absent from the gate that actually runs on every push.
    """

    #: The captures only. The CI-005 fixture is hand-authored (``origin='synthetic'`` in
    #: ``tests/unit/corpus/cases.py``), so it is evidence about castiron, never about PostgREST.
    CAPTURES = sorted(CORPUS_INPUTS.glob('testbed-*.openapi.json'))

    #: Captured ``/rpc/*`` bodies with >1 parameter -- the only ones whose order carries any
    #: information. Asserted by name, so the sweep below cannot pass vacuously if a capture is
    #: replaced by one with no multi-argument function (``CI-072``, ``CI-091``). The five
    #: ``probe_*`` entries arrived with the ``CI-139`` (testbed ``f839fce``, the three STABLE ones)
    #: and ``CI-140`` (testbed ``752649a``, the two VOLATILE ones) recaptures, which seeded them as
    #: migrations precisely so they cannot vanish again; see :class:`TestTheArgumentOrderProbes`.
    INFORMATIVE_BODIES = (
        'create_order',
        'get_customer_stats',
        'probe_mixed',
        'probe_two_optional',
        'probe_two_required',
        'probe_volatile_all_optional',
        'probe_volatile_two_required',
        'reserved_args',
        'search_products',
    )

    @staticmethod
    def _document(path: Path) -> dict[str, Any]:
        """Load one committed capture."""
        loaded: dict[str, Any] = json.loads(path.read_text(encoding='utf-8'))
        return loaded

    @classmethod
    def _body_properties(cls, path_item: dict[str, Any]) -> list[str]:
        """Return the POST body schema's ``properties`` keys, in document order."""
        for parameter in path_item.get('post', {}).get('parameters', []):
            if parameter.get('in') == 'body':
                return list(parameter['schema'].get('properties', {}))
        return []

    @classmethod
    def _get_parameter_names(cls, path_item: dict[str, Any]) -> list[str]:
        """Return the GET operation's query-parameter names, in document order."""
        return [
            parameter['name'] for parameter in path_item.get('get', {}).get('parameters', []) if 'name' in parameter
        ]

    @classmethod
    def _rpc_items(cls, document: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return ``{function name: path item}`` for every ``/rpc/*`` key."""
        return {key[len('/rpc/') :]: item for key, item in document['paths'].items() if key.startswith('/rpc/')}

    def test_the_sweep_covers_both_committed_captures(self) -> None:
        assert [path.name for path in self.CAPTURES] == [
            'testbed-inventory.openapi.json',
            'testbed-public.openapi.json',
        ]

    def test_every_captured_rpc_body_is_in_alphabetical_order(self) -> None:
        # The systematic claim, swept over every real function in both captures rather than
        # argued from one example: PostgREST alphabetizes the POST body. If a future capture
        # contains a body that is NOT sorted, the generator's behaviour has changed and CI-078's
        # whole premise needs re-deriving -- do not "fix" this test to accommodate it.
        informative: list[str] = []
        for path in self.CAPTURES:
            for name, item in self._rpc_items(self._document(path)).items():
                properties = self._body_properties(item)
                assert properties == sorted(properties), (
                    f'{path.name}:/rpc/{name} has a POST body in {properties}, which is not '
                    f'alphabetical. Every captured body has been alphabetical until now.'
                )
                if len(properties) > 1:
                    informative.append(name)

        assert sorted(informative) == list(self.INFORMATIVE_BODIES), sorted(informative)

    def test_search_products_carries_two_orders_and_they_disagree(self) -> None:
        # ⚠ The self-proving witness, and the reason this class needs no live server. ONE
        # document describes ONE function twice: the POST body and the GET query parameters.
        # They are in different orders, so at most one of them can be the declaration order --
        # and the sorted one is the body.
        item = self._rpc_items(self._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))['search_products']
        body = self._body_properties(item)
        query = self._get_parameter_names(item)

        assert body == ['p_limit', 'p_terms']
        assert query == ['p_terms', 'p_limit']
        assert body == sorted(body), 'the POST body is the alphabetized one'
        assert query != sorted(query), (
            'the GET parameters are NOT alphabetized -- that asymmetry is what proves the body '
            'order is imposed by the generator rather than inherited from the function.'
        )
        # The function is declared `(p_terms, p_limit)` in the testbed seed, which is the GET
        # order: PostgREST emits `parameters` in declaration order for a STABLE/IMMUTABLE
        # function. Asserted from the document above, not from that outside knowledge.

    def test_castiron_takes_the_declaration_order_when_the_get_operation_carries_it(self) -> None:
        # ⚠ **This used to pin a DEFECT and now pins its FIX** (`CI-078`, landed). castiron read
        # the alphabetical body and reported `search_products` as `(p_limit, p_terms)` -- the
        # reverse of how it is declared -- even though the same document carries the true order in
        # the GET operation the parser ALREADY opened (it takes VARIADIC from there). A positional
        # RPC call generated from that (CI-012) would type-check, run, and return the wrong answer.
        #
        # The residual is NOT here: a GET operation exists only for a STABLE/IMMUTABLE function,
        # so this recovery covers the read-only half outright. What is only PARTLY recoverable is
        # a VOLATILE function -- see `test_a_volatile_function_recovers_only_its_required_prefix`.
        rows = parse_openapi_document(self._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))
        assert [parameter[0] for parameter in function(rows, 'search_products')[7]] == ['p_terms', 'p_limit']
        assert function(rows, 'search_products')[8] is ParameterOrder.DECLARED

    def test_every_get_bearing_capture_is_built_in_its_get_order(self) -> None:
        # The sweep, not one example (CI-072): for EVERY captured RPC that has a GET, castiron's
        # parameter list is the GET's names filtered to the body's names, and the row declares
        # DECLARED. One function agreeing could be a coincidence; twelve cannot.
        checked: list[str] = []
        for path in self.CAPTURES:
            document = self._document(path)
            rows = parse_openapi_document(document, schema='public')
            for name, item in self._rpc_items(document).items():
                if 'get' not in item:
                    continue
                body_names = set(self._body_properties(item))
                expected = [n for n in self._get_parameter_names(item) if n in body_names]
                assert [parameter[0] for parameter in function(rows, name)[7]] == expected, name
                assert function(rows, name)[8] is ParameterOrder.DECLARED, name
                checked.append(name)

        # Non-vacuous by count, re-measured on this branch rather than inherited: 11 GET-bearing
        # functions in `testbed-public` (8 before the CI-139 probes) + 1 in `testbed-inventory`.
        assert len(checked) == 12, f'expected 12 GET-bearing captured RPCs, swept {sorted(checked)}'

    def test_the_required_array_agrees_with_the_get_order_wherever_both_are_informative(self) -> None:
        # 🔴 **THE CROSS-CHECK for the `required`-is-declaration-order premise.** castiron recovers
        # a VOLATILE function's order from `required` alone. What corroborates that INSIDE one
        # function is a function carrying BOTH encodings: where a GET exists and `required` has
        # >=2 entries, the required names must appear in the GET array in the same relative order.
        # If they ever disagree, `_declaration_order`'s rule 3 is unsound -- stop and re-derive it,
        # do not adjust this test.
        #
        # ⚠ A GET-bearing function is by definition NOT the case rule 3 exists for, so this is
        # corroboration and not the measurement. The measurement is `probe_volatile_two_required`
        # (`CI-140`), where `required` is the entire order signal; see
        # `TestTheArgumentOrderProbes.test_the_required_array_is_the_only_order_a_volatile_probe_can_carry`.
        informative: list[str] = []
        for path in self.CAPTURES:
            for name, item in self._rpc_items(self._document(path)).items():
                required = _post_body_required(item)
                if 'get' not in item or len(required) < 2:
                    continue
                get_order = self._get_parameter_names(item)
                assert [n for n in get_order if n in set(required)] == list(required), (
                    f'{name}: `required` is {required} but the GET array orders those names as '
                    f'{[n for n in get_order if n in set(required)]}. The two ordered encodings '
                    f'disagree, so `required` is NOT declaration order and CI-078 rule 3 is wrong.'
                )
                informative.append(name)

        # ⚠ Non-vacuity is the whole point: before the CI-139 recapture EVERY captured function had
        # `len(required) <= 1`, so this loop ran zero times and would have certified the premise by
        # checking nothing (the CI-083/CI-091 shape). These two probes are what make it real.
        assert sorted(informative) == ['probe_mixed', 'probe_two_required'], sorted(informative)

    def test_the_required_arguments_are_always_the_leading_run_of_the_get_order(self) -> None:
        # The structural half of the same premise, and what licenses "correct prefix + unknown
        # tail" instead of "scattered subset": Postgres forbids a defaulted parameter before a
        # non-defaulted one, so the non-defaulted set is necessarily a PREFIX. PostgREST reflects
        # that. A failure here means the source's model changed, not that the test is wrong.
        #
        # ⚠ GREEN before CI-078 as well as after -- it is a claim about the committed input
        # documents, not about castiron. It earns its place as a tripwire on the evidence.
        checked = 0
        for path in self.CAPTURES:
            for name, item in self._rpc_items(self._document(path)).items():
                if 'get' not in item:
                    continue
                required = _post_body_required(item)
                get_order = self._get_parameter_names(item)
                assert get_order[: len(required)] == required, (
                    f'{name}: `required` is {required}, which is not the leading run of the GET '
                    f'order {get_order}. A defaulted argument now precedes a non-defaulted one.'
                )
                checked += 1
        assert checked == 12, f'expected 12 GET-bearing captured RPCs, swept {checked}'

    def test_a_volatile_function_recovers_only_its_required_prefix(self) -> None:
        # The residual CI-078 leaves behind, asserted rather than described. `create_order` is
        # VOLATILE, so PostgREST emits no GET operation and the ONLY order signal is the POST
        # body's `required` array -- which here has one entry out of three. So position 0 is
        # recovered and the other two are not, and the row says exactly that.
        #
        # This test's ancestor was called `..._declaration_order_is_absent_from_the_document`.
        # That title was false: the order is PARTLY present, in `required`. Its premise assertion
        # (no `get`) is still exactly right and is kept.
        document = self._document(CORPUS_INPUTS / 'testbed-public.openapi.json')
        item = self._rpc_items(document)['create_order']
        assert 'get' not in item, 'create_order stopped being VOLATILE; the CI-078 residual changes shape'

        body = self._body_properties(item)
        assert body == ['p_customer_id', 'p_lines', 'p_status']
        assert body == sorted(body)
        assert _post_body_required(item) == ['p_customer_id']

        rows = parse_openapi_document(document)
        # ⚠ Byte-identical to the alphabetical order it replaced -- a one-element prefix adds
        # nothing here -- and the testbed declares it `(p_customer_id, p_status, p_lines)`, so the
        # last two are still SWAPPED. That is why `CI-078` stays in KNOWN_DEFECTS.
        assert [parameter[0] for parameter in function(rows, 'create_order')[7]] == body
        assert function(rows, 'create_order')[8] is ParameterOrder.DECLARED_PREFIX

    def test_a_get_that_is_not_a_permutation_of_the_body_falls_back_to_required(self) -> None:
        # D7, on a SYNTHETIC document on purpose: this is a test about castiron's fallback for a
        # shape no capture exhibits, not about what PostgREST emits. A GET naming an argument the
        # body lacks must not raise and must not partially reorder -- it drops to the `required`
        # rule, which still yields a correct prefix.
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'get': {'parameters': [{'name': 'p_ghost', 'in': 'query', 'type': 'string', 'format': 'text'}]},
                    'post': {
                        'parameters': [
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {
                                    'required': ['p_zulu'],
                                    'properties': {
                                        'p_alpha': {'format': 'text'},
                                        'p_zulu': {'format': 'text'},
                                    },
                                },
                            }
                        ]
                    },
                }
            },
        }
        rows = parse_openapi_document(document)
        assert [parameter[0] for parameter in function(rows, 'f')[7]] == ['p_zulu', 'p_alpha']
        assert function(rows, 'f')[8] is ParameterOrder.DECLARED_PREFIX

    def test_an_unusable_entry_in_the_get_array_does_not_defeat_the_recovery(self) -> None:
        # A GET `parameters` array carrying something that is not a named object -- the same
        # tolerance `_parse_query_parameters` and `_variadic_parameter_names` already have. The
        # junk is skipped and the remaining names are still a permutation of the body, so this
        # stays DECLARED rather than degrading to the `required` rule. Synthetic: no capture
        # contains this, and asserting it against one would be asserting a coincidence.
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'get': {
                        'parameters': [
                            {'name': 'p_zulu', 'in': 'query', 'type': 'string', 'format': 'text'},
                            'not-an-object',
                            {'in': 'query', 'type': 'string'},  # no `name`
                            {'name': 'p_alpha', 'in': 'query', 'type': 'string', 'format': 'text'},
                        ]
                    },
                    'post': {
                        'parameters': [
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {
                                    'required': ['p_zulu'],
                                    'properties': {
                                        'p_alpha': {'format': 'text'},
                                        'p_zulu': {'format': 'text'},
                                    },
                                },
                            }
                        ]
                    },
                }
            },
        }
        rows = parse_openapi_document(document)
        assert [parameter[0] for parameter in function(rows, 'f')[7]] == ['p_zulu', 'p_alpha']
        assert function(rows, 'f')[8] is ParameterOrder.DECLARED

    def test_a_volatile_function_with_every_argument_required_recovers_its_whole_order(self) -> None:
        # The case rule 3 exists for, and the one CI-012 benefits from most: a VOLATILE mutation
        # with no defaults has its COMPLETE declaration order in `required`, so it is DECLARED --
        # not DECLARED_PREFIX -- even with no GET operation anywhere in the document.
        #
        # Synthetic, and it stays synthetic now that a real one exists: this document has THREE
        # required arguments in a deliberately anti-alphabetical order, which is a stronger
        # arrangement than any capture carries. The real witness landed with the `CI-140`
        # recapture -- `probe_volatile_two_required`, asserted in `TestTheArgumentOrderProbes` --
        # so this is no longer the *only* evidence for the case rule 3 exists for, which is
        # exactly the CI-076 posture the corpus wants: a claim about a real source resting on
        # captured bytes, with the synthetic document kept as the wider-input variant.
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/place_order': {
                    'post': {
                        'parameters': [
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {
                                    # Anti-alphabetical on purpose: a sorted expectation would pass
                                    # even if castiron ignored `required` entirely.
                                    'required': ['p_user', 'p_product', 'p_qty'],
                                    'properties': {
                                        'p_product': {'format': 'text'},
                                        'p_qty': {'format': 'int4'},
                                        'p_user': {'format': 'text'},
                                    },
                                },
                            }
                        ]
                    }
                }
            },
        }
        rows = parse_openapi_document(document)
        assert [parameter[0] for parameter in function(rows, 'place_order')[7]] == ['p_user', 'p_product', 'p_qty']
        assert function(rows, 'place_order')[8] is ParameterOrder.DECLARED
        assert all(parameter[3] is False for parameter in function(rows, 'place_order')[7]), 'none is defaulted'

    def test_a_volatile_function_with_every_argument_defaulted_establishes_nothing(self) -> None:
        # The minimal shape that reaches `ParameterOrder.UNKNOWN`: >=2 arguments, ALL defaulted,
        # and NO GET -- i.e. a VOLATILE all-optional function. `probe_two_optional` is all-optional
        # but STABLE (so it has a GET and lands DECLARED); `create_order` is VOLATILE but has one
        # required argument.
        #
        # ⚠ **This used to be the ONLY place UNKNOWN was reachable, and `CI-140` closed that.** The
        # comment here said the member had no live witness -- a public enum member shipped in
        # `0.3.0` whose entire evidence was the hand-written document below, which is the `CI-076`
        # posture the corpus exists to avoid. The testbed now seeds `probe_volatile_all_optional`
        # (`752649a`), and `TestTheArgumentOrderProbes` asserts UNKNOWN against those captured
        # bytes. This test is kept as the MINIMAL synthetic form, distinguishable from the capture
        # in one way that matters: it carries `required: []` explicitly, where PostgREST v14.14
        # OMITS the key entirely. Both must reach UNKNOWN, and only one of them is a real document.
        #
        # UNKNOWN is NOT a claim the order is wrong -- the body order is kept verbatim, and these
        # two arguments may well be declared alphabetically. It is a refusal to claim.
        document = {
            'swagger': '2.0',
            'definitions': {'t': {'properties': {'a': {'format': 'text'}}}},
            'paths': {
                '/rpc/f': {
                    'post': {
                        'parameters': [
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {
                                    'required': [],
                                    'properties': {
                                        'p_alpha': {'format': 'text'},
                                        'p_zebra': {'format': 'text'},
                                    },
                                },
                            }
                        ]
                    }
                }
            },
        }
        rows = parse_openapi_document(document)
        assert [parameter[0] for parameter in function(rows, 'f')[7]] == ['p_alpha', 'p_zebra']
        assert function(rows, 'f')[8] is ParameterOrder.UNKNOWN


# ---------------------------------------------------------------------------
# The seeded argument-order probes (``CI-139``) -- what the GET order actually IS.
# ---------------------------------------------------------------------------


#: The capture readers belong to :class:`TestRpcParameterOrderInTheRealCaptures` and are
#: referenced rather than re-implemented: two notions of "the POST body" in one file is how the
#: two halves quietly drift apart, and this class exists to compare them.
_READER = TestRpcParameterOrderInTheRealCaptures


def _public_rpc_items() -> dict[str, dict[str, Any]]:
    """Return ``{function name: path item}`` for every ``/rpc/*`` key of the public capture."""
    return _READER._rpc_items(_READER._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))


def _post_body_required(item: dict[str, Any]) -> list[str]:
    """Return the POST body schema's ``required`` array, in document order."""
    for parameter in item.get('post', {}).get('parameters', []):
        if parameter.get('in') == 'body':
            return list(parameter['schema'].get('required', []))
    return []


@pytest.mark.unit
class TestTheArgumentOrderProbes:
    """The five seeded ``probe_*`` functions, and the claims they make falsifiable.

    ``search_products`` above proves the POST body and the GET operation **disagree**, so at most
    one of them can be declaration order. What it cannot say is *which*: with two arguments
    (``p_terms``, ``p_limit``, one of them defaulted) the observed GET array is equally consistent
    with "pg declaration order" and with "required first, then alphabetical within group". Those
    two rules are not interchangeable -- a generated positional RPC call (``CI-012``) is silently
    wrong under the loser -- and the corpus could not tell them apart.

    The ``probe_*`` functions break the tie. They were designed against ``pg_proc.proargnames``
    as the oracle, and the testbed seeds them **as migrations** so the findings are reproducible
    from committed SQL instead of from an ad-hoc ``psql`` session that died with its container.

    **Group 1 -- STABLE (``CI-139``, testbed ``f839fce``).** A GET operation exists, and the GET
    array is the experiment:

    - ``probe_two_required(p_zebra text, p_alpha text)`` -- declaration order is anti-alphabetical.
    - ``probe_two_optional(p_zebra text DEFAULT NULL, p_alpha text DEFAULT NULL)`` -- same, with
      **nothing** ``required``, so "required first" has no group to sort.
    - ``probe_mixed(p_zulu text, p_alpha text, p_beta text DEFAULT NULL)`` -- **the decisive one**:
      required-first-then-alphabetical predicts ``[p_alpha, p_zulu, p_beta]``, declaration order
      predicts ``[p_zulu, p_alpha, p_beta]``, and the document says the latter.

    **Group 2 -- VOLATILE (``CI-140``, testbed ``752649a``).** PostgREST v14.14 emits **no GET**
    for a VOLATILE function, so the POST body's ``required`` array is the document's only order
    signal -- and until this recapture that array had only ever been measured on the STABLE
    probes, i.e. on functions that carry a GET as well, which is exactly where the answer does not
    matter. These two are the first two above with the volatility flipped and **nothing else
    changed**, so any difference is attributable to volatility alone:

    - ``probe_volatile_two_required(p_zebra text, p_alpha text)`` -- ``required`` is the whole
      list, so the order is recovered in FULL with no GET anywhere: ``DECLARED``.
    - ``probe_volatile_all_optional(p_zebra text DEFAULT NULL, p_alpha text DEFAULT NULL)`` -- no
      GET **and** no ``required``, so nothing in the document carries order. This is the **only**
      shape that produces :attr:`~castiron.ir.models.ParameterOrder.UNKNOWN`, and before this
      capture that public enum member -- shipped in ``0.3.0`` -- had no witness anywhere but a
      hand-written document (see
      :meth:`TestRpcParameterOrderInTheRealCaptures.test_a_volatile_function_with_every_argument_defaulted_establishes_nothing`).

    **So the GET operation's query-parameter array is true pg declaration order, and the POST
    body's ``required`` array is its declaration-order prefix** -- the two facts ``CI-078`` turns
    on, now asserted from committed bytes rather than from a note. Every expectation below is
    visible in ``tests/unit/corpus/inputs/testbed-public.openapi.json``; no test here needs a live
    server, and none of them appeals to the seed's SQL as evidence.

    ⚠ ``CI-078`` has **LANDED**: castiron now builds its parameter list in the recovered order,
    so the last test in this class asserts the ``declared`` column where it once asserted
    ``alphabetical``. The document-level tests above it did not move a byte -- they were always
    claims about PostgREST's output, and PostgREST's output did not change.
    """

    #: ``(function, GET query order, POST ``properties`` order, POST ``required`` array)``.
    PROBES: tuple[tuple[str, list[str], list[str], list[str]], ...] = (
        ('probe_two_required', ['p_zebra', 'p_alpha'], ['p_alpha', 'p_zebra'], ['p_zebra', 'p_alpha']),
        ('probe_two_optional', ['p_zebra', 'p_alpha'], ['p_alpha', 'p_zebra'], []),
        ('probe_mixed', ['p_zulu', 'p_alpha', 'p_beta'], ['p_alpha', 'p_beta', 'p_zulu'], ['p_zulu', 'p_alpha']),
    )

    #: The parametrization ids, so a failure names the probe rather than an index.
    PROBE_IDS = [probe[0] for probe in PROBES]

    #: Group 2. ``(function, pg declaration order, POST ``properties`` order, POST ``required``
    #: array, the :class:`~castiron.ir.models.ParameterOrder` castiron must report)``. The
    #: declaration order is the testbed's SQL and is **not** asserted as a document fact for
    #: ``probe_volatile_all_optional`` -- the whole point is that this document cannot express it.
    VOLATILE_PROBES: tuple[tuple[str, list[str], list[str], list[str], ParameterOrder], ...] = (
        (
            'probe_volatile_two_required',
            ['p_zebra', 'p_alpha'],
            ['p_alpha', 'p_zebra'],
            ['p_zebra', 'p_alpha'],
            ParameterOrder.DECLARED,
        ),
        (
            'probe_volatile_all_optional',
            ['p_zebra', 'p_alpha'],
            ['p_alpha', 'p_zebra'],
            [],
            ParameterOrder.UNKNOWN,
        ),
    )

    #: The group-2 parametrization ids.
    VOLATILE_PROBE_IDS = [probe[0] for probe in VOLATILE_PROBES]

    def test_all_five_probes_are_present_in_the_capture(self) -> None:
        # If this fails on a fresh capture, the document did NOT come from a database carrying the
        # merged testbed migrations (752649a or later) -- do not delete a probe to make it pass,
        # re-capture. They are the only functions in the corpus that can distinguish declaration
        # order from required-first-alphabetical (group 1) or reach UNKNOWN at all (group 2).
        present = sorted(name for name in _public_rpc_items() if name.startswith('probe_'))
        assert present == sorted([*self.PROBE_IDS, *self.VOLATILE_PROBE_IDS]), (
            f'the capture carries {present}, not the five seeded argument-order probes. A capture '
            f'without the three STABLE ones cannot settle what the GET parameter array orders by; '
            f'a capture without the two VOLATILE ones leaves `required` measured only where a GET '
            f'also exists, and leaves ParameterOrder.UNKNOWN with no witness from a real database.'
        )

    def test_the_two_groups_are_the_same_functions_with_only_volatility_flipped(self) -> None:
        # ⚠ The one-variable design, asserted rather than trusted. Group 2 is worth having ONLY
        # because it differs from group 1 in exactly one respect: same argument names, same
        # declaration order, same defaultness -- so every difference in the captured document is
        # attributable to volatility and to nothing else. If someone "tidies" a probe's arguments,
        # this goes red before the conclusions drawn from the pair silently stop following.
        items = _public_rpc_items()
        for stable, volatile in (
            ('probe_two_required', 'probe_volatile_two_required'),
            ('probe_two_optional', 'probe_volatile_all_optional'),
        ):
            assert _READER._body_properties(items[stable]) == _READER._body_properties(items[volatile]), (
                f'{stable} and {volatile} no longer take the same arguments, so the pair no longer '
                f'isolates volatility as the single variable.'
            )
            assert _post_body_required(items[stable]) == _post_body_required(items[volatile]), (
                f'{stable} and {volatile} no longer agree on which arguments are defaulted.'
            )
            assert 'get' in items[stable] and 'get' not in items[volatile], (
                f'the volatility signal collapsed: PostgREST is meant to emit a GET for {stable} '
                f'(STABLE) and none for {volatile} (VOLATILE). Both, or neither, means the '
                f'observer changed -- v12.2.3 emits a GET for every function -- and every VOLATILE '
                f'conclusion in this class needs re-deriving against the new runtime.'
            )

    @pytest.mark.parametrize(('name', 'declared', 'alphabetical', 'required'), PROBES, ids=PROBE_IDS)
    def test_the_get_query_parameters_are_in_declaration_order(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str]
    ) -> None:
        item = _public_rpc_items()[name]
        assert 'get' in item, f'{name} stopped being STABLE; PostgREST emits a GET only for STABLE/IMMUTABLE'
        assert _READER._get_parameter_names(item) == declared
        assert declared != sorted(declared), (
            f'{name} was seeded with anti-alphabetical argument names ON PURPOSE. If its GET array '
            f'is sorted, the probe has been renamed or reordered and it no longer discriminates.'
        )

    @pytest.mark.parametrize(('name', 'declared', 'alphabetical', 'required'), PROBES, ids=PROBE_IDS)
    def test_the_post_body_properties_are_alphabetical_and_disagree_with_the_get(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str]
    ) -> None:
        item = _public_rpc_items()[name]
        body = _READER._body_properties(item)
        assert body == alphabetical
        assert body == sorted(body)
        assert body != _READER._get_parameter_names(item), (
            f'{name}: the POST body and the GET array agree, so this probe no longer shows that '
            f'the two encodings are different information.'
        )

    @pytest.mark.parametrize(('name', 'declared', 'alphabetical', 'required'), PROBES, ids=PROBE_IDS)
    def test_the_post_body_required_array_is_in_declaration_order(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str]
    ) -> None:
        # A second, PARTIAL order signal, and the only one a VOLATILE function exposes at all
        # (it has no GET): `required` lists the non-defaulted arguments in declaration order.
        # `probe_two_optional` shows the limit -- everything is defaulted, so the array is empty
        # and recovers nothing. That is why CI-078 splits STABLE from VOLATILE.
        item = _public_rpc_items()[name]
        assert _post_body_required(item) == required
        assert required == [argument for argument in declared if argument in required]

    def test_probe_mixed_rules_out_required_first_then_alphabetical(self) -> None:
        # THE falsification, stated as a comparison rather than an equality so a reader can see
        # what was ruled out. Both hypotheses predict an array for probe_mixed; they differ.
        query = _READER._get_parameter_names(_public_rpc_items()['probe_mixed'])
        required = _post_body_required(_public_rpc_items()['probe_mixed'])

        declaration_order = ['p_zulu', 'p_alpha', 'p_beta']
        required_first_then_alphabetical = sorted(required) + sorted({*declaration_order} - {*required})

        assert required_first_then_alphabetical == ['p_alpha', 'p_zulu', 'p_beta'], 'the rival hypothesis moved'
        assert query == declaration_order
        assert query != required_first_then_alphabetical
        assert query != sorted(query)

    @pytest.mark.parametrize(('name', 'declared', 'alphabetical', 'required'), PROBES, ids=PROBE_IDS)
    def test_castiron_records_the_declaration_order_for_every_probe(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str]
    ) -> None:
        # ⚠ **This used to pin a DEFECT and now pins its FIX.** Before `CI-078` castiron read the
        # POST body and reported `probe_mixed` as (p_alpha, p_beta, p_zulu) while the SAME document
        # said it is declared (p_zulu, p_alpha, p_beta) -- both arrays in hand, and castiron kept
        # the wrong one. It now keeps the right one, and the expectation is the `declared` column.
        #
        # The `alphabetical` column is still asserted, as the thing castiron is NOT doing: an
        # equality against `declared` alone would pass just as happily if the probes were renamed
        # into alphabetical order and the recovery were deleted.
        rows = parse_openapi_document(_READER._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))
        assert [parameter[0] for parameter in function(rows, name)[7]] == declared
        assert declared != alphabetical, f'{name} no longer discriminates: its two orders agree'
        # All three probes are STABLE, so the GET operation establishes the order in FULL -- even
        # `probe_two_optional`, whose `required` array is empty and recovers nothing on its own.
        assert function(rows, name)[8] is ParameterOrder.DECLARED

    @pytest.mark.parametrize(
        ('name', 'declared', 'alphabetical', 'required', 'state'), VOLATILE_PROBES, ids=VOLATILE_PROBE_IDS
    )
    def test_a_volatile_probe_carries_no_get_and_an_alphabetical_body(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str], state: ParameterOrder
    ) -> None:
        # The premise every group-2 conclusion rests on, asserted at the DOCUMENT level so a
        # failure separates "PostgREST changed" from "castiron changed". Two claims: a VOLATILE
        # function gets no GET operation (v14.14 -- the pinned v12.2.3 profile emits one for
        # everything, which is a finding recorded in the testbed README, not a thing to work
        # around here), and its POST body object is alphabetical exactly as a STABLE one's is.
        item = _public_rpc_items()[name]
        assert 'get' not in item, (
            f'{name} carries a GET operation, so it is no longer VOLATILE (or the server is not '
            f'PostgREST v14.14). "No GET" is this document format\'s ONLY volatility signal.'
        )
        body = _READER._body_properties(item)
        assert body == alphabetical
        assert body == sorted(body), 'volatility does not change the body object; it is still name-sorted'
        assert body != declared, f'{name} was seeded anti-alphabetically so the two readings disagree'

    @pytest.mark.parametrize(
        ('name', 'declared', 'alphabetical', 'required'),
        [probe[:4] for probe in VOLATILE_PROBES],
        ids=VOLATILE_PROBE_IDS,
    )
    def test_the_required_array_is_the_only_order_a_volatile_probe_can_carry(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str]
    ) -> None:
        # 🔴 **THE measurement CI-140 exists for.** `required` was believed to be declaration
        # order, but had only ever been checked on functions that ALSO carry a GET -- i.e. where
        # the belief is not load-bearing. `probe_volatile_two_required` is the first captured
        # function where it is the entire answer, and it is seeded anti-alphabetically so the two
        # rival readings disagree. If a PostgREST upgrade ever alphabetizes this array, castiron's
        # ParameterOrder.DECLARED for a volatile mutation becomes a SILENT LIE -- re-derive
        # `_declaration_order`'s rule 3, do not adjust this expectation.
        item = _public_rpc_items()[name]
        assert _post_body_required(item) == required
        assert required == declared[: len(required)], (
            f'{name}: `required` is {required}, which is not a leading run of the declaration '
            f'order {declared}. Rule 3 (`required` is a declaration-order PREFIX) is unsound.'
        )
        if required:
            assert required != sorted(required), (
                f'{name} was seeded so `required` is anti-alphabetical ON PURPOSE. A sorted array '
                f'here is satisfied by both readings and measures nothing.'
            )

    def test_the_all_optional_volatile_probe_is_the_live_witness_for_unknown(self) -> None:
        # ⚠ **The only witness in the whole corpus for a public enum member shipped in 0.3.0.**
        # Both properties are load-bearing and the seed's own comment says so: give either
        # argument no default and `required` recovers a prefix (DECLARED/DECLARED_PREFIX); drop to
        # one argument and a one-element list is trivially in declaration order (DECLARED). Either
        # edit deletes the witness while leaving something that still looks like a probe, so both
        # are asserted here against the captured bytes.
        item = _public_rpc_items()['probe_volatile_all_optional']
        body = _READER._body_properties(item)
        assert 'get' not in item
        assert len(body) >= 2, 'a one-argument list is trivially in declaration order; UNKNOWN is unreachable'
        assert _post_body_required(item) == [], (
            'a `required` array came back, so the order is recoverable again and this stops being the UNKNOWN witness.'
        )

        # ⚠ ABSENT, not present-and-empty -- and the distinction is why the synthetic sibling test
        # (`..._with_every_argument_defaulted_establishes_nothing`, which spells `required: []`
        # literally) is kept rather than deleted as a duplicate. castiron must reach UNKNOWN from
        # BOTH encodings, and only this one is what a real PostgREST v14.14 emits. A parser that
        # read `schema['required']` without a default would raise KeyError on the real document
        # and pass happily on the synthetic one.
        schema = next(p for p in item['post']['parameters'] if p.get('in') == 'body')['schema']
        assert 'required' not in schema, (
            'PostgREST now emits an explicit empty `required` for an all-optional function. That '
            'is a change in the source, not in castiron -- record it before regenerating anything.'
        )

    @pytest.mark.parametrize(
        ('name', 'declared', 'alphabetical', 'required', 'state'), VOLATILE_PROBES, ids=VOLATILE_PROBE_IDS
    )
    def test_castiron_reports_the_measured_state_for_every_volatile_probe(
        self, name: str, declared: list[str], alphabetical: list[str], required: list[str], state: ParameterOrder
    ) -> None:
        # What castiron makes of group 2, and the pair is what makes each half fallible. Same
        # arguments, same declaration order, same volatility, differing only in defaultness:
        #
        #   probe_volatile_two_required -> [p_zebra, p_alpha]  DECLARED   (recovered from `required`)
        #   probe_volatile_all_optional -> [p_alpha, p_zebra]  UNKNOWN    (nothing to recover from)
        #
        # A parser that ignored `required` would report the alphabetical list for BOTH; one that
        # over-claimed would report DECLARED for both. Only the real rule produces this table.
        rows = parse_openapi_document(_READER._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))
        expected = declared if state is ParameterOrder.DECLARED else alphabetical
        assert [parameter[0] for parameter in function(rows, name)[7]] == expected
        assert function(rows, name)[8] is state
        assert declared != alphabetical, f'{name} no longer discriminates: its two orders agree'

    def test_the_capture_witnesses_every_parameter_order_member(self) -> None:
        # ⚠ The census, as an EQUALITY over the enum's members rather than a spot check. Adding a
        # fourth ParameterOrder member fails this test until someone decides -- deliberately --
        # whether the corpus can witness it. Before the CI-140 recapture UNKNOWN was absent here
        # and reachable only from a hand-written document, which is precisely the CI-076 posture
        # this corpus exists to avoid: a claim about a real source resting on synthetic bytes.
        rows = parse_openapi_document(_READER._document(CORPUS_INPUTS / 'testbed-public.openapi.json'))
        census: dict[ParameterOrder, list[str]] = {}
        for row in rows.function_details:
            census.setdefault(row[8], []).append(row[1])

        assert set(census) == set(ParameterOrder), (
            f'the public capture no longer witnesses every ParameterOrder member. Present: '
            f'{sorted(state.name for state in census)}; missing: '
            f'{sorted(state.name for state in set(ParameterOrder) - set(census))}.'
        )
        assert census[ParameterOrder.UNKNOWN] == ['probe_volatile_all_optional']
        assert census[ParameterOrder.DECLARED_PREFIX] == ['create_order']
        assert len(census[ParameterOrder.DECLARED]) == 14


# ---------------------------------------------------------------------------
# Determinism (Hard Rule #9 -- the ``check`` drift-guard depends on this).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeterminism:
    def test_parsing_the_same_document_twice_is_identical(self, document: dict[str, Any]) -> None:
        assert parse_openapi_document(document) == parse_openapi_document(document)

    def test_parsing_a_key_reordered_copy_is_identical(self, document: dict[str, Any]) -> None:
        # PostgREST builds ``definitions`` and ``paths`` from a Haskell hash map, so their
        # document order is NOT contractual -- castiron sorts both. A table's ``properties`` order
        # is preserved as given, because it is real (pg ordinal). A function's is NOT preserved:
        # it is only ALPHABETICAL, never argument position, so `CI-078` reorders it out of the
        # document's two ORDERED encodings (the GET array and the POST body's `required`). Both of
        # those are read positionally from lists, so this determinism claim covers them too.
        reordered = reorder_keys(document)
        assert list(reordered['definitions']) != list(document['definitions'])
        assert list(reordered['paths']) != list(document['paths'])
        assert parse_openapi_document(reordered) == parse_openapi_document(document)

    def test_tables_are_emitted_in_sorted_key_order(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        seen: list[str] = []
        for row in rows.column_details:
            if row[1] not in seen:
                seen.append(row[1])
        assert seen == sorted(document['definitions'])

    def test_columns_keep_their_document_order(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        users = [row[2] for row in rows.column_details if row[1] == 'users']
        assert users == list(document['definitions']['users']['properties'])


# ---------------------------------------------------------------------------
# Table-level SQL comments (CI-009).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTableDescriptions:
    """``definitions.<t>.description`` -> the ``table_details`` 3-tuple contract."""

    def test_fixture_yields_one_row_per_parsed_table(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        table_names = sorted({row[1] for row in rows.column_details})

        assert [row[1] for row in rows.table_details] == table_names

    def test_fixture_descriptions_match_the_document_verbatim(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        parsed = {name: description for _, name, description in rows.table_details}

        assert parsed['users'] == document['definitions']['users']['description']
        assert parsed['orders'] == document['definitions']['orders']['description']
        assert parsed['active_users_view'] == document['definitions']['active_users_view']['description']
        assert parsed == {
            'active_users_view': 'Users with a recent login.',
            'order_items': None,
            'orders': 'Customer orders.',
            'products': None,
            'restricted_table': None,
            'users': 'Application users.',
        }

    def test_exactly_three_tables_carry_a_comment(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)

        assert sum(1 for _, _, description in rows.table_details if description is not None) == 3

    def test_rows_are_in_sorted_table_name_order(self, document: dict[str, Any]) -> None:
        """Determinism (Hard Rule #9): ``definitions`` is a Haskell hash map upstream."""
        rows = parse_openapi_document(document)
        names = [name for _, name, _ in rows.table_details]

        assert names == sorted(names)

    def test_a_key_reordered_document_yields_identical_table_rows(self, document: dict[str, Any]) -> None:
        assert parse_openapi_document(reorder_keys(document)).table_details == (
            parse_openapi_document(document).table_details
        )

    def test_every_row_carries_the_schema(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document, schema='audit')

        assert {row[0] for row in rows.table_details} == {'audit'}

    def test_rows_are_three_tuples(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)

        assert all(len(row) == 3 for row in rows.table_details)

    def test_a_definition_without_a_description_still_produces_a_row(self) -> None:
        """One row per parsed table keeps the contract uniform."""
        rows = parse_openapi_document(minimal_document({'id': {'type': 'integer', 'format': 'int32'}}))

        assert rows.table_details == (('public', 't', None),)

    def test_a_description_is_carried_verbatim(self) -> None:
        """The parser normalizes nothing -- the builder owns that rule."""
        document = minimal_document(
            {'id': {'type': 'integer', 'format': 'int32'}},
            description='  Windows.\r\nSecond line.  ',
        )
        rows = parse_openapi_document(document)

        assert rows.table_details == (('public', 't', '  Windows.\r\nSecond line.  '),)

    @pytest.mark.parametrize('value', [42, 0, True, ['a'], {'a': 1}, None])
    def test_a_non_string_description_is_not_mistaken_for_one(self, value: object) -> None:
        document = minimal_document({'id': {'type': 'integer', 'format': 'int32'}}, description=value)
        rows = parse_openapi_document(document)

        assert rows.table_details == (('public', 't', None),)

    def test_a_definition_with_no_properties_produces_no_table_row(self) -> None:
        """A skipped definition contributes no table, so it must contribute no row either."""
        document = {
            'swagger': '2.0',
            'definitions': {
                'real': {'type': 'object', 'properties': {'id': {'format': 'int32'}}, 'description': 'Kept.'},
                'empty': {'type': 'object', 'properties': {}, 'description': 'Dropped.'},
            },
            'paths': {},
        }
        rows = parse_openapi_document(document)

        assert rows.table_details == (('public', 'real', 'Kept.'),)

    def test_a_non_object_definition_produces_no_table_row(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {
                'real': {'type': 'object', 'properties': {'id': {'format': 'int32'}}, 'description': 'Kept.'},
                'bogus': 'not an object',
            },
            'paths': {},
        }
        rows = parse_openapi_document(document)

        assert rows.table_details == (('public', 'real', 'Kept.'),)

    def test_the_other_row_contracts_are_unchanged(self, document: dict[str, Any]) -> None:
        """CI-074: prove the additive row did not disturb what it was not meant to touch.

        The six pre-CI-009 tuples are compared against the values the existing CI-005 tests
        already assert for this fixture, so a change to any of them fails here too.
        """
        rows = parse_openapi_document(document)

        assert len(rows.column_details) == 27
        assert len(rows.fk_details) == 5
        assert len(rows.constraints) == 11
        assert len(rows.enum_types) == 1
        assert len(rows.enum_type_mapping) == 2
        assert len(rows.function_details) == 4

    def test_table_details_defaults_to_empty(self) -> None:
        """``OpenApiRows`` gained a defaulted field, so existing construction still works."""
        from castiron.sources.openapi.parse import OpenApiRows

        assert OpenApiRows().table_details == ()
