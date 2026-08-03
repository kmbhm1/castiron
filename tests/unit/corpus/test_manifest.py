"""The 128-config fingerprint sweep, and the field-sync check that keeps it complete.

**Why a manifest exists at all.** Measured on the ``testbed-public`` capture: the 128 reachable
config points produce **96 distinct outputs**. Not 128 — the axes *interact*, several
combinations collapse onto the same bytes. Two consequences, and they are the whole design:

- A default-config-only corpus would guard **1 of 96** reachable outputs, while ``check`` users
  run the other 95. A false positive in ``check`` is a broken build for someone who changed
  nothing, so guarding one point is not enough.
- A "one golden per single flag flip" corpus provably **cannot see an interaction**, because an
  interaction is by definition invisible to any single flip.

So the sweep is the full product — 2**7, enumerated, never sampled (CI-072) — fingerprinted into
a committed manifest. Each row carries a sha256 plus structural counters, so a moved row shows
*how* it moved rather than merely that it did.

The five readable Tier-A goldens anchor five of those rows: a self-check asserts each Tier-A
manifest row's sha256 equals the sha256 of the committed golden file, so the hashes cannot drift
away from the text a human has actually read.
"""

import dataclasses

import pytest

from castiron.emitters import EmitterConfig
from tests.unit.corpus.cases import (
    EXPECTED_MANIFEST_ROWS,
    NON_OUTPUT_AFFECTING_FIELD,
    SOURCE_AXIS,
    InputFamily,
    all_config_points,
    config_axes,
    config_key,
    fingerprint_path,
)
from tests.unit.corpus.compare import assert_golden
from tests.unit.corpus.conftest import family_ids, iter_families
from tests.unit.corpus.pipeline import render_manifest


def manifest_rows(family: InputFamily) -> list[str]:
    """Return a committed manifest's data rows (header comments stripped)."""
    text = fingerprint_path(family).read_text(encoding='utf-8')
    return [line for line in text.splitlines() if line and not line.startswith('#')]


@pytest.mark.unit
class TestManifestMatchesTheSweep:
    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_manifest_matches_its_committed_golden(
        self, family: InputFamily, corpus_emissions: dict[str, dict[str, str]]
    ) -> None:
        # This single assertion covers all 128 config points for this input: every sha256, every
        # structural counter and every `compiles` verdict, against a committed constant.
        assert_golden(
            render_manifest(family, corpus_emissions[family.family_id]),
            fingerprint_path(family),
            case=family.family_id,
            what='manifest',
        )


@pytest.mark.unit
class TestTheSweepIsComplete:
    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_manifest_has_exactly_one_row_per_config_point(self, family: InputFamily) -> None:
        rows = manifest_rows(family)
        assert len(rows) == EXPECTED_MANIFEST_ROWS, (
            f'{family.family_id}: {len(rows)} manifest rows, expected {EXPECTED_MANIFEST_ROWS}. '
            f'A short manifest means part of the config space is unguarded.'
        )

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_every_config_key_is_distinct(self, family: InputFamily) -> None:
        keys = [row.split('  ')[0] for row in manifest_rows(family)]
        assert len(set(keys)) == len(keys), f'{family.family_id}: duplicate config keys in the manifest'

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_row_set_is_exactly_the_full_product(self, family: InputFamily) -> None:
        # Enumerated, not sampled: the committed key set must equal the generated one exactly.
        committed = {row.split('  ')[0] for row in manifest_rows(family)}
        generated = {config_key(emitter, source) for emitter, source in all_config_points()}
        assert committed == generated, (
            f'{family.family_id}: the manifest does not cover the config product.\n'
            f'  missing from the manifest: {sorted(generated - committed)[:3]}\n'
            f'  unknown to the case table:  {sorted(committed - generated)[:3]}'
        )

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_rows_are_sorted_so_the_manifest_is_deterministic(self, family: InputFamily) -> None:
        keys = [row.split('  ')[0] for row in manifest_rows(family)]
        assert keys == sorted(keys), f'{family.family_id}: manifest rows are not in config-key order'


