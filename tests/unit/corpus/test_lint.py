"""castiron's own output, put in front of a linter — every reachable emission, every time.

**Why this file has to exist.** ``test_goldens.py:163-168`` names every emitted golden ``*.py.txt``
so that ``ruff format .`` and ``ruff check .`` never see it:

    *"A golden named ``*.py`` would be reformatted into a non-golden -- silently, and the corpus
    would then assert ruff's opinion rather than castiron's."*

That decision is correct and stays. It is also **exactly why CI-092 and `I001` shipped**: nothing
in this repository had ever run a linter over emitted bytes, so castiron published a module that
tripped the linter of the project it was just added to. The hole is closed by **inverting the
relationship** (``CI94-D12``) rather than by renaming a golden: the guard copies emitted text into
a ``tmp_path`` as ``.py`` and runs ruff **there**, ``--isolated``, with an explicit rule
selection. ruff's opinion is asserted *about* the bytes and never applied *to* them.

**Why a subprocess and not :mod:`ast`.** ``CI94-Q3(b)``, re-ruled by the captain: an AST-level
invariant can see an unused import, but it **cannot see ``I001`` at all** -- import ordering is
pure style and survives any assertion about the tree. The honest oracle is ruff itself.

**What it costs, measured here rather than quoted.** ``CI94-D10`` approved this guard on
**+62 ms per interpreter leg**. Re-measured on the machine that wrote it: writing the 384
lintable modules is 24 ms, one ruff pass over them is 88 ms, and the ``python -m`` subprocess
floor is **33 ms** -- where the approving measurement recorded 10 ms. So the approved single-pass
design costs **112 ms** here, and this module costs **~165 ms/leg** (0.66 s across the four-leg
gate) for that pass plus one more that carries both harness self-checks. Everything below is
structured to keep the number of ruff invocations at **two**: ``CI-095`` records that a gate
overage was accepted grudgingly, and a budget quietly exceeded is that failure repeating.

**What castiron promises** (``CI94-Q3(c)``, captain): emitted output is clean under **F**, **UP**
and **I** with **default** ruff settings. It promises nothing about ``E501`` (the longest emitted
line is 101 characters -- clean at >=102 columns, dirty at ruff's default 88, and it is driven by
``Field(description=...)`` carrying the user's own SQL comment) and nothing about non-default rule
sets.

**The guard asserts that promise flatly, and it did not always.** CI-094 shipped in two PRs, and
while the first one was in flight ``CI-092`` was still open -- so this module carried a
``KNOWN_LINT_DEFECTS`` allowance and asserted only that *every finding is owned by a named open
row*. That allowance was deliberately built to **self-close**: a companion test asserted each
listed defect was still **reachable**, so the day CI-092 landed the guard went red and named the
row. It did, the allowance was deleted, and
:meth:`TestEveryReachableEmissionIsLintClean.test_every_reachable_emission_is_clean` now asserts
**clean**. Re-introducing an allowance list is a captain-level decision, not a way to make a red
test green.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.corpus.cases import SYNTHETIC_TORTURE
from tests.unit.corpus.pipeline import ENCODING

#: The rules castiron's output is promised to be clean under, at ruff's own defaults.
PROMISED_RULES = 'F,UP,I'

#: ``synthetic-torture`` is excluded **by name and with a reason**, never by filtering out codes
#: that happen to be inconvenient. Its 128 emissions do not parse at all: ``CI-085`` (column
#: identifiers are not sanitized) is a known, open, deliberately-out-of-scope defect, and the case
#: table declares ``compiles=False`` for it. Linting unparseable text would assert nothing about
#: import hygiene and would hide a real finding behind 960 syntax errors. The day CI-085 lands,
#: delete this constant and watch the guard widen.
EXCLUDED_FAMILY = SYNTHETIC_TORTURE.family_id

#: How ruff spells "this file does not parse". ⚠ **Two spellings, and the skew is live.**
#: `.pre-commit-config.yaml` pins ruff **v0.6.9**, which prints `path:1:7: SyntaxError: ...`;
#: `uv.lock` resolves **0.16.0**, which prints `invalid-syntax:`. That divergence is filed as
#: `CI-105` and is still open, and `pyproject.toml`'s floor is only `ruff>=0.6.0` -- so matching
#: one spelling would make this guard pass vacuously under the other.
SYNTAX_ERROR_SPELLINGS = ('invalid-syntax', 'SyntaxError')

#: How long a single ruff invocation may take before the gate fails instead of hanging. A hung
#: subprocess with no timeout blocks all four legs forever, which is a worse failure than a red
#: test. Generous by two orders of magnitude: the full 384-module sweep measures ~88 ms.
RUFF_TIMEOUT_SECONDS = 120

#: Why the exclusion is legitimate, quoted into the skip so nobody has to go looking.
EXCLUSION_REASON = (
    f'{EXCLUDED_FAMILY} emits deliberately invalid Python (CI-085, open and out of scope for '
    f'CI-094); the case table declares compiles=False for it.'
)


#: Whether ruff is importable, and therefore whether ``python -m ruff`` can run.
#:
#: ⚠ Deliberately **not** :func:`shutil.which` -- measured, it resolved to a path that did not
#: exist; ruff is a dev-group dependency, so the module form is the reliable one. And deliberately
#: :func:`importlib.util.find_spec` rather than a ``--version`` subprocess: this runs at import
#: time on every one of the four gate legs, and a subprocess costs 33 ms here for information a
#: spec lookup gives free. The stronger check -- that ruff actually *ran* -- is enforced by
#: :func:`lint` refusing to interpret an unexpected exit code as "clean".
RUFF_AVAILABLE = importlib.util.find_spec('ruff') is not None

#: A contributor without the dev group gets a **loud** skip rather than an error.
requires_ruff = pytest.mark.skipif(
    not RUFF_AVAILABLE,
    reason='LOUD SKIP: ruff is unavailable, so castiron output is NOT being linted. '
    'Install the dev dependency group (`uv sync`) to restore this guard.',
)


def lint(paths: list[str], select: str = PROMISED_RULES) -> str:
    """Run ruff over ``paths`` with castiron's own configuration deliberately excluded.

    Args:
        paths: Files to lint.
        select: The ruff rule selection.

    Returns:
        ruff's concise output (empty when clean).

    Raises:
        RuntimeError: If ruff exits with anything other than 0 (clean) or 1 (findings).

    ``--isolated`` is load-bearing: without it ruff reads this repository's ``pyproject.toml``
    (120 columns, ``D`` rules) and the test would assert **castiron's house style** rather than
    what a user's default-configured ruff sees.

    ⚠ **The exit-code check is the guard's own guard** (``CI-091``: a harness can be weaker than
    the artifact it tests). Without it, *any* failure to run ruff -- a missing module, a bad flag,
    a future CLI change -- produces empty stdout, and empty stdout reads as "no findings". Every
    assertion in this module would then pass while linting nothing at all, on all four legs, in
    silence. That is the single most likely way this file rots.
    """
    result = subprocess.run(
        [
            sys.executable,
            '-m',
            'ruff',
            'check',
            '--isolated',
            '--no-cache',
            '--select',
            select,
            '--output-format',
            'concise',
            *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=RUFF_TIMEOUT_SECONDS,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(
            f'ruff exited {result.returncode}, which is neither "clean" (0) nor "found something" '
            f'(1). Its output is NOT evidence about castiron.\nstderr: {result.stderr[:1000]}'
        )
    return '\n'.join(line for line in result.stdout.splitlines() if ': ' in line)


def write_as_python(text: str, target: Path) -> Path:
    """Write emitted text to ``target`` **byte-identically**, so ruff lints what a user receives.

    ``newline=''`` and an explicit encoding, exactly as ``pipeline.py`` writes every committed
    artifact: no line-ending translation, no trailing-newline fixup. A guard that lints a
    normalized copy is a guard about a file nobody ships.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding=ENCODING, newline='')
    return target


