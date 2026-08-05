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

   The **mypy** hook carried the same defect and was armed for the very next dispatch: pinned at
   ``v1.11.2`` against a locked mypy of ``2.3.0``, it rejected
   ``src/castiron/sources/openapi/parse.py`` -- a file the next planned PR must touch -- while
   ``make typecheck`` reported ``Success``. It could not be fixed by matching a version, because
   it diverges along two independent axes at once; see
   ``TestTheMypyHookAndTheMypyGateCannotDisagree``. It now runs the project's own toolchain
   through the same Make target ``make validate`` runs, so there is no version to match at all.

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
PYPROJECT = REPO_ROOT / 'pyproject.toml'
RELEASE_WORKFLOW = REPO_ROOT / '.github' / 'workflows' / 'release.yml'

#: Repo-relative names, for failure messages that a reader can act on without decoding a path.
CI_NAME = '.github/workflows/ci.yml'
MAKE_NAME = 'Makefile'
PRE_COMMIT_NAME = '.pre-commit-config.yaml'
LOCK_NAME = 'uv.lock'
PYPROJECT_NAME = 'pyproject.toml'
RELEASE_NAME = '.github/workflows/release.yml'

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

#: The hosted mypy hook CI-105 replaced with a ``repo: local`` one. Asserted **absent**: it is
#: the shape of the bug, not a specific bad version. Re-adding it at any ``rev`` re-creates a
#: second mypy with a second dependency set, which is what diverged from ``make typecheck`` in
#: two independent ways at once (version *and* missing project dependencies).
MYPY_MIRROR_REPO = 'https://github.com/pre-commit/mirrors-mypy'

#: The Make target the pre-push mypy hook must invoke. Not compared as a literal string: the
#: assertion is that whatever target the hook runs is one ``make validate`` *also* runs, which is
#: what makes "the gate and the hook cannot disagree" structural rather than remembered.
VALIDATE_TARGET = 'validate'

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

#: The extra that carries the release build's ``uv``. Referenced by name rather than spelled into
#: each assertion so the two ends of the coupling -- the extra and the ``pip install`` that
#: consumes it -- cannot drift apart silently.
BUILD_EXTRA = 'build'

#: The action whose Docker image ``[tool.semantic_release].build_command`` executes inside.
PSR_ACTION = 'python-semantic-release/python-semantic-release'

#: Environment variables ``build_command`` may reference, keyed by the PSR **major** the release
#: workflow pins. Two sources, both read from the shipped code rather than the docs:
#:
#: * the whitelist ``build_distributions`` passes through (``PATH``, ``HOME``, ``VIRTUAL_ENV``,
#:   and the CI markers) -- identical in v9 and v10, and
#: * the variables PSR *injects*, which are **not** identical: v9.21.2 injects ``NEW_VERSION``
#:   alone (``cli/commands/version.py:638``); ``PACKAGE_NAME`` arrived in v10
#:   (``cli/commands/version.py:672-673`` of the locked 10.6.1).
#:
#: That difference is the CI-115 trap. PSR's own uv-integration guide writes
#: ``uv lock --upgrade-package "$PACKAGE_NAME"``, which under the v9 this repo pins expands to
#: the empty string -- and ``uv lock --upgrade-package ""`` is a hard error
#: ("Empty field is not allowed for PEP508", measured), so the build fails and PSR aborts the
#: release. An unset variable in a shell is silent; only the command it feeds says anything.
PSR_WHITELISTED_ENV = frozenset(
    {
        'PATH',
        'HOME',
        'VIRTUAL_ENV',
        'CI',
        'GITHUB_ACTIONS',
        'GITLAB_CI',
        'GITEA_ACTIONS',
        'BITBUCKET_CI',
        'PSR_DOCKER_GITHUB_ACTION',
    }
)
PSR_BUILD_COMMAND_ENV = {
    9: PSR_WHITELISTED_ENV | {'NEW_VERSION'},
    10: PSR_WHITELISTED_ENV | {'NEW_VERSION', 'PACKAGE_NAME'},
}