@pytest.mark.unit
class TestConfigFieldSync:
    """A new ``EmitterConfig`` toggle must not be able to escape the sweep.

    This is CI-072's "enumerate, do not sample" made structural rather than remembered: adding a
    seventh boolean to :class:`EmitterConfig` doubles the config space, and until the manifests
    are regenerated to cover it these tests are red. Nobody has to remember the rule.
    """

    def test_the_axis_set_is_derived_from_the_real_emitter_config(self) -> None:
        expected = {f.name for f in dataclasses.fields(EmitterConfig) if f.name != NON_OUTPUT_AFFECTING_FIELD}
        expected.add(SOURCE_AXIS)
        assert set(config_axes()) == expected, (
            f'The manifest axis set has drifted from EmitterConfig.\n'
            f'  in EmitterConfig but not swept: {sorted(expected - set(config_axes()))}\n'
            f'  swept but not in EmitterConfig: {sorted(set(config_axes()) - expected)}\n\n'
            f'If a toggle was ADDED: regenerate the manifests -- the sweep doubles to '
            f'{2 ** (len(expected) + 1)} rows and EXPECTED_MANIFEST_ROWS must be updated with it.'
        )

    def test_the_sweep_size_is_two_to_the_number_of_axes(self) -> None:
        assert len(all_config_points()) == 2 ** len(config_axes()) == EXPECTED_MANIFEST_ROWS

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_the_committed_manifest_names_every_axis_in_every_row(self, family: InputFamily) -> None:
        for row in manifest_rows(family):
            key = row.split('  ')[0]
            named = {pair.split('=')[0] for pair in key.split(',')}
            assert named == set(config_axes()), f'{family.family_id}: row names {named}, expected {config_axes()}'

    def test_output_filename_is_excluded_because_it_does_not_affect_content(self) -> None:
        # The licence for excluding it is determinism axis A8, which asserts the claim rather
        # than assuming it. Stated here too so the exclusion is not mistaken for an oversight.
        assert NON_OUTPUT_AFFECTING_FIELD not in config_axes()
        assert NON_OUTPUT_AFFECTING_FIELD in {f.name for f in dataclasses.fields(EmitterConfig)}


@pytest.mark.unit
class TestTheSweepMeasuresWhatItClaims:
    def test_the_public_capture_produces_96_distinct_outputs_from_128_configs(
        self, corpus_emissions: dict[str, dict[str, str]]
    ) -> None:
        # THE measurement this whole design rests on. If it ever becomes 128, the axes stopped
        # interacting and the "single-flag goldens cannot see an interaction" argument weakens;
        # if it collapses toward 1, most of the config space stopped mattering. Either way the
        # corpus's shape should be revisited, so the number is pinned rather than assumed.
        distinct = len(set(corpus_emissions['testbed-public'].values()))
        assert distinct == 96, (
            f'The public capture now produces {distinct} distinct outputs from 128 configs, not '
            f'96. This is the measurement that justifies a full sweep over single-flag goldens '
            f'(spec §3.3) -- if it has moved, the corpus design needs revisiting, not the number.'
        )

    def test_config_axes_genuinely_interact(self, corpus_emissions: dict[str, dict[str, str]]) -> None:
        # Concrete proof of the interaction, not just a count: `infer_generated_primary_keys` is a
        # no-op when `generate_crud_models` is off (nothing renders the identity distinction) and
        # meaningful when it is on. A single-flag-flip corpus would see only one of these.
        emissions = corpus_emissions['testbed-public']

        def key(**overrides: bool) -> str:
            from tests.unit.corpus.cases import SourceOptions

            defaults = {'generate_crud_models': True, 'infer_generated_primary_keys': False}
            values = {**defaults, **overrides}
            return config_key(
                EmitterConfig(generate_crud_models=values['generate_crud_models']),
                SourceOptions(infer_generated_primary_keys=values['infer_generated_primary_keys']),
            )

        crud_off_same = (
            emissions[key(generate_crud_models=False)]
            == emissions[key(generate_crud_models=False, infer_generated_primary_keys=True)]
        )
        crud_on_differs = emissions[key()] != emissions[key(infer_generated_primary_keys=True)]
        assert crud_off_same, 'infer_generated_primary_keys was expected to be a no-op without CRUD models'
        assert crud_on_differs, 'infer_generated_primary_keys was expected to matter with CRUD models'

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_every_row_records_a_compile_verdict(self, family: InputFamily) -> None:
        verdicts = {row.rsplit('  ', 1)[-1] for row in manifest_rows(family)}
        assert verdicts <= {'yes', 'no'}, f'{family.family_id}: bad compiles column {verdicts}'
        # The torture input is the only one where any config fails to parse, and it fails for
        # ALL of them -- the defect is in the identifiers, which no emitter toggle sanitizes.
        expected = {'no'} if family.family_id == 'synthetic-torture' else {'yes'}
        assert verdicts == expected, (
            f'{family.family_id}: compile verdicts are {verdicts}, expected {expected}. For the '
            f'torture input a "yes" means CI-080/CI-085 may be fixed -- see KNOWN_DEFECTS.'
        )
