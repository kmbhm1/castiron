from collections.abc import Callable
from pathlib import Path

import pytest

from castiron.emitters import EmittedFile, EmitterConfig, PydanticEmitter
from castiron.ir import (
    ColumnInfo,
    ForeignKeyInfo,
    RelationshipInfo,
    RelationType,
    Schema,
    TableInfo,
    build_schema,
)

Row = tuple[object, ...]
GOLDEN = Path(__file__).parent / 'golden' / 'schema.py.txt'


def _emit(schema: Schema, config: EmitterConfig | None = None) -> str:
    return PydanticEmitter(config).emit(schema)[0].content


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
        assert _emit(representative_schema) == GOLDEN.read_text()

    def test_golden_is_valid_python(self) -> None:
        compile(GOLDEN.read_text(), '<golden>', 'exec')

    def test_a_zero_function_schema_emits_byte_identically_after_ci_005(self, representative_schema: Schema) -> None:
        """CI-005 backward-compat guard: adding ``Schema.functions`` changed no output.

        ``representative_schema`` is built by the CI-003/CI-004 tuple fixtures with
        ``function_details`` omitted, so it carries an empty function list. The emitted
        module must still match the CI-004 golden byte for byte.
        """
        assert representative_schema.functions == []
        assert _emit(representative_schema) == GOLDEN.read_text()


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

    def test_import_block_is_sorted(self, representative_schema: Schema) -> None:
        import_lines = _emit(representative_schema).split('\n\n\n')[0].splitlines()
        assert import_lines == sorted(import_lines)

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
        assert 'from pydantic import UUID4' in out

    def test_timestamptz(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'created_at: datetime.datetime' in out
        assert 'import datetime' in out

    def test_text_array(self, representative_schema: Schema) -> None:
        assert 'roles: list[str] | None' in _emit(representative_schema)

    def test_enum_class_and_scalar_field(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'class PublicUserStatusEnum(str, Enum):' in out
        assert 'status: PublicUserStatusEnum' in out
        assert 'from enum import Enum' in out

    def test_enum_array_field(self, representative_schema: Schema) -> None:
        assert 'flags: list[PublicUserStatusEnum] | None' in _emit(representative_schema)

    def test_enum_member_reserved_keyword_suffixed(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert 'ACTIVE = "active"' in out
        assert 'IMPORT_ = "import"  # original name was import (reserved keyword)' in out

    def test_string_constraints(self, representative_schema: Schema) -> None:
        out = _emit(representative_schema)
        assert "sku: Annotated[str, StringConstraints(**{'min_length': 10, 'max_length': 10})]" in out
        assert "bio: Annotated[str, StringConstraints(**{'max_length': 500})] | None" in out
        assert 'from typing import Annotated' in out
        assert 'from pydantic import StringConstraints' in out

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
        assert 'from typing import Annotated' not in out

    def test_no_future_annotations_without_relationships(self, build_columns: Callable[..., Row]) -> None:
        columns = [build_columns('t', 'id', 'integer', identity=True)]
        constraints = [('t_pkey', 't', ['id'], 'p', 'PRIMARY KEY (id)')]
        assert 'from __future__ import annotations' not in _emit(build_schema(columns, [], constraints, [], []))


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
        assert 'from enum import Enum' not in out

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
        assert 'from pydantic import ConfigDict' not in out

    def test_configdict_added_when_disabled(self) -> None:
        out = _emit(Schema(tables=[self._table()]), EmitterConfig(disable_model_prefix_protection=True))
        assert 'model_config = ConfigDict(protected_namespaces=())' in out
        assert 'from pydantic import ConfigDict' in out

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
