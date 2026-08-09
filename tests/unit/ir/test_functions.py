"""Unit tests for the IR's function/RPC model and its build-layer normalization.

The nodes are a deliberate build-ahead (captain decision CI5-D1): nothing in ``src/``
consumes ``Schema.functions`` until CI-012, so these tests are the contract CI-011 must
*enrich* rather than redesign.
"""

import json

import pytest

from castiron.ir import (
    EnumInfo,
    FunctionInfo,
    FunctionVolatility,
    ParameterInfo,
    ParameterMode,
    ParameterOrder,
    Row,
    Schema,
    build_schema,
    construct_functions,
)
from castiron.ir.build import (
    PARAMETER_MODE_MAP,
    VOLATILITY_MAP,
    normalize_parameter_mode,
    normalize_volatility,
    split_type_name,
)


def parameter_row(
    name: str,
    raw_type: str,
    raw_mode: str | None = None,
    has_default: bool = False,
    array_element_type: str | None = None,
) -> Row:
    """Build a parameter 5-tuple."""
    return (name, raw_type, raw_mode, has_default, array_element_type)


def function_row(
    name: str,
    *,
    schema: str = 'public',
    description: str | None = None,
    return_type: str | None = None,
    returns_set: bool | None = None,
    raw_volatility: str | None = None,
    is_read_only: bool | None = None,
    parameters: list[Row] | None = None,
    parameter_order: ParameterOrder = ParameterOrder.UNKNOWN,
) -> Row:
    """Build a function 9-tuple.

    ``parameter_order`` defaults to ``UNKNOWN`` -- the conservative value, matching
    :class:`FunctionInfo`'s own default, so a row that says nothing claims nothing.
    """
    return (
        schema,
        name,
        description,
        return_type,
        returns_set,
        raw_volatility,
        is_read_only,
        parameters or [],
        parameter_order,
    )


# ---------------------------------------------------------------------------
# The nodes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFunctionNodes:
    def test_parameter_defaults(self) -> None:
        param = ParameterInfo(name='user_id', raw_type='integer')
        assert param.mode is ParameterMode.IN
        assert param.has_default is False
        assert param.array_element_type is None
        assert param.enum_info is None
        assert str(param) == 'ParameterInfo(user_id, integer)'

    def test_function_defaults_are_tri_state(self) -> None:
        function = FunctionInfo(name='get_user_stats')
        assert function.schema == 'public'
        assert function.parameters == []
        # "Unknown is None, never a guess" -- the Q-2 posture.
        assert function.return_type is None
        assert function.returns_set is None
        assert function.volatility is None
        assert function.is_read_only is None
        assert function.description is None
        # `UNKNOWN` is the same posture in enum clothing: a hand-built node makes NO claim about
        # its parameter order, so a consumer that forgets to look gets keyword-only arguments.
        assert function.parameter_order is ParameterOrder.UNKNOWN
        assert str(function) == 'FunctionInfo(public.get_user_stats)'

    def test_the_nodes_are_mutable_like_every_other_ir_node(self) -> None:
        function = FunctionInfo(name='f')
        function.parameters.append(ParameterInfo(name='a', raw_type='text'))
        function.volatility = FunctionVolatility.IMMUTABLE
        assert function.parameters[0].name == 'a'
        assert function.volatility is FunctionVolatility.IMMUTABLE

    def test_the_enums_are_string_enums(self) -> None:
        assert FunctionVolatility.VOLATILE == 'VOLATILE'
        assert ParameterMode.INOUT == 'INOUT'
        assert [member.value for member in FunctionVolatility] == ['VOLATILE', 'STABLE', 'IMMUTABLE']
        assert [member.value for member in ParameterMode] == ['IN', 'OUT', 'INOUT', 'VARIADIC', 'TABLE']
        assert ParameterOrder.DECLARED_PREFIX == 'DECLARED_PREFIX'
        assert [member.value for member in ParameterOrder] == ['DECLARED', 'DECLARED_PREFIX', 'UNKNOWN']


