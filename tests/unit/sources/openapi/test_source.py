"""End-to-end tests for the OpenAPI source: document -> Schema IR -> emitted Pydantic models.

This is the Phase-0 exit criterion minus the network. It also pins the *fidelity floor* as
a tested contract (what this source structurally cannot see) and the backward-compatibility
guarantee that a zero-function schema still emits byte-identically.
"""

import ast
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from castiron.emitters.config import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
from castiron.ir import (
    ConstraintType,
    FunctionVolatility,
    ParameterMode,
    ParameterOrder,
    RelationType,
    Schema,
    build_schema,
)
from castiron.sources import (
    SourceError,
    SourceFetchError,
    SourceParseError,
    build_schema_from_document,
    load_openapi_schema,
)
from castiron.sources.openapi import fetch as fetch_module
from castiron.sources.openapi.parse import parse_openapi_document
from tests.unit.sources.openapi.conftest import GOLDEN_DIR

SOURCE_DIR = Path(str(fetch_module.__file__)).parent


def table(schema: Schema, name: str) -> Any:
    """Return the table named ``name``."""
    return next(t for t in schema.tables if t.name == name)


def col(schema: Schema, table_name: str, column_name: str) -> Any:
    """Return the column named ``column_name`` on ``table_name``."""
    return next(c for c in table(schema, table_name).columns if c.name == column_name)


def emit(schema: Schema) -> str:
    """Render ``schema`` through the Pydantic emitter and return the single file's text."""
    files = PydanticEmitter(EmitterConfig()).emit(schema)
    assert len(files) == 1
    return files[0].content


