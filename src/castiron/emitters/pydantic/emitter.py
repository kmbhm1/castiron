"""The Pydantic v2 emitter -- supabase-pydantic's trust moat, ported onto the Schema IR.

``PydanticEmitter(config).emit(schema)`` renders a :class:`castiron.ir.Schema` into a
single Pydantic v2 module: enum classes, custom base classes, Base/Insert/Update ("Row"
+ CRUD) models, and operational classes that carry nested foreign-key relationship
fields (CI4-D-scope). Fidelity ported from supabase-pydantic's
``PydanticFastAPIClassWriter`` / ``PydanticFastAPIWriter``:

- per-column type resolution (jsonb, arrays, datetime, uuid, ...) via
  :func:`castiron.types.resolve_column_type`, with enum columns overlaid from
  ``col.enum_info``;
- ``text`` + ``length()`` CHECK constraints -> ``Annotated[str, StringConstraints(...)]``
  (constraints are parsed out of the single-column CHECK ``constraint_definition``, not
  ``max_length``);
- identity columns omitted from Insert/Update; defaults/nullable -> optional; Update ->
  all optional;
- ``model_``-prefix handling via ``ConfigDict(protected_namespaces=())``;
- foreign-key / relationship fields with singular vs pluralized names and self-ref
  handling.

The emitter treats the ``Schema`` as read-only (it never mutates ``fk.relation_type`` --
the self-ref effective type is computed in a local; supabase-pydantic mutates it) and its
output is deterministic (Hard Rule #9): tables in ``Schema.tables`` order, enums from the
IR's sorted ``schema.enums``, fields via ``sort_and_separate_columns``, imports a single
sorted set, ``from __future__ import annotations`` when relationship fields need forward
references. castiron owns this output shape; it is not byte-identical to supabase-pydantic.
"""

import re
from enum import Enum

from castiron.emitters.base import EmittedFile, Emitter, render_import_block, section_comment
from castiron.emitters.config import EmitterConfig
from castiron.ir import ColumnInfo, RelationType, Schema, SortedColumns, TableInfo
from castiron.ir.build import column_name_reserved_exceptions, string_is_reserved
from castiron.types import PYDANTIC_TYPE_MAP, resolve_column_type
from castiron.utils.naming import pluralize, python_class_name, python_member_name, singularize, to_pascal_case

#: Indentation unit for generated code (4 spaces -- clean, PEP 8-style output).
IND = '    '
#: Name of the shared custom base model.
CUSTOM_MODEL_NAME = 'CustomModel'
#: Length-constraint pattern: ``length(col) <op> N`` inside a CHECK definition.
_LENGTH_PATTERN = r'length\((\w+)\)\s*([=<>]+)\s*(\d+)'


class _ClassVariant(Enum):
    """The variant of a generated model class."""

    BASE = 'base'
    BASE_WITH_PARENT = 'base_with_parent'
    PARENT = 'parent'
    INSERT = 'insert'
    UPDATE = 'update'


def _parse_length_constraint(constraint_def: str | None) -> dict[str, int] | None:
    """Parse ``length(col) <op> N`` clauses from a CHECK definition.

    Args:
        constraint_def: The single-column CHECK constraint definition, or ``None``.

    Returns:
        A dict with ``min_length`` and/or ``max_length``, or ``None`` if none are found.
    """
    if not constraint_def:
        return None

    matches = re.findall(_LENGTH_PATTERN, constraint_def)
    if not matches:
        return None

    result: dict[str, int] = {}
    for _, operator, raw_value in matches:
        value = int(raw_value)
        if operator == '=':
            result['min_length'] = value
            result['max_length'] = value
        elif operator == '>=':
            result['min_length'] = value
        elif operator == '<=':
            result['max_length'] = value

    return result or None


