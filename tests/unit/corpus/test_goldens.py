"""Golden equality, validity-follows-status, and byte hygiene (determinism axis A7).

Three claims, in increasing strength:

1. **Equality** — what this branch produces is byte-for-byte what is committed. This is the
   claim ``castiron check`` (CI-021) will make to users, so a false positive here is a broken
   build for someone who changed nothing.
2. **Validity follows status** — an ``asserted`` golden must ``compile()``; a ``characterized``
   golden must match its declared ``compiles`` value **exactly**, including ``False``. That last
   part is what makes the corpus tell the truth on the day a bug is fixed rather than silently
   accepting the improvement.
3. **Hygiene (A7)** — every committed artifact is read and compared as **bytes**: no ``\\r``,
   exactly one trailing ``\\n``, no trailing whitespace on any line, UTF-8 with no BOM. This is
   both a determinism property and the pre-commit-hook-safety guarantee that
   ``.pre-commit-config.yaml``'s exclusion protects structurally.

⚠ **Determinism axis A6 (interpreters 3.10-3.13) is asserted by the GATE, not by these tests.**
Spawning a second interpreter from a test is not portable — uv-managed pythons need not be on
``PATH`` in CI. The whole corpus runs on all four legs of ``make test-matrix`` and of CI, and
that is the evidence. ⚠ ``make validate-fast`` is single-interpreter and does **not** cover this
axis; it is not the gate (CI-081/CI-082).
"""

import re

import pytest

from tests.unit.corpus.cases import CASES, FINGERPRINT_DIR, GOLDEN_DIR, INPUTS_DIR, REPO_ROOT, CorpusCase
from tests.unit.corpus.compare import assert_golden
from tests.unit.corpus.conftest import case_ids, iter_cases
from tests.unit.corpus.pipeline import module_compiles, render_ir_golden

#: §6.3's naming rule, machine-checked rather than remembered. CI-067 is the precedent: a test
#: file named ``openapi.json?apikey=<SECRET>`` was illegal on Windows and green only because the
#: matrix is ``ubuntu-latest``.
ARTIFACT_NAME = re.compile(r'^[a-z0-9][a-z0-9._-]*$')

#: The three directories holding committed corpus data (as opposed to Python modules).
ARTIFACT_DIRS = (INPUTS_DIR, GOLDEN_DIR, FINGERPRINT_DIR)


def artifact_files() -> list[str]:
    """Return every committed corpus data file, as repo-relative strings (sorted).

    Enumerated by walking the tree, **not** read from the case table: the point is to catch a
    file the case table does not know about.
    """
    paths = [path for directory in ARTIFACT_DIRS for path in sorted(directory.rglob('*')) if path.is_file()]
    return [str(path) for path in paths]


@pytest.mark.unit
class TestIrGoldens:
    """The Schema IR golden — what the *source* produced, before any emitter touched it."""

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_the_ir_matches_its_committed_golden(self, case: CorpusCase, corpus_irs: dict[object, object]) -> None:
        # Separating the IR golden from the emitted golden is what localizes a cause: an emitted
        # golden that moved while the IR did not is an EMITTER change; an IR that moved is a
        # SOURCE change, and the emitted golden must have moved too.
        schema = corpus_irs[(case.family.family_id, case.source_options)]
        assert_golden(render_ir_golden(schema), case.golden_ir, case=case.case_id, what='Schema IR')  # type: ignore[arg-type]


