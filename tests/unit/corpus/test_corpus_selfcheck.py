"""Self-checks: make "the corpus silently ran nothing" impossible.

**The failure being designed against is not hypothetical.** An unfiltered
``pytest_collection_modifyitems`` in a subdirectory conftest once marked all 950 unit tests
``integration``; ``make test`` deselected **1 024 of 1 024** and **exited 0**. The lesson
(CI-083) is *read the count, not the exit code* — and a corpus that collects zero goldens is the
identical failure wearing a friendlier face: the suite is green and every golden test simply did
not exist.

⚠ Note the corrected premise: pytest's exit-5 fires on **total** deselection. The silent shape is
**partial** deselection (``184 passed, 1236 deselected, exit 0``), because it reads like success.

So: a hard-coded case count, collected-id equality, every declared artifact present, no unclaimed
file, exactly 128 manifest rows per input, an axis set derived from the real ``EmitterConfig``,
defect bookkeeping closing in both directions, and the Tier-A ↔ manifest sha cross-check.
"""

import dataclasses
from pathlib import Path

import pytest

from castiron.emitters import EmitterConfig
from tests.unit.corpus.cases import (
    CASES,
    EXPECTED_CASE_COUNT,
    FAMILIES,
    FINGERPRINT_DIR,
    GOLDEN_DIR,
    INPUTS_DIR,
    KNOWN_DEFECTS,
    CorpusCase,
    fingerprint_path,
)
from tests.unit.corpus.conftest import case_ids, iter_cases
from tests.unit.corpus.pipeline import sha256_text


@pytest.mark.unit
class TestTheCorpusIsNotEmpty:
    def test_the_case_count_is_the_declared_literal(self) -> None:
        # A literal, so growing or shrinking the corpus is a deliberate edit to a number rather
        # than a silent consequence of an import that stopped resolving.
        assert len(CASES) == EXPECTED_CASE_COUNT

    def test_every_case_id_is_unique_and_follows_the_naming_rule(self) -> None:
        ids = [case.case_id for case in CASES]
        assert len(set(ids)) == len(ids), 'duplicate case ids'
        for case in CASES:
            assert case.case_id.startswith(f'{case.family.family_id}-'), (
                f'{case.case_id} must be <input-family>-<config-name> so the id, the golden stem '
                f'and the manifest header are one greppable string'
            )

    def test_the_collected_parametrization_ids_equal_the_declared_case_ids(self) -> None:
        # `case_ids()` is what every parametrized corpus test is driven by, so if it ever returned
        # a subset the whole corpus would quietly shrink while staying green.
        assert case_ids() == [case.case_id for case in CASES]
        assert len(case_ids()) == EXPECTED_CASE_COUNT

    def test_every_input_family_is_reached_by_at_least_one_case(self) -> None:
        reached = {case.family.family_id for case in CASES}
        assert reached == {family.family_id for family in FAMILIES}, (
            f'input families with no case: {sorted({f.family_id for f in FAMILIES} - reached)}. '
            f'A committed input nothing reads is a file that guards nothing.'
        )


@pytest.mark.unit
class TestEveryDeclaredArtifactExists:
    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_the_cases_artifacts_are_present_and_non_empty(self, case: CorpusCase) -> None:
        expected = [case.family.input_path, case.golden_ir, fingerprint_path(case.family)]
        if case.golden_module is not None:
            expected.append(case.golden_module)
        for path in expected:
            assert path.is_file(), f'{case.case_id}: declared artifact is missing: {path}'
            assert path.stat().st_size > 0, f'{case.case_id}: declared artifact is empty: {path}'

    def test_no_unclaimed_file_exists_under_the_artifact_directories(self) -> None:
        # Catches an orphan left behind by a renamed case -- a file nothing reads, which would
        # otherwise sit in the tree looking like coverage.
        claimed: set[Path] = set()
        for family in FAMILIES:
            claimed.add(family.input_path)
            if family.provenance_path is not None:
                claimed.add(family.provenance_path)
            claimed.add(fingerprint_path(family))
        for case in CASES:
            claimed.add(case.golden_ir)
            if case.golden_module is not None:
                claimed.add(case.golden_module)

        on_disk = {
            path
            for directory in (INPUTS_DIR, GOLDEN_DIR, FINGERPRINT_DIR)
            for path in directory.rglob('*')
            if path.is_file()
        }
        orphans = sorted(str(path.relative_to(GOLDEN_DIR.parent)) for path in on_disk - claimed)
        assert orphans == [], f'files under the corpus artifact directories that no case claims: {orphans}'


