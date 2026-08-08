import ast
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from castiron.emitters import EmittedFile, EmitterConfig, PydanticEmitter
from castiron.emitters.pydantic.emitter import IND
from castiron.ir import (
    ColumnInfo,
    EnumInfo,
    ForeignKeyInfo,
    RelationshipInfo,
    RelationType,
    Schema,
    TableInfo,
    build_schema,
)
from castiron.sources.openapi import build_schema_from_document
from castiron.utils.naming import python_class_name
from tests.unit.utils.test_naming import ENUM_LABEL_CORPUS, crafted_class_private_label

Row = tuple[object, ...]
GOLDEN = Path(__file__).parent / 'golden' / 'schema.py.txt'


def _emit(schema: Schema, config: EmitterConfig | None = None) -> str:
    return PydanticEmitter(config).emit(schema)[0].content


def _imported(text: str) -> set[str]:
    """Every symbol the emitted module imports, as ``module.Name`` (or the bare module).

    ⚠ Assert against **this**, never against a whole ``'from pydantic import ConfigDict'`` line.
    CI-094 merges same-module imports onto one line, so a line-literal assertion is really an
    assertion about which *other* symbols happen to be imported alongside -- it breaks (or, worse,
    silently passes for the wrong reason) the next time the vocabulary moves.
    """
    symbols: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.ImportFrom):
            symbols.update(f'{node.module}.{alias.name}' for alias in node.names)
        elif isinstance(node, ast.Import):
            symbols.update(alias.name for alias in node.names)
    return symbols