@pytest.fixture(scope='session')
def linted_emissions(
    corpus_emissions: dict[str, dict[str, str]], tmp_path_factory: pytest.TempPathFactory
) -> list[str]:
    """Every lintable emission written to disk once per session, as ``.py``.

    Session-scoped because writing 384 modules is the only real cost here (measured ~26 ms); ruff
    itself is ~37 ms for the whole set.
    """
    root = tmp_path_factory.mktemp('emissions')
    paths: list[str] = []
    for family_id, emissions in sorted(corpus_emissions.items()):
        if family_id == EXCLUDED_FAMILY:
            continue
        for index, (_, text) in enumerate(sorted(emissions.items())):
            paths.append(str(write_as_python(text, root / family_id / f'{index:03d}.py')))
    return paths


@pytest.fixture(scope='session')
def sweep_findings(linted_emissions: list[str]) -> list[str]:
    """ruff's findings over the whole sweep, from **one** invocation per session.

    ⚠ Deliberately one call, not one per assertion. ``CI-095`` records that the captain accepted
    a gate overage grudgingly, and ``CI94-D10`` approved this guard on a measured **+62 ms per
    interpreter leg** -- which is the cost of a *single* pass. Seven passes measured +0.34 s, five
    times the number the decision was made on, and a budget silently exceeded is the ``CI-095``
    failure repeating. Every assertion below filters this list; ``I001`` and ``F401`` are both
    inside ``PROMISED_RULES``, so nothing is lost by not re-running ruff with a narrower select.
    """
    return lint(linted_emissions).splitlines()