@pytest.mark.unit
class TestDefectBookkeeping:
    """Closes in **both** directions, so neither a stale entry nor an unwitnessed one survives."""

    def test_every_defect_a_case_cites_exists(self) -> None:
        for case in CASES:
            unknown = [row for row in case.defects if row not in KNOWN_DEFECTS]
            assert unknown == [], f'{case.case_id} cites unknown defect(s) {unknown}'

    def test_every_known_defect_is_carried_by_at_least_one_case(self) -> None:
        carried = {row for case in CASES for row in case.defects}
        orphaned = sorted(set(KNOWN_DEFECTS) - carried)
        assert orphaned == [], (
            f'KNOWN_DEFECTS entries no case carries: {orphaned}. Either the defect was fixed (then '
            f'delete the entry and its witness test) or a case stopped citing it (then find out why).'
        )

    def test_every_known_defect_has_at_least_one_witness_test(self) -> None:
        # The direction that matters most: an entry with no witness is a claim nothing checks.
        witnesses = (Path(__file__).parent / 'test_witnesses.py').read_text(encoding='utf-8')
        missing = [row for row in KNOWN_DEFECTS if f"'{row}'" not in witnesses]
        assert missing == [], (
            f'KNOWN_DEFECTS entries with no witness in test_witnesses.py: {missing}. A named defect '
            f'that nothing asserts is documentation, not a guard.'
        )

    def test_every_defect_describes_what_correct_output_would_be(self) -> None:
        # `why_it_is_wrong` is what makes a witness failure actionable rather than merely loud;
        # an empty one would render a useless message at exactly the wrong moment.
        for row_id, defect in KNOWN_DEFECTS.items():
            assert defect.row_id == row_id
            assert len(defect.summary) > 20, f'{row_id}: summary is too thin to be useful'
            assert len(defect.why_it_is_wrong) > 60, f'{row_id}: does not say what correct output looks like'

    def test_a_characterized_case_names_its_defects_and_an_asserted_one_has_none(self) -> None:
        for case in CASES:
            if case.status == 'asserted':
                assert case.defects == (), f'{case.case_id}: asserted cases carry no defects'
            else:
                assert case.defects, f'{case.case_id}: characterized cases must name what is wrong'

    def test_at_least_one_case_is_fully_asserted(self) -> None:
        # A corpus in which EVERY golden is characterized would prove nothing about correctness --
        # it would only be a catalogue of bugs. The control matters.
        asserted = [case.case_id for case in CASES if case.status == 'asserted']
        assert asserted, 'no corpus case is fully asserted; the corpus has no correctness control'


@pytest.mark.unit
class TestTheTiersAreTiedTogether:
    """The load-bearing cross-check: the readable goldens anchor five of the 128 hashed rows."""

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_the_manifest_row_sha_equals_the_committed_golden_file_sha(self, case: CorpusCase) -> None:
        assert case.golden_module is not None
        rows = {
            line.split('  ')[0]: line.split('  ')[1]
            for line in fingerprint_path(case.family).read_text(encoding='utf-8').splitlines()
            if line and not line.startswith('#')
        }
        assert case.config_key in rows, f'{case.case_id}: its config point has no manifest row'
        assert rows[case.config_key] == sha256_text(case.golden_module.read_text(encoding='utf-8')), (
            f'{case.case_id}: the manifest row hash and the committed golden file disagree. One of '
            f'them was regenerated without the other, so the manifest has drifted away from the '
            f'text a human actually read.'
        )


