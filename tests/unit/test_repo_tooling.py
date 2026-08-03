"""Guards for three repository-configuration traps whose failure mode is silence.

None of them breaks a build. All three change what a machine *sees* without telling anyone, which
is why they are asserted here rather than trusted to review:

1. **CI-093** -- a bare ``MANIFEST`` line in ``.gitignore`` also matched a ``manifest/``
   **directory**, because git compares ignore patterns case-insensitively when
   ``core.ignorecase`` is set (it is, on macOS). Its contents then vanish from ``git add -A``
   *and* from ``git status``, with no error -- while on Linux CI the same pattern matches
   nothing. That is silent cross-platform divergence in the file set itself. It cost the CI-007
   corpus dispatch four files before anyone noticed.

   ⚠ Anchoring the pattern (``/MANIFEST``) does **not** fix this: anchoring constrains *where* a
   pattern may match, not *how case is compared*. Measured, git 2.55.0 on APFS::

       rule                  root manifest/ (macOS)   root manifest/ (Linux)
       /MANIFEST             IGNORED                  tracked
       (line deleted)        tracked                  tracked

   The line was deleted rather than anchored: castiron builds with **hatchling**, has no
   ``setup.py`` and no ``setup.cfg``, and therefore cannot produce the setuptools/distutils
   sdist artifact the line existed for.

2. **CI-086** -- CI ran bare ``pytest``. The live-source suite under ``tests/integration/``
   skips itself when ``CASTIRON_TEST_POSTGREST_URL`` is unset, so CI was offline by **absence of
   configuration** rather than by construction. Exporting that variable in the workflow would
   have quietly made the suite network-dependent, with every check still green.

3. **CI-105** -- ``.pre-commit-config.yaml`` pinned ``ruff-pre-commit`` at ``v0.6.9`` while
   ``uv.lock`` resolved ``ruff 0.16.0``. ``make lint`` / ``make format`` / ``make validate`` run
   the **locked** ruff; the pre-push hooks run the **pinned** one. A green ``make validate``
   therefore did not imply a green ``git push`` -- the CI-081 shape (a gate covering something
   different from the check after it) on the lint/format axis. It cost real time twice: PR #13
   (``2d590a9``), where the two versions wrapped a long boolean ``assert`` differently and the
   push failed with "files were modified by this hook", and PR #15, where
   ``isinstance(x, (A, B))`` passed all four ``make validate`` legs and was rejected at push
   because 0.6.9 still carried ``UP038`` and 0.16.0 removed it.

   ⚠ The drift was **invisible at rest**: pre-commit hands a hook only the *changed* paths, so
   the two ``UP038`` sites already in ``src/`` (``cli/config.py``, ``ir/models.py``) sat armed for
   whichever PR next touched those files. Nothing goes red until someone's unrelated push does.
   Note ``pre-commit autoupdate`` re-opens this gap by design -- it moves ``rev`` and knows
   nothing about ``uv.lock`` -- which is precisely why the equality is asserted rather than
   remembered.

These follow the precedent of
``tests/unit/corpus/test_goldens.py::TestTheToolingActuallyProtectsTheseBytes``: repository
configuration that some other file depends on is asserted, because "remember to update the
config" is not a mechanism.
"""

import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

# ``tomllib`` is stdlib from 3.11; the 3.10 leg uses the dev-group ``tomli``, mirroring the gate
# in ``src/castiron/cli/config.py``. Unlike PyYAML (see ``workflow_pytest_commands``), ``tomli``
# is a *direct* dev dependency on every interpreter, so depending on it here is not a hidden
# transitive bet. ``uv.lock`` is parsed rather than scanned because ``name = "ruff"`` appears in
# it three times -- once as the package, twice inside dependency lists -- and only one of those
# occurrences carries the resolved version.
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

#: ``tests/unit`` -> ``tests`` -> the repository root.
REPO_ROOT = Path(__file__).parents[2]

CI_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'ci.yml'
MAKEFILE = REPO_ROOT / 'Makefile'
PRE_COMMIT_CONFIG = REPO_ROOT / '.pre-commit-config.yaml'
UV_LOCK = REPO_ROOT / 'uv.lock'

