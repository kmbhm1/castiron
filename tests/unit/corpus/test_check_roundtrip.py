"""``gen`` then ``check`` must be clean at **every** reachable config point. Zero false positives.

This is the strongest guarantee in CI-021b, and it guards the failure mode that would make people
rip a drift guard out: a `check` that reports drift on a tree it just generated. Once a user has
seen that once, the tool is off — no message can recover it.

So the claim is not sampled. Every corpus input family is written to disk at **all 128 config
points** and compared with the real :func:`castiron.cli.check.compare_emitted_files`, the same
function the command runs. 4 families x 128 points = **512** write-and-compare round trips.

Cost, measured rather than assumed (CI-095 budget culture) — see the ``-DEV`` audit doc for the
numbers on this machine. It is cheap for a structural reason: ``emissions_for_family`` is memoized
per process and the whole corpus already emits all 512 modules for ``test_lint.py``, so this is one
extra write-and-read pass, not a re-emit.

⚠ **A failure here is a finding, never a test to weaken.** Either ``check`` has a false positive or
the emitter is nondeterministic (Hard Rule #9). Narrowing the sweep would hide both.
"""

from pathlib import Path

import pytest

from castiron.cli.check import compare_emitted_files
from castiron.emitters import EmittedFile
from tests.unit.corpus.cases import InputFamily
from tests.unit.corpus.conftest import family_ids, iter_families

#: The file name every round trip writes under ``tmp_path``. One name reused for all 128 points of
#: a family: each point is written, compared, and superseded, so the test holds one file rather
#: than 128 -- which is what keeps the I/O cost linear in bytes and not in directory entries.
ROUND_TRIP_FILENAME = 'schema.py'


@pytest.mark.unit
@pytest.mark.parametrize('family', iter_families(), ids=family_ids())
def test_every_config_point_round_trips_clean(
    family: InputFamily,
    corpus_emissions: dict[str, dict[str, str]],
    tmp_path: Path,
) -> None:
    """Write each emission and assert the comparator calls it a match.

    Args:
        family: The corpus input family under test.
        corpus_emissions: The session-scoped 128-point sweep, reused rather than recomputed.
        tmp_path: A private output directory. Nothing here touches the committed corpus.
    """
    emissions = corpus_emissions[family.family_id]
    assert len(emissions) == 128, f'expected the full 128-point sweep, got {len(emissions)}'

    target = tmp_path / ROUND_TRIP_FILENAME
    for config_key, text in sorted(emissions.items()):
        # newline='' so Python writes \n exactly as the emitter produced it -- the same guarantee
        # `castiron.cli.output._write` makes with newline='\n'. Without it, this test would pass on
        # Windows for the wrong reason (the write translating, and the read translating back).
        target.write_text(text, encoding='utf-8', newline='')
        emitted = [EmittedFile(path=ROUND_TRIP_FILENAME, content=text)]

        comparisons = compare_emitted_files(emitted, tmp_path)

        assert len(comparisons) == 1
        assert comparisons[0].status == 'match', (
            f'{family.family_id} @ {config_key}: castiron check reports drift against bytes it '
            f'just emitted. That is EITHER a false positive in the comparator OR nondeterminism '
            f'in the emitter (Hard Rule #9) -- both are findings. Do not narrow this sweep.'
        )


@pytest.mark.unit
def test_the_round_trip_can_still_see_drift(tmp_path: Path, corpus_emissions: dict[str, dict[str, str]]) -> None:
    """Positive control (CI-072): prove the sweep above would fail if the comparator went blind.

    A test that only ever asserts ``'match'`` passes just as happily against a comparator that
    returns ``'match'`` unconditionally. This exhibits the other answer on the same inputs.
    """
    family_id = family_ids()[0]
    text = next(iter(sorted(corpus_emissions[family_id].values())))
    target = tmp_path / ROUND_TRIP_FILENAME
    target.write_text(text.replace('\n', '\n', 1) + '# hand edit\n', encoding='utf-8', newline='')

    comparisons = compare_emitted_files([EmittedFile(path=ROUND_TRIP_FILENAME, content=text)], tmp_path)
    assert comparisons[0].status == 'differs'
