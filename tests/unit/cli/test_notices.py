"""The fidelity notices: the identity-PK warning, the identifier-repair warning, and the sync guard."""

import logging
from typing import Any

import pytest

from castiron.cli import notices
from castiron.cli.notices import (
    MAX_NAMED_TABLES,
    OPENAPI_FIDELITY_NOTE,
    identity_pk_candidates,
    identity_pk_warning,
    renamed_class_stem_warning,
    renamed_class_stems,
    renamed_enum_class_warning,
    renamed_enum_classes,
    repaired_column_names,
    repaired_column_warning,
    report,
)
from castiron.ir import ColumnInfo, EnumInfo, Schema, TableInfo
from castiron.sources import build_schema_from_document
from castiron.sources.openapi import INTEGER_FAMILY
from castiron.utils.naming import ClassStem, EnumClass, python_class_names, python_class_stems


def table(name: str, *columns: ColumnInfo) -> TableInfo:
    return TableInfo(name=name, columns=list(columns))


def pk(name: str = 'id', raw_type: str = 'integer', **overrides: Any) -> ColumnInfo:
    fields: dict[str, Any] = {'is_nullable': False, 'primary': True, **overrides}
    return ColumnInfo(name=name, raw_type=raw_type, **fields)


@pytest.mark.unit
class TestSyncGuard:
    def test_the_rule_is_the_source_s_own_constant(self) -> None:
        # One definition, not two that drift (Hard Rule #6). The CLI notice and the source's
        # inference must agree about what "an integer primary key" means.
        assert notices.INTEGER_PK_TYPES is INTEGER_FAMILY


@pytest.mark.unit
class TestIdentityPkCandidates:
    def test_it_finds_the_openapi_fixture_s_surrogate_keys(self, openapi_fixture_document: dict[str, Any]) -> None:
        schema = build_schema_from_document(openapi_fixture_document)
        assert identity_pk_candidates(schema) == ['orders', 'products', 'restricted_table', 'users']

    def test_the_inference_removes_every_candidate(self, openapi_fixture_document: dict[str, Any]) -> None:
        schema = build_schema_from_document(openapi_fixture_document, infer_generated_primary_keys=True)
        assert identity_pk_candidates(schema) == []

    @pytest.mark.parametrize('raw_type', sorted(INTEGER_FAMILY))
    def test_every_integer_width_qualifies(self, raw_type: str) -> None:
        assert identity_pk_candidates(Schema(tables=[table('t', pk(raw_type=raw_type))])) == ['t']

    def test_a_non_integer_key_does_not_qualify(self) -> None:
        assert identity_pk_candidates(Schema(tables=[table('t', pk(raw_type='uuid'))])) == []

    def test_a_key_with_a_default_does_not_qualify(self) -> None:
        assert identity_pk_candidates(Schema(tables=[table('t', pk(default='0'))])) == []

    def test_a_key_already_known_to_be_identity_does_not_qualify(self) -> None:
        assert identity_pk_candidates(Schema(tables=[table('t', pk(is_identity=True))])) == []

    def test_a_nullable_key_does_not_qualify(self) -> None:
        assert identity_pk_candidates(Schema(tables=[table('t', pk(is_nullable=True))])) == []

    def test_a_composite_key_does_not_qualify(self) -> None:
        schema = Schema(tables=[table('t', pk('a'), pk('b'))])
        assert identity_pk_candidates(schema) == []

    def test_a_table_with_no_primary_key_does_not_qualify(self) -> None:
        schema = Schema(tables=[table('t', ColumnInfo(name='x', raw_type='integer'))])
        assert identity_pk_candidates(schema) == []