def _import_block(text: str) -> str:
    """The module's import block: every line above the first ``#`` section comment.

    The emitted body always opens with ``section_comment(...)``, and no import line starts with
    ``#`` -- so the split point is exact rather than heuristic.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith('#'):
            return '\n'.join(lines[:index]).rstrip('\n')
    raise AssertionError('the emitted module has no section comment, so it has no body')


# --------------------------------------------------------------------------- basics


@pytest.mark.unit
class TestEmitBasics:
    def test_returns_single_emitted_file(self, representative_schema: Schema) -> None:
        result = PydanticEmitter().emit(representative_schema)
        assert len(result) == 1
        assert isinstance(result[0], EmittedFile)
        assert result[0].path == 'schema.py'

    def test_output_filename_honored(self, representative_schema: Schema) -> None:
        result = PydanticEmitter(EmitterConfig(output_filename='models/db.py')).emit(representative_schema)
        assert result[0].path == 'models/db.py'

    def test_output_is_valid_python(self, representative_schema: Schema) -> None:
        compile(_emit(representative_schema), '<generated>', 'exec')


# --------------------------------------------------------------------------- golden


@pytest.mark.unit
class TestGolden:
    def test_matches_golden(self, representative_schema: Schema) -> None:
        assert _emit(representative_schema) == GOLDEN.read_text(encoding='utf-8')

    def test_golden_is_valid_python(self) -> None:
        compile(GOLDEN.read_text(encoding='utf-8'), '<golden>', 'exec')

    def test_a_zero_function_schema_emits_byte_identically_after_ci_005(self, representative_schema: Schema) -> None:
        """CI-005 backward-compat guard: adding ``Schema.functions`` changed no output.

        ``representative_schema`` is built by the CI-003/CI-004 tuple fixtures with
        ``function_details`` omitted, so it carries an empty function list. The emitted
        module must still match the CI-004 golden byte for byte.
        """
        assert representative_schema.functions == []
        assert _emit(representative_schema) == GOLDEN.read_text(encoding='utf-8')


# --------------------------------------------------------------------------- determinism


@pytest.mark.unit
class TestDeterminism:
    def test_emit_twice_byte_identical(self, representative_schema: Schema) -> None:
        assert _emit(representative_schema) == _emit(representative_schema)

    def test_rebuilt_schema_emits_identically(self, build_columns: Callable[..., Row]) -> None:
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'name', 'text'),
            build_columns('t', 'tags', 'ARRAY', nullable=True, array_element_type='text'),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        first = build_schema(columns, [], constraints, [], [])
        second = build_schema(columns, [], constraints, [], [])
        assert _emit(first) == _emit(second)

    def test_import_block_is_grouped_the_way_isort_groups_it(self, representative_schema: Schema) -> None:
        # ⚠ This REPLACES `test_import_block_is_sorted`, which asserted `lines == sorted(lines)`
        # -- the flat contract CI-094 abolished, and the contract that put I001 in every emitted
        # module (`import datetime` sorts after every `from ...` line as a raw string).
        sections = _import_block(_emit(representative_schema)).split('\n\n')
        assert sections[0] == 'from __future__ import annotations'
        for section in sections[1:]:
            lines = section.splitlines()
            kinds = [0 if line.startswith('import ') else 1 for line in lines]
            assert kinds == sorted(kinds), f'a plain import follows a from-import in {lines}'
            # ⚠ Per KIND, not across the whole section. A section deliberately puts every
            # `import X` before every `from X import ...`, so the concatenated module list is NOT
            # sorted in general -- `import zoneinfo` + `from datetime import date` is correct
            # isort output that a whole-section check would reject. It passed here only because
            # 'datetime' < 'enum' happens to hold.
            for kind in (0, 1):
                modules = [line.split()[1] for line, k in zip(lines, kinds) if k == kind]
                assert modules == sorted(modules, key=str.lower), f'{modules} out of order in {lines}'
        stdlib, third_party = sections[1].splitlines(), sections[2].splitlines()
        assert [line.split()[1] for line in stdlib] == ['datetime', 'enum', 'typing']
        assert third_party == ['from pydantic import UUID4, BaseModel, Field, Json, StringConstraints']

    def test_exactly_one_blank_line_separates_the_imports_from_the_body(self, representative_schema: Schema) -> None:
        # Measured, both directions: ruff accepts exactly ONE blank line before a comment and
        # exactly TWO before code, and the section after the imports always opens with a
        # `# SECTION` comment. castiron emitted two for its whole life, which alone made every
        # module I001-dirty. `ruff format` agrees with the one-blank form.
        out = _emit(representative_schema)
        block = _import_block(out)
        assert out.startswith(f'{block}\n\n#')
        assert not out.startswith(f'{block}\n\n\n')

    def test_future_annotations_is_first_line(self, representative_schema: Schema) -> None:
        assert _emit(representative_schema).startswith('from __future__ import annotations\n')


# --------------------------------------------------------------------------- validity


@pytest.mark.unit
class TestValidity:
    def test_generated_base_model_validates(self, build_columns: Callable[..., Row]) -> None:
        columns = [
            build_columns('person', 'id', 'integer', identity=True),
            build_columns('person', 'email', 'text', nullable=True),
            build_columns('person', 'age', 'integer'),
        ]
        constraints = [('person_pkey', 'person', ['id'], 'p', 'PRIMARY KEY (id)')]
        schema = build_schema(columns, [], constraints, [], [])
        code = _emit(schema)
        namespace: dict[str, object] = {}
        exec(code, namespace)  # noqa: S102 - executing our own generated output is the validity check
        model = namespace['PersonBaseSchema']
        instance = model(id=1, email='a@b.com', age=30)  # type: ignore[operator]
        assert instance.age == 30

    def test_generated_model_rejects_wrong_type(self, build_columns: Callable[..., Row]) -> None:
        import pydantic

        columns = [build_columns('person', 'age', 'integer')]
        schema = build_schema(columns, [], [], [], [])
        namespace: dict[str, object] = {}
        exec(_emit(schema), namespace)  # noqa: S102
        model = namespace['PersonBaseSchema']
        with pytest.raises(pydantic.ValidationError):
            model(age='not-an-int')  # type: ignore[operator]


# --------------------------------------------------------------------------- fidelity


@pytest.mark.unit
class TestFidelity:
    def test_jsonb_union(self, representative_schema: Schema) -> None:
        assert 'metadata: dict | list[dict] | list[Any] | Json | None' in _emit(representative_schema)

    def test_uuid(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'company_id: UUID4' in out
        assert 'pydantic.UUID4' in _imported(out)

    def test_timestamptz(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'created_at: datetime.datetime' in out
        assert 'datetime' in _imported(out)

    def test_text_array(self, representative_schema: Schema) -> None:
        assert 'roles: list[str] | None' in _emit(representative_schema)

    def test_enum_class_and_scalar_field(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'class PublicUserStatusEnum(str, Enum):' in out
        assert 'status: PublicUserStatusEnum' in out
        assert 'enum.Enum' in _imported(out)

    def test_enum_array_field(self, representative_schema: Schema) -> None:
        assert 'flags: list[PublicUserStatusEnum] | None' in _emit(representative_schema)

    def test_enum_member_reserved_keyword_suffixed(self, representative_schema: Schema) -> None:
        # ⚠ The label in the comment is now rendered through `_py_string`, so it reads
        # `"import"` and not `import`. That is `CI94-D3`, and it is the ONLY reason this
        # assertion moved -- the member name, the value literal and the note text are unchanged.
        # It is not cosmetic: see TestCi080TheCommentIsTotalOverItsInput below for a label that
        # reaches this exact line carrying newlines.
        out = _emit(representative_schema)
        assert 'ACTIVE = "active"' in out
        assert 'IMPORT_ = "import"  # original name was "import" (reserved keyword)' in out

    def test_string_constraints(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert "sku: Annotated[str, StringConstraints(**{'min_length': 10, 'max_length': 10})]" in out
        assert "bio: Annotated[str, StringConstraints(**{'max_length': 500})] | None" in out
        assert {'typing.Annotated', 'pydantic.StringConstraints'} <= _imported(out)

    def test_description_field(self, representative_schema: Schema) -> None:
        assert 'email: str | None = Field(default=None, description="User email")' in _emit(representative_schema)

    def test_min_and_max_length_constraint(self, build_columns: Callable[..., Row]) -> None:
        columns = [build_columns('t', 'code', 'text')]
        constraints = [('t_code', 't', ['code'], 'c', 'CHECK (length(code) >= 4 AND length(code) <= 20)')]
        out = _emit(build_schema(columns, [], constraints, [], []))
        assert "code: Annotated[str, StringConstraints(**{'min_length': 4, 'max_length': 20})]" in out

    def test_reserved_column_aliased(self, build_columns: Callable[..., Row]) -> None:
        # 'class' is reserved -> renamed to field_class with an alias.
        columns = [build_columns('t', 'class', 'text', nullable=True)]
        out = _emit(build_schema(columns, [], [], [], []))
        assert 'field_class: str | None = Field(default=None, alias="class")' in out

    def test_description_escaping(self) -> None:
        table = TableInfo(
            name='t',
            columns=[ColumnInfo(name='note', raw_type='text', is_nullable=True, description='a "quoted" word')],
        )
        out = _emit(Schema(tables=[table]))
        assert 'description="a \\"quoted\\" word"' in out


# --------------------------------------------------------------------------- imports


@pytest.mark.unit
class TestImports:
    def test_annotated_only_when_length_constraint(self, build_columns: Callable[..., Row]) -> None:
        # A text column with a non-length CHECK must NOT pull in Annotated.
        columns = [build_columns('t', 'title', 'text')]
        constraints = [('t_title', 't', ['title'], 'c', "CHECK (title <> '')")]
        out = _emit(build_schema(columns, [], constraints, [], []))
        assert 'typing.Annotated' not in _imported(out)

    def test_no_future_annotations_without_relationships(self, build_columns: Callable[..., Row]) -> None:
        columns = [build_columns('t', 'id', 'integer', identity=True)]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        assert '__future__.annotations' not in _imported(_emit(build_schema(columns, [], constraints, [], [])))


@pytest.mark.unit
class TestFieldIsImportedOnlyWhenUsed:
    """``from pydantic import Field`` was unconditional, and that was a live ``F401``.

    Measured across the full 4-input × 128-config sweep: **32 of 512** reachable emissions
    imported ``Field`` and never called it. The trigger is a schema whose columns are all NOT
    NULL with no comments and no foreign keys, emitted with ``--no-crud-models
    --no-null-parent-classes`` -- an ordinary invocation, not a corner.

    ``_imports`` decides by searching the **already-rendered body** for ``'= Field('``
    (``CI94-D9``) rather than by re-deriving ``_render_column``'s conditions, so the two can
    never drift apart.
    """

    def _all_not_null(self, build_columns: Callable[..., Row]) -> Schema:
        columns = [build_columns('t', 'a', 'text'), build_columns('t', 'b', 'integer')]
        return build_schema(columns, [], [], [], [])

    def test_it_is_absent_when_nothing_calls_it(self, build_columns: Callable[..., Row]) -> None:
        config = EmitterConfig(generate_crud_models=False, add_null_parent_classes=False)
        out = _emit(self._all_not_null(build_columns), config)
        assert '= Field(' not in out
        assert 'pydantic.Field' not in _imported(out)

    def test_it_is_present_whenever_something_calls_it(self, build_columns: Callable[..., Row]) -> None:
        out = _emit(self._all_not_null(build_columns), EmitterConfig(generate_crud_models=True))
        assert '= Field(' in out
        assert 'pydantic.Field' in _imported(out)

    def test_the_field_import_tracks_the_field_call_in_both_directions(self, representative_schema: Schema) -> None:
        # ⚠ Named for what it checks. It asserts the Field import iff the Field call, over the
        # config axis that drives it -- NOT the general "no undefined name" property, which is
        # the corpus ruff sweep's job (`test_lint.py`, F821 over all 384 lintable emissions).
        # The direction that matters more is the second: a body calling Field without importing
        # it is a NameError at import time, not a lint finding.
        for config in (EmitterConfig(), EmitterConfig(generate_crud_models=False, generate_enums=False)):
            out = _emit(representative_schema, config)
            assert ('= Field(' in out) == ('pydantic.Field' in _imported(out))

    def test_a_column_comment_containing_the_sentinel_fails_conservatively(self) -> None:
        # The documented failure mode of reading the rendered body: a description carrying the
        # literal `= Field(` imports Field unnecessarily. That costs a lint finding and never a
        # broken module -- pinned here so the trade-off is a decision rather than a surprise.
        table = TableInfo(
            name='t',
            columns=[ColumnInfo(name='note', raw_type='text', is_nullable=False, description='see x = Field(1)')],
        )
        out = _emit(Schema(tables=[table]), EmitterConfig(generate_crud_models=False))
        assert 'pydantic.Field' in _imported(out)
        compile(out, '<generated>', 'exec')


# --------------------------------------------------------------------------- config matrix


@pytest.mark.unit
class TestConfigMatrix:
    def test_crud_models_disabled(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema, EmitterConfig(generate_crud_models=False))
        assert 'class UserInsert' not in out
        assert 'class UserUpdate' not in out
        assert 'class CustomModelInsert' not in out
        assert 'class UserBaseSchema' in out

    def test_enums_disabled_falls_back_to_str(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema, EmitterConfig(generate_enums=False))
        assert 'class PublicUserStatusEnum' not in out
        assert 'status: str' in out
        assert 'flags: list[str] | None' in out
        assert 'enum.Enum' not in _imported(out)

    def test_singular_names(self, build_columns: Callable[..., Row]) -> None:
        columns = [build_columns('users', 'id', 'integer', identity=True)]
        constraints = [('users_pkey', 'users', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []), EmitterConfig(singular_names=True))
        assert 'class UserBaseSchema' in out
        assert 'class UsersBaseSchema' not in out

    def test_add_null_parent_classes(self, build_columns: Callable[..., Row]) -> None:
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'name', 'text'),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []), EmitterConfig(add_null_parent_classes=True))
        assert 'class TParent(CustomModel):' in out
        assert 'class TBaseSchema(TParent):' in out
        # Parent fields are all nullable.
        parent_block = out.split('class TParent(CustomModel):')[1].split('class TBaseSchema')[0]
        assert 'name: str | None = Field(default=None)' in parent_block
        assert 'id: int | None = Field(default=None)' in parent_block


@pytest.mark.unit
class TestModelPrefixProtection:
    def _table(self) -> TableInfo:
        return TableInfo(
            name='thing',
            columns=[
                ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True),
                ColumnInfo(name='model_type', raw_type='varchar', is_nullable=True),
            ],
        )

    def test_configdict_absent_by_default(self) -> None:
        out = _emit(Schema(tables=[self._table()]))
        assert 'ConfigDict(protected_namespaces=())' not in out
        assert 'pydantic.ConfigDict' not in _imported(out)

    def test_configdict_added_when_disabled(self) -> None:
        out = _emit(Schema(tables=[self._table()]), EmitterConfig(disable_model_prefix_protection=True))
        assert 'model_config = ConfigDict(protected_namespaces=())' in out
        assert 'pydantic.ConfigDict' in _imported(out)

    def test_configdict_not_added_without_model_columns(self) -> None:
        table = TableInfo(
            name='thing', columns=[ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True)]
        )
        out = _emit(Schema(tables=[table]), EmitterConfig(disable_model_prefix_protection=True))
        assert 'ConfigDict' not in out


# --------------------------------------------------------------------------- FK relationships


@pytest.mark.unit
class TestForeignKeyFields:
    def _op_class(self, out: str, name: str) -> str:
        marker = f'class {name}('
        return out.split(marker, 1)[1].split('\nclass ', 1)[0]

    def test_relationship_types(self, relationship_tables: list[TableInfo]) -> None:
        out = _emit(Schema(tables=relationship_tables))
        post = self._op_class(out, 'Post')
        assert 'user: User | None = Field(default=None)' in post  # ONE_TO_ONE
        assert 'tags: list[Tag] | None = Field(default=None)' in post  # MANY_TO_MANY relationship
        user = self._op_class(out, 'User')
        assert 'posts: list[Post] | None = Field(default=None)' in user  # ONE_TO_MANY reverse
        tag = self._op_class(out, 'Tag')
        assert 'posts: list[Post] | None = Field(default=None)' in tag  # MANY_TO_MANY reverse

    def test_self_referential(self) -> None:
        employee = TableInfo(
            name='Employee',
            columns=[
                ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True),
                ColumnInfo(name='manager_id', raw_type='integer', is_nullable=True, is_foreign_key=True),
            ],
            foreign_keys=[
                ForeignKeyInfo(
                    constraint_name='Employee_manager_id_fkey',
                    column_name='manager_id',
                    foreign_table_name='Employee',
                    foreign_column_name='id',
                    relation_type=RelationType.ONE_TO_ONE,
                ),
            ],
            relationships=[
                RelationshipInfo(
                    table_name='Employee', related_table_name='Employee', relation_type=RelationType.ONE_TO_MANY
                ),
            ],
        )
        out = self._op_class(_emit(Schema(tables=[employee])), 'Employee')
        assert 'employee: Employee | None = Field(default=None)' in out
        assert 'employees: list[Employee] | None = Field(default=None)' in out

    def test_pluralization(self) -> None:
        book = TableInfo(
            name='Book',
            columns=[ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True)],
            foreign_keys=[
                ForeignKeyInfo(
                    constraint_name='c1',
                    column_name='category_id',
                    foreign_table_name='Category',
                    foreign_column_name='id',
                    relation_type=RelationType.MANY_TO_MANY,
                ),
                ForeignKeyInfo(
                    constraint_name='c2',
                    column_name='child_id',
                    foreign_table_name='Child',
                    foreign_column_name='id',
                    relation_type=RelationType.MANY_TO_MANY,
                ),
            ],
        )
        out = self._op_class(_emit(Schema(tables=[book])), 'Book')
        assert 'categories: list[Category] | None = Field(default=None)' in out
        assert 'children: list[Child] | None = Field(default=None)' in out

    def test_many_to_one_with_and_without_fk(self, build_columns: Callable[..., Row]) -> None:
        # Built through the pipeline so is_foreign_key + relation types are inferred.
        columns = [
            build_columns('user', 'id', 'integer', identity=True),
            build_columns('user', 'company_id', 'uuid'),
            build_columns('company', 'id', 'uuid'),
        ]
        fks = [('public', 'user', 'company_id', 'public', 'company', 'id', 'user_company_fk')]
        constraints = [
            ('user_pkey', 'user', ['id'], 'p', 'PRIMARY KEY (id)'),
            ('company_pkey', 'company', ['id'], 'p', 'PRIMARY KEY (id)'),
            ('user_company_fk', 'user', ['company_id'], 'f', 'FOREIGN KEY (company_id) REFERENCES company(id)'),
        ]
        out = _emit(build_schema(columns, fks, constraints, [], []))
        assert 'company: Company | None = Field(default=None)' in self._op_class(out, 'User')
        assert 'users: list[User] | None = Field(default=None)' in self._op_class(out, 'Company')

    def test_include_foreign_keys_disabled(self, relationship_tables: list[TableInfo]) -> None:
        out = _emit(Schema(tables=relationship_tables), EmitterConfig(include_foreign_keys=False))
        assert 'user: User' not in out
        assert '# Foreign Keys' not in out
        # Operational classes still emitted, but as pass.
        assert 'class Post(PostBaseSchema):' in out
        assert 'from __future__ import annotations' not in out

    def test_duplicate_field_names_suppressed(self) -> None:
        # Two FKs to the same target -> the second field name is suppressed.
        table = TableInfo(
            name='Link',
            columns=[
                ColumnInfo(name='a_id', raw_type='integer', is_nullable=False, is_foreign_key=True),
                ColumnInfo(name='b_id', raw_type='integer', is_nullable=False, is_foreign_key=True),
            ],
            foreign_keys=[
                ForeignKeyInfo(
                    constraint_name='c1',
                    column_name='a_id',
                    foreign_table_name='Node',
                    foreign_column_name='id',
                    relation_type=RelationType.ONE_TO_ONE,
                ),
                ForeignKeyInfo(
                    constraint_name='c2',
                    column_name='b_id',
                    foreign_table_name='Node',
                    foreign_column_name='id',
                    relation_type=RelationType.ONE_TO_ONE,
                ),
            ],
        )
        out = self._op_class(_emit(Schema(tables=[table])), 'Link')
        assert out.count('node: Node | None') == 1


# --------------------------------------------------------------------------- edge cases


@pytest.mark.unit
class TestEdgeCases:
    def test_empty_schema(self) -> None:
        out = _emit(Schema())
        assert 'class CustomModel(BaseModel):' in out
        # No table-derived classes or sections.
        assert 'BaseSchema' not in out
        assert '# BASE CLASSES' not in out
        assert '# OPERATIONAL CLASSES' not in out
        compile(out, '<empty>', 'exec')

    def test_view_has_no_primary_keys(self, build_columns: Callable[..., Row]) -> None:
        columns = [
            build_columns('v_users', 'id', 'integer', table_type='VIEW'),
            build_columns('v_users', 'name', 'text', table_type='VIEW'),
        ]
        out = _emit(build_schema(columns, [], [], [], []))
        assert 'class VUsersBaseSchema(CustomModel):' in out
        base_block = out.split('class VUsersBaseSchema(CustomModel):')[1].split('\n\n\n')[0]
        assert '# Primary Keys' not in base_block
        compile(out, '<view>', 'exec')

    def test_table_with_no_columns(self) -> None:
        out = _emit(Schema(tables=[TableInfo(name='empty')]))
        assert 'class EmptyBaseSchema(CustomModel):' in out
        compile(out, '<noc>', 'exec')

    def test_non_pk_identity_column_omitted_from_crud(self, build_columns: Callable[..., Row]) -> None:
        # A non-primary identity column is still omitted from Insert/Update.
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'seq', 'integer', identity=True),
            build_columns('t', 'name', 'text'),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []))
        insert_block = out.split('class TInsert(')[1].split('\n\n\n')[0]
        assert 'seq:' not in insert_block
        assert 'name:' in insert_block
        # The base Row model still carries it.
        base_block = out.split('class TBaseSchema(')[1].split('\n\n\n')[0]
        assert 'seq: int' in base_block


@pytest.mark.unit
class TestParseLengthConstraintHelper:
    def test_none_and_empty(self) -> None:
        from castiron.emitters.pydantic.emitter import _parse_length_constraint

        assert _parse_length_constraint(None) is None
        assert _parse_length_constraint('') is None

    def test_no_length_clause(self) -> None:
        from castiron.emitters.pydantic.emitter import _parse_length_constraint

        assert _parse_length_constraint("CHECK (name <> '')") is None

    def test_equality_sets_both_bounds(self) -> None:
        from castiron.emitters.pydantic.emitter import _parse_length_constraint

        assert _parse_length_constraint('CHECK (length(x) = 5)') == {'min_length': 5, 'max_length': 5}


@pytest.mark.unit
class TestForeignKeyFieldBranches:
    """Directly exercise every relation-type / has-fk branch of the FK field renderer."""

    def _table(self, has_fk_column: bool) -> TableInfo:
        columns = [ColumnInfo(name='ref_id', raw_type='integer', is_nullable=False, is_foreign_key=has_fk_column)]
        return TableInfo(name='Source', columns=columns)

    @pytest.mark.parametrize(
        ('relation', 'has_fk', 'expected'),
        [
            (RelationType.ONE_TO_ONE, True, 'target: Target | None = Field(default=None)'),
            (RelationType.MANY_TO_ONE, True, 'target: Target | None = Field(default=None)'),
            (RelationType.MANY_TO_ONE, False, 'targets: list[Target] | None = Field(default=None)'),
            (RelationType.ONE_TO_MANY, True, 'target: Target | None = Field(default=None)'),
            (RelationType.ONE_TO_MANY, False, 'targets: list[Target] | None = Field(default=None)'),
            (RelationType.MANY_TO_MANY, True, 'targets: list[Target] | None = Field(default=None)'),
            (None, True, 'targets: list[Target] | None = Field(default=None)'),
        ],
    )
    def test_relation_branches(self, relation: RelationType | None, has_fk: bool, expected: str) -> None:
        emitter = PydanticEmitter()
        result = emitter._foreign_key_field(self._table(has_fk), 'Target', 'ref_id', relation)
        assert result == expected


@pytest.mark.unit
class TestDefaultedColumnInsert:
    def test_non_nullable_defaulted_column_is_optional_in_insert(self, build_columns: Callable[..., Row]) -> None:
        # A non-nullable column with a DB default is required for the Row model but
        # optional for Insert (the default fills it in).
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'active', 'boolean', default='true'),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []))
        base_block = out.split('class TBaseSchema(')[1].split('\n\n\n')[0]
        assert 'active: bool' in base_block
        assert 'active: bool | None' not in base_block
        insert_block = out.split('class TInsert(')[1].split('\n\n\n')[0]
        assert 'active: bool | None = Field(default=None)' in insert_block
        assert '# Optional fields' in insert_block


# --------------------------------------------------------------------------- string literals


#: The adversarial alphabet every generated string literal must survive. A SQL comment is
#: arbitrary user text landing inside a Python source file, so these are the encodings the
#: input actually arrives in (CI-063), not a sample.
ADVERSARIAL_TEXT = [
    'line one\nline two',
    'para one\n\npara two',
    'a\r\nb',
    'a\rb',
    'The "app" users.',
    'Ends a docstring: """ oops',
    '""""',
    'ends with a quote "',
    'Windows path C:\\temp\\new and a regex \\d+',
    'ends with a backslash \\',
    'Ünïcødé — 表 — 🚀',
    'a\tb',
    'a\x0cb',
    'a\u2028b',
    'a\x1bb',
    'a\x00b',
    '\x00',
    '\n\n  Indented?\n\n',
    'Summary here.\n  - bullet one\n  - bullet two',
    'Note:\nThis is a Primary Key.<pk/>',
    '   ',
    '',
    'x' * 400,
]