#: python-semantic-release keys that v8 deleted and that nothing has read since. They are asserted
#: **absent**, in the CI-093 / CI-097 spirit: the danger is not that they are stale, it is that
#: ``upload_to_pypi = false`` *reads* like "this project does not publish to PyPI" while the
#: publish happens anyway, from the explicit ``pypa/gh-action-pypi-publish`` step in the release
#: workflow. ``RawConfig`` declares no ``model_config``, so pydantic's default ``extra="ignore"``
#: swallows them without so much as a warning -- there is no version of this trap that announces
#: itself. Verified dead by whole-tree grep of PSR v9.21.2 (the commit ``@v9`` resolves to) and of
#: the installed 10.6.1: zero hits outside their own changelogs. The live successor of
#: ``upload_to_release`` is ``[tool.semantic_release.publish].upload_to_vcs_release``, which
#: already defaults to ``True``.
DEAD_SEMANTIC_RELEASE_KEYS = ('branch', 'upload_to_pypi', 'upload_to_release')

#: Matches ``$VAR`` and ``${VAR}`` -- how an unset variable enters a shell command silently.
ENV_REFERENCE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')


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


def local_hook_entry(text: str, hook_id: str) -> str | None:
    """Return the ``entry:`` command of one hook defined under ``repo: local``.

    Comment lines are skipped, which is load-bearing for the same reason as in
    ``pinned_hook_rev``: the local mypy stanza is preceded by a comment block that contains the
    literal text ``make typecheck-matrix``, so a parser that matched the first line *containing*
    ``entry:`` or a bare ``make`` would read documentation as configuration.

    Args:
        text: The whole ``.pre-commit-config.yaml``.
        hook_id: The hook's ``id:``, e.g. ``'mypy'``.

    Returns:
        The entry command, or None if no local hook with that id exists.
    """
    in_local_repo = False
    in_target_hook = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('- repo:'):
            in_local_repo = stripped.partition('- repo:')[2].strip() == 'local'
            in_target_hook = False
            continue
        if stripped.startswith('- id:'):
            in_target_hook = in_local_repo and stripped.partition('- id:')[2].strip() == hook_id
            continue
        if in_target_hook and stripped.startswith('entry:'):
            return stripped.partition('entry:')[2].strip()
    return None


def make_prerequisites(text: str, target: str) -> list[str]:
    """Return the prerequisite targets on one Make target's rule line.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'validate'``.

    Returns:
        The prerequisites, e.g. ``['lint', 'typecheck-matrix', 'test-matrix']``. Empty when the
        target does not exist or has no prerequisites. A trailing ``## help text`` comment -- the
        convention every target in this Makefile uses -- is stripped first, so its words are
        never mistaken for prerequisites.
    """
    for line in text.splitlines():
        if not re.match(rf'^{re.escape(target)}:', line):
            continue
        return line.partition(':')[2].partition('##')[0].split()
    return []


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


def build_command_steps(command: str) -> list[str]:
    """Split a ``build_command`` into the individual commands a shell would run.

    Blank lines and ``#`` comments are dropped; everything else is stripped of the indentation
    the TOML multi-line string carries for readability. **Order is preserved and load-bearing** --
    every assertion in ``TestTheReleaseBuildCommandCanRunWhereItRuns`` is about sequence, because
    python-semantic-release hands this whole block to a single ``sh -c`` (``shell()`` in
    ``cli/commands/version.py``) rather than running the lines one at a time.

    Args:
        command: The ``[tool.semantic_release].build_command`` value.

    Returns:
        The commands, in the order the shell would reach them.
    """
    steps = []
    for raw_line in command.splitlines():
        stripped = raw_line.strip()
        if stripped and not stripped.startswith('#'):
            steps.append(stripped)
    return steps


def first_step_starting_with(steps: list[str], *prefix: str) -> int | None:
    """Return the index of the first step whose leading tokens are ``prefix``.

    Token-level for the reason ``invokes_pytest`` is: a substring test for ``uv`` matches the word
    inside ``uv.lock``, inside a path, and inside prose, and this module's whole subject is
    configuration that lies quietly.

    Args:
        steps: The steps from ``build_command_steps``.
        *prefix: The leading tokens to match, e.g. ``'uv', 'build'``.

    Returns:
        The index, or None if no step starts with those tokens.
    """
    for index, step in enumerate(steps):
        if command_tokens(step)[: len(prefix)] == list(prefix):
            return index
    return None