def _py_string(value: str) -> str:
    """Render ``value`` as a double-quoted Python string literal (embedded quotes escaped)."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


class PydanticEmitter(Emitter):
    """Emit Pydantic v2 models from a :class:`castiron.ir.Schema`."""

    def __init__(self, config: EmitterConfig | None = None) -> None:
        """Initialize the emitter.

        Args:
            config: Behavioral toggles; defaults to :class:`EmitterConfig` defaults.
        """
        self.config = config if config is not None else EmitterConfig()

    def emit(self, schema: Schema) -> list[EmittedFile]:
        """Render ``schema`` to a single in-memory :class:`EmittedFile`.

        Args:
            schema: The schema to render (read-only).

        Returns:
            A one-element list: the emitted module at ``config.output_filename``.
        """
        return [EmittedFile(self.config.output_filename, self._write(schema))]

    # ------------------------------------------------------------------ file assembly

    def _write(self, schema: Schema) -> str:
        """Assemble the full module text from its sections."""
        sections = [self._imports(schema)]
        enum_section = self._enum_section(schema)
        if enum_section:
            sections.append(enum_section)
        sections.append(self._custom_section())
        base_section = self._base_section(schema)
        if base_section:
            sections.append(base_section)
        operational_section = self._operational_section(schema)
        if operational_section:
            sections.append(operational_section)
        return '\n\n\n'.join(sections) + '\n'

    def _imports(self, schema: Schema) -> str:
        """Build the sorted, deduplicated import block for the module."""
        imports = {'from pydantic import BaseModel', 'from pydantic import Field'}

        if self.config.include_foreign_keys and any(t.foreign_keys or t.relationships for t in schema.tables):
            imports.add('from __future__ import annotations')

        if self.config.generate_enums and any(c.enum_info is not None for t in schema.tables for c in t.columns):
            imports.add('from enum import Enum')

        if self.config.disable_model_prefix_protection and any(
            self._has_model_prefix_columns(t) for t in schema.tables
        ):
            imports.add('from pydantic import ConfigDict')

        if self._needs_string_constraints(schema):
            imports.add('from typing import Annotated')
            imports.add('from pydantic import StringConstraints')

        for table in schema.tables:
            for column in table.columns:
                if column.enum_info is not None:
                    continue
                imports.update(resolve_column_type(column, PYDANTIC_TYPE_MAP).imports)

        return render_import_block(imports)

    def _needs_string_constraints(self, schema: Schema) -> bool:
        """Whether any ``text`` column carries a ``length()`` CHECK constraint."""
        return any(
            column.raw_type.lower() == 'text'
            and column.constraint_definition is not None
            and 'length(' in column.constraint_definition.lower()
            for table in schema.tables
            for column in table.columns
        )

    # ------------------------------------------------------------------ enum classes

    def _enum_section(self, schema: Schema) -> str | None:
        """Render the enum classes from the IR's deduplicated, sorted enum registry."""
        if not self.config.generate_enums or not schema.enums:
            return None

        classes = []
        for enum in schema.enums:
            lines = [f'class {python_class_name(enum)}(str, Enum):']
            for value in enum.values:
                member = python_member_name(value).upper()
                comment = ''
                if string_is_reserved(member.lower()) or column_name_reserved_exceptions(member.lower()):
                    member = f'{member}_'
                    comment = f'  # original name was {value} (reserved keyword)'
                lines.append(f'{IND}{member} = {_py_string(value)}{comment}')
            classes.append('\n'.join(lines))

        comment = section_comment('Enum Types', ['These are generated from Postgres user-defined enum types.'])
        return '\n\n\n'.join([comment, *classes])

    # ------------------------------------------------------------------ custom bases

    def _custom_section(self) -> str:
        """Render the shared ``CustomModel`` (+ Insert/Update) base classes."""
        classes = [f'class {CUSTOM_MODEL_NAME}(BaseModel):\n{IND}"""Base model class with common features."""']
        if self.config.generate_crud_models:
            classes.append(
                f'class {CUSTOM_MODEL_NAME}Insert({CUSTOM_MODEL_NAME}):\n'
                f'{IND}"""Base model for insert operations with common features."""'
            )
            classes.append(
                f'class {CUSTOM_MODEL_NAME}Update({CUSTOM_MODEL_NAME}):\n'
                f'{IND}"""Base model for update operations with common features."""'
            )
        comment = section_comment(
            'Custom Classes',
            ['Custom model classes defining common features shared by the generated schemas.'],
        )
        return '\n\n\n'.join([comment, *classes])

    # ------------------------------------------------------------------ base/CRUD classes

    def _base_section(self, schema: Schema) -> str | None:
        """Render the Parent (optional), Base, Insert, and Update class sections."""
        subsections: list[str] = []

        if self.config.add_null_parent_classes:
            parent_classes = [self._render_class(t, _ClassVariant.PARENT) for t in schema.tables]
            if parent_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Parent Classes',
                                ['All fields nullable; useful for refining models via inheritance.'],
                            ),
                            *parent_classes,
                        ]
                    )
                )
            base_variant = _ClassVariant.BASE_WITH_PARENT
        else:
            base_variant = _ClassVariant.BASE

        base_classes = [self._render_class(t, base_variant) for t in schema.tables]
        if base_classes:
            subsections.append(
                '\n\n\n'.join(
                    [
                        section_comment('Base Classes', ['These are the base Row models that include all fields.']),
                        *base_classes,
                    ]
                )
            )

        if self.config.generate_crud_models:
            insert_classes = [self._render_class(t, _ClassVariant.INSERT) for t in schema.tables]
            if insert_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Insert Classes',
                                ['Models for insert operations; auto-generated fields are optional.'],
                            ),
                            *insert_classes,
                        ]
                    )
                )
            update_classes = [self._render_class(t, _ClassVariant.UPDATE) for t in schema.tables]
            if update_classes:
                subsections.append(
                    '\n\n\n'.join(
                        [
                            section_comment(
                                'Update Classes', ['Models for update operations; all fields are optional.']
                            ),
                            *update_classes,
                        ]
                    )
                )

        return '\n\n\n'.join(subsections) if subsections else None

    def _render_class(self, table: TableInfo, variant: _ClassVariant) -> str:
        """Render one model class (header, docstring, optional config, body sections)."""
        null_defaults = variant == _ClassVariant.PARENT
        name = self._write_name(table, variant)
        metaclass = self._metaclass(table, variant)
        header = f'class {name}({metaclass}):'
        docstring = f'{IND}"""{self._class_name(table)} {self._qualifier(variant)} Schema."""'

        blocks: list[str] = []
        if self.config.disable_model_prefix_protection and self._has_model_prefix_columns(table):
            blocks.append(f'{IND}model_config = ConfigDict(protected_namespaces=())')
        blocks.extend(self._body_sections(table, variant, null_defaults))

        if blocks:
            return f'{header}\n{docstring}\n\n' + '\n\n'.join(blocks)
        return f'{header}\n{docstring}'

    def _body_sections(self, table: TableInfo, variant: _ClassVariant, null_defaults: bool) -> list[str]:
        """Render the column sections (primary keys, columns, required/optional) for a class."""
        separated: SortedColumns = table.sort_and_separate_columns(separate_primary_key=True)

        if variant in (_ClassVariant.INSERT, _ClassVariant.UPDATE):
            return self._crud_sections(separated, variant)

        sections: list[str] = []
        pk = self._column_section(
            'Primary Keys', [self._render_column(c, variant, null_defaults) for c in separated.primary_keys]
        )
        if pk:
            sections.append(pk)
        cols = self._column_section(
            'Columns', [self._render_column(c, variant, null_defaults) for c in separated.remaining]
        )
        if cols:
            sections.append(cols)
        return sections

    def _crud_sections(self, separated: SortedColumns, variant: _ClassVariant) -> list[str]:
        """Render the Insert/Update column sections (identity omitted; optionality bucketed)."""
        sections: list[str] = []

        pk = self._column_section(
            'Primary Keys',
            [self._render_column(c, variant, False) for c in separated.primary_keys if not c.is_identity],
        )
        if pk:
            sections.append(pk)

        if variant == _ClassVariant.UPDATE:
            cols = self._column_section(
                'Columns', [self._render_column(c, variant, False) for c in separated.remaining if not c.is_identity]
            )
            if cols:
                sections.append(cols)
            return sections

        required: list[ColumnInfo] = []
        optional: list[ColumnInfo] = []
        for column in separated.remaining:
            if column.is_identity:
                continue
            if column.has_default or column.is_generated or column.is_nullable:
                optional.append(column)
            else:
                required.append(column)

        req = self._column_section('Required fields', [self._render_column(c, variant, False) for c in required])
        if req:
            sections.append(req)
        opt = self._column_section('Optional fields', [self._render_column(c, variant, False) for c in optional])
        if opt:
            sections.append(opt)
        return sections

    def _column_section(self, title: str, columns: list[str]) -> str | None:
        """Render a titled, indented block of column lines, or ``None`` when empty."""
        rendered = [c for c in columns if c]
        if not rendered:
            return None
        body = '\n'.join(f'{IND}{c}' for c in rendered)
        return f'{IND}# {title}\n{body}'

    def _render_column(self, column: ColumnInfo, variant: _ClassVariant, null_defaults: bool) -> str:
        """Render a single field line for a column.

        Identity columns are pre-filtered by the Insert/Update section builder, so they
        never reach this method for a CRUD variant.
        """
        base_type = self._base_type(column)

        force_optional = variant == _ClassVariant.UPDATE
        if variant == _ClassVariant.INSERT and (column.has_default or column.is_generated):
            force_optional = True

        type_str = self._apply_constraints(column, base_type)

        nullable = bool(column.is_nullable) or null_defaults or force_optional
        if nullable:
            type_str = f'{type_str} | None'

        field_args: dict[str, str] = {}
        if nullable:
            field_args['default'] = 'None'
        if column.alias is not None:
            field_args['alias'] = _py_string(column.alias)
        if column.description is not None:
            field_args['description'] = _py_string(column.description)

        line = f'{column.name}: {type_str}'
        if field_args:
            line += ' = Field(' + ', '.join(f'{key}={value}' for key, value in field_args.items()) + ')'
        return line

    def _base_type(self, column: ColumnInfo) -> str:
        """Resolve the column's base type string, overlaying an enum class when present."""
        resolution = resolve_column_type(column, PYDANTIC_TYPE_MAP)
        is_array = resolution.python_type.startswith('list[') or column.raw_type.lower().endswith('[]')

        if column.enum_info is None:
            return resolution.python_type

        if self.config.generate_enums:
            enum_type = python_class_name(column.enum_info)
            return f'list[{enum_type}]' if is_array else enum_type
        return 'list[str]' if is_array else 'str'

    def _apply_constraints(self, column: ColumnInfo, base_type: str) -> str:
        """Wrap a ``str`` base type in ``Annotated[str, StringConstraints(...)]`` when applicable."""
        if base_type != 'str' or column.raw_type.lower() != 'text' or not column.constraint_definition:
            return base_type
        constraints = _parse_length_constraint(column.constraint_definition)
        if not constraints:
            return base_type
        ordered: dict[str, int] = {}
        if 'min_length' in constraints:
            ordered['min_length'] = constraints['min_length']
        if 'max_length' in constraints:
            ordered['max_length'] = constraints['max_length']
        return f'Annotated[str, StringConstraints(**{ordered})]'

    # ------------------------------------------------------------------ operational classes

    def _operational_section(self, schema: Schema) -> str | None:
        """Render the operational classes (which carry FK relationship fields)."""
        if not schema.tables:
            return None
        classes = [self._render_operational_class(t) for t in schema.tables]
        comment = section_comment(
            'Operational Classes',
            ['Extend these models to add custom behavior; they carry relationship fields.'],
        )
        return '\n\n\n'.join([comment, *classes])

    def _render_operational_class(self, table: TableInfo) -> str:
        """Render one operational class, appending FK relationship fields or ``pass``."""
        name = self._class_name(table)
        base_name = f'{name}BaseSchema'
        header = f'class {name}({base_name}):'
        docstring = (
            f'{IND}"""{name} Schema for Pydantic.\n\n'
            f'{IND}Inherits from {base_name}. Add any customization here.\n'
            f'{IND}"""'
        )

        fields = self._foreign_columns(table) if self.config.include_foreign_keys else []
        if fields:
            body = f'{IND}# Foreign Keys\n' + '\n'.join(f'{IND}{f}' for f in fields)
            return f'{header}\n{docstring}\n\n{body}'
        return f'{header}\n{docstring}\n\n{IND}pass'

    def _foreign_columns(self, table: TableInfo) -> list[str]:
        """Build the nested relationship field lines for a table's operational class.

        Foreign keys map to a singular field (``author: User``) or a pluralized list
        (``posts: list[Post]``) by relationship type; reverse relationships not already
        covered by a foreign key are synthesized. The effective self-referential relation
        type is computed in a local variable -- the shared IR is never mutated.
        """
        used: set[str] = set()
        fields: list[str] = []

        for fk in table.foreign_keys:
            field_def = self._foreign_key_field(table, fk.foreign_table_name, fk.column_name, fk.relation_type)
            field_name = field_def.split(':', 1)[0].strip()
            if field_name not in used:
                used.add(field_name)
                fields.append(field_def)

        for rel in table.relationships:
            is_self_ref = rel.related_table_name.lower() == table.name.lower()
            already_covered = any(fk.foreign_table_name == rel.related_table_name for fk in table.foreign_keys)
            if not is_self_ref and already_covered:
                continue
            field_name = pluralize(rel.related_table_name.lower())
            if field_name not in used:
                used.add(field_name)
                target = self._proper_name(rel.related_table_name)
                fields.append(f'{field_name}: list[{target}] | None = Field(default=None)')

        return fields

    def _foreign_key_field(
        self, table: TableInfo, foreign_table_name: str, column_name: str, relation_type: RelationType | None
    ) -> str:
        """Render one foreign-key relationship field line (name + type by relation type)."""
        target = self._proper_name(foreign_table_name)
        base_field_name = foreign_table_name.lower()
        table_name = table.name.lower()
        we_have_foreign_key = any(c.name == column_name and c.is_foreign_key for c in table.columns)

        effective = relation_type
        if foreign_table_name.lower() == table_name:
            for rel in table.relationships:
                if rel.related_table_name.lower() == table_name:
                    effective = rel.relation_type
                    break

        if effective == RelationType.ONE_TO_ONE:
            type_hint = target
            field_name = singularize(base_field_name)
        elif effective == RelationType.MANY_TO_ONE:
            if we_have_foreign_key:
                type_hint = target
                field_name = singularize(base_field_name)
            else:
                type_hint = f'list[{target}]'
                field_name = pluralize(base_field_name)
        elif effective == RelationType.ONE_TO_MANY:
            if we_have_foreign_key:
                type_hint = target
                field_name = base_field_name
            else:
                type_hint = f'list[{target}]'
                field_name = pluralize(base_field_name)
        else:  # MANY_TO_MANY or unset
            type_hint = f'list[{target}]'
            field_name = pluralize(base_field_name)

        return f'{field_name}: {type_hint} | None = Field(default=None)'

    # ------------------------------------------------------------------ naming helpers

    def _class_name(self, table: TableInfo) -> str:
        """The PascalCase class stem for a table (singularized when configured)."""
        name = singularize(table.name) if self.config.singular_names else table.name
        return to_pascal_case(name)

    def _proper_name(self, name: str) -> str:
        """The PascalCase class name for a related table name (singularized when configured)."""
        processed = singularize(name) if self.config.singular_names else name
        return to_pascal_case(processed)

    def _write_name(self, table: TableInfo, variant: _ClassVariant) -> str:
        """The class name for a table + variant (e.g. ``UserBaseSchema``, ``UserInsert``)."""
        stem = self._class_name(table)
        if variant == _ClassVariant.INSERT:
            return f'{stem}Insert'
        if variant == _ClassVariant.UPDATE:
            return f'{stem}Update'
        if variant == _ClassVariant.PARENT:
            return f'{stem}Parent'
        return f'{stem}BaseSchema'

    def _metaclass(self, table: TableInfo, variant: _ClassVariant) -> str:
        """The parent class a variant inherits from."""
        if variant == _ClassVariant.INSERT:
            return f'{CUSTOM_MODEL_NAME}Insert'
        if variant == _ClassVariant.UPDATE:
            return f'{CUSTOM_MODEL_NAME}Update'
        if variant == _ClassVariant.BASE_WITH_PARENT:
            return f'{self._class_name(table)}Parent'
        return CUSTOM_MODEL_NAME

    def _qualifier(self, variant: _ClassVariant) -> str:
        """The docstring qualifier for a variant (``Base``, ``Insert``, ...)."""
        if variant == _ClassVariant.INSERT:
            return 'Insert'
        if variant == _ClassVariant.UPDATE:
            return 'Update'
        if variant == _ClassVariant.PARENT:
            return '(Nullable) Parent'
        return 'Base'

    def _has_model_prefix_columns(self, table: TableInfo) -> bool:
        """Whether any column would collide with Pydantic's ``model_`` protected namespace."""
        return any(
            (c.alias is not None and c.alias.lower().startswith('model_'))
            or c.name.lower().startswith('field_model_')
            or c.name.lower().startswith('model')
            for c in table.columns
        )