#: The corpus the **executing** enum test runs over: CI-009's adversarial text (which exercises
#: the value literal and the comment) UNION the naming corpus (which exercises the member name).
#: Order-preserving and de-duplicated, so the parametrization ids stay stable.
EXECUTED_ENUM_LABELS = list(dict.fromkeys([*ADVERSARIAL_TEXT, *ENUM_LABEL_CORPUS]))


def _parametrized_argvalues(func: object, argname: str) -> list[object]:
    """Return the values a ``@pytest.mark.parametrize`` decorator actually feeds ``argname``.

    ⚠ Reads the decorator rather than the constant the decorator is *supposed* to name. A guard
    that asserts a relationship between two module-level constants cannot see a test being
    re-pointed at a third one, which is exactly how round 0's defect would have been re-committed
    invisibly.
    """
    for mark in getattr(func, 'pytestmark', []):
        if mark.name == 'parametrize' and mark.args[0] == argname:
            return list(mark.args[1])
    raise AssertionError(f'{func!r} has no @parametrize over {argname!r} -- the guard is blind')


@pytest.mark.unit
class TestPyStringLiteral:
    """``_py_string`` must round-trip any text a SQL comment can carry.

    Regression guard for a live bug on ``main`` @ ``026af0f`` (CI9-Q1): the helper escaped
    only ``\\`` and ``"``, so a newline emitted an unterminated string literal and the
    generated module raised ``SyntaxError`` at import. A multi-line ``COMMENT ON COLUMN``
    is ordinary SQL and PostgREST carries it verbatim in
    ``properties.<c>.description``.
    """

    @pytest.mark.parametrize('text', ADVERSARIAL_TEXT)
    def test_round_trips_through_literal_eval(self, text: str) -> None:
        from castiron.emitters.pydantic.emitter import _py_string

        assert ast.literal_eval(_py_string(text)) == text

    @pytest.mark.parametrize('text', ADVERSARIAL_TEXT)
    def test_literal_is_single_line(self, text: str) -> None:
        """No raw newline may survive into the literal -- that is the actual defect."""
        from castiron.emitters.pydantic.emitter import _py_string

        rendered = _py_string(text)
        assert '\n' not in rendered
        assert '\r' not in rendered

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            ('a\tb', '"a\\tb"'),
            ('a\x08b', '"a\\bb"'),
            ('a\x0cb', '"a\\fb"'),
            ('a\x0bb', '"a\\u000bb"'),
            ('a\x07b', '"a\\u0007b"'),
            ('a\x1bb', '"a\\u001bb"'),
            ('a\x00b', '"a\\u0000b"'),
        ],
    )
    def test_control_characters_are_escaped_not_raw(self, raw: str, expected: str) -> None:
        r"""The deliberate behaviour change vs. the old helper, stated precisely.

        ⚠ This is **not** a strict no-op, and it is not only the TAB. The previous escaping
        emitted every one of these raw inside the literal -- legal Python, but unescaped;
        ``json.dumps`` escapes them. All parse either way, so nothing was broken before and
        nothing is broken now, but the escaped rendering is the correct one and is what a
        reader of generated code expects.

        Enumerated rather than sampled (CI-072), and pinned so the difference stays
        deliberate. No committed golden contains a tab, a newline or a control character,
        which is why neither golden moves. The genuinely *fixed* inputs -- ``\n``, ``\r``,
        ``\r\n`` -- are covered by the round-trip tests above; those produced a module that
        did not parse at all.
        """
        from castiron.emitters.pydantic.emitter import _py_string

        assert _py_string(raw) == expected
        assert raw[1] not in _py_string(raw)
        assert ast.literal_eval(_py_string(raw)) == raw

    def test_non_ascii_is_not_escaped(self) -> None:
        """``ensure_ascii=False`` keeps generated text readable (the file is written UTF-8)."""
        from castiron.emitters.pydantic.emitter import _py_string

        assert _py_string('Ünïcødé 🚀') == '"Ünïcødé 🚀"'

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('A short profile blurb.', '"A short profile blurb."'),
            ('The customer who placed the order.', '"The customer who placed the order."'),
            ('User email', '"User email"'),
            ('class', '"class"'),
            ('active', '"active"'),
            ('quote "q"', '"quote \\"q\\""'),
            ('back\\slash', '"back\\\\slash"'),
        ],
    )
    def test_golden_literals_are_unchanged(self, text: str, expected: str) -> None:
        """Every literal shape present in a committed golden renders exactly as before."""
        from castiron.emitters.pydantic.emitter import _py_string

        assert _py_string(text) == expected

    def test_multiline_column_comment_emits_parseable_python(self, build_columns: Callable[..., Row]) -> None:
        """End-to-end: on ``main`` this module raised ``SyntaxError`` at ``compile()``."""
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'note', 'text', nullable=True, description='line one\nline two'),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []))

        compile(out, '<generated>', 'exec')
        assert 'description="line one\\nline two"' in out

    @pytest.mark.parametrize('text', ADVERSARIAL_TEXT)
    def test_any_column_comment_emits_parseable_python(self, text: str, build_columns: Callable[..., Row]) -> None:
        """Every adversarial shape survives the ``Field(description=...)`` path."""
        columns = [
            build_columns('t', 'id', 'integer', identity=True),
            build_columns('t', 'note', 'text', nullable=True, description=text),
        ]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        out = _emit(build_schema(columns, [], constraints, [], []))

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')

    @pytest.mark.parametrize('text', ADVERSARIAL_TEXT)
    def test_any_enum_label_renders_a_valid_value_literal(self, text: str, build_columns: Callable[..., Row]) -> None:
        """The other reachable ``_py_string`` call site: an enum member's *value*.

        The value literal (the right-hand side) is ``CI9-Q1``'s; the member *name* (the left-hand
        side) is ``CI-080``'s. This test asserted only the former and **explicitly declined** to
        assert that the module compiles, because ``python_member_name`` was ``value.lower()`` and
        ``CREATE TYPE mood AS ENUM ('in progress')`` emitted ``IN PROGRESS = "in progress"``. That
        developer was right not to pin a live defect as correct (``CI-074``).

        CI-080 is now **fixed**, so the split is *asserted* rather than tolerated: the value
        literal is still exactly the label, **and** the module now parses. Keeping both halves in
        one test is what stops a future change from trading one for the other -- a name transform
        that also "helpfully" normalized the value would satisfy either assertion alone.
        """
        from castiron.emitters.pydantic.emitter import _py_string

        columns = [build_columns('t', 'status', 'USER-DEFINED', udt_name='mood')]
        enum_types = [('mood', 'public', 'owner', 'E', True, 'e', [text, 'other'])]
        enum_mapping = [('status', 't', 'public', 'mood', 'E', '')]
        out = _emit(build_schema(columns, [], [], enum_types, enum_mapping))

        literal = _py_string(text)
        assert f'= {literal}' in out
        assert ast.literal_eval(literal) == text
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')

    @pytest.mark.parametrize('text', EXECUTED_ENUM_LABELS)
    def test_any_enum_label_yields_an_addressable_member(self, text: str, build_columns: Callable[..., Row]) -> None:
        """Parsing is not enough -- ``ﬁ``/``fi`` parse fine and raise ``TypeError`` at *import*.

        So the module is actually **executed** here and the enum class looked up by value. A
        ``compile()``-only assertion would pass on a module whose members collapse into one
        binding (``CI94-Q1``'s NFKC ruling), whose name Python mangles, or which ``EnumMeta``
        rejects or silently drops.

        🔴 **The corpus, not the assertion, is what failed in fix round 0.** This test was already
        the right shape -- it executes -- but it ran over ``ADVERSARIAL_TEXT`` alone, which is
        CI-009's *docstring and comment* corpus and contains no symmetric-punctuation label. The
        trigger (``'"quoted"'`` -> ``_QUOTED_``, a ``_sunder_`` name) was sitting committed in the
        *naming* corpus the whole time, in a test that only asserted ``.isidentifier()``. The
        right corpus and the right assertion existed and were pointed at each other's targets, so
        they now run over the **union** and ``test_the_emitter_executes_the_naming_corpus_too``
        fails if that is ever undone.
        """
        columns = [build_columns('t', 'status', 'USER-DEFINED', udt_name='mood')]
        enum_types = [('mood', 'public', 'owner', 'E', True, 'e', [text, 'other'])]
        enum_mapping = [('status', 't', 'public', 'mood', 'E', '')]
        out = _emit(build_schema(columns, [], [], enum_types, enum_mapping))

        namespace: dict[str, object] = {}
        exec(compile(out, '<generated>', 'exec'), namespace)  # noqa: S102 - executing our own output IS the assertion
        enum_class = namespace['PublicMoodEnum']
        assert enum_class(text).value == text  # type: ignore[operator]  # a runtime Enum lookup
        assert enum_class('other').value == 'other'  # type: ignore[operator]
        assert len(list(enum_class)) == 2, 'a label was dropped or two members collapsed into one'  # type: ignore[call-overload]

    def test_the_whole_naming_corpus_emits_one_working_enum(self, build_columns: Callable[..., Row]) -> None:
        # All 54 hostile labels in ONE emitted enum, executed. The single-label parametrization
        # above cannot see a collision between two of them, and the collision suffix is exactly
        # what produced the name-mangled `__2` that dropped four labels on py3.11+.
        columns = [build_columns('t', 'status', 'USER-DEFINED', udt_name='mood')]
        enum_types = [('mood', 'public', 'owner', 'E', True, 'e', list(ENUM_LABEL_CORPUS))]
        enum_mapping = [('status', 't', 'public', 'mood', 'E', '')]
        out = _emit(build_schema(columns, [], [], enum_types, enum_mapping))

        namespace: dict[str, object] = {}
        exec(compile(out, '<generated>', 'exec'), namespace)  # noqa: S102 - see above
        enum_class = namespace['PublicMoodEnum']
        assert len(list(enum_class)) == len(ENUM_LABEL_CORPUS), 'a label was dropped or mangled away'  # type: ignore[call-overload]
        for label in ENUM_LABEL_CORPUS:
            assert enum_class(label).value == label  # type: ignore[operator]

    def test_the_emitter_executes_the_naming_corpus_too(self) -> None:
        """The executing test must never again run over a corpus missing the naming shapes.

        ⚠ **This reads the ``@parametrize`` decorator, not the constant it is supposed to use.**
        The first version asserted ``set(ENUM_LABEL_CORPUS) <= set(EXECUTED_ENUM_LABELS)`` -- true
        *by construction*, since ``EXECUTED_ENUM_LABELS`` is defined as that union. It never
        looked at what the executing test is actually parametrized over, so it fired on a spelling
        of the mistake that never happened and stayed green on the one that did: re-pointing the
        decorator at ``ADVERSARIAL_TEXT`` (round 0's exact defect) left the whole suite passing.
        Mutation-tested both ways this time.
        """
        parametrized = _parametrized_argvalues(
            TestPyStringLiteral.test_any_enum_label_yields_an_addressable_member, 'text'
        )
        assert set(ENUM_LABEL_CORPUS) <= set(parametrized), (
            'the executing enum test is parametrized over a corpus that omits '
            f'{sorted(set(ENUM_LABEL_CORPUS) - set(parametrized))!r}. That is the round-0 defect: '
            'the assertion executes the module, but over labels that cannot produce the shape.'
        )
        assert set(ADVERSARIAL_TEXT) <= set(parametrized), 'CI-009 coverage was dropped'

    @pytest.mark.parametrize('text', ADVERSARIAL_TEXT)
    def test_any_column_alias_emits_parseable_python(self, text: str) -> None:
        """The third call site: ``Field(alias=...)``, set for a reserved column name."""
        table = TableInfo(
            name='t',
            columns=[ColumnInfo(name='field_class', raw_type='text', is_nullable=False, alias=text)],
        )
        out = _emit(Schema(tables=[table]))

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')