def first_step_installing_extra(steps: list[str], extra: str) -> int | None:
    """Return the index of the first step that installs one of this project's extras.

    Matches any token *ending* in ``[<extra>]``, so it reads ``.[build]``, ``'.[build]'`` and
    ``cast-iron[build]`` alike -- the installer and the spelling of the target are free to change
    without turning this guard into a red test about nothing.

    Args:
        steps: The steps from ``build_command_steps``.
        extra: The extra's name, e.g. ``'build'``.

    Returns:
        The index, or None if no step installs it.
    """
    marker = f'[{extra}]'
    for index, step in enumerate(steps):
        if any(token.endswith(marker) for token in command_tokens(step)):
            return index
    return None


def flag_argument(steps: list[str], flag: str) -> str | None:
    """Return the argument given to ``flag`` by the first step that passes it.

    Args:
        steps: The steps from ``build_command_steps``.
        flag: The flag, e.g. ``'--upgrade-package'``.

    Returns:
        The following token, or None if no step passes the flag (or passes it with no argument).
    """
    for step in steps:
        tokens = command_tokens(step)
        if flag in tokens:
            position = tokens.index(flag)
            if position + 1 < len(tokens):
                return tokens[position + 1]
    return None


def env_references(command: str) -> set[str]:
    """Return every environment variable a shell command interpolates.

    Args:
        command: A shell command, or a whole ``build_command`` block.

    Returns:
        The variable names, without their ``$`` or braces.
    """
    return {match.group(1) for match in ENV_REFERENCE.finditer(command)}


def pinned_action_major(text: str, action: str) -> int | None:
    """Return the major version a workflow pins one GitHub Action at.

    Comment lines are skipped for the reason recorded in ``pinned_hook_rev``: the release
    workflow's header comment names actions in prose, and a parser that matched the first line
    *containing* the action would read documentation as configuration.

    Args:
        text: The whole workflow file.
        action: The ``owner/repo`` of the action, without a ref.

    Returns:
        The major from an ``@vN`` ref, or None if the action is absent or pinned some other way
        (a sha, a full ``vN.N.N``, a branch).
    """
    pattern = re.compile(rf'uses:\s*{re.escape(action)}@v(\d+)\s*$')
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith('#'):
            continue
        match = pattern.search(stripped)
        if match:
            return int(match.group(1))
    return None


def pyproject_table(*path: str) -> dict[str, object]:
    """Return one table from ``pyproject.toml``, or an empty dict if it does not exist.

    Args:
        *path: The table's key path, e.g. ``'tool', 'semantic_release'``.

    Returns:
        The table. Empty when any key along the path is missing, so callers assert on contents
        rather than guarding every lookup.
    """
    table: object = tomllib.loads(PYPROJECT.read_text(encoding='utf-8'))
    for key in path:
        if not isinstance(table, dict) or key not in table:
            return {}
        table = table[key]
    return table if isinstance(table, dict) else {}


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

    def test_a_comment_naming_a_make_target_is_not_an_entry(self) -> None:
        # The real local-mypy stanza is preceded by a comment block containing the literal
        # `make typecheck-matrix`, so this is the file's actual shape, not a hypothetical.
        document = '\n'.join(
            [
                'repos:',
                '  # Invoking `make typecheck-matrix` removes the sync surface.',
                '  #   entry: make something-else',
                '  - repo: local',
                '    hooks:',
                '      - id: mypy',
                '        entry: make typecheck-matrix',
            ]
        )
        assert local_hook_entry(document, 'mypy') == 'make typecheck-matrix'

    def test_a_hosted_hooks_entry_is_never_read_as_local(self) -> None:
        document = '\n'.join(
            [
                'repos:',
                f'  - repo: {MYPY_MIRROR_REPO}',
                '    rev: v1.11.2',
                '    hooks:',
                '      - id: mypy',
                '        entry: not-a-local-hook',
            ]
        )
        assert local_hook_entry(document, 'mypy') is None

    def test_another_local_hooks_entry_is_never_returned(self) -> None:
        document = '\n'.join(
            [
                'repos:',
                '  - repo: local',
                '    hooks:',
                '      - id: pytest',
                '        entry: make test',
                '      - id: mypy',
                '        entry: make typecheck-matrix',
            ]
        )
        assert local_hook_entry(document, 'mypy') == 'make typecheck-matrix'
        assert local_hook_entry(document, 'pytest') == 'make test'

    def test_a_help_comment_is_not_a_prerequisite(self) -> None:
        # Every target in the real Makefile carries a `## help text` comment, whose words would
        # otherwise be read as targets -- `make validate` would appear to run `The`, `pre-push`...
        makefile = 'validate: lint typecheck-matrix test-matrix ## The pre-push gate: lint + typecheck\n'
        assert make_prerequisites(makefile, 'validate') == ['lint', 'typecheck-matrix', 'test-matrix']

    def test_an_absent_target_has_no_prerequisites(self) -> None:
        assert make_prerequisites('validate: lint\n', 'nonexistent') == []


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


