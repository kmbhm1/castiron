"""Unit tests for the pure PostgREST OpenAPI parser.

Every test here loads (or builds) a plain ``dict`` -- **no test opens a socket and no test
mocks HTTP**, which is the whole point of the fetch/parse split.
"""

import copy
from typing import Any

import pytest

from castiron.ir import Row
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
        assert ('public', 'orders', 'user_id', 'public', 'users', 'id', 'orders_user_id_fkey') in rows.fk_details


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
        assert primary_keys['users'] == ('users_pkey', 'users', ['id'], 'p', None)
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
        ) in foreign_keys
        assert len(foreign_keys) == len(rows.fk_details)

    def test_unique_check_and_exclude_constraints_are_never_produced(self, document: dict[str, Any]) -> None:
        # The fidelity floor: the document contains none of these, anywhere.
        rows = parse_openapi_document(document)
        assert {row[3] for row in rows.constraints} == {'p', 'f'}

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

    def test_a_view_gets_no_primary_key_row(self, document: dict[str, Any]) -> None:
        # ``TableInfo.primary_key()`` is empty for a VIEW by definition, so synthesizing a
        # PK constraint would leave ``col.primary`` and ``primary_key()`` disagreeing.
        rows = parse_openapi_document(document)
        view_constraints = [row for row in rows.constraints if row[1] == 'active_users_view']
        assert [row[3] for row in view_constraints] == ['f']


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
    @pytest.mark.parametrize(
        ('path_item', 'definition', 'expected'),
        [
            ({'get': {}, 'post': {}}, {'required': ['id']}, 'BASE TABLE'),
            ({'get': {}, 'post': {}}, {}, 'BASE TABLE'),
            ({'get': {}}, {'required': ['id']}, 'BASE TABLE'),
            ({'get': {}}, {}, 'VIEW'),
        ],
    )
    def test_only_read_only_and_all_nullable_is_a_view(
        self, path_item: dict[str, Any], definition: dict[str, Any], expected: str
    ) -> None:
        assert classify_table_type('t', definition, {'/t': path_item}) == expected

    def test_an_empty_required_array_does_not_count_as_a_signal(self) -> None:
        assert classify_table_type('t', {'required': []}, {'/t': {'get': {}}}) == 'VIEW'

    def test_a_missing_path_item_is_treated_as_read_only(self) -> None:
        assert classify_table_type('t', {}, {}) == 'VIEW'

    @pytest.mark.parametrize('method', ['post', 'patch', 'delete'])
    def test_any_write_method_proves_a_base_table(self, method: str) -> None:
        assert classify_table_type('t', {}, {'/t': {'get': {}, method: {}}}) == 'BASE TABLE'

    def test_the_fixture_classifies_the_view_and_the_read_only_table(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert column(rows, 'active_users_view', 'id')[7] == 'VIEW'
        # GET-only but has NOT NULL columns -> stays a BASE TABLE (biased heuristic).
        assert column(rows, 'restricted_table', 'id')[7] == 'BASE TABLE'


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

    def test_parameter_order_is_preserved_and_has_default_comes_from_required(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'get_user_stats')[7] == [
            ('user_id', 'integer', None, False, None),
            ('since', 'date', None, True, None),
        ]

    def test_parameter_types_are_normalized_like_columns(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'create_order')[7] == [
            ('user_id', 'bigint', None, False, None),
            ('status', 'order_status', None, False, None),
            ('items', 'text[]', None, True, 'text'),
        ]

    def test_a_no_argument_function_yields_an_empty_parameter_list(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'ping')[7] == []

    def test_a_variadic_argument_is_only_visible_through_the_get_operation(self, document: dict[str, Any]) -> None:
        rows = parse_openapi_document(document)
        assert function(rows, 'search_products')[7] == [
            ('terms', 'text[]', 'v', False, 'text'),
            ('limit_to', 'integer', None, True, None),
        ]

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
# Determinism (Hard Rule #9 -- the ``check`` drift-guard depends on this).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDeterminism:
    def test_parsing_the_same_document_twice_is_identical(self, document: dict[str, Any]) -> None:
        assert parse_openapi_document(document) == parse_openapi_document(document)

    def test_parsing_a_key_reordered_copy_is_identical(self, document: dict[str, Any]) -> None:
        # PostgREST builds ``definitions`` and ``paths`` from a Haskell hash map, so their
        # document order is NOT contractual -- castiron sorts both. Column and parameter
        # order, by contrast, is real (pg ordinal / argument position) and is preserved.
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