# --------------------------------------------------------------------------- table docstrings


def _described(description: str | None, name: str = 'users') -> Schema:
    """A one-table schema whose table carries ``description``."""
    return Schema(
        tables=[
            TableInfo(
                name=name,
                description=description,
                columns=[ColumnInfo(name='id', raw_type='integer', is_nullable=False, primary=True)],
            )
        ]
    )


@pytest.mark.unit
class TestTableDocstring:
    """``TableInfo.description`` rendered as a class-docstring body paragraph (CI-009).

    Captain's ruling CI9-Q2 (A): **every** generated class for the table carries it, with
    no per-class exception. It is appended after the summary line and never replaces it --
    the summary is the only line that says which variant you are reading.
    """

    def test_no_description_is_byte_identical_to_the_pre_ci_009_form(self) -> None:
        out = _emit(_described(None))

        assert '    """Users Base Schema."""' in out
        assert '    """Users Insert Schema."""' in out
        assert '    """Users Update Schema."""' in out

    def test_a_single_line_description_renders_the_exact_block(self) -> None:
        out = _emit(_described('Application users.'))

        assert (
            'class UsersBaseSchema(CustomModel):\n    """Users Base Schema.\n\n    Application users.\n    """' in out
        )

    def test_the_operational_class_puts_it_between_summary_and_trailer(self) -> None:
        out = _emit(_described('Application users.'))

        assert (
            'class Users(UsersBaseSchema):\n'
            '    """Users Schema for Pydantic.\n'
            '\n'
            '    Application users.\n'
            '\n'
            '    Inherits from UsersBaseSchema. Add any customization here.\n'
            '    """'
        ) in out

    def test_the_summary_line_is_never_replaced(self) -> None:
        """Each variant keeps its own identity line."""
        out = _emit(_described('Application users.'))

        for summary in ('Users Base Schema.', 'Users Insert Schema.', 'Users Update Schema.'):
            assert f'    """{summary}\n' in out

    def test_every_generated_class_carries_it(self) -> None:
        """CI9-Q2 (A). CI6-Q7: enumerate the classes rather than sampling one."""
        out = _emit(_described('Application users.'))

        assert out.count('    Application users.\n') == 4
        for header in (
            'class UsersBaseSchema(CustomModel):',
            'class UsersInsert(CustomModelInsert):',
            'class UsersUpdate(CustomModelUpdate):',
            'class Users(UsersBaseSchema):',
        ):
            block = out.split(header)[1].split('\n\n\n')[0]
            assert 'Application users.' in block, header

    def test_the_parent_class_carries_it_too(self) -> None:
        """The opt-in fifth class kind — the uniform rule has no exception."""
        out = _emit(_described('Application users.'), EmitterConfig(add_null_parent_classes=True))

        assert 'class UsersParent(CustomModel):' in out
        assert out.count('    Application users.\n') == 5

    @pytest.mark.parametrize('blank', ['', '   ', '\n\t\n', '\r\n', '  \r\n  \t ', '\x00', '\x00\x00\x00', ' \x00 '])
    def test_a_blank_description_emits_byte_identically_to_none(self, blank: str) -> None:
        """No stray blank line, no empty paragraph — 'no comment' has one rendering."""
        assert _emit(_described(blank)) == _emit(_described(None))

    def test_a_described_and_an_undescribed_table_coexist(self) -> None:
        schema = Schema(
            tables=[
                TableInfo(
                    name='users',
                    description='Application users.',
                    columns=[ColumnInfo(name='id', raw_type='integer', is_nullable=False)],
                ),
                TableInfo(
                    name='products',
                    columns=[ColumnInfo(name='id', raw_type='integer', is_nullable=False)],
                ),
            ]
        )
        out = _emit(schema)

        assert '    """Users Base Schema.\n\n    Application users.\n    """' in out
        assert '    """Products Base Schema."""' in out

    def test_emitting_twice_is_byte_identical_with_a_description(self) -> None:
        """Hard Rule #9 with the new field populated."""
        schema = _described('Application users.')
        assert _emit(schema) == _emit(schema)

    def test_a_multi_line_description_preserves_relative_indentation(self) -> None:
        out = _emit(_described('Application users.\n  - soft-deleted rows are kept\n  - see also: orders'))

        assert (
            '    """Users Base Schema.\n'
            '\n'
            '    Application users.\n'
            '      - soft-deleted rows are kept\n'
            '      - see also: orders\n'
            '    """'
        ) in out

    def test_a_blank_line_inside_the_body_carries_no_indent(self) -> None:
        """Trailing whitespace in generated code is a W291 for the user and pure diff noise."""
        out = _emit(_described('Para one.\n\nPara two.'))

        assert '    """Users Base Schema.\n\n    Para one.\n\n    Para two.\n    """' in out
        assert '    \n' not in out

    def test_no_generated_line_has_trailing_whitespace(self) -> None:
        out = _emit(_described('Line one.   \n   \nLine two.\t'))

        assert not [line for line in out.splitlines() if line != line.rstrip()]

    def test_a_long_comment_is_not_wrapped_or_truncated(self) -> None:
        """``section_comment``'s 120-col reflow must NOT be reused here."""
        long_text = 'x' * 400
        out = _emit(_described(long_text))

        assert f'    {long_text}\n' in out