@pytest.mark.unit
class TestEmittedGoldens:
    """The emitted module — the bytes a user's repository actually receives."""

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_the_module_matches_its_committed_golden(self, case: CorpusCase, case_modules: dict[str, str]) -> None:
        assert case.golden_module is not None, f'{case.case_id} declares no module golden'
        assert_golden(case_modules[case.case_id], case.golden_module, case=case.case_id, what='emitted module')

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_emitting_twice_from_one_ir_is_byte_identical(
        self, case: CorpusCase, case_modules: dict[str, str], corpus_irs: dict[object, object]
    ) -> None:
        # Determinism axis A1, on the exact IR object the golden came from: this catches emitter
        # STATEFULNESS, which a fresh-IR comparison cannot. Enumerated over configs by
        # test_manifest.py, which compares every one of the 128 points against a committed
        # constant -- strictly stronger than self-consistency, so A1-on-Tier-A is not a CI-072
        # sampling gap.
        from tests.unit.corpus.pipeline import emit_module

        schema = corpus_irs[(case.family.family_id, case.source_options)]
        assert emit_module(schema, case.emitter_config) == case_modules[case.case_id]  # type: ignore[arg-type]


@pytest.mark.unit
class TestValidityFollowsStatus:
    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_the_module_compiles_exactly_as_declared(self, case: CorpusCase, case_modules: dict[str, str]) -> None:
        actual = module_compiles(case_modules[case.case_id])
        if case.compiles:
            assert actual, (
                f'{case.case_id}: the emitted module no longer parses as Python, and the case '
                f'table says it must. castiron has started emitting invalid code.'
            )
            return
        assert actual is False, (
            f'{case.case_id}: the emitted module NOW PARSES, and the case table declares '
            f'compiles=False.\n\n'
            f'That is very likely GOOD NEWS: this case is characterized for {list(case.defects)}, '
            f'and one of those rows has probably been fixed.\n\n'
            f"If so: regenerate this golden, drop the fixed row from the case's `defects`, delete "
            f'its entry from KNOWN_DEFECTS along with its witness test, flip `compiles` to True '
            f'(and `status` to "asserted" if no defect remains), and say so in the PR.\n'
            f'If not: castiron changed the emitted output for some other reason -- Hard Rule #9.'
        )

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_an_asserted_case_carries_no_known_defect(self, case: CorpusCase) -> None:
        # 'asserted' is a strong claim -- no known defect reaches these bytes -- and this is what
        # stops it decaying into "nobody has looked".
        if case.status == 'asserted':
            assert case.defects == (), f'{case.case_id} is asserted but cites defects {case.defects}'
            assert case.compiles, f'{case.case_id} is asserted, so its module must parse'
        else:
            assert case.defects, f'{case.case_id} is characterized but names no defect'


@pytest.mark.unit
class TestByteHygiene:
    """Determinism axis A7 — read and compared as bytes, never as decoded text."""

    @pytest.mark.parametrize('path', artifact_files())
    def test_the_artifact_is_hook_safe_and_platform_stable(self, path: str) -> None:
        from pathlib import Path

        raw = Path(path).read_bytes()
        assert raw, f'{path}: committed artifact is empty'
        assert b'\r' not in raw, f'{path}: contains a carriage return, so its bytes are platform-dependent'
        assert not raw.startswith(b'\xef\xbb\xbf'), f'{path}: starts with a UTF-8 BOM'
        assert raw.endswith(b'\n'), f'{path}: does not end with a newline (end-of-file-fixer would rewrite it)'
        assert not raw.endswith(b'\n\n'), f'{path}: ends with more than one newline'
        text = raw.decode('utf-8')  # raises UnicodeDecodeError if it is not UTF-8
        offenders = [index + 1 for index, line in enumerate(text.split('\n')) if line != line.rstrip()]
        assert offenders == [], (
            f"{path}: lines {offenders[:5]} carry trailing whitespace. pre-commit's "
            f'trailing-whitespace hook would rewrite this file in place, so the committed golden '
            f'would stop being what the emitter produces. The hook is excluded from these paths '
            f'(see .pre-commit-config.yaml) -- this assertion is what reports the drift instead.'
        )

    @pytest.mark.parametrize('path', artifact_files())
    def test_the_artifact_name_is_portable(self, path: str) -> None:
        from pathlib import Path

        name = Path(path).name
        assert ARTIFACT_NAME.match(name), (
            f'{name!r} does not match {ARTIFACT_NAME.pattern}. Lowercase, no spaces, no "?" and '
            f'no ":" -- CI-067 shipped a test file whose name was illegal on Windows and green '
            f'only because the matrix is ubuntu-latest.'
        )

    @pytest.mark.parametrize('path', artifact_files())
    def test_the_artifact_is_under_the_large_file_limit(self, path: str) -> None:
        from pathlib import Path

        size = Path(path).stat().st_size
        assert size < 500 * 1024, f"{path} is {size} bytes; pre-commit's check-added-large-files rejects >=500 KB"

    def test_an_emitted_golden_never_ends_in_dot_py(self) -> None:
        # `ruff format .` and `ruff check .` run over the whole repo. A golden named `*.py` would
        # be reformatted into a non-golden -- silently, and the corpus would then assert ruff's
        # output rather than castiron's.
        offenders = [case.case_id for case in CASES if case.golden_module and case.golden_module.suffix == '.py']
        assert offenders == [], f'{offenders}: an emitted golden must end .py.txt so ruff leaves it alone'

    def test_the_corpus_enumerates_every_artifact_it_ships(self) -> None:
        # CI6-Q7: an "every artifact is hygienic" claim is worth nothing without the count of what
        # "every" covered. 13 goldens/fingerprints + 3 inputs + 3 provenance records.
        assert len(artifact_files()) == 19