@pytest.mark.unit
class TestTheMypyHookAndTheMypyGateCannotDisagree:
    """CI-105, second half -- the same defect as the ruff pin, closed a different way.

    The ruff hook could be fixed by matching a version, because ruff needs no environment. mypy
    cannot: a hosted hook diverges from ``make typecheck`` along **two independent axes**, and
    each was measured separately on this tree --

    * **version** -- mypy 1.11.2 *with* the project's dependencies installed still reported
      ``sources/openapi/parse.py:746 no-any-return``, which mypy 2.3.0 does not (2.x narrows
      ``Any`` through an ``isinstance`` guard). Purely the version.
    * **dependencies** -- mypy 2.3.0 in an environment *without* the project's dependencies
      reported 25 ``untyped-decorator`` errors across ``cli/``, because ``click`` degrades to
      ``Any``. Purely the environment.

    So neither ``rev``-bumping nor ``additional_dependencies`` closes it alone, and doing both
    would leave two hand-synced declarations to drift later. The hook therefore invokes the
    project's own toolchain through the *same Make target* ``make validate`` runs, which is what
    these tests assert: not a version equality, but the absence of anything to keep in sync.
    """

    def test_the_mypy_hook_invokes_a_make_target_that_validate_also_runs(self) -> None:
        entry = local_hook_entry(PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), 'mypy')
        assert entry is not None, (
            f'{PRE_COMMIT_NAME} has no local `mypy` hook. If the pre-push typecheck was removed, '
            f'the only ENFORCED typecheck went with it -- `make validate` is a convention, the '
            f'hooks are what actually runs at push. Restore it or take that to the captain.'
        )
        tokens = shlex.split(entry)
        assert tokens and tokens[0] == 'make', (
            f'the local mypy hook runs {entry!r}, which does not go through `make`. Invoking mypy '
            f'directly re-opens CI-105: the hook would then carry its own idea of which mypy, '
            f'which flags and which interpreters, and could drift from {MAKE_NAME} exactly as the '
            f'mirrors-mypy pin did.'
        )
        prerequisites = make_prerequisites(MAKEFILE.read_text(encoding='utf-8'), VALIDATE_TARGET)
        # Anti-vacuity (CI-083): an empty prerequisite list would make the membership check below
        # pass for nothing at all.
        assert prerequisites, f'the `{VALIDATE_TARGET}` target of {MAKE_NAME} has no prerequisites -- was it renamed?'
        assert len(tokens) == 2, (
            f'the local mypy hook runs `{entry}`, which is not a bare `make <target>`. Extra '
            f'arguments are how the hook starts meaning something {MAKE_NAME} does not, so keep '
            f'the difference in the target, not on the command line.'
        )
        assert tokens[1] in prerequisites, (
            f'the pre-push mypy hook runs `{entry}`, but `make {VALIDATE_TARGET}` runs '
            f'{prerequisites}. The hook must invoke a target {VALIDATE_TARGET} also invokes, or '
            f'the two can reach different verdicts on the same code -- which is the whole of '
            f'CI-105: a green `make validate` that does not imply a green `git push`.\n'
            f'Change both together, or point the hook at one of {prerequisites}.'
        )

    def test_no_hosted_hook_brings_a_second_mypy(self) -> None:
        pinned = pinned_hook_rev(PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), MYPY_MIRROR_REPO)
        assert pinned is None, (
            f'{PRE_COMMIT_NAME} pins {MYPY_MIRROR_REPO} at v{pinned} again. That hook runs its own '
            f'mypy in an environment pre-commit builds, with NO project dependencies -- so it '
            f'disagrees with `make typecheck` on both the version axis (measured: 1.11.2 flagged '
            f'parse.py:746 that 2.3.0 does not) and the dependency axis (measured: 25 '
            f'`untyped-decorator` errors in cli/ because click becomes Any). `rev` bumping fixes '
            f'the first and `additional_dependencies` the second; only running the mypy this repo '
            f'already resolves fixes both without leaving something to hand-sync. See CI-105.'
        )

    def test_this_guard_can_still_see_a_hook_that_left_the_makefile(self, tmp_path: Path) -> None:
        """Positive control: prove both assertions above can go red (CI-072).

        ``test_the_mypy_hook_invokes_a_make_target_that_validate_also_runs`` asserts membership in
        a list, and ``test_no_hosted_hook_brings_a_second_mypy`` asserts a None. Both are shapes
        that pass loudly when the parser underneath them has quietly stopped working -- a
        ``local_hook_entry`` that returned the first ``entry:`` in the file, or a
        ``pinned_hook_rev`` blind to a stanza it should see, would leave both green forever.
        Each is therefore re-run against a config that IS trapped.
        """
        makefile = MAKEFILE.read_text(encoding='utf-8')
        # 1. A hook that calls mypy directly instead of through make -- the pre-CI-105 shape.
        direct = '\n'.join(
            [
                'repos:',
                '  - repo: local',
                '    hooks:',
                '      - id: mypy',
                '        entry: uv run mypy --strict src',
                '        language: system',
            ]
        )
        assert local_hook_entry(direct, 'mypy') == 'uv run mypy --strict src'
        assert shlex.split(str(local_hook_entry(direct, 'mypy')))[0] != 'make', (
            'the control could not build a hook that bypasses make, so the assertion it controls '
            'cannot be shown to fail.'
        )
        # 2. A hook pointed at a real target that `make validate` does NOT run. `typecheck` is the
        #    single-interpreter target -- a plausible, wrong edit, since it typechecks fine and
        #    would silently stop covering three of the four CI interpreters.
        prerequisites = make_prerequisites(makefile, VALIDATE_TARGET)
        assert 'typecheck' not in prerequisites and 'typecheck-matrix' in prerequisites, (
            f'this control assumes `make {VALIDATE_TARGET}` runs `typecheck-matrix` and not '
            f'`typecheck`; it runs {prerequisites}. Re-pick the wrong-but-plausible target.'
        )
        # 3. A restored mirrors-mypy stanza must still be visible to the absence check.
        restored = '\n'.join(
            ['repos:', f'  - repo: {MYPY_MIRROR_REPO}', '    rev: v1.11.2', '    hooks:', '      - id: mypy']
        )
        (tmp_path / 'config.yaml').write_text(restored, encoding='utf-8')
        seen = pinned_hook_rev((tmp_path / 'config.yaml').read_text(encoding='utf-8'), MYPY_MIRROR_REPO)
        assert seen == '1.11.2', (
            f'pinned_hook_rev read {seen!r} from a config that plainly re-adds {MYPY_MIRROR_REPO} '
            f'at v1.11.2. test_no_hosted_hook_brings_a_second_mypy would therefore pass even with '
            f'the hosted hook restored, which makes it theatre (CI-072).'
        )