@pytest.mark.unit
class TestTableDocstringAdversarial:
    """The mandatory adversarial matrix (L7 / CI-063).

    A table comment is arbitrary user text landing inside a Python **docstring** in
    generated code. Each shape must (a) compile with warnings escalated to errors — a
    ``SyntaxWarning: invalid escape sequence`` fails the test — and (b) round-trip into the
    class's ``__doc__``.
    """

    #: ``(comment, what it would break without the corresponding rule)``.
    CASES = [
        ('Line one.\nLine two.', 'multi-line body'),
        ('Para one.\n\nPara two.', 'blank line inside the body'),
        ('Line one.\r\nLine two.\r\n', 'CR in generated output (CI-063)'),
        ('Line one.\rLine two.', 'lone CR'),
        ('The "app" users.', 'quote escaping'),
        ('Ends a docstring: """ oops', 'docstring injection — terminates the literal early'),
        ('""""', 'four quotes'),
        ('ends with a quote "', 'quote adjacent to the closing delimiter'),
        ('Windows path C:\\temp\\new and a regex \\d+', 'tab/newline escapes; \\d SyntaxWarning'),
        ('ends with a backslash \\', 'escapes the closing delimiter'),
        ('Ünïcødé — 表 — 🚀', 'non-ASCII'),
        ('a\tb', 'tab'),
        ('a\x0cb', 'FORM FEED — splitlines() would split here'),
        ('a\u2028b', 'LINE SEPARATOR — splitlines() would split here'),
        ('a\x1bb', 'ESC control character'),
        ('a\x00b', 'NUL -- the ONE code point no escaping saves; raw NUL breaks import'),
        ('\x00\x00', 'NUL-only -- must emit as if absent (D6)'),
        ('  \x00  ', 'NUL surrounded by whitespace -- still absent (D6)'),
        ('\n\n  Indented?\n\n', 'leading/trailing blank lines'),
        ('Summary here.\n  - bullet one\n  - bullet two', 'relative indentation preserved'),
        ('Note:\nThis is a Primary Key.<pk/>', 'PostgREST column marker text passes through'),
        ('   ', 'whitespace only — must emit as if absent'),
        ('', 'empty — must emit as if absent'),
        ('x' * 400, 'no wrapping, no truncation, no 120-col reflow'),
    ]

    @pytest.mark.parametrize(('text', 'why'), CASES, ids=[repr(c[0])[:40] for c in CASES])
    def test_the_module_compiles_with_warnings_as_errors(self, text: str, why: str) -> None:
        out = _emit(_described(text))

        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')

    @pytest.mark.parametrize(('text', 'why'), CASES, ids=[repr(c[0])[:40] for c in CASES])
    def test_the_comment_round_trips_out_of_the_emitted_source(self, text: str, why: str) -> None:
        r"""The original comment must be recoverable from the **emitted module**.

        Read via :mod:`ast`, deliberately, and asserted **exactly** rather than by
        containment.

        ⚠ Do not reach for ``exec`` + ``__doc__`` here. **CPython 3.13 dedents docstrings at
        compile time** (and expands tabs to the 8-column tab stop while doing so), so
        ``a\tb`` arrives as ``a   b`` on 3.13 and as ``a\tb`` on 3.10-3.12. That is CPython's
        *rendering* of our output, not our output: the emitted ``.py`` bytes are identical on
        every interpreter, which is what Hard Rule #9 governs. The dedent is a **compiler**
        step, not a parser step, so ``ast`` sees the same literal on 3.10, 3.12 and 3.13 --
        verified on all three.

        This assertion is the exact inverse of the renderer (normalize -> strip -> escape ->
        indent), so it is a genuine round-trip rather than a mirror of the implementation:
        ``ast`` returns the *decoded* string, so the escaping is undone by the parser, not by
        the test.
        """
        module = ast.parse(_emit(_described(text)))
        cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'UsersBaseSchema')
        doc = ast.get_docstring(cls, clean=False)
        assert doc is not None

        normalized = text.replace('\r\n', '\n').replace('\r', '\n').replace('\x00', '').strip()
        expected = '\n'.join(line.rstrip() for line in normalized.split('\n')) if normalized else ''

        if not expected:
            assert doc == 'Users Base Schema.'
            return

        prefix, suffix = 'Users Base Schema.\n\n', f'\n{IND}'
        assert doc.startswith(prefix), doc
        assert doc.endswith(suffix), doc
        body = doc[len(prefix) : -len(suffix)]
        recovered = '\n'.join(line[len(IND) :] if line else '' for line in body.split('\n'))

        assert recovered == expected

    @pytest.mark.parametrize(('text', 'why'), CASES, ids=[repr(c[0])[:40] for c in CASES])
    def test_the_emitted_bytes_do_not_depend_on_the_interpreter(self, text: str, why: str) -> None:
        """Hard Rule #9 is about the bytes we write, which no interpreter version rewrites.

        Pinned separately from the round-trip so the 3.13 docstring-dedent behaviour can
        never be mistaken for a determinism regression in the emitter.
        """
        out = _emit(_described(text))

        assert out == _emit(_described(text))
        assert ast.dump(ast.parse(out)) == ast.dump(ast.parse(out))

    @pytest.mark.parametrize(('text', 'why'), CASES, ids=[repr(c[0])[:40] for c in CASES])
    def test_no_carriage_return_reaches_generated_output(self, text: str, why: str) -> None:
        """Byte stability across platforms (Hard Rule #9)."""
        assert '\r' not in _emit(_described(text))

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            # \u26a0 Exact bytes, not "each line appears somewhere". A `\r\n` folded by
            # `.replace('\r', '\n')` alone yields TWO newlines, which a containment
            # assertion happily accepts and which silently doubles the paragraph.
            ('Line one.\r\nLine two.\r\n', '    """Users Base Schema.\n\n    Line one.\n    Line two.\n    """'),
            ('Line one.\rLine two.', '    """Users Base Schema.\n\n    Line one.\n    Line two.\n    """'),
            ('Line one.\nLine two.', '    """Users Base Schema.\n\n    Line one.\n    Line two.\n    """'),
            ('a\r\n\r\nb', '    """Users Base Schema.\n\n    a\n\n    b\n    """'),
            ('\r\na\r\n', '    """Users Base Schema.\n\n    a\n    """'),
        ],
        ids=['crlf', 'lone-cr', 'lf', 'crlf-blank-line', 'crlf-wrapped'],
    )
    def test_line_endings_normalize_to_exact_bytes(self, text: str, expected: str) -> None:
        """Every CR spelling must collapse to the *same* bytes as the LF spelling."""
        assert expected in _emit(_described(text))

    def test_all_three_line_ending_spellings_emit_identically(self) -> None:
        crlf = _emit(_described('Line one.\r\nLine two.'))
        cr = _emit(_described('Line one.\rLine two.'))
        lf = _emit(_described('Line one.\nLine two.'))

        assert crlf == lf
        assert cr == lf

    @pytest.mark.parametrize(
        'char',
        ['\x0b', '\x0c', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'],
    )
    def test_only_lf_is_treated_as_a_line_break(self, char: str) -> None:
        """⚠ ``str.splitlines()`` also breaks on every one of these.

        Using it would silently re-indent a legal Postgres comment at that point.
        Enumerated, not sampled (CI-072).
        """
        out = _emit(_described(f'a{char}b'))

        assert f'    a{char}b\n' in out

    def test_a_triple_quote_cannot_terminate_the_docstring_early(self) -> None:
        """The injection case, asserted on the bytes rather than only via compile()."""
        out = _emit(_described('Ends a docstring: """ oops'))

        assert '    Ends a docstring: \\"\\"\\" oops\n' in out
        namespace: dict[str, object] = {}
        exec(out, namespace)  # noqa: S102
        assert 'Ends a docstring: """ oops' in namespace['UsersBaseSchema'].__doc__  # type: ignore[union-attr,operator]

    def test_a_trailing_backslash_cannot_escape_the_closing_delimiter(self) -> None:
        out = _emit(_described('ends with a backslash \\'))

        assert '    ends with a backslash \\\\\n    """' in out
        namespace: dict[str, object] = {}
        exec(out, namespace)  # noqa: S102
        # The docstring's own closing indent follows, so this is containment, not a suffix:
        # what matters is that ONE literal backslash survived and did not eat the delimiter.
        doc = namespace['UsersBaseSchema'].__doc__  # type: ignore[union-attr]
        assert 'ends with a backslash \\\n' in doc


@pytest.mark.unit
class TestCi009BackwardCompatibility:
    def test_a_description_less_schema_emits_byte_identically_after_ci_009(self, representative_schema: Schema) -> None:
        """CI-009 backward-compat guard: adding ``TableInfo.description`` changed no output.

        ``representative_schema`` is the CI-004 golden anchor and deliberately carries **no**
        table description — it is the unchanged control. An unchanged CI-004 golden is the
        proof that this row is additive.
        """
        assert all(t.description is None for t in representative_schema.tables)
        assert _emit(representative_schema) == GOLDEN.read_text(encoding='utf-8')


@pytest.mark.unit
class TestDocstringTextHelper:
    """Direct contract tests for ``_docstring_text``.

    Its documented return for "nothing to render" is ``None``, not ``''``. Only
    ``_class_docstring`` calls it today, and that caller filters on truthiness — so an
    implementation returning ``''`` is invisible end to end. Pinning the helper directly
    keeps the stated contract real rather than incidental, and stops a future second caller
    from inheriting an undocumented empty string.
    """

    @pytest.mark.parametrize('blank', [None, '', '   ', '\n\t\n', '\r\n', '  \r\n  \t '])
    def test_nothing_to_render_returns_none(self, blank: str | None) -> None:
        from castiron.emitters.pydantic.emitter import _docstring_text

        assert _docstring_text(blank) is None

    @pytest.mark.parametrize(
        ('raw', 'expected'),
        [
            ('Application users.', '    Application users.'),
            ('  Application users.  ', '    Application users.'),
            ('a\r\nb', '    a\n    b'),
            ('a\rb', '    a\n    b'),
            ('a\nb', '    a\n    b'),
            ('a\n\nb', '    a\n\n    b'),
            ('a\n  indented', '    a\n      indented'),
            ('trailing   \nspace', '    trailing\n    space'),
            ('"quoted"', '    \\"quoted\\"'),
            ('back\\slash', '    back\\\\slash'),
        ],
    )
    def test_exact_rendering(self, raw: str, expected: str) -> None:
        from castiron.emitters.pydantic.emitter import _docstring_text

        assert _docstring_text(raw) == expected


@pytest.mark.unit
class TestClassDocstringHelper:
    """Direct contract tests for ``_class_docstring`` (assembly, not escaping)."""

    def test_summary_only(self) -> None:
        from castiron.emitters.pydantic.emitter import _class_docstring

        assert _class_docstring('X Base Schema.', None) == '    """X Base Schema."""'

    def test_summary_and_description(self) -> None:
        from castiron.emitters.pydantic.emitter import _class_docstring

        assert _class_docstring('X Base Schema.', 'Users.') == '    """X Base Schema.\n\n    Users.\n    """'

    def test_summary_and_trailer_only(self) -> None:
        from castiron.emitters.pydantic.emitter import _class_docstring

        result = _class_docstring('X Schema for Pydantic.', None, '    Inherits from Y.')
        assert result == '    """X Schema for Pydantic.\n\n    Inherits from Y.\n    """'

    def test_description_precedes_the_trailer(self) -> None:
        from castiron.emitters.pydantic.emitter import _class_docstring

        result = _class_docstring('X Schema for Pydantic.', 'Users.', '    Inherits from Y.')
        assert result == '    """X Schema for Pydantic.\n\n    Users.\n\n    Inherits from Y.\n    """'

    @pytest.mark.parametrize('blank', ['', '   ', '\n\t\n'])
    def test_a_blank_description_is_indistinguishable_from_none(self, blank: str) -> None:
        from castiron.emitters.pydantic.emitter import _class_docstring

        assert _class_docstring('X Base Schema.', blank) == _class_docstring('X Base Schema.', None)


@pytest.mark.unit
class TestNulByte:
    """``U+0000`` is the one code point no escaping saves (reviewer finding, fix round 2).

    A raw NUL anywhere in a module makes CPython raise
    ``SyntaxError: source code string cannot contain null bytes`` at import -- so castiron
    would *write* schema.py successfully and the user's import would fail. That is the exact
    failure shape the ``_py_string`` fix exists to prevent, and before this fix the PR only
    half-closed it: the **column**-comment path went from broken to safe while the **new**
    table-docstring path became the one place a NUL still broke.

    Postgres text cannot contain NUL, but that is a property of one source, not of the input:
    the OpenAPI source accepts any JSON document via ``--from``, and ``\x00`` is a
    perfectly ordinary JSON escape.
    """

    NUL = '\x00'

    def test_a_nul_is_removed_from_the_docstring(self) -> None:
        out = _emit(_described(f'a{self.NUL}b'))

        assert self.NUL not in out
        assert '    ab\n' in out

    def test_the_emitted_module_compiles_and_has_no_nul_byte(self) -> None:
        out = _emit(_described(f'before{self.NUL}after'))

        assert self.NUL not in out
        assert out.encode('utf-8').count(b'\x00') == 0
        compile(out, '<generated>', 'exec')

    @pytest.mark.parametrize('text', ['\x00', '\x00\x00', '  \x00  ', '\n\x00\t'])
    def test_a_nul_only_comment_is_indistinguishable_from_absent(self, text: str) -> None:
        """Decision D6 -- and the reason NUL is *stripped* rather than escaped.

        Rendering it as a visible ``\\x00`` or as a real NUL escape would both make these
        emit a docstring body of invisible characters.
        """
        assert _emit(_described(text)) == _emit(_described(None))

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('\x00  a', '    a'),
            ('  a\x00', '    a'),
            ('\x00 a \x00', '    a'),
            ('\x00' + chr(10) + '  a', '    a'),
        ],
        ids=['nul-before-leading-ws', 'nul-after-trailing-ws', 'nul-both-sides', 'nul-before-newline'],
    )
    def test_nul_is_removed_before_stripping_not_after(self, text: str, expected: str) -> None:
        """Order matters: removing NUL *after* ``.strip()`` leaves stray indentation.

        ``.strip()`` does not treat NUL as whitespace, so for ``"\x00  a"`` a
        strip-then-remove implementation yields ``"  a"`` -- rendered as six spaces of
        indent instead of four. Found by mutant N3, which the symmetric NUL cases could not
        kill.
        """
        from castiron.emitters.pydantic.emitter import _docstring_text

        assert _docstring_text(text.replace('NUL', self.NUL)) == expected

    def test_the_ir_still_carries_the_nul(self) -> None:
        """Nothing is lost from the system of record -- only the rendered docstring drops it.

        The builder deliberately does **not** strip NUL, so drift detection still sees it.
        """
        table = TableInfo(name='users', description=f'a{self.NUL}b')

        assert table.description == f'a{self.NUL}b'
        assert Schema(tables=[table]).as_dict()['tables'][0]['description'] == f'a{self.NUL}b'

    def test_end_to_end_from_an_openapi_document(self) -> None:
        """The realistic path: any JSON document via ``--from``, not only PostgREST output."""
        from castiron.sources import build_schema_from_document

        document = {
            'swagger': '2.0',
            'definitions': {
                'users': {
                    'type': 'object',
                    'description': f'a{self.NUL}b',
                    'properties': {'id': {'type': 'integer', 'format': 'int32'}},
                }
            },
            'paths': {'/users': {'get': {}, 'post': {}}},
        }
        schema = build_schema_from_document(document)
        assert schema.tables[0].description == f'a{self.NUL}b'

        out = _emit(schema)
        assert out.encode('utf-8').count(b'\x00') == 0
        compile(out, '<generated>', 'exec')

    def test_the_column_comment_path_is_also_nul_safe(self) -> None:
        """The sibling path, fixed by commit 1 -- pinned so the two cannot diverge again."""
        from castiron.emitters.pydantic.emitter import _py_string

        rendered = _py_string(f'a{self.NUL}b')
        assert self.NUL not in rendered
        assert ast.literal_eval(rendered) == f'a{self.NUL}b'