@pytest.mark.unit
class TestIdentityPkWarning:
    def test_one_table_reads_singular(self) -> None:
        assert identity_pk_warning(['users']).startswith('1 table has an integer primary key')

    def test_it_names_up_to_three_tables(self) -> None:
        message = identity_pk_warning(['a', 'b', 'c'])
        assert '(a, b, c)' in message
        assert 'more' not in message

    def test_beyond_three_it_collapses_the_tail(self) -> None:
        message = identity_pk_warning(['alpha', 'bravo', 'charlie', 'delta', 'echo'])
        assert f'(alpha, bravo, charlie and {5 - MAX_NAMED_TABLES} more)' in message
        assert 'delta' not in message
        assert 'echo' not in message

    def test_it_names_the_exact_flag_and_config_key(self) -> None:
        message = identity_pk_warning(['a'])
        assert '--infer-generated-primary-keys' in message
        assert 'infer-generated-primary-keys = true' in message

    def test_it_carries_no_documentation_url(self) -> None:
        # CI6-D10 point 4: the docs page does not exist yet; a link to a 404 is worse than none.
        assert 'http' not in identity_pk_warning(['a'])


def repaired(name: str, alias: str) -> ColumnInfo:
    """A column whose emitted identifier ``name`` differs from its wire name ``alias``."""
    return ColumnInfo(name=name, raw_type='text', alias=alias)


@pytest.mark.unit
class TestRepairedColumnNames:
    """CI-085's discriminator: an identifier repair is noteworthy, the shipped rename is not."""

    def test_it_reports_an_identifier_repair(self) -> None:
        schema = Schema(tables=[table('t', repaired('field_2fast', '2fast'), repaired('space_name', 'space name'))])
        assert repaired_column_names(schema, disable_model_prefix_protection=False) == [
            ('t', '2fast', 'field_2fast'),
            ('t', 'space name', 'space_name'),
        ]

    @pytest.mark.parametrize(
        ('name', 'alias'),
        [
            ('field_class', 'class'),  # a keyword -- the shipped path
            ('field_import', 'import'),
            ('field_model_config', 'model_config'),  # the model_ protected namespace
        ],
    )
    def test_it_stays_quiet_for_the_shipped_reserved_word_path(self, name: str, alias: str) -> None:
        # 🔴 A schema whose only aliased column is `class` works today and must NOT grow a new
        # warning: the behaviour did not change, so there is nothing to tell the user.
        schema = Schema(tables=[table('t', repaired(name, alias))])
        assert repaired_column_names(schema, disable_model_prefix_protection=False) == []

    def test_the_model_prefix_flag_makes_a_model_column_noteworthy_again(self) -> None:
        # With `--no-model-prefix-protection` the source does not rename `model_*` at all, so an
        # aliased `model_x` could only have come from the identifier repair. The discriminator has
        # to read the same flag the schema was built with or it mis-classifies exactly here.
        schema = Schema(tables=[table('t', repaired('field_model_config', 'model_config'))])
        assert repaired_column_names(schema, disable_model_prefix_protection=True) == [
            ('t', 'model_config', 'field_model_config')
        ]

    def test_a_curated_exception_that_was_renamed_is_still_reported(self) -> None:
        # `id` is exempt from the reserved rule, so an alias on it cannot have come from that
        # path -- it is an NFKC collision or a repair, and the user should hear about it.
        schema = Schema(tables=[table('t', repaired('id_2', 'id'))])
        assert repaired_column_names(schema, disable_model_prefix_protection=False) == [('t', 'id', 'id_2')]

    def test_an_unaliased_column_is_never_reported(self) -> None:
        schema = Schema(tables=[table('t', ColumnInfo(name='ok_column', raw_type='text'))])
        assert repaired_column_names(schema, disable_model_prefix_protection=False) == []

    def test_it_reads_the_real_pipeline_end_to_end(self) -> None:
        # Built through the real source rather than by hand, so the notice cannot drift from what
        # `castiron gen` actually produces.
        document = {
            'swagger': '2.0',
            'definitions': {
                'hostile': {
                    'type': 'object',
                    'required': [],
                    'properties': {
                        'ok_column': {'type': 'string', 'format': 'text'},
                        'class': {'type': 'string', 'format': 'text'},
                        '2fast': {'type': 'string', 'format': 'text'},
                    },
                }
            },
        }
        schema = build_schema_from_document(document)
        assert repaired_column_names(schema, disable_model_prefix_protection=False) == [
            ('hostile', '2fast', 'field_2fast')
        ]