@pytest.mark.unit
class TestTheConfigContractHolds:
    def test_a_case_never_disagrees_with_itself_about_prefix_protection(self) -> None:
        # `castiron.cli.gen` passes ONE flag to both `build_schema_from_document` and
        # `EmitterConfig`. A case whose two halves disagree would be testing a configuration no
        # user can produce.
        for case in CASES:
            assert case.source_options.disable_model_prefix_protection == (
                case.emitter_config.disable_model_prefix_protection
            ), f'{case.case_id}: source and emitter disagree about disable_model_prefix_protection'

    def test_source_options_holds_exactly_the_two_documented_keywords(self) -> None:
        # Guards the §12 debt note: SourceOptions must not grow into a general config object
        # while it waits to be replaced by a real type from src/ in CI-010.
        from tests.unit.corpus.cases import SourceOptions

        names = {f.name for f in dataclasses.fields(SourceOptions)}
        assert names == {'infer_generated_primary_keys', 'disable_model_prefix_protection'}

    def test_the_case_table_uses_the_real_emitter_config_type(self) -> None:
        # Hard Rule #6, asserted rather than assumed: no parallel model shape, no dict of booleans.
        for case in CASES:
            assert type(case.emitter_config) is EmitterConfig


@pytest.mark.unit
class TestTheRegenerationToolStaysInItsLane:
    def test_it_never_writes_an_input_or_a_provenance_record(self, regeneration_write_set: dict[Path, str]) -> None:
        # Re-capturing an input and regenerating a golden are DIFFERENT operations with different
        # provenance. A tool that could quietly rewrite `inputs/` could make a stale capture look
        # fresh, and the golden derived from it unfalsifiable.
        trespass = sorted(str(path) for path in regeneration_write_set if path.is_relative_to(INPUTS_DIR))
        assert trespass == [], f'the regeneration tool would write corpus inputs: {trespass}'

    def test_its_write_set_is_a_subset_of_golden_and_fingerprints(
        self, regeneration_write_set: dict[Path, str]
    ) -> None:
        for path in regeneration_write_set:
            writable_root = path.is_relative_to(GOLDEN_DIR) or path.is_relative_to(FINGERPRINT_DIR)
            assert writable_root, f'the regeneration tool would write {path}, outside golden/ and fingerprints/'

    def test_it_does_not_offer_to_rewrite_the_ci_005_golden_this_row_does_not_own(
        self, regeneration_write_set: dict[Path, str]
    ) -> None:
        # `openapi-fixture-default` COMPARES against CI-005's committed golden but must never
        # write it: three other modules assert against those bytes, and acceptance criterion 14
        # requires them byte-unchanged from origin/main.
        fixture_case = next(case for case in CASES if case.case_id == 'openapi-fixture-default')
        assert fixture_case.golden_module is not None
        assert fixture_case.golden_module not in regeneration_write_set
        # ...and it IS still compared: the case declares it, so test_goldens.py asserts on it.
        assert fixture_case.golden_module.is_file()

    def test_it_renders_every_committed_artifact(self, regeneration_write_set: dict[Path, str]) -> None:
        # Otherwise `regenerate` could exit 0 while a golden it never rendered had drifted.
        rendered = set(regeneration_write_set)
        expected = {case.golden_ir for case in CASES}
        expected |= {c.golden_module for c in CASES if c.golden_module and c.golden_module.is_relative_to(GOLDEN_DIR)}
        expected |= {fingerprint_path(family) for family in FAMILIES}
        assert rendered == expected


@pytest.mark.unit
class TestTheCorpusConftestHasNoCollectionHook:
    def test_it_implements_no_session_level_collection_hook(self) -> None:
        # The CI-083 mechanism itself, asserted. A `pytest_collection_modifyitems` in a
        # subdirectory conftest is handed the WHOLE session's item list, not just this
        # directory's -- which is exactly how 1 024 of 1 024 tests were deselected while the gate
        # reported success.
        source = (Path(__file__).parent / 'conftest.py').read_text(encoding='utf-8')
        for hook in ('def pytest_collection_modifyitems', 'def pytest_collection_finish', 'def pytest_collectstart'):
            assert hook not in source, (
                f'{hook} appeared in the corpus conftest. If it is genuinely needed it MUST be '
                f'path-scoped as tests/integration/conftest.py does, and the deselect counts must '
                f'be verified in both directions before and after (CI-083).'
            )