@pytest.mark.unit
class TestCi080TheReproducerNowCompiles:
    """The CI-080 WORKPLAN reproducer, end to end through the real OpenAPI source.

    On ``main``, ``castiron gen`` on this document exited **0**, printed ``wrote .../schema.py``
    and emitted a module whose every model was unreachable::

        class PublicJobStateEnum(str, Enum):
            IN PROGRESS = "in progress"
            DONE! = "done!"
            2FAST = "2fast"          # SyntaxError: invalid decimal literal

    Exit 0 is what made it a release blocker rather than a bug: a ``check``-mode user would have
    seen green. Driven from the document rather than from a hand-built ``Schema`` so the whole
    path -- parse, build, emit -- is the thing under test.
    """

    LABELS = ['in progress', 'done!', '2fast']

    @pytest.fixture
    def emitted(self) -> str:
        document = {
            'swagger': '2.0',
            'info': {'title': 'ci-080', 'version': '0'},
            'paths': {'/jobs': {'get': {}, 'post': {}}},
            'definitions': {
                'jobs': {
                    'type': 'object',
                    'required': ['id', 'state'],
                    'properties': {
                        'id': {
                            'description': 'Note:\nThis is a Primary Key.<pk/>',
                            'format': 'int32',
                            'type': 'integer',
                        },
                        'state': {'enum': self.LABELS, 'format': 'public.job_state', 'type': 'string'},
                    },
                }
            },
        }
        return _emit(build_schema_from_document(document))

    def test_the_module_parses(self, emitted: str) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(emitted, '<generated>', 'exec')

    def test_the_module_imports_and_every_label_round_trips(self, emitted: str) -> None:
        # ⚠ Deliberately an execution, not a parse. `py_compile` cannot see a duplicate member
        # name -- that raises `TypeError` when the class body runs.
        namespace: dict[str, object] = {}
        exec(compile(emitted, '<generated>', 'exec'), namespace)  # noqa: S102 - executing our own output IS the assertion
        enum_class = namespace['PublicJobStateEnum']
        assert [m.name for m in enum_class] == ['IN_PROGRESS', 'DONE_', '_2FAST']  # type: ignore[union-attr]
        for label in self.LABELS:
            assert enum_class(label).value == label  # type: ignore[operator]

    def test_a_leading_underscore_member_is_addressable(self, emitted: str) -> None:
        # `_2FAST` is not `_sunder_`, not dunder and not name-mangled -- but the whole fix rests
        # on that, so it is asserted on the interpreter running the test rather than assumed.
        namespace: dict[str, object] = {}
        exec(compile(emitted, '<generated>', 'exec'), namespace)  # noqa: S102 - see above
        enum_class = namespace['PublicJobStateEnum']
        assert enum_class._2FAST.value == '2fast'  # type: ignore[union-attr]