@pytest.mark.unit
class TestRepairedColumnWarning:
    def test_one_column_reads_singular(self) -> None:
        message = repaired_column_warning([('t', '2fast', 'field_2fast')])
        assert message.startswith('1 column name is not usable as a Python field name and was renamed')

    def test_it_names_up_to_three_columns(self) -> None:
        message = repaired_column_warning([('t', f'w{i}', f'p{i}') for i in range(3)])
        assert '(t.w0 -> p0, t.w1 -> p1, t.w2 -> p2)' in message
        assert 'more' not in message

    def test_beyond_three_it_collapses_the_tail(self) -> None:
        message = repaired_column_warning([('t', f'w{i}', f'p{i}') for i in range(5)])
        assert f'and {5 - MAX_NAMED_TABLES} more)' in message
        assert 'w3' not in message and 'w4' not in message

    def test_it_says_the_wire_name_is_preserved(self) -> None:
        # The single most important sentence: the rename is cosmetic at the Python level and the
        # generated models still read and write the real column.
        assert 'Field(alias=...)' in repaired_column_warning([('t', '2fast', 'field_2fast')])

    def test_it_carries_no_documentation_url(self) -> None:
        # House style, matching `identity_pk_warning`: no link to a page that does not exist.
        assert 'http' not in repaired_column_warning([('t', '2fast', 'field_2fast')])


@pytest.mark.unit
class TestReport:
    def test_the_repair_warning_fires_once_for_the_whole_run(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(
            tables=[
                table('a', repaired('field_2fast', '2fast'), repaired('space_name', 'space name')),
                table('b', repaired('kebab_case', 'kebab-case')),
            ]
        )
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=True, from_openapi=True, disable_model_prefix_protection=False)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f'CI5-D11: one aggregated warning per run, not per column -- got {warnings}'
        assert warnings[0].startswith('3 column names are not usable')

    def test_the_repair_warning_stays_quiet_for_the_shipped_reserved_path(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        schema = Schema(tables=[table('t', repaired('field_class', 'class'))])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=True, from_openapi=True, disable_model_prefix_protection=False)
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_the_warning_fires_when_the_inference_is_off(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk())])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=False, from_openapi=True, disable_model_prefix_protection=False)
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_the_warning_stays_quiet_when_the_inference_is_on(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk())])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=True, from_openapi=True, disable_model_prefix_protection=False)
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    def test_the_warning_stays_quiet_when_no_table_would_change(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk(raw_type='uuid'))])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=False, from_openapi=True, disable_model_prefix_protection=False)
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    def test_the_fidelity_note_is_info_level(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger='castiron.cli.notices'):
            report(
                Schema(), infer_generated_primary_keys=True, from_openapi=True, disable_model_prefix_protection=False
            )
        assert [record.getMessage() for record in caplog.records] == [OPENAPI_FIDELITY_NOTE]

    def test_the_fidelity_note_is_skipped_for_another_source(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger='castiron.cli.notices'):
            report(
                Schema(), infer_generated_primary_keys=True, from_openapi=False, disable_model_prefix_protection=False
            )
        assert caplog.records == []


def enum_classes(*names: str, schema: str = 'public', reserved: frozenset[str] = frozenset()) -> list[EnumClass]:
    """Resolve class names for ``names`` through the real allocator -- never hand-built entries."""
    return python_class_names([EnumInfo(name=name, values=[], schema=schema) for name in names], reserved)