@pytest.fixture(scope='session')
def probe_findings(corpus_emissions: dict[str, dict[str, str]], tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """The two harness self-checks, folded into **one** ruff invocation.

    ``torture.py`` is one ``synthetic-torture`` emission (it must still fail to parse, which is
    what keeps :data:`EXCLUDED_FAMILY` honest); ``long.py`` is a 100-character line, legal under
    castiron's own 120-column configuration and illegal at ruff's 88-column default (which is what
    proves ``--isolated`` is still biting). Two claims, two files, one subprocess -- the
    subprocess floor is 33 ms on this hardware and there are four gate legs.
    """
    root = tmp_path_factory.mktemp('probes')
    text = next(iter(sorted(corpus_emissions[EXCLUDED_FAMILY].items())))[1]
    write_as_python(text, root / 'torture.py')
    write_as_python(f'x = {"a" * 100!r}\n', root / 'long.py')
    return lint([str(root / 'torture.py'), str(root / 'long.py')], select=f'{PROMISED_RULES},E501').splitlines()


@pytest.mark.unit
@requires_ruff
class TestEveryReachableEmissionIsLintClean:
    """All 512 reachable emissions (4 inputs × 128 config points), not the 5 readable goldens.

    ``CI94-D10``: 128 configs produce 96 distinct outputs on ``testbed-public`` alone, so a
    five-module check would guard 5 of them. Enumerating the sweep is what makes the claim
    "castiron emits lint-clean code" mean the thing it sounds like (``CI-072``).
    """

    def test_the_sweep_covers_every_reachable_emission(self, linted_emissions: list[str]) -> None:
        assert len(linted_emissions) == 384, (
            f'expected 3 lintable families × 128 config points, got {len(linted_emissions)}. A '
            f'guard that silently narrows is worse than none.'
        )

    def test_i001_is_zero_across_the_whole_sweep(self, sweep_findings: list[str]) -> None:
        # THE claim of CI-094's import-hygiene half, asserted alone so nothing can dilute it.
        # Measured on origin/main: 512 of 512 emissions reported it.
        unsorted_blocks = [line for line in sweep_findings if ' I001 ' in line]
        assert unsorted_blocks == [], 'castiron emitted an unsorted import block:\n' + '\n'.join(unsorted_blocks[:20])

    def test_every_reachable_emission_is_clean(self, sweep_findings: list[str]) -> None:
        # 🔴 **"Clean", flat out.** This assertion used to be "every finding is owned by a named
        # open row", backed by a `KNOWN_LINT_DEFECTS` allowance, because CI-092 was still open
        # when the guard was written: `cidr` imported an `IPv6Network` it never used and `point`
        # resolved to the deprecated `typing.Tuple`, so 256 findings were expected.
        #
        # That allowance was built to **self-close**, exactly as `cases.KNOWN_DEFECTS` does for
        # the goldens: its companion `test_a_known_lint_defect_is_still_reachable` went red the
        # moment CI-092 landed and said so by name. It did, and the correct response was to
        # DELETE the allowance rather than mute the signal -- which is what this line is.
        #
        # castiron now promises F/UP/I cleanliness under default ruff settings (`CI94-Q3(c)`), so
        # there is no longer any such thing as an acceptable finding here. A new one is either a
        # regression or a row that has to be opened, argued and ruled before it can be tolerated.
        assert sweep_findings == [], (
            f'castiron emitted code that trips ruff under `--select {PROMISED_RULES}` at its own '
            f'defaults. This is the promise CI-094 shipped; do NOT add an allowance list back '
            f'without a captain ruling:\n' + '\n'.join(sweep_findings[:20])
        )

    def test_the_ci_092_shapes_specifically_are_gone(self, sweep_findings: list[str]) -> None:
        # Named separately from the blanket assertion above so a future rule-selection change
        # cannot quietly stop covering the two shapes CI-092 was actually about. Measured on
        # `origin/main` @ `0a70513`: 128 `F401` on `ipaddress.IPv6Network`, 128 `UP035` on
        # `typing.Tuple`, 320 `UP006` -- all of them `testbed-public`'s.
        for code, symbol in (('F401', 'IPv6Network'), ('UP035', 'Tuple'), ('UP006', 'Tuple')):
            offenders = [line for line in sweep_findings if f' {code} ' in line and symbol in line]
            assert offenders == [], f'CI-092 has regressed ({code} on {symbol}):\n' + '\n'.join(offenders[:10])

    def test_the_excluded_family_is_excluded_by_name_and_still_fails_for_the_stated_reason(
        self, probe_findings: list[str]
    ) -> None:
        # The exclusion is only honest if it is still true. If synthetic-torture ever starts
        # parsing, CI-085 has been fixed and this guard must widen to cover it -- which is a red
        # test here rather than a silent 384-instead-of-512 that nobody notices.
        hits = [
            line
            for line in probe_findings
            if 'torture.py' in line and any(spelling in line for spelling in SYNTAX_ERROR_SPELLINGS)
        ]
        assert hits, (
            f'{EXCLUDED_FAMILY} now parses. {EXCLUSION_REASON} If CI-085 has landed, delete '
            f'EXCLUDED_FAMILY so the guard covers all 512 emissions.'
        )

    def test_no_emission_imports_field_without_using_it(self, sweep_findings: list[str]) -> None:
        # Stated separately from the sweep because this is the shape CI-094 fixed rather than one
        # it inherited: `from pydantic import Field` was unconditional, and measured on
        # origin/main **32 of these 512** emissions imported it and never called it. All 32 were
        # `testbed-inventory` at `--no-crud-models --no-null-parent-classes` -- an ordinary
        # invocation. Kept separate so no future allowance list can ever be written that covers it.
        offenders = [line for line in sweep_findings if ' F401 ' in line and 'Field' in line]
        assert offenders == [], 'castiron imported `pydantic.Field` into a module that never calls it:\n' + '\n'.join(
            offenders[:10]
        )


@pytest.mark.unit
@requires_ruff
class TestTheGuardLintsWhatCastironActuallyWrites:
    """`CI-091`: a harness can be weaker than the artifact it guards. These close that."""

    def test_the_linted_copy_is_byte_identical_to_the_emitted_text(
        self, corpus_emissions: dict[str, dict[str, str]], tmp_path: Path
    ) -> None:
        text = corpus_emissions['testbed-public'][sorted(corpus_emissions['testbed-public'])[0]]
        target = write_as_python(text, tmp_path / 'copy.py')
        assert target.read_bytes() == text.encode(ENCODING)

    def test_ruff_is_isolated_from_castirons_own_configuration(self, probe_findings: list[str]) -> None:
        # Proof that `--isolated` bites: a 100-character line is legal under castiron's own
        # 120-column config and illegal under ruff's 88-column default. If this guard ever stopped
        # being isolated, it would silently start asserting the house style.
        assert [line for line in probe_findings if 'long.py' in line and ' E501 ' in line]

    def test_a_ruff_that_did_not_run_can_never_look_clean(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The negative control for `lint`'s exit-code check, and the reason it exists. Measured:
        # `ruff check --select NO_SUCH_RULE_9999` exits **2** with **empty stdout**, and "nothing
        # on stdout" is indistinguishable from "no findings" to every assertion in this module.
        # Without the check in `lint`, the whole file passes while linting nothing.
        #
        # Faked rather than provoked with a real bad invocation: the real one costs another 33 ms
        # subprocess on all four gate legs (CI-095) and proves exactly the same contract.
        def failed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=2, stdout='', stderr='ruff: error')

        monkeypatch.setattr(subprocess, 'run', failed)
        with pytest.raises(RuntimeError, match='neither "clean"'):
            lint(['whatever.py'])

    def test_no_committed_golden_anywhere_is_named_dot_py(self) -> None:
        # The other half of CI94-D12, asserted here so the two decisions read as one design:
        # goldens stay `*.py.txt` and out of `ruff format .`'s reach; this module lints a COPY.
        #
        # ⚠ Scans EVERY `golden/` directory in the suite, not just the corpus's. Two of the three
        # live outside it (`emitters/pydantic/golden/`, `sources/openapi/golden/`) and are exactly
        # the two no tool regenerates -- so a `.py` name there would be silently reformatted by
        # `ruff format .` with nothing to restore it. `test_goldens.py`'s sibling assertion reads
        # the CASE TABLE, which cannot see a directory the table does not list.
        from tests.unit.corpus.cases import REPO_ROOT

        golden_dirs = sorted((REPO_ROOT / 'tests').rglob('golden'))
        assert len(golden_dirs) == 3, f'expected 3 golden directories, found {golden_dirs}'
        offenders = sorted(str(p.relative_to(REPO_ROOT)) for d in golden_dirs for p in d.rglob('*.py'))
        assert offenders == [], f'{offenders}: a committed golden must end .py.txt so ruff leaves it alone'


@pytest.mark.unit
class TestTheGuardRunsInTheGate:
    """⚠ Deliberately **outside** ``requires_ruff`` -- a skipped guard is not a guard.

    ``make validate`` runs ``uv run pytest`` inside the project environment, where ruff is a
    dev-group dependency. If everything above quietly skipped there, castiron's output would stop
    being linted and every gate leg would still print green.
    """

    def test_ruff_is_available_so_nothing_above_silently_skipped(self) -> None:
        assert RUFF_AVAILABLE, (
            'ruff is not importable, so every emitted-output lint assertion in this module '
            'skipped. Run `uv sync` to install the dev dependency group.'
        )
