"""The corpus inputs and their provenance records.

An input is the only corpus artifact the regeneration tool may **not** write: re-capturing a
document and regenerating a golden are different operations, with different provenance and a
different reviewer question. These tests make the provenance record more than a comment — the
document is asked to corroborate it, so the record cannot quietly lie about which PostgREST
produced it or how much of it survived the copy.
"""

import json
from typing import Any

import pytest

from tests.unit.corpus.cases import FAMILIES, InputFamily
from tests.unit.corpus.conftest import family_ids, iter_families

#: Keys a ``captured`` record must carry, and a ``synthetic`` one must NOT.
CAPTURE_ONLY_KEYS = ('seed_revision', 'postgrest_version', 'source_repo', 'captured_with')


def _provenance(family: InputFamily) -> dict[str, Any]:
    """Read a family's provenance record."""
    assert family.provenance_path is not None
    record: dict[str, Any] = json.loads(family.provenance_path.read_text(encoding='utf-8'))
    return record


@pytest.mark.unit
class TestProvenanceExists:
    def test_every_corpus_input_carries_a_provenance_record(self) -> None:
        # The CI-005 fixture is the one input without one: it predates the corpus and lives under
        # tests/unit/sources/openapi/. Its origin is pinned in the case table instead, and
        # test_witnesses.py asserts the CI-076 shape that makes 'synthetic' load-bearing.
        missing = [f.family_id for f in FAMILIES if f.provenance_path is not None and not f.provenance_path.is_file()]
        assert missing == []
        assert [f.family_id for f in FAMILIES if f.provenance_path is None] == ['openapi-fixture']

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_input_document_exists_and_is_json(self, family: InputFamily) -> None:
        assert family.input_path.is_file(), f'{family.family_id}: input document is missing'
        document = json.loads(family.input_path.read_text(encoding='utf-8'))
        assert document['swagger'] == '2.0', f'{family.family_id}: not a PostgREST Swagger 2.0 document'


@pytest.mark.unit
class TestProvenanceMatchesTheDocument:
    """The record is corroborated by the document, so it cannot drift away from it unnoticed."""

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_recorded_postgrest_version_is_the_documents_own(
        self, family: InputFamily, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        # The document carries its own producer version, so the record cannot lie about it.
        if family.provenance_path is None or family.origin != 'captured':
            pytest.skip(f'{family.family_id} is not a capture; it records no PostgREST version by design')
        record = _provenance(family)
        assert corpus_documents[family.family_id]['info']['version'] == record['postgrest_version']

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_recorded_counts_match_the_document(
        self, family: InputFamily, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        # A truncated or half-overwritten capture fails HERE, with two integers, rather than 4 000
        # lines into a golden diff where the cause is unrecoverable.
        if family.provenance_path is None:
            pytest.skip('the CI-005 fixture carries no provenance record (see the case table)')
        record = _provenance(family)
        document = corpus_documents[family.family_id]
        assert (len(document['definitions']), len(document['paths'])) == (
            record['definition_count'],
            record['path_count'],
        )

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_recorded_schema_is_the_one_the_case_table_reads(self, family: InputFamily) -> None:
        if family.provenance_path is None:
            pytest.skip('the CI-005 fixture carries no provenance record (see the case table)')
        assert _provenance(family)['schema'] == family.schema

    def test_the_recorded_sha256_is_the_committed_documents_own(self) -> None:
        # A capture is committed bytes; recording its digest means a silent in-place edit of a
        # 151 KB JSON document is caught by a one-line assertion instead of a golden diff.
        import hashlib

        for family in FAMILIES:
            if family.provenance_path is None or family.origin != 'captured':
                continue
            digest = hashlib.sha256(family.input_path.read_bytes()).hexdigest()
            assert digest == _provenance(family)['sha256'], f'{family.family_id}: capture bytes changed'


@pytest.mark.unit
class TestSyntheticInputsCannotMasqueradeAsEvidence:
    """The structural half of the CI-076 lesson.

    CI-076 happened because a hand-authored fixture *carried a docstring certifying it was
    attested by a real PostgREST*, and the shape it contained turned out to be one PostgREST
    cannot emit. A label is not enough: a synthetic input must be structurally incapable of
    claiming a seed revision or a PostgREST version.
    """

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_a_synthetic_input_records_no_seed_or_postgrest_version(self, family: InputFamily) -> None:
        if family.provenance_path is None:
            pytest.skip('the CI-005 fixture carries no provenance record (see the case table)')
        record = _provenance(family)
        if family.origin == 'synthetic':
            present = [key for key in CAPTURE_ONLY_KEYS if key in record]
            assert present == [], (
                f'{family.family_id} is synthetic but its provenance record carries {present}. '
                f'A hand-authored document must never be able to stand as evidence about a real '
                f'source -- that is exactly how CI-076 happened.'
            )
        assert record['origin'] == family.origin

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_a_capture_records_both_a_seed_revision_and_a_postgrest_version(self, family: InputFamily) -> None:
        if family.provenance_path is None or family.origin != 'captured':
            pytest.skip(f'{family.family_id} is not a capture')
        record = _provenance(family)
        assert all(record.get(key) for key in CAPTURE_ONLY_KEYS), (
            f'{family.family_id} is a capture, so it must say WHICH apparatus produced it. Without '
            f'a seed revision a golden derived from it is unfalsifiable: you can no longer tell '
            f'"castiron changed" from "the schema changed" (SEED-D2).'
        )


@pytest.mark.unit
class TestTheCapturesCarryNoCredential:
    """A committed capture is a public artifact. SEED-D8 authorized it *because* it is key-free."""

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_document_carries_no_hostname_and_no_key(self, family: InputFamily) -> None:
        if family.origin != 'captured':
            pytest.skip(f'{family.family_id} is not a capture')
        text = family.input_path.read_text(encoding='utf-8')
        document = json.loads(text)
        # The API key travels in a header, never the body; `host` is the container-local bind
        # address, so the document is port- and hostname-independent.
        assert document['host'] == '0.0.0.0:3000'
        for forbidden in ('apikey', 'Bearer ', 'service_role', 'eyJ'):
            assert forbidden not in text, f'{family.family_id}: found {forbidden!r} in a committed capture'