@pytest.mark.unit
class TestCi080TheCommentIsTotalOverItsInput:
    """``CI94-D3``: the label inside the ``#`` comment goes through ``_py_string`` too.

    The CI-094 spec called this unreachable-but-correct. **It is reachable**, and only *because*
    of the CI-080 fix: a guard that fires on the *sanitized* name can be reached by a label whose
    raw text carries a newline, and the raw label would then split the comment across three lines
    and break the module. CI-009's standing lesson: a renderer that injects user text into
    generated source must be total over its input domain, not over its best-behaved caller.

    ⚠ **Which guard catches the example moved in fix round 1, and the test says so rather than
    being quietly re-pointed.** Originally ``'\\n\\ndoc\\n\\n'`` mapped to ``__DOC__`` and fired
    the *reserved-keyword* guard (``dir(builtins)`` contains ``__doc__``). The sunder/dunder guard
    now runs first and claims it, so the note reads ``reserved by Enum``. The reachability claim
    is unchanged -- the same label, the same comment line, the same newline -- and the collision
    route below never depended on a builtin at all.
    """

    @staticmethod
    def _module(labels: list[str]) -> str:
        table = TableInfo(
            name='t',
            columns=[ColumnInfo(name='c', raw_type='USER-DEFINED', is_nullable=True)],
        )
        return _emit(Schema(tables=[table], enums=[EnumInfo(name='mood', values=labels, schema='public')]))

    def test_a_newline_bearing_label_reaches_a_guard_comment(self) -> None:
        from castiron.utils.naming import python_member_names

        members = python_member_names(EnumInfo(name='mood', values=['\n\ndoc\n\n'], schema='public'))
        assert members[0].note == 'reserved by Enum', 'the premise: a newline-bearing label DOES reach a guard'
        assert members[0].name == '__DOC___'
        out = self._module(['\n\ndoc\n\n'])
        assert '# original name was "\\n\\ndoc\\n\\n" (reserved by Enum)' in out
        compile(out, '<generated>', 'exec')

    def test_a_newline_bearing_label_reaches_the_collision_comment(self) -> None:
        # The more direct route, and it needs no builtin at all: two labels that sanitize alike,
        # with the newline-bearing one SECOND so it is the one that carries a note.
        out = self._module(['a_b', 'a\nb'])
        assert '# original name was "a\\nb" (name collision)' in out
        compile(out, '<generated>', 'exec')

    @pytest.mark.parametrize('label', ADVERSARIAL_TEXT)
    def test_no_adversarial_label_can_break_a_comment(self, label: str) -> None:
        # Enumerated over the same 23 shapes the value-literal path uses. `'x' * 400` is in there,
        # so the comment is also proved not to need wrapping. The middle label is the one forced
        # through the collision path, between two labels that both sanitize to `A_B`.
        labels = ['a_b', label, 'a b']
        out = self._module(labels)
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')

        # ⚠ The assertion that actually detects a split comment: ONE line per member, so a label
        # that leaked a newline into the `#` comment shows up as an extra line even in the cases
        # where the result still happens to parse.
        #
        # ⚠ `split('\n')`, NOT `splitlines()`. They disagree, and the disagreement is the point:
        # `str.splitlines()` also breaks on U+2028/U+2029/U+000B/U+000C, which CPython's
        # *tokenizer* does not treat as line terminators -- and `ADVERSARIAL_TEXT` carries
        # `'a b'` precisely because it is that kind of trap. A comment is terminated by a
        # source line break, so the source's own delimiter is the one to count. (`_py_string`
        # already escapes `\r` and `\n`, which are the two that would really end a line.)
        body = out.split('class PublicMoodEnum(str, Enum):\n')[1].split('\n\n\n')[0]
        assert len(body.split('\n')) == len(labels), body