#: Repo-relative names, for failure messages that a reader can act on without decoding a path.
CI_NAME = '.github/workflows/ci.yml'
MAKE_NAME = 'Makefile'
PRE_COMMIT_NAME = '.pre-commit-config.yaml'
LOCK_NAME = 'uv.lock'

#: The pre-commit repo whose pin must equal the locked ``ruff``. ruff is coupled this tightly
#: because it needs no environment at all: it reads ``[tool.ruff]`` and the source and nothing
#: else, so hook and gate differ ONLY by version, and equal versions means identical verdicts.
#:
#: ⚠ This guard deliberately covers ruff **only**. ``mirrors-mypy`` is pinned at ``v1.11.2`` while
#: ``uv.lock`` resolves mypy ``2.3.0`` -- the same drift, a full major wide -- but its fix is not
#: the same fix: that hook runs mypy in an isolated env with no project dependencies, so it
#: reports errors ``make typecheck`` cannot (``click`` degrades to ``Any``, and ``--strict`` then
#: calls every decorated CLI function untyped). Bumping its ``rev`` alone would not align it.
#: Tracked as CI-105's sibling; asserting it here before it is fixed would only add a red test.
RUFF_HOOK_REPO = 'https://github.com/astral-sh/ruff-pre-commit'

#: Paths that must stay trackable. The first is the first mate's minimal reproduction; the second
#: is the real one -- what the CI-007 corpus dispatch tried to commit before renaming the
#: directory to ``fingerprints/`` to dodge the collision. Neither exists; ``git check-ignore``
#: answers for hypothetical paths, which is exactly what makes this assertion cheap.
SHADOWED_PATHS = ('manifest/f.txt', 'tests/unit/corpus/manifest/rows.json')

#: The flags that carry the "offline suite, 90% floor" invariant. Asserted by **presence and
#: value**, never by comparing whole command strings: a whole-string comparison breaks on
#: innocuous reformatting, and a test that cries wolf gets edited until it stops.
LOAD_BEARING_FLAGS = {
    '-m': 'not integration',
    '--cov': 'src/castiron',
    '--cov-fail-under': '90',
}

#: Make targets that run the WHOLE suite, and must therefore exclude the live-source half.
#: ``test-unit`` and ``test-integration`` are deliberately absent -- each selects its own marker.
WHOLE_SUITE_TARGETS = ('test', 'coverage', 'test-matrix')


def command_tokens(line: str) -> list[str]:
    """Tokenize one physical command line, dropping any trailing line-continuation backslash.

    Args:
        line: One line of a Makefile recipe or a workflow ``run:``.

    Returns:
        The shell tokens, or ``[]`` if the line is not tokenizable on its own (a continuation
        fragment with an unbalanced quote, say). Callers guard against an empty result with an
        explicit anti-vacuity assertion.
    """
    try:
        return shlex.split(line.rstrip().rstrip('\\').rstrip())
    except ValueError:
        return []


def invokes_pytest(line: str) -> bool:
    """Report whether a line *runs* pytest, as opposed to merely mentioning it.

    Token-level on purpose. ``test-matrix``'s recipe contains
    ``printf '\\n=== pytest on py%s ===\\n'`` and CI's step is named ``Test (pytest, ...)``;
    a substring check calls both of those invocations and fails on a banner.

    Args:
        line: One line of a Makefile recipe or a workflow ``run:``.

    Returns:
        True if ``pytest`` appears as its own token.
    """
    return 'pytest' in command_tokens(line)


def parse_pytest_flags(command: str) -> dict[str, str]:
    """Split one shell command into its flags, mapping flag name to value.

    Handles both spellings pytest accepts: ``--cov=X`` (joined) and ``-m X`` (separate). A flag
    with no value maps to the empty string.

    Args:
        command: One physical command line, with any trailing line-continuation backslash.

    Returns:
        The flags found, e.g. ``{'-m': 'not integration', '--cov': 'src/castiron'}``.
    """
    tokens = command_tokens(command)
    flags: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith('-'):
            name, separator, value = token.partition('=')
            if separator:
                flags[name] = value
            elif name == '-m' and index + 1 < len(tokens):
                flags[name] = tokens[index + 1]
                index += 1
            else:
                flags[name] = ''
        index += 1
    return flags