# ---------------------------------------------------------------------------
# Raw-code normalization (mirrors CONSTRAINT_TYPE_MAP, decision D3).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalization:
    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            ('v', FunctionVolatility.VOLATILE),
            ('s', FunctionVolatility.STABLE),
            ('i', FunctionVolatility.IMMUTABLE),
            ('V', FunctionVolatility.VOLATILE),
            ('z', None),
            (None, None),
        ],
    )
    def test_normalize_volatility(self, raw: str | None, expected: FunctionVolatility | None) -> None:
        assert normalize_volatility(raw) is expected

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            ('i', ParameterMode.IN),
            ('o', ParameterMode.OUT),
            ('b', ParameterMode.INOUT),
            ('v', ParameterMode.VARIADIC),
            ('t', ParameterMode.TABLE),
            ('T', ParameterMode.TABLE),
            ('z', ParameterMode.IN),
            (None, ParameterMode.IN),
        ],
    )
    def test_normalize_parameter_mode(self, raw: str | None, expected: ParameterMode) -> None:
        assert normalize_parameter_mode(raw) is expected

    def test_the_maps_cover_the_pg_vocabulary(self) -> None:
        assert set(VOLATILITY_MAP) == {'v', 's', 'i'}
        assert set(PARAMETER_MODE_MAP) == {'i', 'o', 'b', 'v', 't'}

    @pytest.mark.parametrize(
        ('given', 'expected'),
        [
            ('order_status', (None, 'order_status')),
            ('_order_status', (None, 'order_status')),
            ('__order_status', (None, 'order_status')),
            ('order_status[]', (None, 'order_status')),
            ('_order_status[]', (None, 'order_status')),
            ('"FourthType"', (None, 'FourthType')),
            # The namespace is preserved, not discarded -- it is the ONLY thing that tells
            # ``public.status`` apart from ``audit.status``.
            ('public.order_status', ('public', 'order_status')),
            ('audit.order_status[]', ('audit', 'order_status')),
            ('test.schema.order_status', ('test.schema', 'order_status')),
            ('', (None, '')),
        ],
    )
    def test_split_type_name(self, given: str, expected: tuple[str | None, str]) -> None:
        assert split_type_name(given) == expected