@pytest.mark.unit
class TestAnEmptyEnumStillEmitsAParseableModule:
    """``CREATE TYPE t AS ENUM ()`` is legal Postgres, and it produced an unparseable module.

    ⚠ **Pre-existing, not a CI-094 regression** -- verified byte-identical on ``origin/main`` @
    ``0a70513``. Folded into this row because it is the same defect class the row exists to close
    (unparseable output at exit 0) and this is the last code row before an immutable publish. It
    is a **separate commit** so it stays severable.

    The emitter wrote ``class PublicJobStateEnum(str, Enum):`` with no body -- an
    ``IndentationError`` that takes the whole module with it, exactly like CI-080 did.
    """

    @staticmethod
    def _document(labels: list[str]) -> dict[str, object]:
        return {
            'swagger': '2.0',
            'info': {'title': 'empty-enum', 'version': '0'},
            'paths': {'/jobs': {'get': {}, 'post': {}}},
            'definitions': {
                'jobs': {
                    'type': 'object',
                    'required': ['id'],
                    'properties': {
                        'id': {
                            'description': 'Note:\nThis is a Primary Key.<pk/>',
                            'format': 'int32',
                            'type': 'integer',
                        },
                        'state': {'enum': labels, 'format': 'public.job_state', 'type': 'string'},
                    },
                }
            },
        }

    def test_it_is_reachable_through_the_real_source_path(self) -> None:
        # Not a hand-built Schema: PostgREST's `"enum": []` really does reach `schema.enums`,
        # which is what makes this a user-facing defect rather than a theoretical one.
        schema = build_schema_from_document(self._document([]))
        assert [(e.name, e.values) for e in schema.enums] == [('job_state', [])]

    def test_the_emitted_module_parses(self) -> None:
        out = _emit(build_schema_from_document(self._document([])))
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            compile(out, '<generated>', 'exec')

    def test_the_empty_enum_class_exists_and_is_empty(self) -> None:
        # `pass`, not a skipped class: the column still annotates itself `PublicJobStateEnum`,
        # so omitting the class would trade an IndentationError for a NameError.
        out = _emit(build_schema_from_document(self._document([])))
        assert 'class PublicJobStateEnum(str, Enum):\n    pass' in out
        assert 'PublicJobStateEnum' in out.split('# BASE CLASSES')[1]

        namespace: dict[str, object] = {}
        exec(compile(out, '<generated>', 'exec'), namespace)  # noqa: S102 - executing IS the assertion
        assert list(namespace['PublicJobStateEnum']) == []  # type: ignore[call-overload]

    def test_a_non_empty_enum_gains_no_pass(self) -> None:
        # The counter-witness: `pass` must appear only when the body would otherwise be empty.
        out = _emit(build_schema_from_document(self._document(['ok'])))
        assert 'class PublicJobStateEnum(str, Enum):\n    OK = "ok"' in out
        assert 'Enum):\n    pass' not in out


@pytest.mark.unit
class TestCi114TheEnumImportTracksTheRegistry:
    """``CI-114``: the ``Enum`` import and the enum classes had **two sources of truth**.

    ``_imports`` gated ``from enum import Enum`` on a **column** carrying ``enum_info``, while
    :meth:`~castiron.emitters.pydantic.emitter.PydanticEmitter._enum_section` renders from
    ``schema.enums``. An enum reachable only through the registry therefore emitted its class with
    no import, and the module raised ``NameError: name 'Enum' is not defined`` at import -- with
    ``castiron gen`` exiting **0**. Same defect class as CI-080 and CI-110: output that does not
    run, reported as success.

    🔴 **Why the existing tests did not catch it, stated so it is not repeated.**
    :class:`TestCi080TheCommentIsTotalOverItsInput` builds *exactly* this shape (a table whose only
    column is a bare ``USER-DEFINED`` with ``enum_info=None``, plus a registry enum) and stayed
    green throughout, because every one of its assertions ``compile()``s. **A missing import is
    invisible to a parse** -- it is a ``NameError`` at *execution*. Hence
    :meth:`test_an_enum_with_no_referencing_column_executes_at_import` below, which is the
    assertion that would have caught this.
    """

    #: A registry enum with **no** column referencing it -- `enum_info` is None everywhere.
    @staticmethod
    def _schema() -> Schema:
        return Schema(
            tables=[TableInfo(name='users', columns=[ColumnInfo(name='id', raw_type='integer', primary=True)])],
            enums=[EnumInfo(name='order_status', values=['pending', 'active'], schema='public')],
        )

    def test_the_reproducer_really_has_no_column_carrying_enum_info(self) -> None:
        # The premise, asserted rather than assumed: if a future edit to `_schema` attached the
        # enum to the column, every test below would pass for the wrong reason.
        schema = self._schema()
        assert all(c.enum_info is None for t in schema.tables for c in t.columns)
        assert [e.name for e in schema.enums] == ['order_status']

    def test_an_enum_with_no_referencing_column_still_imports_Enum(self) -> None:
        out = _emit(self._schema())
        assert 'class PublicOrderStatusEnum(str, Enum):' in out
        assert 'enum.Enum' in _imported(out)

    def test_an_enum_with_no_referencing_column_executes_at_import(self) -> None:
        # ⚠ Deliberately an execution, not a parse -- see the class docstring. On `main` this
        # raised `NameError: name 'Enum' is not defined` while `compile()` was perfectly happy.
        out = _emit(self._schema())
        namespace: dict[str, object] = {}
        exec(compile(out, '<generated>', 'exec'), namespace)  # noqa: S102 - executing IS the assertion
        enum_class = namespace['PublicOrderStatusEnum']
        assert [m.name for m in enum_class] == ['PENDING', 'ACTIVE']  # type: ignore[union-attr]
        for label in ('pending', 'active'):
            assert enum_class(label).value == label  # type: ignore[operator]

    @pytest.mark.parametrize(
        ('config', 'enums'),
        [
            (EmitterConfig(generate_enums=False), [EnumInfo(name='order_status', values=['pending'])]),
            (None, []),
        ],
        ids=['generate_enums=False', 'no enums in the registry'],
    )
    def test_the_import_is_absent_when_there_are_no_enum_classes(
        self, config: EmitterConfig | None, enums: list[EnumInfo]
    ) -> None:
        # The counter-witness, over BOTH axes that can suppress the enum section. Without it,
        # "always import Enum" would satisfy every assertion above -- and an unconditional import
        # is an F401 in every user's file, which the corpus ruff sweep would then have to catch.
        schema = Schema(
            tables=[TableInfo(name='users', columns=[ColumnInfo(name='id', raw_type='integer', primary=True)])],
            enums=enums,
        )
        out = _emit(schema, config)
        assert 'enum.Enum' not in _imported(out)
        assert '(str, Enum):' not in out


@pytest.mark.unit
class TestCi113TheEmitterPassesTheClassNameItRenders:
    """``CI-113`` end to end: the member names and the class header must agree.

    ``naming.py``'s own tests cannot establish this. They prove
    :func:`~castiron.utils.naming.python_member_names` repairs a class-private name **for the class
    name it derived**; they cannot prove the emitter renders the header from that *same* name. Only
    driving :class:`~castiron.emitters.PydanticEmitter` and executing its output can, and the label
    below is the one that made the two disagree.

    On ``main`` this module executed fine and **silently lost a label** on py3.11+, while py3.10
    kept it under a mangled name -- ``castiron gen`` exit 0 either way. That is ``CI94-Q1``'s one
    non-negotiable ("never drop a variant") and Hard Rule #9's interpreter-independence, breached
    together. **There is deliberately no ``sys.version_info`` branch below**: one unbranched
    assertion, green on all four gate legs, is the statement that the interpreter-dependence is
    gone.
    """

    ENUM = EnumInfo(name='order_status', values=[], schema='public')

    def test_a_crafted_label_survives_the_real_emitted_class_name(self) -> None:
        class_name = python_class_name(self.ENUM)
        label = crafted_class_private_label(class_name)
        labels = [label, 'ok']
        schema = Schema(
            tables=[TableInfo(name='t', columns=[ColumnInfo(name='c', raw_type='USER-DEFINED', is_nullable=True)])],
            enums=[EnumInfo(name='order_status', values=labels, schema='public')],
        )
        out = _emit(schema)

        # The class really is emitted under the name the member transform was derived from.
        assert f'class {class_name}(str, Enum):' in out

        # ⚠ `simplefilter('error')` is part of the assertion, not hygiene: on py3.10 a
        # class-private member is KEPT and announced only by a DeprecationWarning, so silence is
        # the only evidence that leg agrees with the other three.
        namespace: dict[str, object] = {}
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            exec(compile(out, '<generated>', 'exec'), namespace)  # noqa: S102 - executing IS the assertion

        enum_class = namespace[class_name]
        assert len(list(enum_class)) == len(labels), 'a label was swallowed by the class-name clause'  # type: ignore[call-overload]
        for value in labels:
            assert enum_class(value).value == value  # type: ignore[operator]