def workflow_pytest_commands(text: str) -> list[str]:
    """Return every pytest invocation in a workflow document.

    Only ``run:`` values are considered -- a ``run:`` key with the command inline, or the body of
    a ``run: |`` block scalar. **A step's ``name:`` is never a command**, which matters more than
    it looks: relying on tokenization to exclude it works only for names whose punctuation
    happens to fuse the word, and ``- name: Run pytest`` is an entirely ordinary rename. That
    would otherwise be read as an invocation with no flags, and every assertion below would
    report a missing marker on a command that does not exist.

    Parsed textually rather than with PyYAML, matching the reasoning already recorded in
    ``test_goldens.py``: pyyaml is only a *transitive* dependency here (pre-commit pulls it in),
    and a test that silently depends on someone else's requirement is one ``uv sync`` away from
    an unexplained collection error.

    Args:
        text: The whole workflow file.

    Returns:
        The pytest invocations, stripped, in file order.
    """
    commands = []
    block_indent: int | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.rstrip('\n').lstrip())
        if block_indent is not None:
            if not stripped or stripped.startswith('#'):
                continue
            if indent > block_indent:
                if invokes_pytest(stripped):
                    commands.append(stripped)
                continue
            block_indent = None  # the block ended; this line is re-read as ordinary YAML below
        if not stripped or stripped.startswith('#'):
            continue
        key, separator, remainder = stripped.partition('run:')
        if not separator or key.strip().strip('-').strip():
            continue
        if remainder.strip() in ('|', '>', '|-', '>-', '|+', '>+'):
            block_indent = indent
            continue
        if invokes_pytest(remainder):
            commands.append(remainder.strip())
    return commands


def pinned_hook_rev(text: str, repo: str) -> str | None:
    """Return the ``rev:`` pinned for one pre-commit repo, with any leading ``v`` stripped.

    Parsed textually rather than with PyYAML, for the reason given in
    ``workflow_pytest_commands``: pyyaml reaches this repo only transitively (through pre-commit
    itself), and a test that silently depends on someone else's requirement is one ``uv sync``
    away from an unexplained collection error.

    Comment lines are skipped explicitly. That is load-bearing, not tidiness -- the ruff stanza
    carries a comment block that mentions other version numbers, and a parser that read the first
    line *containing* ``rev:`` would happily return one of those.

    Args:
        text: The whole ``.pre-commit-config.yaml``.
        repo: The repo URL, exactly as it appears after ``- repo:``.

    Returns:
        The pinned revision without its ``v`` prefix (``'0.16.0'``), or None if the repo has no
        stanza or its stanza has no ``rev:``.
    """
    inside = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('- repo:'):
            inside = stripped.partition('- repo:')[2].strip() == repo
            continue
        if inside and stripped.startswith('rev:'):
            return stripped.partition('rev:')[2].strip().strip('\'"').lstrip('v')
    return None


def locked_version(text: str, package: str) -> str | None:
    """Return the version ``uv.lock`` resolves for one package.

    Args:
        text: The whole ``uv.lock``.
        package: The distribution name, e.g. ``'ruff'``.

    Returns:
        The resolved version, or None if the lock has no entry for that package.
    """
    document = tomllib.loads(text)
    for entry in document.get('package', []):
        if entry.get('name') == package:
            version: str | None = entry.get('version')
            return version
    return None


def ci_pytest_commands() -> list[str]:
    """Return every pytest invocation in the repository's CI workflow.

    Returns:
        The invocations found in ``.github/workflows/ci.yml``.
    """
    return workflow_pytest_commands(CI_WORKFLOW.read_text(encoding='utf-8'))


def make_pytest_commands(target: str) -> list[str]:
    """Return every pytest invocation in one Make target's recipe.

    Args:
        target: The target name, e.g. ``'test'``.

    Returns:
        The recipe's pytest lines, stripped. Empty if the target does not run pytest.
    """
    lines = MAKEFILE.read_text(encoding='utf-8').splitlines()
    commands = []
    inside = False
    for line in lines:
        if re.match(rf'^{re.escape(target)}:', line):
            inside = True
            continue
        if inside:
            if not line.startswith('\t'):
                break
            if invokes_pytest(line):
                commands.append(line.strip())
    return commands