@pytest.mark.unit
class TestRenamedEnumClasses:
    def test_a_well_behaved_registry_reports_nothing(self) -> None:
        # The common case, and the one that must stay silent: a notice on every run is noise.
        assert renamed_enum_classes(enum_classes('order_status', 'mood')) == []

    def test_it_reports_a_repair_and_a_collision(self) -> None:
        renamed = renamed_enum_classes(enum_classes('order status', 'order-status', 'order_status'))
        assert [entry.enum.name for entry in renamed] == ['order status', 'order-status']

    def test_it_reports_in_emission_order(self) -> None:
        renamed = renamed_enum_classes(enum_classes('a b', 'c d', 'order_status'))
        assert [entry.name for entry in renamed] == ['PublicABEnum', 'PublicCDEnum']

    def test_the_default_reports_nothing(self) -> None:
        assert renamed_enum_classes(()) == []


@pytest.mark.unit
class TestRenamedEnumClassWarning:
    def test_one_type_reads_singular(self) -> None:
        message = renamed_enum_class_warning(renamed_enum_classes(enum_classes('order status')))
        assert message.startswith('1 enum type is not emitted under the class name')

    def test_it_names_up_to_three_types(self) -> None:
        renamed = renamed_enum_classes(enum_classes('a b', 'c d', 'e f'))
        message = renamed_enum_class_warning(renamed)
        assert 'public.a b -> PublicABEnum (identifier repair)' in message
        assert 'more' not in message

    def test_beyond_three_it_collapses_the_tail(self) -> None:
        renamed = renamed_enum_classes(enum_classes('a b', 'c d', 'e f', 'g h', 'i j'))
        message = renamed_enum_class_warning(renamed)
        assert f'and {5 - MAX_NAMED_TABLES} more)' in message
        assert 'g h' not in message and 'i j' not in message

    def test_a_suffixed_type_names_what_took_the_bare_name(self) -> None:
        # 🔴 The captain's requirement (CI-128-Q4): the `_2` is baffling unless the message says
        # what holds the bare name.
        renamed = renamed_enum_classes(enum_classes('order status', 'order_status'))
        message = renamed_enum_class_warning(renamed)
        assert 'public.order status -> PublicOrderStatusEnum_2' in message
        assert 'PublicOrderStatusEnum is taken by public.order_status' in message

    def test_a_type_suffixed_by_a_model_name_says_so(self) -> None:
        renamed = renamed_enum_classes(enum_classes('order_status', reserved=frozenset({'PublicOrderStatusEnum'})))
        assert 'taken by another class in this module' in renamed_enum_class_warning(renamed)

    def test_it_says_the_type_name_is_preserved(self) -> None:
        # The reassurance sentence, matching `repaired_column_warning`'s: nothing is lost.
        message = renamed_enum_class_warning(renamed_enum_classes(enum_classes('order status')))
        assert 'preserved in a comment above each class' in message

    def test_it_carries_no_documentation_url(self) -> None:
        message = renamed_enum_class_warning(renamed_enum_classes(enum_classes('order status')))
        assert 'http' not in message


@pytest.mark.unit
class TestReportEnumClasses:
    def test_the_enum_warning_fires_once_for_the_whole_run(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(),
                infer_generated_primary_keys=True,
                from_openapi=True,
                disable_model_prefix_protection=False,
                enum_classes=enum_classes('order status', 'order-status', 'order_status'),
            )
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f'CI5-D11: one aggregated warning per run -- got {warnings}'
        assert warnings[0].startswith('2 enum types are not emitted')

    def test_it_stays_quiet_for_a_well_behaved_registry(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(),
                infer_generated_primary_keys=True,
                from_openapi=True,
                disable_model_prefix_protection=False,
                enum_classes=enum_classes('order_status'),
            )
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_the_parameter_defaults_to_reporting_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        # Backward compatible: a caller that emits no Python classes reports nothing rather than
        # guessing what some emitter would have named them.
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(), infer_generated_primary_keys=True, from_openapi=True, disable_model_prefix_protection=False
            )
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def class_stems(*names: str, reserved: frozenset[str] = frozenset()) -> list[ClassStem]:
    """Resolve stems for ``names`` through the real allocator -- never hand-built entries."""
    return python_class_stems(list(names), suffixes=('', 'BaseSchema', 'Parent', 'Insert', 'Update'), reserved=reserved)