# ---------------------------------------------------------------------------
# construct_functions.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConstructFunctions:
    def test_no_rows_yields_no_functions(self) -> None:
        assert construct_functions([]) == []

    def test_a_row_maps_field_for_field(self) -> None:
        functions = construct_functions(
            [
                function_row(
                    'get_user_stats',
                    description='Stats.',
                    raw_volatility='s',
                    is_read_only=True,
                    parameters=[parameter_row('user_id', 'integer'), parameter_row('since', 'date', has_default=True)],
                )
            ]
        )
        assert len(functions) == 1
        function = functions[0]
        assert function.name == 'get_user_stats'
        assert function.schema == 'public'
        assert function.description == 'Stats.'
        assert function.volatility is FunctionVolatility.STABLE
        assert function.is_read_only is True
        assert [(p.name, p.raw_type, p.has_default) for p in function.parameters] == [
            ('user_id', 'integer', False),
            ('since', 'date', True),
        ]

    def test_the_row_schema_wins_over_the_fallback(self) -> None:
        functions = construct_functions([function_row('f', schema='api')], schema='public')
        assert functions[0].schema == 'api'

    def test_a_blank_row_schema_falls_back_to_the_argument(self) -> None:
        functions = construct_functions([function_row('f', schema='')], schema='api')
        assert functions[0].schema == 'api'

    def test_a_none_parameter_list_is_tolerated(self) -> None:
        row = ('public', 'f', None, None, None, None, None, None, ParameterOrder.UNKNOWN)
        assert construct_functions([row])[0].parameters == []

    def test_parameter_modes_are_normalized(self) -> None:
        functions = construct_functions(
            [function_row('f', parameters=[parameter_row('terms', 'text[]', 'v', array_element_type='text')])]
        )
        param = functions[0].parameters[0]
        assert param.mode is ParameterMode.VARIADIC
        assert param.array_element_type == 'text'

    def test_an_enum_parameter_links_to_the_registry(self) -> None:
        enums = [EnumInfo(name='order_status', values=['a', 'b'])]
        functions = construct_functions(
            [function_row('f', parameters=[parameter_row('status', 'order_status')])], enums=enums
        )
        assert functions[0].parameters[0].enum_info is enums[0]

    def test_a_qualified_or_array_enum_token_still_links(self) -> None:
        enums = [EnumInfo(name='order_status', values=['a'])]
        functions = construct_functions(
            [
                function_row(
                    'f',
                    parameters=[
                        parameter_row('a', 'public.order_status'),
                        parameter_row('b', 'order_status[]', array_element_type='order_status'),
                    ],
                )
            ],
            enums=enums,
        )
        assert [p.enum_info for p in functions[0].parameters] == [enums[0], enums[0]]

    def test_a_qualified_token_picks_the_enum_from_its_own_schema(self) -> None:
        # Two enums may share a bare name across schemas; matching on the name alone
        # silently linked the parameter to the wrong member list.
        public_status = EnumInfo(name='status', values=['open', 'closed'], schema='public')
        audit_status = EnumInfo(name='status', values=['created', 'deleted'], schema='audit')
        functions = construct_functions(
            [
                function_row(
                    'f',
                    parameters=[
                        parameter_row('p', 'audit.status'),
                        parameter_row('q', 'public.status'),
                        parameter_row('r', 'audit.status[]', array_element_type='audit.status'),
                    ],
                )
            ],
            enums=[public_status, audit_status],
        )
        assert [p.enum_info for p in functions[0].parameters] == [audit_status, public_status, audit_status]

    def test_a_qualified_token_for_an_unknown_schema_stays_unlinked(self) -> None:
        # "Unknown is None, never a guess": a token that names a schema castiron has no
        # enum for must not silently fall back to a same-named enum elsewhere.
        functions = construct_functions(
            [function_row('f', parameters=[parameter_row('p', 'other.status')])],
            enums=[EnumInfo(name='status', values=['open'], schema='public')],
        )
        assert functions[0].parameters[0].enum_info is None

    def test_a_bare_token_still_links_by_name(self) -> None:
        enums = [EnumInfo(name='status', values=['open'], schema='public')]
        functions = construct_functions([function_row('f', parameters=[parameter_row('p', 'status')])], enums=enums)
        assert functions[0].parameters[0].enum_info is enums[0]

    def test_an_unknown_enum_token_stays_unlinked(self) -> None:
        functions = construct_functions(
            [function_row('f', parameters=[parameter_row('status', 'order_status')])],
            enums=[EnumInfo(name='other', values=['a'])],
        )
        assert functions[0].parameters[0].enum_info is None

    @pytest.mark.parametrize('token', ['', '_', '[]'])
    def test_a_token_that_normalizes_to_nothing_never_links(self, token: str) -> None:
        functions = construct_functions(
            [function_row('f', parameters=[parameter_row('a', token)])], enums=[EnumInfo(name='x', values=[])]
        )
        assert functions[0].parameters[0].enum_info is None

    def test_function_order_is_row_order(self) -> None:
        functions = construct_functions([function_row('z'), function_row('a')])
        assert [f.name for f in functions] == ['z', 'a']

    @pytest.mark.parametrize('state', list(ParameterOrder), ids=[member.value for member in ParameterOrder])
    def test_the_parameter_order_state_round_trips_from_row_element_8(self, state: ParameterOrder) -> None:
        # Every member, not a sample (CI-072): the build layer must be a pass-through for all
        # three, including the two no OpenAPI capture currently produces.
        functions = construct_functions([function_row('f', parameter_order=state)])
        assert functions[0].parameter_order is state

    def test_construct_functions_does_not_reorder_parameters(self) -> None:
        # Ordering is the SOURCE's responsibility (Hard Rule #9, and this function's docstring
        # says so). CI-078 moved the reorder into `sources/openapi/parse.py`; if it ever leaks
        # down here, two layers would be deciding one fact and a source that already knows the
        # right order could not express it.
        rows = [
            function_row(
                'f',
                parameters=[
                    parameter_row('p_zulu', 'text'),
                    parameter_row('p_alpha', 'text'),
                    parameter_row('p_beta', 'text', has_default=True),
                ],
                parameter_order=ParameterOrder.DECLARED,
            )
        ]
        assert [p.name for p in construct_functions(rows)[0].parameters] == ['p_zulu', 'p_alpha', 'p_beta']

    def test_a_row_that_says_nothing_leaves_the_conservative_default(self) -> None:
        # The 9th element is not optional at the boundary -- `function_row` supplies it -- but a
        # source that has established nothing says so with UNKNOWN rather than omitting it.
        assert construct_functions([function_row('f')])[0].parameter_order is ParameterOrder.UNKNOWN


