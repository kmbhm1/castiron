"""The fidelity notices: the identity-PK warning and its sync guard."""

import logging
from typing import Any

import pytest

from castiron.cli import notices
from castiron.cli.notices import (
    MAX_NAMED_TABLES,
    OPENAPI_FIDELITY_NOTE,
    identity_pk_candidates,
    identity_pk_warning,
    report,
)
from castiron.ir import ColumnInfo, Schema, TableInfo
from castiron.sources import build_schema_from_document
from castiron.sources.openapi import INTEGER_FAMILY


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


@pytest.mark.unit
class TestReport:
    def test_the_warning_fires_when_the_inference_is_off(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk())])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=False, from_openapi=True)
        assert any(record.levelno == logging.WARNING for record in caplog.records)

    def test_the_warning_stays_quiet_when_the_inference_is_on(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk())])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=True, from_openapi=True)
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    def test_the_warning_stays_quiet_when_no_table_would_change(self, caplog: pytest.LogCaptureFixture) -> None:
        schema = Schema(tables=[table('users', pk(raw_type='uuid'))])
        with caplog.at_level(logging.WARNING, logger='castiron.cli.notices'):
            report(schema, infer_generated_primary_keys=False, from_openapi=True)
        assert [record for record in caplog.records if record.levelno == logging.WARNING] == []

    def test_the_fidelity_note_is_info_level(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger='castiron.cli.notices'):
            report(Schema(), infer_generated_primary_keys=True, from_openapi=True)
        assert [record.getMessage() for record in caplog.records] == [OPENAPI_FIDELITY_NOTE]

    def test_the_fidelity_note_is_skipped_for_another_source(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger='castiron.cli.notices'):
            report(Schema(), infer_generated_primary_keys=True, from_openapi=False)
        assert caplog.records == []