@pytest.mark.unit
class TestRenamedClassStems:
    def test_a_well_behaved_schema_reports_nothing(self) -> None:
        # The common case, and the one that must stay silent: a notice on every run is noise.
        assert renamed_class_stems(class_stems('order_lines', 'users')) == []

    def test_it_reports_a_repair_and_a_collision(self) -> None:
        renamed = renamed_class_stems(class_stems('order lines', 'order-lines', 'order_lines'))
        assert [entry.source for entry in renamed] == ['order lines', 'order-lines']

    def test_the_default_reports_nothing(self) -> None:
        assert renamed_class_stems(()) == []


@pytest.mark.unit
class TestRenamedClassStemWarning:
    def test_one_table_reads_singular(self) -> None:
        message = renamed_class_stem_warning(renamed_class_stems(class_stems('order lines')))
        assert message.startswith('1 table is not emitted under the class name')

    def test_it_names_up_to_three_tables(self) -> None:
        renamed = renamed_class_stems(class_stems('a b', 'c d', 'e f'))
        message = renamed_class_stem_warning(renamed)
        assert 'a b -> AB (identifier repair)' in message
        assert 'more' not in message

    def test_beyond_three_it_collapses_the_tail(self) -> None:
        renamed = renamed_class_stems(class_stems('a b', 'c d', 'e f', 'g h', 'i j'))
        message = renamed_class_stem_warning(renamed)
        assert f'and {5 - MAX_NAMED_TABLES} more)' in message
        assert 'g h' not in message and 'i j' not in message

    def test_a_suffixed_table_names_what_took_the_bare_name(self) -> None:
        # 🔴 The captain's requirement (CI-128-Q4), carried to tables: the `_2` is baffling unless
        # the message says what holds the bare name.
        renamed = renamed_class_stems(class_stems('order lines', 'order_lines'))
        message = renamed_class_stem_warning(renamed)
        assert 'order lines -> OrderLines_2' in message
        assert 'OrderLines is taken by order_lines' in message

    def test_a_table_suffixed_by_a_module_name_says_so(self) -> None:
        renamed = renamed_class_stems(class_stems('custom_model', reserved=frozenset({'CustomModel'})))
        assert 'taken by another class in this module' in renamed_class_stem_warning(renamed)

    def test_it_says_the_table_name_is_preserved(self) -> None:
        message = renamed_class_stem_warning(renamed_class_stems(class_stems('order lines')))
        assert 'preserved in a comment above each generated class' in message

    def test_it_carries_no_documentation_url(self) -> None:
        message = renamed_class_stem_warning(renamed_class_stems(class_stems('order lines')))
        assert 'http' not in message


@pytest.mark.unit
class TestReportClassStems:
    def test_the_table_warning_fires_once_for_the_whole_run(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(),
                infer_generated_primary_keys=True,
                from_openapi=True,
                disable_model_prefix_protection=False,
                class_stems=class_stems('order lines', 'order-lines', 'order_lines'),
            )
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, f'CI5-D11: one aggregated warning per run -- got {warnings}'
        assert warnings[0].startswith('2 tables are not emitted')

    def test_it_stays_quiet_for_a_well_behaved_schema(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(),
                infer_generated_primary_keys=True,
                from_openapi=True,
                disable_model_prefix_protection=False,
                class_stems=class_stems('order_lines'),
            )
        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

    def test_the_table_warning_precedes_the_enum_one(self, caplog: pytest.LogCaptureFixture) -> None:
        # Allocation order, made visible: a stem displaces an enum class, never the other way round,
        # so the message explaining the displacement should be read first.
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(
                Schema(),
                infer_generated_primary_keys=True,
                from_openapi=True,
                disable_model_prefix_protection=False,
                enum_classes=enum_classes('order status'),
                class_stems=class_stems('order lines'),
            )
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert [w.split()[1] for w in warnings] == ['table', 'enum']