# ---------------------------------------------------------------------------
# build_schema integration + backward compatibility.
# ---------------------------------------------------------------------------


COLUMNS: list[Row] = [
    ('public', 'users', 'id', None, 'NO', 'integer', None, 'BASE TABLE', None, None, None, None),
    ('public', 'users', 'status', None, 'YES', 'order_status', None, 'BASE TABLE', None, None, None, None),
]
CONSTRAINTS: list[Row] = [('users_pkey', 'users', ['id'], 'p', None, False)]
ENUM_TYPES: list[Row] = [('order_status', 'public', '', 'E', True, 'e', ['pending', 'shipped'])]
ENUM_MAPPING: list[Row] = [('status', 'users', 'public', 'order_status', 'E', '')]


@pytest.mark.unit
class TestBuildSchemaIntegration:
    def test_function_details_is_optional_and_defaults_to_empty(self) -> None:
        # The backward-compat contract: every pre-CI-005 call site still works untouched.
        schema = build_schema(COLUMNS, [], CONSTRAINTS, ENUM_TYPES, ENUM_MAPPING)
        assert schema.functions == []

    def test_function_details_populates_schema_functions(self) -> None:
        schema = build_schema(
            COLUMNS,
            [],
            CONSTRAINTS,
            ENUM_TYPES,
            ENUM_MAPPING,
            function_details=[
                function_row('f', raw_volatility='v', is_read_only=False, parameters=[parameter_row('a', 'integer')])
            ],
        )
        assert [f.name for f in schema.functions] == ['f']
        assert schema.functions[0].volatility is FunctionVolatility.VOLATILE

    def test_parameters_link_to_the_schemas_enum_registry(self) -> None:
        schema = build_schema(
            COLUMNS,
            [],
            CONSTRAINTS,
            ENUM_TYPES,
            ENUM_MAPPING,
            function_details=[function_row('f', parameters=[parameter_row('status', 'order_status')])],
        )
        assert schema.enums
        assert schema.functions[0].parameters[0].enum_info is schema.enums[0]

    def test_the_positional_signature_is_unchanged_through_argument_seven(self) -> None:
        # ``function_details`` is appended LAST precisely so this keeps working.
        schema = build_schema(COLUMNS, [], CONSTRAINTS, ENUM_TYPES, ENUM_MAPPING, 'public', False)
        assert [t.name for t in schema.tables] == ['users']
        assert schema.functions == []

    def test_building_twice_is_deterministic(self) -> None:
        rows = [function_row('b'), function_row('a', parameters=[parameter_row('x', 'text')])]
        first = build_schema(COLUMNS, [], CONSTRAINTS, ENUM_TYPES, ENUM_MAPPING, function_details=rows)
        second = build_schema(COLUMNS, [], CONSTRAINTS, ENUM_TYPES, ENUM_MAPPING, function_details=rows)
        assert first == second
        assert json.dumps(first.as_dict()) == json.dumps(second.as_dict())

    def test_the_schema_node_still_constructs_positionally(self) -> None:
        schema = Schema([], [])
        assert schema.functions == []