@pytest.mark.unit
class TestGitignoreDoesNotShadowADirectory:
    """CI-093 -- asserted through git's own matcher, not by reading ``.gitignore`` for a string.

    ⚠ ``core.ignorecase`` is forced **on** so the assertion means the same thing everywhere.
    Without it this guard is blind exactly where it matters: measured, a repo whose
    ``.gitignore`` contains a bare ``MANIFEST`` reports ``manifest/f.txt`` as **not ignored**
    under ``core.ignorecase=false``. Left to the ambient setting, the trap could be re-added and
    all four CI legs would stay green while every macOS developer silently lost files.
    """

    @pytest.mark.parametrize('path', SHADOWED_PATHS)
    def test_no_ignore_rule_hides_a_manifest_directory(self, path: str) -> None:
        result = subprocess.run(
            ['git', '-c', 'core.ignorecase=true', 'check-ignore', '-v', path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # 0 = ignored, 1 = not ignored, anything else = git itself failed. Distinguishing 128
        # from 1 matters: an error would otherwise read as "not ignored" and pass vacuously.
        assert result.returncode in (0, 1), (
            f'git check-ignore failed ({result.returncode}) instead of answering: '
            f'{result.stderr.strip()!r}. This test needs to run inside a git checkout.'
        )
        assert result.returncode == 1, (
            f'{path} is IGNORED under case-insensitive matching, so on macOS it would vanish '
            f'from `git add -A` AND from `git status` with no error, while Linux CI tracked it '
            f'normally -- the same silent divergence as CI-093. The rule doing it:\n'
            f'  {result.stdout.strip()}\n'
            f'A bare `MANIFEST` line matches a `manifest/` directory case-insensitively. '
            f'Anchoring it to `/MANIFEST` does NOT fix that; the line was deleted because '
            f'castiron builds with hatchling and cannot produce the setuptools artifact it '
            f'existed for. Restore it only alongside a build backend that can.'
        )

    def test_the_guard_above_can_still_see_a_shadowing_rule(self, tmp_path: Path) -> None:
        """Positive control: prove the detector still detects, in a repo that IS trapped.

        Without this, the guard above asserts one thing -- "not ignored" -- which is also what it
        would report if ``-c core.ignorecase=true`` stopped casefolding the exclude matcher
        altogether. It would then stay green forever while the trap became re-armable, which is
        the CI-091 shape (a harness weaker than the thing it guards) that this module's own
        docstring invokes. The measurement that makes the guard meaningful has to be *executed*,
        not asserted in prose.

        Verified on a real case-sensitive volume, not only by simulation: with the flag forced on,
        git casefolds the ignore matcher even where the filesystem does not, so this control holds
        on Linux CI exactly as it does on macOS.
        """
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, capture_output=True)
        (tmp_path / '.gitignore').write_text('MANIFEST\n', encoding='utf-8')
        result = subprocess.run(
            ['git', '-c', 'core.ignorecase=true', 'check-ignore', '-v', 'manifest/f.txt'],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f'a bare `MANIFEST` line did NOT shadow `manifest/f.txt` under '
            f'`core.ignorecase=true` (git said {result.returncode}). That is the CI-093 trap '
            f'failing to reproduce, which means test_no_ignore_rule_hides_a_manifest_directory '
            f'is no longer testing anything: it would pass whether or not the rule came back. '
            f'Most likely git changed how `core.ignorecase` affects `check-ignore`. Find a '
            f'detection method that works before trusting that guard again.'
        )


@pytest.mark.unit
class TestTheWorkflowParserReadsCommandsNotLabels:
    """The parser is the load-bearing half of every CI assertion below, so it is tested directly.

    Each case here is a regression the reviewer measured against the previous implementation,
    which partitioned on ``run:`` and fell back to the whole line: a step *named* ``Run pytest``
    was read as an invocation, and the resulting failure named a command that does not exist.
    """

    def test_a_step_named_after_pytest_is_not_an_invocation(self) -> None:
        document = '\n'.join(
            [
                'jobs:',
                '  quality:',
                '    steps:',
                '      - name: Run pytest',
                '        run: uv run pytest -m "not integration"',
            ]
        )
        assert workflow_pytest_commands(document) == ['uv run pytest -m "not integration"']

    @pytest.mark.parametrize('label', ('- name: Run pytest', '- name: pytest', '- name: Test (pytest, 90% floor)'))
    def test_no_step_label_is_ever_read_as_a_command(self, label: str) -> None:
        assert workflow_pytest_commands(f'      {label}\n') == []

    def test_a_block_scalar_run_is_read(self) -> None:
        document = '\n'.join(
            [
                '      - name: Test',
                '        run: |',
                '          echo hello',
                '          uv run pytest -m "not integration" --cov-fail-under=90',
                '      - name: After',
                '        run: echo done',
            ]
        )
        assert workflow_pytest_commands(document) == ['uv run pytest -m "not integration" --cov-fail-under=90']

    def test_the_real_workflow_has_exactly_one_invocation(self) -> None:
        # Anti-vacuity for every test in the next class: they iterate this list, so an empty or
        # over-full list would quietly change what they mean.
        assert len(ci_pytest_commands()) == 1


@pytest.mark.unit
class TestTheOfflineSuiteIsOfflineByConstruction:
    """CI-086 -- the marker, the coverage source and the floor, in CI and in the Makefile.

    These two files encode the same invariant in two places and have already drifted once, which
    is the argument for ``run: make test``. CI stays open-coded deliberately -- a workflow should
    state the contract it enforces, and ``make test`` carries developer-iteration flags (``-vv``)
    that must not reach CI by inheritance -- so the drift is caught here instead. Note this
    catches drift in **either** direction, including a Makefile-side regression that delegating
    to ``make test`` could not catch at all.
    """

    def test_ci_runs_the_offline_suite_with_the_floor(self) -> None:
        commands = ci_pytest_commands()
        # Anti-vacuity (CI-083): an empty list would pass every assertion below it.
        assert commands, f'{CI_NAME} no longer invokes pytest at all -- did the test step move or get renamed?'
        for command in commands:
            flags = parse_pytest_flags(command)
            for flag, expected in LOAD_BEARING_FLAGS.items():
                assert flags.get(flag) == expected, (
                    f'{CI_NAME} runs pytest without `{flag} {expected}`, found {flags.get(flag)!r}. '
                    f'`-m "not integration"` is what keeps CI offline BY CONSTRUCTION rather than by '
                    f'`CASTIRON_TEST_POSTGREST_URL` happening to be unset in the runner; `--cov` and '
                    f'`--cov-fail-under` are the 90% floor (Hard Rule #8). Restore it in {CI_NAME}, '
                    f'or update LOAD_BEARING_FLAGS here and in {MAKE_NAME} deliberately.'
                )

    def test_ci_and_make_test_agree_on_the_load_bearing_flags(self) -> None:
        ci_commands = ci_pytest_commands()
        make_commands = make_pytest_commands('test')
        assert ci_commands and make_commands, (
            f'expected a pytest invocation in both {CI_NAME} and the `test` target of {MAKE_NAME}; '
            f'found {len(ci_commands)} and {len(make_commands)}'
        )
        ci_flags = parse_pytest_flags(ci_commands[0])
        make_flags = parse_pytest_flags(make_commands[0])
        for flag in LOAD_BEARING_FLAGS:
            assert ci_flags.get(flag) == make_flags.get(flag), (
                f'{CI_NAME} and the `test` target of {MAKE_NAME} disagree on `{flag}`: '
                f'{ci_flags.get(flag)!r} in CI vs {make_flags.get(flag)!r} in {MAKE_NAME}. '
                f'These two encode ONE invariant -- the offline suite with the 90% floor -- and '
                f'whichever one you just changed, change the other. Only these flags are compared; '
                f'the rest of each command line is free to differ (CI has no `-vv`, by design).'
            )

    @pytest.mark.parametrize('target', WHOLE_SUITE_TARGETS)
    def test_every_whole_suite_make_target_excludes_the_live_source_suite(self, target: str) -> None:
        commands = make_pytest_commands(target)
        assert commands, f'the `{target}` target of {MAKE_NAME} no longer invokes pytest -- was it renamed?'
        for command in commands:
            assert parse_pytest_flags(command).get('-m') == 'not integration', (
                f'the `{target}` target of {MAKE_NAME} runs the whole suite without '
                f'`-m "not integration"`:\n  {command}\n'
                f'A developer with CASTIRON_TEST_POSTGREST_URL exported would then get a '
                f'`make validate` that opens sockets, falsifying the offline guarantee this file, '
                f'CONTRIBUTING.md, tests/integration/README.md and tests/integration/conftest.py '
                f'all sell. Targets that deliberately select one half of the suite '
                f'(`test-unit`, `test-integration`) are not listed in WHOLE_SUITE_TARGETS.'
            )


@pytest.mark.unit
class TestTheRevParserReadsPinsNotProse:
    """The parser is the load-bearing half of the CI-105 guard, so it is tested on its own.

    Each case is a way the real file could make a naive parser lie. The ruff stanza in particular
    carries a long comment block that names other versions -- the exact hazard that a
    ``'rev:' in line`` check walks straight into.
    """

    def test_a_pin_is_read_without_its_v_prefix(self) -> None:
        document = '\n'.join(['repos:', f'  - repo: {RUFF_HOOK_REPO}', '    rev: v0.16.0', '    hooks: []'])
        assert pinned_hook_rev(document, RUFF_HOOK_REPO) == '0.16.0'

    def test_a_comment_naming_a_version_is_not_a_pin(self) -> None:
        document = '\n'.join(
            [
                'repos:',
                f'  - repo: {RUFF_HOOK_REPO}',
                '    # was rev: v0.6.9 -- do not go back',
                '    # autoupdate would move this to rev: v0.16.1',
                '    rev: v0.16.0',
            ]
        )
        assert pinned_hook_rev(document, RUFF_HOOK_REPO) == '0.16.0'

    def test_another_repos_pin_is_never_returned(self) -> None:
        document = '\n'.join(
            [
                'repos:',
                '  - repo: https://github.com/pre-commit/mirrors-mypy',
                '    rev: v1.11.2',
                f'  - repo: {RUFF_HOOK_REPO}',
                '    rev: v0.16.0',
            ]
        )
        assert pinned_hook_rev(document, RUFF_HOOK_REPO) == '0.16.0'

    def test_a_repo_that_ends_before_its_rev_yields_nothing(self) -> None:
        # A stanza with no `rev:` must return None rather than leaking the NEXT repo's pin --
        # otherwise a mangled config could compare the wrong two numbers and pass.
        document = '\n'.join(
            [
                'repos:',
                f'  - repo: {RUFF_HOOK_REPO}',
                '    hooks: []',
                '  - repo: https://github.com/pre-commit/mirrors-mypy',
                '    rev: v1.11.2',
            ]
        )
        assert pinned_hook_rev(document, RUFF_HOOK_REPO) is None

    def test_an_absent_repo_yields_nothing(self) -> None:
        assert pinned_hook_rev('repos: []\n', RUFF_HOOK_REPO) is None

    def test_a_dependency_reference_is_not_the_resolved_version(self) -> None:
        # `name = "ruff"` appears in uv.lock three times: once as the package table, and twice
        # inside dependency lists that carry no version. A textual scan returns whichever came
        # first; only the package table is the answer.
        document = '\n'.join(
            [
                '[[package]]',
                'name = "castiron"',
                'version = "0.0.0"',
                '',
                '[package.metadata]',
                'requires-dist = [{ name = "ruff", specifier = ">=0.6.0" }]',
                '',
                '[[package]]',
                'name = "ruff"',
                'version = "9.9.9"',
            ]
        )
        assert locked_version(document, 'ruff') == '9.9.9'

    def test_an_unlocked_package_yields_nothing(self) -> None:
        assert locked_version('[[package]]\nname = "ruff"\nversion = "1.0.0"\n', 'black') is None


@pytest.mark.unit
class TestTheRuffHookAndTheRuffGateAreTheSameRuff:
    """CI-105 -- the pre-push hook's ruff and ``make lint``'s ruff must be one version.

    ``uv.lock`` is the anchor rather than ``ruff --version`` off ``PATH``: the lock is the
    checked-in, reviewable declaration that ``uv run`` materializes, so it is what a PR diff
    shows and what CI resolves. Comparing against an installed binary would make the guard's
    verdict depend on the freshness of whatever ``.venv`` happened to be active.
    """

    def test_the_pinned_hook_rev_equals_the_locked_ruff_version(self) -> None:
        pinned = pinned_hook_rev(PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), RUFF_HOOK_REPO)
        locked = locked_version(UV_LOCK.read_text(encoding='utf-8'), 'ruff')
        # Anti-vacuity (CI-083): `None == None` would pass this loudly-named test while asserting
        # nothing at all -- which is exactly what a renamed repo URL or a restructured lock would
        # produce.
        assert pinned is not None, (
            f'{PRE_COMMIT_NAME} has no `rev:` for {RUFF_HOOK_REPO}. Either the stanza was removed '
            f'-- in which case nothing lints at pre-push and this guard is moot -- or the repo URL '
            f'changed and RUFF_HOOK_REPO here needs to change with it.'
        )
        assert locked is not None, f'{LOCK_NAME} has no resolved version for `ruff`; is it still a dev dependency?'
        assert pinned == locked, (
            f'the pre-push ruff hook runs ruff {pinned} but `make lint` / `make format` / '
            f'`make validate` run the locked ruff {locked}.\n'
            f'That gap is the CI-105 trap: `make validate` can go green on code the push then '
            f'rejects, for a rule or a formatting decision that only one of the two versions has. '
            f'It is invisible until it fires, because pre-commit hands hooks only the CHANGED '
            f'paths -- so the failure lands on an unrelated PR.\n'
            f'Fix: set `rev: v{locked}` in {PRE_COMMIT_NAME}, or move both together with\n'
            f'  uv lock --upgrade-package ruff && uv sync   # then match `rev:` to the new version\n'
            f'Do NOT resolve this by pinning `ruff` in pyproject.toml down to the hook -- that '
            f'freezes the project on an old linter to satisfy a config line.'
        )

    def test_this_guard_can_still_see_a_drifted_pin(self, tmp_path: Path) -> None:
        """Positive control: prove the assertion above can go red (CI-072).

        Without this, ``test_the_pinned_hook_rev_equals_the_locked_ruff_version`` asserts one
        thing -- "equal" -- which is also what it would report if ``pinned_hook_rev`` quietly
        started returning the locked version, or None on both sides of a comparison that had lost
        its anti-vacuity guards. The config is mutated back to the exact rev CI-105 removed, and
        the comparison is re-run through the same function, so a guard that cannot detect the
        original bug fails here instead of passing forever.
        """
        locked = locked_version(UV_LOCK.read_text(encoding='utf-8'), 'ruff')
        assert locked is not None and locked != '0.6.9', (
            f'this control re-arms the original CI-105 trap by pinning the hook to v0.6.9, which '
            f'requires the locked ruff to be something else; uv.lock says {locked!r}.'
        )
        drifted = PRE_COMMIT_CONFIG.read_text(encoding='utf-8').replace(f'rev: v{locked}', 'rev: v0.6.9')
        # The replacement must have bitten. A no-op edit would leave the file aligned and the
        # assertion below would "detect drift" that is not there -- a control proving nothing.
        assert drifted != PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), (
            f'could not re-arm the trap: no `rev: v{locked}` line found to mutate in '
            f'{PRE_COMMIT_NAME}. The pin is probably written in a form pinned_hook_rev reads but '
            f'this control does not (a quoted rev, a commit sha). Update the mutation.'
        )
        (tmp_path / 'config.yaml').write_text(drifted, encoding='utf-8')
        remutated = pinned_hook_rev((tmp_path / 'config.yaml').read_text(encoding='utf-8'), RUFF_HOOK_REPO)
        assert remutated == '0.6.9', (
            f'pinned_hook_rev read {remutated!r} from a config pinned at v0.6.9. It is no longer '
            f'reporting what the file says, so the guard above is not testing anything.'
        )
        assert remutated != locked, (
            f'a hook pinned at v0.6.9 compared EQUAL to the locked ruff {locked}. The comparison '
            f'in test_the_pinned_hook_rev_equals_the_locked_ruff_version cannot fail, which makes '
            f'it theatre (CI-072). Fix the comparison before trusting it.'
        )