@pytest.mark.unit
class TestTheToolingActuallyProtectsTheseBytes:
    """The exclusions are only worth something if they still name the real directories.

    Both guards are **path-based**, so renaming an artifact directory silently unprotects it while
    every other test stays green. That is not hypothetical: this corpus's fingerprint directory
    was renamed mid-row (``manifest/`` collides with ``.gitignore``'s ``MANIFEST`` on a
    case-insensitive filesystem), and for one commit the exclusions still pointed at the old name.
    These two tests are what turn "remember to update the config" into a red test.
    """

    def test_pre_commit_excludes_every_artifact_directory_from_the_rewriting_hooks(self) -> None:
        # Parsed textually rather than with PyYAML: pyyaml is only a TRANSITIVE dependency here
        # (pre-commit pulls it in), and a test that silently depends on someone else's
        # requirement is one `uv sync` away from an unexplained collection error.
        from pathlib import Path

        config = (REPO_ROOT / '.pre-commit-config.yaml').read_text(encoding='utf-8')
        for hook_id in ('trailing-whitespace', 'end-of-file-fixer'):
            block = config[config.index(f'- id: {hook_id}') :].splitlines()[:4]
            exclude = [line for line in block if 'exclude:' in line]
            assert exclude, (
                f'{hook_id} declares no exclude. It REWRITES TEXT FILES IN PLACE, so a committed '
                f'golden would stop being what the emitter produces and the next `make test` '
                f'would go red for a change nobody made.'
            )

        pattern = re.search(r"exclude: &golden_bytes '([^']+)'", config)
        assert pattern, 'the shared golden-bytes exclude anchor is gone from .pre-commit-config.yaml'
        compiled = re.compile(pattern.group(1))
        for path in artifact_files():
            relative = str(Path(path).relative_to(REPO_ROOT))
            assert compiled.search(relative), (
                f'the rewriting hooks would edit {relative} in place. The exclude pattern '
                f'{pattern.group(1)!r} no longer covers every corpus artifact -- most likely a '
                f'directory was renamed without updating .pre-commit-config.yaml.'
            )

    def test_gitattributes_marks_every_artifact_directory_binary_safe(self) -> None:
        text = (REPO_ROOT / '.gitattributes').read_text(encoding='utf-8')
        declared = {line.split()[0] for line in text.splitlines() if line.strip() and not line.startswith('#')}
        for directory in ARTIFACT_DIRS:
            relative = directory.relative_to(REPO_ROOT)
            assert f'{relative}/**' in declared, (
                f'{relative}/ is not listed in .gitattributes, so a Windows clone with '
                f'core.autocrlf=true would rewrite its bytes to CRLF on checkout and every '
                f'comparison against it would fail for a reason unrelated to castiron.'
            )