@pytest.mark.unit
class TestTheReleaseBuildCommandCanRunWhereItRuns:
    """CI-115 -- the release build command, asserted against the environment it actually runs in.

    That environment is not the runner. ``python-semantic-release/python-semantic-release`` is a
    **Docker** action (``runs: {using: docker, image: Dockerfile}``), and its image is
    ``FROM python:3.13-bookworm`` with a pip venv at ``/psr/.venv``. It has no ``uv``, and the one
    ``astral-sh/setup-uv`` installed in the workflow lives on the host and is not mounted in. So
    ``build_command = "uv build"`` -- which every other file in this repo would suggest is the
    obvious spelling -- was ``uv: command not found``, and PSR aborts the release on a failed
    build.

    The second failure was quieter. ``uv.lock`` pins ``cast-iron`` **by version**; PSR rewrites
    ``[project].version`` and then commits, so the lock disagreed with the project definition
    from inside the release commit outward. python-semantic-release documents both against
    itself in ``configuration-guides/uv_integration.rst``.

    ⚠ None of this is reachable by running it: a release run publishes to PyPI. These assertions
    are therefore the only automated check that exists on the release path, and they are
    deliberately about **order and environment** rather than about matching a command string --
    the two things that were wrong, and the two things a reader of the config cannot see.
    """

    def test_uv_is_installed_before_anything_invokes_it(self) -> None:
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        installs = first_step_installing_extra(steps, BUILD_EXTRA)
        uses = first_step_starting_with(steps, 'uv')
        # Anti-vacuity (CI-083): a build_command that never mentions uv would pass an ordering
        # comparison between two Nones while asserting nothing at all.
        assert uses is not None, (
            f'{PYPROJECT_NAME}: `build_command` never invokes `uv`. If the build genuinely moved '
            f'off uv this whole guard is moot and should go; if it did not, the steps are spelled '
            f'in a form first_step_starting_with cannot read.'
        )
        assert installs is not None, (
            f'{PYPROJECT_NAME}: `build_command` runs `uv` but never installs it. It executes '
            f"inside the PSR action's own Docker image (python:3.13-bookworm), which has no uv "
            f"and cannot see the runner's -- so this is `uv: command not found`, and PSR aborts "
            f'the release on a failed build. Install it from the `{BUILD_EXTRA}` extra first.'
        )
        assert installs < uses, (
            f'{PYPROJECT_NAME}: `build_command` invokes uv at step {uses} but does not install it '
            f'until step {installs}:\n  '
            + '\n  '.join(steps)
            + '\nThe image has no uv of its own, so the earlier call is `command not found`.'
        )

    def test_the_command_stops_at_the_first_failure(self) -> None:
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        assert steps and steps[0] == 'set -e', (
            f'{PYPROJECT_NAME}: `build_command` must begin with `set -e`; it begins with '
            f'{steps[0] if steps else "nothing"!r}.\n'
            f'PSR runs this entire block as ONE `sh -c` string (`shell()`, '
            f'cli/commands/version.py), so without `set -e` the exit status PSR sees is the LAST '
            f"command's. A failed `uv lock` followed by a successful `uv build` would then exit "
            f'0 and publish the stale lock this command exists to prevent -- silently, which is '
            f'strictly worse than the bug it replaced.'
        )

    def test_the_lock_is_refreshed_and_staged_before_the_distributions_are_built(self) -> None:
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        relock = first_step_starting_with(steps, 'uv', 'lock')
        stage = first_step_starting_with(steps, 'git', 'add')
        build = first_step_starting_with(steps, 'uv', 'build')
        assert relock is not None, (
            f'{PYPROJECT_NAME}: `build_command` never re-locks. PSR rewrites `[project].version` '
            f'before this command runs, and {LOCK_NAME} pins `cast-iron` by version -- so the '
            f'release commit would ship a lock that disagrees with the project definition it '
            f'sits next to, and the next `uv` run in CI would fail on it.'
        )
        assert stage is not None, (
            f'{PYPROJECT_NAME}: `build_command` re-locks but never stages {LOCK_NAME}. PSR commits '
            f'with a bare `git commit -m` and NO pathspec (`gitproject.py`), so the refresh rides '
            f'the version-bump commit only if something put it in the index first.'
        )
        assert build is not None, f'{PYPROJECT_NAME}: `build_command` never builds a distribution.'
        assert relock < stage < build, (
            f'{PYPROJECT_NAME}: `build_command` must re-lock (step {relock}), stage (step '
            f'{stage}), then build (step {build}), in that order:\n  ' + '\n  '.join(steps) + '\n'
            'Staging before the re-lock stages the stale bytes; building before the re-lock puts '
            'a stale lock inside the sdist that is about to be published.'
        )

    def test_every_variable_the_command_reads_is_one_the_pinned_psr_major_provides(self) -> None:
        command = str(pyproject_table('tool', 'semantic_release').get('build_command', ''))
        major = pinned_action_major(RELEASE_WORKFLOW.read_text(encoding='utf-8'), PSR_ACTION)
        assert major is not None, (
            f'{RELEASE_NAME} does not pin {PSR_ACTION} at a bare `@vN`. This guard reads the major '
            f'to decide which variables `build_command` may reference; teach pinned_action_major '
            f"the new pin style, or record the exact version's injected set below."
        )
        available = PSR_BUILD_COMMAND_ENV.get(major)
        assert available is not None, (
            f'{RELEASE_NAME} pins {PSR_ACTION} at v{major}, which PSR_BUILD_COMMAND_ENV does not '
            f'describe. Bumping the PSR major is exactly when this needs re-checking -- read '
            f"`build_command_env` in that version's cli/commands/version.py and add the row. "
            f'(v11 also REMOVES the `angular` commit parser, so check `commit_parser` at the same '
            f'time.)'
        )
        unavailable = env_references(command) - available
        assert not unavailable, (
            f'{PYPROJECT_NAME}: `build_command` reads {sorted(unavailable)}, which PSR v{major} '
            f'does not put in the build environment. PSR passes a WHITELIST, not os.environ -- an '
            f'unlisted name is simply empty, and a shell says nothing about that.\n'
            f"`PACKAGE_NAME` is the specific trap: PSR's own uv-integration guide uses it, but it "
            f'was added in v10, and under v9 `uv lock --upgrade-package ""` fails with "Empty '
            f'field is not allowed for PEP508". Spell the project name out, or move the action to '
            f'a major that injects the variable.'
        )

    def test_the_relocked_package_is_this_project(self) -> None:
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        argument = flag_argument(steps, '--upgrade-package')
        name = pyproject_table('project').get('name')
        assert argument is not None, (
            f'{PYPROJECT_NAME}: `build_command` passes no `--upgrade-package`. A bare `uv lock` is '
            f'measurably equivalent here (both produce the same one-line lock diff), so if that is '
            f'the deliberate choice, delete this test with it -- do not leave it asserting a flag '
            f'nobody passes.'
        )
        assert argument == name, (
            f'{PYPROJECT_NAME}: `build_command` re-locks {argument!r} but this project is {name!r}. '
            f'The name is spelled out rather than taken from `$PACKAGE_NAME` because the pinned '
            f'PSR major does not inject that variable (see the test above), which is why the '
            f'literal has to be tied to `[project].name` here instead of trusted. uv would reject '
            f'the wrong name outright -- during the release run, after the version bump has '
            f'already been applied.'
        )

    def test_the_build_extra_pins_uv_and_never_reaches_the_runtime_set(self) -> None:
        extras = pyproject_table('project', 'optional-dependencies')
        requirements = extras.get(BUILD_EXTRA)
        assert isinstance(requirements, list) and requirements, (
            f'{PYPROJECT_NAME}: `[project.optional-dependencies].{BUILD_EXTRA}` is missing or '
            f'empty, but `build_command` installs `.[{BUILD_EXTRA}]` to get its uv.'
        )
        specifiers = [str(requirement) for requirement in requirements]
        assert any(specifier.startswith('uv') for specifier in specifiers), (
            f'{PYPROJECT_NAME}: the `{BUILD_EXTRA}` extra is {specifiers}, which does not provide '
            f'uv -- the one thing `build_command` installs it for.'
        )
        assert all(re.search(r'[=<>~!]', specifier) for specifier in specifiers), (
            f'{PYPROJECT_NAME}: the `{BUILD_EXTRA}` extra {specifiers} is unpinned. The release '
            f'build is the one place a {LOCK_NAME} rewrite is COMMITTED and PUBLISHED, so it is '
            f'the one place the uv version must not be "whatever shipped this morning".'
        )
        # The invariant that actually protects users. Extras are opt-in, so this cannot leak into
        # `pip install cast-iron` -- measured on the built wheel's METADATA, which carries
        # `Requires-Dist: uv~=0.11.32; extra == "build"` and leaves the four runtime
        # `Requires-Dist` lines untouched. Asserted at the source rather than by building a wheel:
        # the leak this guards against is someone moving the line, and a moved line shows here.
        runtime = pyproject_table('project').get('dependencies', [])
        assert isinstance(runtime, list)
        leaked = [str(requirement) for requirement in runtime if str(requirement).startswith('uv')]
        assert not leaked, (
            f'{PYPROJECT_NAME}: {leaked} is in `[project].dependencies`, so `pip install cast-iron` '
            f"now drags a 20MB build tool into every user's runtime. uv belongs in the "
            f'`{BUILD_EXTRA}` extra, which nothing installs by accident.'
        )

    @pytest.mark.parametrize('key', DEAD_SEMANTIC_RELEASE_KEYS)
    def test_no_dead_semantic_release_key_returns(self, key: str) -> None:
        table = pyproject_table('tool', 'semantic_release')
        assert key not in table, (
            f'{PYPROJECT_NAME}: `[tool.semantic_release].{key}` is a v7-era key that PSR v8 '
            f"deleted. `RawConfig` declares no `model_config`, so pydantic's default "
            f'`extra="ignore"` accepts it and does nothing -- no error, no warning.\n'
            f'`upload_to_pypi = false` is why these are asserted absent rather than left alone: it '
            f'reads like "this project does not publish to PyPI", and the publish happens anyway '
            f'from the `pypa/gh-action-pypi-publish` step in {RELEASE_NAME}. A reader who trusts '
            f'it is wrong about the single most consequential thing this config does. See the '
            f'same shape in CI-093 and CI-097.\n'
            f'Live equivalents: `branches` (the table below) and '
            f'`[tool.semantic_release.publish].upload_to_vcs_release` (already defaults to true).'
        )

    def test_these_guards_can_still_see_the_original_bugs(self) -> None:
        """Positive control: prove the assertions above go red on the config CI-115 replaced.

        Without this they assert one thing -- "fine" -- which is also what they would report if
        ``build_command_steps`` quietly started returning ``[]``, or if every lookup began
        resolving to None on both sides of a comparison. The pre-CI-115 config is reconstructed
        and pushed through the same helpers, so a guard that cannot detect the bug it was written
        for fails here instead of passing forever (CI-072).
        """
        # 1. The original one-liner: uv used, never installed.
        original = build_command_steps('uv build')
        assert first_step_starting_with(original, 'uv') == 0, 'the control cannot even see `uv build` as a uv call'
        assert first_step_installing_extra(original, BUILD_EXTRA) is None, (
            f'first_step_installing_extra claims `uv build` installs the `{BUILD_EXTRA}` extra, so '
            f'test_uv_is_installed_before_anything_invokes_it would have passed on the exact '
            f'config that broke the release.'
        )
        # 2. The vendor's own recipe, verbatim -- correct for v10, fatal under the v9 pinned here.
        vendor = 'uv lock --upgrade-package "$PACKAGE_NAME"'
        assert env_references(vendor) == {'PACKAGE_NAME'}, (
            f'env_references read {env_references(vendor)} from {vendor!r}. It is what decides '
            f'whether build_command depends on a variable its PSR major never sets, so a version '
            f'that cannot see this reference protects nothing.'
        )
        assert 'PACKAGE_NAME' not in PSR_BUILD_COMMAND_ENV[9] and 'PACKAGE_NAME' in PSR_BUILD_COMMAND_ENV[10], (
            'PSR_BUILD_COMMAND_ENV no longer records that PACKAGE_NAME is a v10 addition, which '
            'is the entire distinction the variable check exists to draw.'
        )
        assert flag_argument(build_command_steps(vendor), '--upgrade-package') == '$PACKAGE_NAME', (
            "flag_argument does not read the vendor recipe's package argument, so "
            'test_the_relocked_package_is_this_project would pass on a command that re-locks '
            'nothing.'
        )
        # 3. A restored dead key must still be visible. Asserted through a parsed table, because
        #    the real check is `key not in table` and a parser that dropped unknown keys would
        #    make every one of those assertions vacuously true.
        restored = tomllib.loads('[tool.semantic_release]\nupload_to_pypi = false\n')
        assert 'upload_to_pypi' in restored['tool']['semantic_release'], (
            'a table that plainly contains `upload_to_pypi` does not read as containing it, so '
            'test_no_dead_semantic_release_key_returns is theatre.'
        )