# ---------------------------------------------------------------------------
# The IR the source produces.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSchemaShape:
    def test_tables_are_alphabetical(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        names = [t.name for t in schema.tables]
        assert names == sorted(names)
        assert names == [
            'active_users_view',
            'order_items',
            'orders',
            'products',
            'restricted_table',
            'users',
        ]

    def test_table_types_follow_the_two_signal_heuristic(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert table(schema, 'active_users_view').table_type == 'VIEW'
        assert table(schema, 'restricted_table').table_type == 'BASE TABLE'
        assert table(schema, 'users').table_type == 'BASE TABLE'

    def test_primary_key_and_foreign_key_flags_propagate(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert col(schema, 'users', 'id').primary is True
        assert col(schema, 'orders', 'user_id').is_foreign_key is True
        assert table(schema, 'orders').primary_key() == ['id']
        assert table(schema, 'order_items').primary_key() == ['order_id', 'product_id']
        assert table(schema, 'order_items').primary_is_composite() is True

    def test_the_bridge_table_is_detected(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert table(schema, 'order_items').is_bridge is True
        assert table(schema, 'orders').is_bridge is False

    def test_relationships_are_derived_from_the_markers(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        related = {(r.related_table_name, r.relation_type) for r in table(schema, 'users').relationships}
        assert related == {('orders', RelationType.ONE_TO_MANY)}
        assert {fk.foreign_table_name for fk in table(schema, 'order_items').foreign_keys} == {'orders', 'products'}

    def test_synthesized_foreign_key_constraint_names(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        names = {fk.constraint_name for fk in table(schema, 'orders').foreign_keys}
        assert 'orders_user_id_fkey' in names

    def test_enums_are_deduplicated_and_sorted(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert [(e.schema, e.name) for e in schema.enums] == [('public', 'order_status')]
        assert schema.enums[0].values == ['pending', 'shipped', 'cancelled']

    def test_an_enum_array_column_links_through_its_element_type(self, document: dict[str, Any]) -> None:
        # The document carries no labels for ``order_status[]``; it links only because the
        # same enum appears on a scalar column elsewhere.
        labels = col(schema_of(document), 'orders', 'labels')
        assert labels.array_element_type == 'order_status'
        assert labels.enum_info is not None
        assert labels.enum_info.name == 'order_status'

    def test_an_unlinked_enum_array_degrades_to_an_untyped_list(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {
                't': {
                    'properties': {'labels': {'format': 'lonely_enum[]', 'type': 'array', 'items': {'type': 'string'}}}
                }
            },
            'paths': {'/t': {'get': {}, 'post': {}}},
        }
        schema = build_schema_from_document(document)
        labels = col(schema, 't', 'labels')
        assert labels.array_element_type == 'lonely_enum'
        assert labels.enum_info is None
        assert 'labels: list[Any] | None' in emit(schema)

    def test_an_enum_array_links_even_when_its_table_sorts_first(self) -> None:
        # The cross-link must not depend on the scalar column's table being parsed first.
        # ``aaa`` (the array) sorts before ``zzz`` (the scalar that supplies the labels).
        document = {
            'swagger': '2.0',
            'definitions': {
                'aaa': {'properties': {'labels': {'format': 'mood[]', 'type': 'array', 'items': {'type': 'string'}}}},
                'zzz': {'properties': {'mood': {'format': 'mood', 'type': 'string', 'enum': ['happy', 'sad']}}},
            },
            'paths': {'/aaa': {'get': {}, 'post': {}}, '/zzz': {'get': {}, 'post': {}}},
        }
        schema = build_schema_from_document(document)
        labels = col(schema, 'aaa', 'labels')
        assert labels.enum_info is not None
        assert labels.enum_info.values == ['happy', 'sad']
        assert 'labels: list[PublicMoodEnum] | None' in emit(schema)

    def test_a_reserved_column_name_is_aliased(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        field_class = col(schema, 'users', 'field_class')
        assert field_class.alias == 'class'

    def test_the_model_prefix_flag_reaches_keys_and_constraints(self) -> None:
        # Regression: the flag is exposed on this public entrypoint, so it must reach
        # *every* place a column name is standardized -- not just the column rows.
        document = {
            'swagger': '2.0',
            'definitions': {
                'parent': {
                    'required': ['model_id'],
                    'properties': {
                        'model_id': {
                            'format': 'int32',
                            'type': 'integer',
                            'description': 'Note:\nThis is a Primary Key.<pk/>',
                        }
                    },
                },
                'child': {
                    'required': ['model_ref'],
                    'properties': {
                        'model_ref': {
                            'format': 'int32',
                            'type': 'integer',
                            'description': (
                                'Note:\nThis is a Foreign Key to '
                                "`parent.model_id`.<fk table='parent' column='model_id'/>"
                            ),
                        }
                    },
                },
            },
            'paths': {'/parent': {'get': {}, 'post': {}}, '/child': {'get': {}, 'post': {}}},
        }

        protected = build_schema_from_document(document)
        assert table(protected, 'parent').primary_key() == ['field_model_id']
        assert col(protected, 'parent', 'field_model_id').primary is True
        assert col(protected, 'child', 'field_model_ref').is_foreign_key is True

        unprotected = build_schema_from_document(document, disable_model_prefix_protection=True)
        assert table(unprotected, 'parent').primary_key() == ['model_id']
        assert col(unprotected, 'parent', 'model_id').primary is True
        assert col(unprotected, 'child', 'model_ref').is_foreign_key is True
        assert table(unprotected, 'child').foreign_keys[0].column_name == 'model_ref'


# ---------------------------------------------------------------------------
# Enum namespaces -- two enums may share a bare name across schemas.
# ---------------------------------------------------------------------------


TWO_NAMESPACE_DOCUMENT: dict[str, Any] = {
    'swagger': '2.0',
    'definitions': {
        't': {
            'type': 'object',
            'properties': {
                'a': {'format': 'public.status', 'type': 'string', 'enum': ['active', 'archived']},
                'b': {'format': 'audit.status', 'type': 'string', 'enum': ['created', 'deleted']},
            },
        }
    },
    'paths': {'/t': {'get': {}, 'post': {}}},
}


@pytest.mark.unit
class TestEnumNamespaces:
    """Regression cover for the namespace collision.

    A schema-qualified ``format`` token (PostgREST emits one whenever the type is
    outside ``search_path``) means a single document can carry two enums with the same
    bare name in different schemas. Matching on the bare name alone silently gave both
    columns the *same* member list.
    """

    def test_columns_resolve_to_their_own_schemas_enum(self) -> None:
        schema = build_schema_from_document(TWO_NAMESPACE_DOCUMENT)
        a = col(schema, 't', 'a')
        b = col(schema, 't', 'b')
        assert a.enum_info is not None
        assert b.enum_info is not None
        assert (a.enum_info.schema, a.enum_info.values) == ('public', ['active', 'archived'])
        assert (b.enum_info.schema, b.enum_info.values) == ('audit', ['created', 'deleted'])

    def test_the_emitted_enum_classes_carry_their_own_members(self) -> None:
        emitted = emit(build_schema_from_document(TWO_NAMESPACE_DOCUMENT))
        assert 'class AuditStatusEnum(str, Enum):\n    CREATED = "created"\n    DELETED = "deleted"' in emitted
        assert 'class PublicStatusEnum(str, Enum):\n    ACTIVE = "active"\n    ARCHIVED = "archived"' in emitted
        assert 'a: PublicStatusEnum | None' in emitted
        assert 'b: AuditStatusEnum | None' in emitted

    def test_a_bare_token_binds_to_the_schema_under_construction(self) -> None:
        """A bare ``format`` token *means* the default schema, not "whichever sorts first".

        PostgREST omits the schema prefix exactly when the type is in ``search_path``, so
        ``status`` and ``audit.status`` are different types. Enum rows arrive sorted by
        ``(namespace, type_name)``, so a name-only match binds a bare token to ``audit``.
        """
        document = {
            'swagger': '2.0',
            'definitions': {
                't': {
                    'properties': {
                        'a': {'format': 'status', 'type': 'string', 'enum': ['active', 'archived']},
                        'b': {'format': 'audit.status', 'type': 'string', 'enum': ['created', 'deleted']},
                        'tags': {'format': 'status[]', 'type': 'array', 'items': {'type': 'string'}},
                    }
                }
            },
            'paths': {'/t': {'get': {}, 'post': {}}},
        }
        schema = build_schema_from_document(document)
        tags = col(schema, 't', 'tags')
        assert tags.enum_info is not None
        assert (tags.enum_info.schema, tags.enum_info.values) == ('public', ['active', 'archived'])
        assert 'tags: list[PublicStatusEnum] | None' in emit(schema)

    def test_a_bare_parameter_token_binds_to_the_schema_under_construction(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {
                't': {
                    'properties': {
                        'a': {'format': 'status', 'type': 'string', 'enum': ['active', 'archived']},
                        'b': {'format': 'audit.status', 'type': 'string', 'enum': ['created', 'deleted']},
                    }
                }
            },
            'paths': {
                '/t': {'get': {}, 'post': {}},
                '/rpc/f': {
                    'post': {
                        'parameters': [
                            {
                                'name': 'args',
                                'in': 'body',
                                'schema': {'properties': {'p': {'format': 'status', 'type': 'string'}}},
                            }
                        ]
                    }
                },
            },
        }
        parameter = build_schema_from_document(document).functions[0].parameters[0]
        assert parameter.enum_info is not None
        assert (parameter.enum_info.schema, parameter.enum_info.values) == ('public', ['active', 'archived'])

    def test_a_bare_token_falls_back_when_the_default_schema_has_no_such_enum(self) -> None:
        # Nothing in ``public`` is named ``status``, so the only candidate wins -- a bare
        # token carries no other information.
        document = {
            'swagger': '2.0',
            'definitions': {
                't': {
                    'properties': {
                        'b': {'format': 'audit.status', 'type': 'string', 'enum': ['created', 'deleted']},
                        'tags': {'format': 'status[]', 'type': 'array', 'items': {'type': 'string'}},
                    }
                }
            },
            'paths': {'/t': {'get': {}, 'post': {}}},
        }
        tags = col(build_schema_from_document(document), 't', 'tags')
        assert tags.enum_info is not None
        assert (tags.enum_info.schema, tags.enum_info.values) == ('audit', ['created', 'deleted'])

    def test_a_qualified_array_element_links_to_its_own_schema(self) -> None:
        document = {
            'swagger': '2.0',
            'definitions': {
                't': {
                    'properties': {
                        'a': {'format': 'public.status', 'type': 'string', 'enum': ['active', 'archived']},
                        'b': {'format': 'audit.status', 'type': 'string', 'enum': ['created', 'deleted']},
                        'tags': {
                            'format': 'public.status[]',
                            'type': 'array',
                            'items': {'type': 'string'},
                        },
                    }
                }
            },
            'paths': {'/t': {'get': {}, 'post': {}}},
        }
        # ``public`` deliberately, not ``audit``: enum rows sort by (namespace, type_name),
        # so an ``audit`` expectation is satisfied by name-only matching too and the test
        # could never fail.
        tags = col(build_schema_from_document(document), 't', 'tags')
        assert tags.enum_info is not None
        assert (tags.enum_info.schema, tags.enum_info.values) == ('public', ['active', 'archived'])

    def test_a_function_parameter_links_to_its_own_schema(self) -> None:
        document = dict(TWO_NAMESPACE_DOCUMENT)
        document['paths'] = {
            '/t': {'get': {}, 'post': {}},
            '/rpc/f': {
                'post': {
                    'parameters': [
                        {
                            'name': 'args',
                            'in': 'body',
                            'schema': {
                                'properties': {
                                    'p': {'format': 'audit.status', 'type': 'string'},
                                    'q': {'format': 'public.status', 'type': 'string'},
                                }
                            },
                        }
                    ]
                }
            },
        }
        function = build_schema_from_document(document).functions[0]
        p, q = function.parameters
        assert p.enum_info is not None
        assert q.enum_info is not None
        assert (p.enum_info.schema, p.enum_info.values) == ('audit', ['created', 'deleted'])
        assert (q.enum_info.schema, q.enum_info.values) == ('public', ['active', 'archived'])

    def test_the_schema_argument_flows_through(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document, schema='api')
        assert {t.schema for t in schema.tables} == {'api'}
        assert {f.schema for f in schema.functions} == {'api'}


def schema_of(document: dict[str, Any]) -> Schema:
    """Build the schema for ``document`` (helper for one-liner assertions)."""
    return build_schema_from_document(document)


# ---------------------------------------------------------------------------
# Functions (the CI5-D1 build-ahead).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFunctions:
    def test_functions_are_in_name_order(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert [f.name for f in schema.functions] == ['create_order', 'get_user_stats', 'ping', 'search_products']

    def test_the_fill_matrix_for_a_volatile_function(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        create_order = schema.functions[0]
        assert create_order.name == 'create_order'
        assert create_order.schema == 'public'
        assert create_order.volatility is FunctionVolatility.VOLATILE
        assert create_order.is_read_only is False
        assert create_order.return_type is None
        assert create_order.returns_set is None
        assert create_order.description == 'Create an order\n\nInserts a row and returns its id.'
        # NOT the alphabetical POST-body order (`[items, status, user_id]`) any more. `CI-078`
        # reorders out of the body's `required` array, which is `[user_id, status]` here -- so the
        # first two positions are recovered and `items`, the one defaulted argument, is not.
        # The row says exactly that much and no more.
        assert [(p.name, p.raw_type, p.mode, p.has_default) for p in create_order.parameters] == [
            ('user_id', 'bigint', ParameterMode.IN, False),
            ('status', 'order_status', ParameterMode.IN, False),
            ('items', 'text[]', ParameterMode.IN, True),
        ]
        assert create_order.parameter_order is ParameterOrder.DECLARED_PREFIX
        assert next(p for p in create_order.parameters if p.name == 'items').array_element_type == 'text'

    def test_a_non_volatile_function_leaves_volatility_unknown(self, document: dict[str, Any]) -> None:
        # The document distinguishes VOLATILE from *not*, never STABLE from IMMUTABLE.
        schema = build_schema_from_document(document)
        stats = next(f for f in schema.functions if f.name == 'get_user_stats')
        assert stats.volatility is None
        assert stats.is_read_only is True

    def test_a_parameter_enum_links_when_the_type_is_known(self, document: dict[str, Any]) -> None:
        # ⚠ Looked up BY NAME, not by index. This used to read `parameters[1]`, which was the
        # alphabetical position of `status`; under `CI-078` the list is reordered and index 1 is
        # still `status` -- by coincidence. An assertion that survives on a coincidence is not an
        # assertion, so the coincidence is removed rather than relied on.
        schema = build_schema_from_document(document)
        create_order = next(f for f in schema.functions if f.name == 'create_order')
        status = next(p for p in create_order.parameters if p.name == 'status')
        assert status.enum_info is not None
        assert status.enum_info.values == ['pending', 'shipped', 'cancelled']

    def test_a_variadic_parameter_is_recovered_from_the_get_operation(self, document: dict[str, Any]) -> None:
        # Compares a DICT, so it is order-insensitive and stayed green across `CI-078` -- which is
        # itself worth noting: the reorder did not disturb mode recovery, even though both facts
        # are read out of the same GET operation.
        schema = build_schema_from_document(document)
        search = next(f for f in schema.functions if f.name == 'search_products')
        modes = {p.name: p.mode for p in search.parameters}
        assert modes == {'limit_to': ParameterMode.IN, 'terms': ParameterMode.VARIADIC}

    def test_a_stable_function_is_built_in_full_declaration_order(self, document: dict[str, Any]) -> None:
        # The counterpart to `test_the_fill_matrix_for_a_volatile_function`: a GET operation exists
        # for a STABLE/IMMUTABLE function, so the whole order is established rather than a prefix.
        # Asserted end-to-end through `build_schema_from_document`, not just at the row boundary.
        schema = build_schema_from_document(document)
        search = next(f for f in schema.functions if f.name == 'search_products')
        assert [p.name for p in search.parameters] == ['terms', 'limit_to']
        assert search.parameter_order is ParameterOrder.DECLARED

    def test_every_function_declares_how_much_of_its_order_is_known(self, document: dict[str, Any]) -> None:
        # Enumerated, not sampled (CI-072). `ping` is DECLARED on arity alone (D6), `create_order`
        # is the only partial one, and nothing in this fixture reaches UNKNOWN.
        schema = build_schema_from_document(document)
        assert {f.name: f.parameter_order for f in schema.functions} == {
            'create_order': ParameterOrder.DECLARED_PREFIX,
            'get_user_stats': ParameterOrder.DECLARED,
            'ping': ParameterOrder.DECLARED,
            'search_products': ParameterOrder.DECLARED,
        }

    def test_a_no_argument_function_has_no_parameters(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert next(f for f in schema.functions if f.name == 'ping').parameters == []

    def test_functions_never_reach_the_emitted_output(self, document: dict[str, Any]) -> None:
        # Nothing consumes Schema.functions until CI-012; the Pydantic emitter ignores it.
        schema = build_schema_from_document(document)
        assert schema.functions
        emitted = emit(schema)
        for name in ('create_order', 'get_user_stats', 'search_products'):
            assert name not in emitted


# ---------------------------------------------------------------------------
# The fidelity floor, as a tested contract.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFidelityFloor:
    def test_an_integer_surrogate_primary_key_looks_like_a_plain_required_column(
        self, document: dict[str, Any]
    ) -> None:
        # PostgREST drops nextval(...) defaults, so castiron cannot know this is generated.
        schema = build_schema_from_document(document)
        users_id = col(schema, 'users', 'id')
        assert users_id.is_identity is False
        assert users_id.is_generated is False
        assert users_id.has_default is False
        # ...and the visible consequence: it is a *required* field on the Insert model.
        # The docstring here carries the table's SQL comment as a body paragraph (CI-009);
        # the subject of this assertion is still `id: int` under `# Primary Keys`.
        insert_block = (
            'class UsersInsert(CustomModelInsert):\n'
            '    """Users Insert Schema.\n\n    Application users.\n    """\n\n'
            '    # Primary Keys\n    id: int\n'
        )
        assert insert_block in emit(schema)

    def test_the_opt_in_inference_makes_it_optional_on_insert(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document, infer_generated_primary_keys=True)
        users_id = col(schema, 'users', 'id')
        assert users_id.is_identity is True
        assert users_id.is_generated is True
        # Identity columns are omitted from Insert/Update entirely.
        emitted = emit(schema)
        assert (
            '    """Users Insert Schema.\n\n    Application users.\n    """\n\n    # Required fields\n    email: str'
        ) in emitted

    def test_no_check_or_exclude_constraints_exist_anywhere(self, document: dict[str, Any]) -> None:
        # The document carries no constraint information at all. The only UNIQUE rows that
        # can exist are a view's downgraded `<pk/>` markers (CI5-D14a) -- never a real
        # unique constraint, and never a CHECK, which is why this source produces no
        # ``Annotated[str, StringConstraints(...)]``.
        schema = build_schema_from_document(document)
        kinds = {c.type for t in schema.tables for c in t.constraints}
        assert kinds <= {ConstraintType.PRIMARY_KEY, ConstraintType.FOREIGN_KEY, ConstraintType.UNIQUE}
        assert ConstraintType.CHECK not in kinds
        assert ConstraintType.EXCLUDE not in kinds
        unique_tables = [t.name for t in schema.tables if t.has_unique_constraint()]
        assert unique_tables == ['active_users_view']
        assert all(c.constraint_definition is None for t in schema.tables for c in t.columns)
        assert 'StringConstraints' not in emit(schema)

    def test_every_view_column_is_nullable(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        view = table(schema, 'active_users_view')
        assert all(c.is_nullable for c in view.columns)

    def test_a_view_carries_no_primary_key_even_though_the_document_marks_one(self, document: dict[str, Any]) -> None:
        # The document DOES mark view primary keys (PostgREST propagates keys through
        # views, spec §3.2), but ``TableInfo.primary_key()`` is defined to be empty for a
        # VIEW. The source therefore drops the marker rather than leaving the IR
        # self-contradicting: ``col.primary`` and ``primary_key()`` must agree, because
        # emitters read one or the other.
        assert (
            'This is a Primary Key.<pk/>'
            in document['definitions']['active_users_view']['properties']['id']['description']
        )

        schema = build_schema_from_document(document)
        view = table(schema, 'active_users_view')
        assert view.table_type == 'VIEW'
        assert view.primary_key() == []
        assert all(c.primary is False for c in view.columns)
        assert all(c.type is not ConstraintType.PRIMARY_KEY for c in view.constraints)
        # Foreign keys on a view ARE still carried -- only the PK marker is dropped.
        assert col(schema, 'active_users_view', 'favorite_product_id').is_foreign_key is True

    def test_a_views_key_survives_as_a_unique_constraint(self, document: dict[str, Any]) -> None:
        # Dropping the marker entirely also destroyed the only evidence that the view's key
        # column is unique, so any FK pointing AT a view degraded to MANY_TO_MANY and was
        # emitted as a plural list. The marker is the document's own statement -- it is
        # downgraded to UNIQUE, not discarded (decision CI5-D14a).
        schema = build_schema_from_document(document)
        view = table(schema, 'active_users_view')
        unique = [c for c in view.constraints if c.type is ConstraintType.UNIQUE]
        assert [c.columns for c in unique] == [['id']]
        assert col(schema, 'active_users_view', 'id').is_unique is True

    def test_a_foreign_key_pointing_at_a_view_stays_many_to_one(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        fk = next(
            f for f in table(schema, 'restricted_table').foreign_keys if f.foreign_table_name == 'active_users_view'
        )
        assert fk.relation_type is RelationType.MANY_TO_ONE
        # A restricted_table row references exactly ONE view row -- singular, not a list.
        # Scoped to RestrictedTable's own class: ``products`` legitimately holds a plural
        # ``active_users_views`` list (the view is the FK *source* there).
        # The second split is load-bearing: without it the slice runs to EOF and every later
        # class (`class Users`, ...) would count as "RestrictedTable's own".
        after_header = emit(schema).split('class RestrictedTable(RestrictedTableBaseSchema):')[1]
        restricted_class = after_header.split('\nclass ')[0]
        assert 'active_users_view: ActiveUsersView | None = Field(default=None)' in restricted_class
        assert 'active_users_views: list[ActiveUsersView]' not in restricted_class

    def test_a_base_table_still_gets_its_primary_key(self, document: dict[str, Any]) -> None:
        # Guard rail for the view fix: it must not touch BASE TABLE classification.
        schema = build_schema_from_document(document)
        assert table(schema, 'restricted_table').primary_key() == ['id']
        assert col(schema, 'restricted_table', 'id').primary is True

    def test_smallint_and_integer_are_indistinguishable(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert col(schema, 'users', 'id').raw_type == 'integer'
        assert col(schema, 'orders', 'id').raw_type == 'bigint'

    def test_no_function_reports_a_return_type_or_set_returning_flag(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert schema.functions
        assert all(f.return_type is None for f in schema.functions)
        assert all(f.returns_set is None for f in schema.functions)

    def test_a_composite_foreign_key_is_invisible(self, document: dict[str, Any]) -> None:
        # Every FK castiron recovers spans exactly one column -- the marker cannot say more.
        schema = build_schema_from_document(document)
        fk_constraints = [c for t in schema.tables for c in t.constraints if c.type == ConstraintType.FOREIGN_KEY]
        assert fk_constraints
        assert all(len(c.columns) == 1 for c in fk_constraints)

    def test_a_marker_naming_a_table_the_document_does_not_contain_yields_no_relationship(self) -> None:
        # CI-084. Privileges filter relations, so a `<fk/>` marker can name a table the API role
        # cannot see. The edge is unbuildable, so `is_foreign_key` stays False -- but the FOREIGN
        # KEY constraint is RETAINED, because it is the only evidence the database has one.
        document = {
            'swagger': '2.0',
            'definitions': {
                'child': {
                    'required': ['ref_id'],
                    'properties': {
                        'ref_id': {
                            'format': 'int32',
                            'type': 'integer',
                            'description': ("Note:\nForeign Key to `invisible.id`.<fk table='invisible' column='id'/>"),
                        }
                    },
                }
            },
            'paths': {'/child': {'get': {}, 'post': {}}},
        }
        schema = build_schema_from_document(document)
        child = table(schema, 'child')
        assert [t.name for t in schema.tables] == ['child']
        assert col(schema, 'child', 'ref_id').is_foreign_key is False
        assert child.foreign_keys == []
        assert [(c.type, c.constraint_definition) for c in child.constraints] == [
            (ConstraintType.FOREIGN_KEY, 'FOREIGN KEY (ref_id) REFERENCES invisible(id)')
        ]

    def test_every_constraint_name_this_source_produces_is_declared_synthesized(self, document: dict[str, Any]) -> None:
        # CI-090, and NOT foreign-key-specific: the document carries no constraint name anywhere,
        # so PRIMARY KEY (`<t>_pkey`) and a view's downgraded UNIQUE (`<t>_<cols>_key`) are
        # manufactured from pg's default templates exactly as the FK name is.
        schema = build_schema_from_document(document)
        constraints = [c for t in schema.tables for c in t.constraints]
        edges = [fk for t in schema.tables for fk in t.foreign_keys]
        assert len(constraints) == 11 and len(edges) == 10  # non-vacuous
        assert {c.type for c in constraints} == {
            ConstraintType.PRIMARY_KEY,
            ConstraintType.FOREIGN_KEY,
            ConstraintType.UNIQUE,
        }
        assert all(c.name_is_synthesized is True for c in constraints)
        assert all(fk.name_is_synthesized is True for fk in edges)

    def test_a_reverse_edge_inherits_the_flag_from_the_edge_it_mirrors(self, document: dict[str, Any]) -> None:
        # A reverse edge reuses the forward edge's `constraint_name`, so it must reuse its
        # provenance. `users` holds no `<fk/>` marker of its own; every edge on it is a mirror.
        schema = build_schema_from_document(document)
        users = table(schema, 'users')
        assert users.foreign_keys, 'users should carry only reverse edges'
        assert all(fk.name_is_synthesized is True for fk in users.foreign_keys)

    def test_the_builder_does_not_claim_synthesis_by_default(self, document: dict[str, Any]) -> None:
        # The CI-010 contract, asserted today: the flag rides the ROW. A source that reports
        # `pg_constraint.conname` passes False and nothing in the builder second-guesses it.
        rows = parse_openapi_document(document)
        honest_fks = [(*row[:7], False) for row in rows.fk_details]
        honest_constraints = [(*row[:5], False) for row in rows.constraints]
        schema = build_schema(
            rows.column_details,
            honest_fks,
            honest_constraints,
            rows.enum_types,
            rows.enum_type_mapping,
        )
        constraints = [c for t in schema.tables for c in t.constraints]
        edges = [fk for t in schema.tables for fk in t.foreign_keys]
        assert len(constraints) == 11 and len(edges) == 10  # non-vacuous
        assert not any(c.name_is_synthesized for c in constraints)
        assert not any(fk.name_is_synthesized for fk in edges)


# ---------------------------------------------------------------------------
# End-to-end: valid, golden-stable, deterministic output.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmittedOutput:
    def test_it_matches_the_committed_golden(self, document: dict[str, Any]) -> None:
        golden = (GOLDEN_DIR / 'schema.py.txt').read_text(encoding='utf-8')
        assert emit(build_schema_from_document(document)) == golden

    def test_the_emitted_module_is_valid_python_and_instantiable(self, document: dict[str, Any]) -> None:
        emitted = emit(build_schema_from_document(document))
        module = ModuleType('castiron_openapi_generated')
        sys.modules[module.__name__] = module
        try:
            exec(compile(emitted, '<castiron-generated>', 'exec'), module.__dict__)
            product = module.__dict__['ProductsBaseSchema'](id=1, name='Anvil')
            assert product.id == 1
            assert product.name == 'Anvil'
            enum_class = module.__dict__['PublicOrderStatusEnum']
            assert enum_class('pending').value == 'pending'
            user = module.__dict__['UsersInsert'](id=1, email='a@example.com')
            assert user.status is None
        finally:
            del sys.modules[module.__name__]

    def test_emitting_the_same_schema_twice_is_byte_identical(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert emit(schema) == emit(schema)

    def test_building_twice_from_the_same_document_emits_identically(self, document: dict[str, Any]) -> None:
        assert emit(build_schema_from_document(document)) == emit(build_schema_from_document(document))

    def test_a_key_reordered_document_emits_identically(self, document: dict[str, Any]) -> None:
        reordered = json.loads(json.dumps(document))
        reordered['definitions'] = dict(reversed(list(reordered['definitions'].items())))
        reordered['paths'] = dict(reversed(list(reordered['paths'].items())))
        assert emit(build_schema_from_document(reordered)) == emit(build_schema_from_document(document))

    def test_as_dict_is_json_serializable_and_stable(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)
        assert json.dumps(schema.as_dict()) == json.dumps(build_schema_from_document(document).as_dict())
        assert schema.as_dict()['functions'][0]['volatility'] == 'VOLATILE'


# ---------------------------------------------------------------------------
# Errors surface through the source entrypoints.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrors:
    def test_an_openapi3_document_is_refused(self, openapi3_document: dict[str, Any]) -> None:
        with pytest.raises(SourceParseError):
            build_schema_from_document(openapi3_document)

    def test_an_empty_document_is_refused(self, empty_definitions_document: dict[str, Any]) -> None:
        with pytest.raises(SourceParseError):
            build_schema_from_document(empty_definitions_document)

    def test_the_error_classes_share_one_base(self) -> None:
        assert issubclass(SourceFetchError, SourceError)
        assert issubclass(SourceParseError, SourceError)


# ---------------------------------------------------------------------------
# load_openapi_schema (the only entrypoint that fetches).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadOpenApiSchema:
    def test_it_fetches_then_builds(self, monkeypatch: pytest.MonkeyPatch, document: dict[str, Any]) -> None:
        captured: dict[str, Any] = {}

        def fake_fetch(url: str, *, key: str | None, schema: str, timeout: float) -> dict[str, Any]:
            captured.update(url=url, key=key, schema=schema, timeout=timeout)
            return document

        monkeypatch.setattr(fetch_module, 'fetch_openapi_document', fake_fetch)
        monkeypatch.setattr('castiron.sources.openapi.source.fetch_openapi_document', fake_fetch)

        schema = load_openapi_schema(
            'https://abc.supabase.co',
            key='anon-key',
            schema='public',
            timeout=1.5,
            infer_generated_primary_keys=True,
        )

        assert captured == {'url': 'https://abc.supabase.co', 'key': 'anon-key', 'schema': 'public', 'timeout': 1.5}
        assert [t.name for t in schema.tables] == sorted(t.name for t in schema.tables)
        assert col(schema, 'users', 'id').is_identity is True


# ---------------------------------------------------------------------------
# Structural guarantees (acceptance criteria that are about the code, not behavior).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestModuleHygiene:
    @pytest.mark.parametrize('module_name', ['parse.py', 'source.py'])
    def test_the_pure_modules_import_no_io_machinery(self, module_name: str) -> None:
        tree = ast.parse((SOURCE_DIR / module_name).read_text(encoding='utf-8'))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split('.')[0])
        assert imported.isdisjoint({'urllib', 'socket', 'http', 'pathlib'})

    def test_the_sources_package_is_free_of_third_party_runtime_deps(self) -> None:
        stdlib_or_castiron = {
            'castiron',
            'collections',
            'dataclasses',
            'http',
            'json',
            'logging',
            're',
            'ssl',
            'typing',
            'urllib',
        }
        for path in sorted(SOURCE_DIR.parent.rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split('.')[0] in stdlib_or_castiron, path
                elif isinstance(node, ast.ImportFrom) and node.module:
                    assert node.module.split('.')[0] in stdlib_or_castiron, path


# ---------------------------------------------------------------------------
# Table-level SQL comments (CI-009), end to end.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTableDescriptions:
    """``definitions.<t>.description`` reaching ``TableInfo.description``."""

    def test_a_base_table_carries_its_comment(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)

        assert table(schema, 'users').description == 'Application users.'
        assert table(schema, 'orders').description == 'Customer orders.'

    def test_a_view_carries_its_comment_too(self, document: dict[str, Any]) -> None:
        """A VIEW is a table for this purpose; ``COMMENT ON VIEW`` populates the same field."""
        view = table(build_schema_from_document(document), 'active_users_view')

        assert view.table_type == 'VIEW'
        assert view.description == 'Users with a recent login.'

    def test_an_uncommented_table_is_none(self, document: dict[str, Any]) -> None:
        schema = build_schema_from_document(document)

        assert table(schema, 'products').description is None
        assert table(schema, 'order_items').description is None
        assert table(schema, 'restricted_table').description is None

    def test_every_table_description_matches_the_document(self, document: dict[str, Any]) -> None:
        """CI6-Q7: enumerate the tables rather than sampling two of them."""
        schema = build_schema_from_document(document)

        for name, definition in document['definitions'].items():
            expected = definition.get('description')
            assert table(schema, name).description == expected, name

    def test_the_comment_reaches_as_dict(self, document: dict[str, Any]) -> None:
        as_dict = build_schema_from_document(document).as_dict()
        by_name = {t['name']: t for t in as_dict['tables']}

        assert by_name['users']['description'] == 'Application users.'
        assert by_name['products']['description'] is None
        assert json.dumps(as_dict) == json.dumps(build_schema_from_document(document).as_dict())

    def test_building_twice_yields_the_same_descriptions(self, document: dict[str, Any]) -> None:
        first = build_schema_from_document(document)
        second = build_schema_from_document(document)

        assert [t.description for t in first.tables] == [t.description for t in second.tables]

    def test_a_reordered_document_yields_the_same_descriptions(self, document: dict[str, Any]) -> None:
        import copy

        reordered = copy.deepcopy(document)
        reordered['definitions'] = dict(reversed(list(reordered['definitions'].items())))

        assert {t.name: t.description for t in build_schema_from_document(reordered).tables} == {
            t.name: t.description for t in build_schema_from_document(document).tables
        }

    def test_the_comment_reaches_the_emitted_docstring(self, document: dict[str, Any]) -> None:
        out = emit(build_schema_from_document(document))

        assert (
            'class UsersBaseSchema(CustomModel):\n    """Users Base Schema.\n\n    Application users.\n    """' in out
        )

    def test_an_uncommented_table_emits_a_one_line_docstring(self, document: dict[str, Any]) -> None:
        out = emit(build_schema_from_document(document))

        assert '    """Products Base Schema."""' in out
        assert '    """OrderItems Base Schema."""' in out
