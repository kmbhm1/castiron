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

The file has since grown a second family, on the same principle but a sharper edge: the **release
path** (CI-115, CI-121, CI-122, CI-123). Nothing there can be checked by running it -- a release run
publishes to PyPI -- and each of those rows is a live failure that surfaced only mid-release, after
the version bump and the tag push had already happened. These assertions are the only automated
check that path has.

CI-123 adds the sharpest case of that: a ``rehearse`` input that makes the same workflow either cut
a real release or upload to TestPyPI. **Which mode a step belongs to is invisible at rest and
unrunnable in a test** -- a rehearsal that started python-semantic-release would recreate the exact
half-released state (bumped version, pushed tag, created Release) that two sessions were spent
unwinding, and a real release whose publish step named the test index would report success while
publishing nothing anyone can install. So the mode separation is asserted structurally, from a
step-level parse of the workflow.

A third family covers the **local dev tooling** (CI-107, CI-108), and it shares the release
family's defining property rather than the first family's: both rows were checks that had quietly
stopped meaning anything.

* **CI-107** -- ``uv run vulture src/``, named in ``CLAUDE.md`` as the project's dead-code check
  and shipped as ``make vulture``, could not pass. It exited 3 (vulture's ``DeadCode``: it ran
  correctly and found something) with 22 findings, all at 60% confidence and all false positives,
  against no ``[tool.vulture]`` config and no whitelist anywhere in the tree. A check that is
  always red is indistinguishable from a check that is right, so the first true finding would have
  read as more of the same. ``TestTheVultureAllowlistIsExactlyWhatSrcNeeds`` asserts the allowlist
  covers exactly today's findings -- neither fewer (``make vulture`` goes red) nor more (a stale
  entry mutes a name project-wide, forever, invisibly).

* **CI-108** -- the lint hook was declared as ``ruff``, which upstream retains only as a deprecated
  alias of ``ruff-check``. Nothing was broken; both ids run ``ruff check --force-exclude`` at
  ``v0.16.0``. It is asserted because of *when* it would break: on the alias's removal, at
  pre-push, on whichever PR next moves ``rev`` -- the same deferred, misattributed failure as
  CI-105, in the same stanza.
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
SRC_DIR = REPO_ROOT / 'src'

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

#: The linting hook's current id, and the deprecated spelling it replaced (CI-108). Upstream's
#: ``.pre-commit-hooks.yaml`` at ``v0.16.0`` declares ``ruff-check``, ``ruff-format``, and then
#: ``ruff`` under a literal ``# Legacy alias`` heading. All three resolve to
#: ``ruff check --force-exclude`` today, so this is asserted for durability rather than behaviour:
#: an id upstream itself calls legacy is a removal waiting to happen, and its removal would land as
#: a bare "hook id not found" on whichever unrelated PR next moves ``rev``.
RUFF_LINT_HOOK_ID = 'ruff-check'
RUFF_LEGACY_HOOK_ID = 'ruff'
RUFF_FORMAT_HOOK_ID = 'ruff-format'

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

#: The one name on ``PSR_WHITELISTED_ENV`` that a step on the RUNNER can set, which is what makes
#: it the whole attack surface between the two halves of the release job (CI-121). ``UV_PYTHON``
#: and ``UV_CACHE_DIR`` are exported by the same action and are harmless precisely because they are
#: **not** whitelisted -- they never reach ``build_command``.
POISONED_ENV = 'VIRTUAL_ENV'

#: The action that set it. ``astral-sh/setup-uv`` does more than put uv on ``PATH``: given a
#: ``python-version`` input its ``setupPython()`` runs ``uv venv --python <ver>`` -- creating the
#: workspace ``.venv`` **itself**, before any ``uv sync`` -- and then calls
#: ``core.exportVariable('VIRTUAL_ENV', path.resolve('.venv'))``. ``exportVariable`` writes to
#: ``$GITHUB_ENV``, so the value is JOB-scoped and inherits into the ``python-semantic-release``
#: **container** action, with the path translated to ``/github/workspace/.venv``. That venv's
#: interpreter symlink resolves only on the runner, so uv sees a broken *active* environment and
#: ``uv build`` fails hard. Read from the shipped ``dist/setup/index.js`` of ``@v5``, not the docs.
SETUP_UV_ACTION = 'astral-sh/setup-uv'

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

#: Commit types that must reach the changelog: the ones a *user* of the package can observe. This
#: is the captain's CI-122 ruling, pinned rather than derived, because it is a product decision --
#: if it changes, that should be a conversation, not a silently drifting release body.
USER_FACING_COMMIT_TYPES = frozenset({'feat', 'fix', 'perf'})

#: An ``exclude_commit_patterns`` entry that this guard can reason about: ``^`` plus a bare commit
#: type. PSR applies these with ``re.match`` over the WHOLE commit message
#: (``changelog/release_history.py:159`` of 9.21.2), so a prefix is all that is needed and ``^ci``
#: covers the scoped ``ci(release): ...`` spelling too -- verified end to end, not assumed.
ANCHORED_COMMIT_TYPE = re.compile(r'\^([a-z]+)')

#: The upload action. It appears **twice** in the release workflow now -- once for real PyPI and
#: once for TestPyPI -- which is why the CI-123 assertions go through ``workflow_steps`` rather
#: than ``uses_action`` / ``pinned_action_major``: both of those return on the FIRST match and
#: would silently answer about whichever step happens to come first.
PYPI_PUBLISH_ACTION = 'pypa/gh-action-pypi-publish'

#: The action that attaches ``dist/*`` to the GitHub Release. A real-release step, and one that
#: has never executed -- see CI-116 for why it is this action and not ``upload-to-gh-release``.
PSR_PUBLISH_ACTION = 'python-semantic-release/publish-action'

#: The ``workflow_dispatch`` input that selects rehearsal mode (CI-123).
REHEARSE_INPUT = 'rehearse'

#: The index a rehearsal uploads to. Spelled out rather than pattern-matched: "some URL that
#: mentions test.pypi.org" is not the assertion -- ``https://test.pypi.org/legacy/`` is the upload
#: endpoint, and a near-miss (no trailing slash, ``/simple/``, the web root) fails at upload time,
#: i.e. in the one mode where a failure is meant to be cheap but is still a wasted dispatch.
TEST_PYPI_URL = 'https://test.pypi.org/legacy/'

#: Both spellings of the publish action's index input, asserted **absent** on the real step.
#: Read from the action's own ``action.yml``: the canonical kebab-case ``repository-url`` carries
#: NO default, the deprecated ``repository_url`` alias defaults to
#: ``https://upload.pypi.org/legacy/``, and the composite passes
#: ``${{ inputs.repository-url || inputs.repository_url }}``. So **unset is real PyPI**, and any
#: value on the real step silently redirects a real release to another index.
REPOSITORY_URL_KEYS = ('with.repository-url', 'with.repository_url')

#: Both spellings of the input that would make a re-run of the rehearsal report success without
#: uploading anything. Asserted absent by captain ruling (CI-123-Q2): TestPyPI is permanent per
#: ``(name, version)``, so ``cast-iron 0.0.0`` can be uploaded exactly once, and a second run must
#: fail loudly on "File already exists" rather than produce a green that means nothing.
SKIP_EXISTING_KEYS = ('with.skip-existing', 'with.skip_existing')

#: The spelling of the input context that must never appear in a condition. For a
#: ``workflow_dispatch`` boolean, ``inputs.rehearse`` is a real boolean while
#: ``github.event.inputs.rehearse`` is the STRING ``'false'`` when unchecked -- and GitHub's falsy
#: set is ``false, 0, -0, "", '', null``, which does not contain ``'false'``. So the string
#: spelling is always truthy and ``== true`` against it is always false: both directions silently
#: invert a mode.
STRING_FLAVOURED_INPUTS = 'github.event.inputs'

#: The file whose name IS the CI-121 mechanism. ``$GITHUB_ENV`` is job-scoped, so anything written
#: to it inherits into every later step, including the container action -- which is how
#: ``VIRTUAL_ENV`` reached ``build_command``. ``setup-uv`` was one producer, not the category, so
#: the assertion is about the channel rather than about that action.
JOB_SCOPED_ENV_FILE = 'GITHUB_ENV'

#: The number of steps the release job has: app token, checkout, PSR, publish to PyPI, upload to
#: the GitHub Release, rehearsal build, rehearsal publish. Asserted so that a parse which silently
#: returns fewer (or none) fails HERE rather than making every loop below pass vacuously (CI-083).
RELEASE_STEP_COUNT = 7

#: The keys ``workflow_steps`` recognises at the top level of a step. A ``- <key>:`` line whose key
#: is one of these STARTS a step; a nested line whose key is not one of these (an ``env:`` child,
#: say) is ignored rather than promoted, so a step's shape cannot be widened by accident.
STEP_KEYS = frozenset({'name', 'id', 'if', 'uses', 'run', 'with', 'env', 'shell', 'continue-on-error'})

#: Every YAML block-scalar header. A ``run: |`` body must be consumed as a block and never re-read
#: as keys -- ``workflow_pytest_commands`` already does this, and the same technique applies here.
BLOCK_SCALAR_HEADERS = frozenset({'|', '>', '|-', '>-', '|+', '>+'})


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


def hosted_hook_ids(text: str, repo: str) -> list[str]:
    """Return the ``id:`` of every hook declared under one hosted pre-commit repo.

    Parsed textually, for the reason ``pinned_hook_rev`` gives. Skipping comment lines is once
    again load-bearing rather than tidy, and this time the trap is *in the stanza being read*: the
    ruff block carries a comment explaining that ``ruff-check`` replaced the legacy ``ruff``, so a
    parser that matched any line mentioning an id would report the very alias the test forbids --
    and ``test_the_lint_hook_uses_the_current_id_not_the_legacy_alias`` would fail on prose.

    Args:
        text: The whole ``.pre-commit-config.yaml``.
        repo: The repo URL, exactly as it appears after ``- repo:``.

    Returns:
        The hook ids in declaration order. Empty when the repo has no stanza.
    """
    ids = []
    inside = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('- repo:'):
            inside = stripped.partition('- repo:')[2].strip() == repo
            continue
        if inside and stripped.startswith('- id:'):
            ids.append(stripped.partition('- id:')[2].strip().strip('\'"'))
    return ids


def vulture_findings(paths: list[str], ignore_names: list[str], min_confidence: int) -> list[tuple[str, str]]:
    """Return the ``(name, location)`` pairs vulture reports as unused, using vulture's own scanner.

    The real check is re-run rather than approximated. A textual stand-in -- "does each allowlisted
    name still appear in ``src/``?" -- would pass on a symbol that had been deleted but was still
    named in a docstring, and three of the entries are named in ``ir/build.py`` docstrings, so the
    cheap version would have a known blind spot on the exact rot it exists to catch.

    ``vulture`` is imported inside the function so a missing dev dependency degrades to one failed
    test rather than a collection error for the whole module. It is a *direct* ``[dependency-groups]
    dev`` entry, so this is not the hidden transitive bet ``workflow_pytest_commands`` avoids.

    Args:
        paths: Paths to scan, as ``vulture`` would receive them on the command line.
        ignore_names: Names to suppress, i.e. ``[tool.vulture] ignore_names``.
        min_confidence: The confidence floor, i.e. ``[tool.vulture] min_confidence``.

    Returns:
        ``(name, location)`` pairs, de-duplicated and sorted. ``location`` is repo-relative where
        the finding lies inside the repository, and absolute otherwise (a ``tmp_path`` fixture).
        Three names are flagged at two sites each, so pairs are returned rather than names alone --
        it lets a caller check *where* a suppression applies, not just that one exists.
    """
    from vulture import Vulture

    scanner = Vulture(verbose=False, ignore_names=ignore_names, ignore_decorators=[])
    scanner.scavenge(paths)
    findings = set()
    for item in scanner.get_unused_code(min_confidence=min_confidence):
        path = Path(item.filename)
        location = path.relative_to(REPO_ROOT).as_posix() if path.is_relative_to(REPO_ROOT) else path.as_posix()
        findings.add((item.name, location))
    return sorted(findings)


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


def first_step_unsetting(steps: list[str], variable: str) -> int | None:
    """Return the index of the first step that clears one environment variable.

    Token-level, like every other reader in this module: a substring test for ``VIRTUAL_ENV``
    matches the name inside a comment, inside an ``export``, and inside the prose of a neighbouring
    command, none of which clear anything.

    Args:
        steps: The steps from ``build_command_steps``.
        variable: The variable name, e.g. ``'VIRTUAL_ENV'``.

    Returns:
        The index, or None if no step unsets it.
    """
    for index, step in enumerate(steps):
        tokens = command_tokens(step)
        if tokens and tokens[0] == 'unset' and variable in tokens[1:]:
            return index
    return None


def uses_action(text: str, action: str) -> bool:
    """Report whether a workflow *invokes* one action, as opposed to merely naming it.

    Distinct from ``pinned_action_major``, which answers None both for "absent" and for "pinned in
    some other way" -- a distinction that does not matter when reading a pin and matters entirely
    when asserting an absence, where the second case would pass vacuously.

    Comment lines are skipped, and that is load-bearing rather than tidy here: the release workflow
    carries a comment block explaining why this very action was removed, which names it and quotes
    its ``python-version`` input. A parser that matched the first line *containing* the action would
    read that warning as the thing it warns about.

    Args:
        text: The whole workflow file.
        action: The ``owner/repo`` of the action, without a ref.

    Returns:
        True if some non-comment line invokes it at any ref.
    """
    pattern = re.compile(rf'^uses:\s*{re.escape(action)}(@|\s*$)')
    for raw_line in text.splitlines():
        stripped = raw_line.strip().lstrip('-').strip()
        if raw_line.strip().startswith('#'):
            continue
        if pattern.search(stripped):
            return True
    return False


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


def non_comment_lines(text: str) -> list[str]:
    """Return the lines of a document that are neither blank nor a whole-line comment.

    Load-bearing rather than tidy, for the reason ``uses_action`` records: ``release.yml``
    documents its own failure history in prose, and after CI-123 that prose quotes
    ``github.event.inputs``, ``$GITHUB_ENV`` and ``astral-sh/setup-uv`` -- the exact strings the
    guards forbid. A reader that saw comments would fail permanently on the documentation
    explaining why it exists, and the only way to green it would be to delete the explanation.

    Args:
        text: The whole file.

    Returns:
        The remaining lines, with their original indentation.
    """
    return [line for line in text.splitlines() if line.strip() and not line.strip().startswith('#')]


def scalar_value(value: str) -> str:
    """Return one YAML scalar with its surrounding quotes removed.

    Args:
        value: Everything after the first ``:`` on a line.

    Returns:
        The stripped value, unquoted only when it both opens and closes with the same quote --
        so a value that merely *contains* an apostrophe is left alone.
    """
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in '\'"':
        return stripped[1:-1]
    return stripped


def workflow_steps(text: str) -> list[dict[str, str]]:
    """Return one flat dict per step of a workflow job, in file order.

    Keys are the step's own (``name``, ``id``, ``uses``, ``if``, ``run``) plus every ``with:``
    child flattened to ``with.<key>``. Flattening keeps the return type a plain ``dict[str, str]``
    -- no nested unions, no ``Any`` -- which is what lets the assertions below read a nested input
    with a single lookup.

    Parsed textually rather than with PyYAML, for the reason recorded in
    ``workflow_pytest_commands``: pyyaml reaches this repo only *transitively* (through
    pre-commit), and a test that silently depends on someone else's requirement is one
    ``uv sync`` away from an unexplained collection error.

    Comments are skipped (see ``non_comment_lines``), block scalars are consumed as a block and
    never re-read as keys, and a nested key that is not a step key is ignored rather than
    promoted.

    Args:
        text: The whole workflow file.

    Returns:
        The steps, in file order. Empty for a document with no steps -- callers assert a count
        first, because an empty list makes every loop below pass vacuously (CI-083).
    """
    steps: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    with_indent: int | None = None
    block_key: str | None = None
    block_indent = 0
    block_body: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if block_key is not None and current is not None:
            if not stripped:
                continue
            if indent > block_indent:
                block_body.append(stripped)
                continue
            current[block_key] = '\n'.join(block_body)  # the block ended; re-read this line below
            block_key, block_body = None, []
        if not stripped or stripped.startswith('#'):
            continue
        item = stripped.startswith('- ')
        key, separator, value = (stripped[2:].strip() if item else stripped).partition(':')
        if not separator:
            continue
        key, value = key.strip(), scalar_value(value)
        if item and key in STEP_KEYS:
            current = {}
            steps.append(current)
            with_indent = None
        if current is None:
            continue
        if with_indent is not None and indent <= with_indent:
            with_indent = None
        if with_indent is not None:
            key = f'with.{key}'
        elif key not in STEP_KEYS:
            continue
        if value in BLOCK_SCALAR_HEADERS:
            block_key, block_indent, block_body = key, indent, []
            continue
        if key == 'with' and not value:
            with_indent = indent
            continue
        current[key] = value
    if block_key is not None and current is not None:
        current[block_key] = '\n'.join(block_body)
    return steps


def yaml_block(text: str, *path: str) -> list[str]:
    """Return the raw lines nested under one mapping key path.

    Textual for the same reason as everything else here. Each key narrows the search to the block
    the previous one opened, so a key name that also occurs elsewhere in the document cannot be
    picked up from outside its parent.

    Args:
        text: The whole document.
        *path: The key path, e.g. ``'on', 'workflow_dispatch', 'inputs'``.

    Returns:
        The lines strictly more indented than the last key, with their indentation intact. Empty
        when any key along the path is missing.
    """
    lines = non_comment_lines(text)
    for key in path:
        index = next((position for position, line in enumerate(lines) if line.strip().startswith(f'{key}:')), None)
        if index is None:
            return []
        indent = len(lines[index]) - len(lines[index].lstrip())
        block = []
        for line in lines[index + 1 :]:
            if len(line) - len(line.lstrip()) <= indent:
                break
            block.append(line)
        lines = block
    return lines


def workflow_dispatch_input(text: str, name: str) -> dict[str, str]:
    """Return the declaration of one ``workflow_dispatch`` input.

    Args:
        text: The whole workflow file.
        name: The input's name, e.g. ``'rehearse'``.

    Returns:
        Its fields (``description``, ``type``, ``default``), unquoted. Empty when the workflow
        declares no such input -- which the caller asserts against, because an empty dict would
        otherwise make a ``.get('default') != 'true'`` check pass on an input that is gone.
    """
    fields: dict[str, str] = {}
    for line in yaml_block(text, 'on', 'workflow_dispatch', 'inputs', name):
        key, separator, value = line.strip().partition(':')
        if separator:
            fields[key.strip()] = scalar_value(value)
    return fields


def normalized_condition(step: dict[str, str]) -> str:
    """Return a step's ``if`` free of its expression wrapper and of all whitespace.

    ⚠ After normalization ``!inputs.rehearse`` **contains** ``inputs.rehearse``, so a
    rehearsal-only predicate has to exclude the negation explicitly. That substring relation is
    the single most likely way this guard becomes theatre, which is why
    ``excludes_a_real_release`` states it and the positive control exercises it.

    Args:
        step: One step from ``workflow_steps``.

    Returns:
        The condition, e.g. ``"!inputs.rehearse&&steps.release.outputs.released=='true'"``. The
        empty string for a step with no condition -- which fails every mode assertion, as it
        should: an ungated step runs in both modes.
    """
    return ''.join(step.get('if', '').replace('${{', '').replace('}}', '').split())


def steps_using(steps: list[dict[str, str]], action: str) -> list[dict[str, str]]:
    """Return every step that invokes one action, at any ref.

    Identified by ``uses`` rather than by ``name``, so renaming a step cannot silently empty the
    set a guard iterates. The ``@`` is required, so ``python-semantic-release/publish-action`` and
    ``python-semantic-release/python-semantic-release`` cannot be confused for one another.

    Args:
        steps: The steps from ``workflow_steps``.
        action: The ``owner/repo`` of the action, without a ref.

    Returns:
        The matching steps, in file order.
    """
    return [step for step in steps if step.get('uses', '').startswith(f'{action}@')]


def steps_running_a_command(steps: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return every step that runs a shell command rather than invoking an action.

    Args:
        steps: The steps from ``workflow_steps``.

    Returns:
        The steps carrying a ``run``, in file order.
    """
    return [step for step in steps if 'run' in step]


def repository_url(step: dict[str, str]) -> str | None:
    """Return the index a publish step names, reading either spelling of the input.

    Args:
        step: One step from ``workflow_steps``.

    Returns:
        The URL, or None when the step names no index at all -- which, for
        ``pypa/gh-action-pypi-publish``, **is** real PyPI (see ``REPOSITORY_URL_KEYS``).
    """
    for key in REPOSITORY_URL_KEYS:
        if key in step:
            return step[key]
    return None


def excludes_a_rehearsal(step: dict[str, str]) -> bool:
    """Report whether a step is unreachable in rehearsal mode.

    Args:
        step: One step from ``workflow_steps``.

    Returns:
        True if its condition carries the negated input.
    """
    return f'!inputs.{REHEARSE_INPUT}' in normalized_condition(step)


def excludes_a_real_release(step: dict[str, str]) -> bool:
    """Report whether a step is unreachable on a real release.

    The negation is excluded explicitly: after ``normalized_condition`` the string
    ``!inputs.rehearse`` contains ``inputs.rehearse``, so a bare membership test would accept the
    *real-only* condition as rehearsal-only and both mode assertions would become one tautology.

    Args:
        step: One step from ``workflow_steps``.

    Returns:
        True if its condition requires the input and does not negate it.
    """
    condition = normalized_condition(step)
    return f'inputs.{REHEARSE_INPUT}' in condition and f'!inputs.{REHEARSE_INPUT}' not in condition


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


def pyproject_string_list(table: dict[str, object], key: str) -> list[str]:
    """Return one TOML array-of-strings from an already-loaded table.

    Args:
        table: The table to read, as returned by :func:`pyproject_table`.
        key: The key holding the array.

    Returns:
        The strings in that array. Empty when the key is missing or is not an array of strings, so
        the assertions below fail on the contents rather than on a ``TypeError``.
    """
    value = table.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def excluded_commit_types(patterns: list[str]) -> set[str]:
    """Return the commit types a list of ``exclude_commit_patterns`` actually suppresses.

    Args:
        patterns: The raw pattern strings from ``[tool.semantic_release.changelog]``.

    Returns:
        One type per pattern of the form ``^<type>``. A pattern of any other shape contributes
        nothing -- which is the point: the caller compares this against the parser's own
        ``allowed_tags`` and so notices a pattern that suppresses nothing.
    """
    types = set()
    for pattern in patterns:
        match = ANCHORED_COMMIT_TYPE.fullmatch(pattern)
        if match:
            types.add(match.group(1))
    return types


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


def release_workflow_text() -> str:
    """Return the release workflow, whole.

    Returns:
        The contents of ``.github/workflows/release.yml``.
    """
    return RELEASE_WORKFLOW.read_text(encoding='utf-8')


def release_steps() -> list[dict[str, str]]:
    """Return the parsed steps of the release job.

    Returns:
        The steps, in file order, as ``workflow_steps`` reads them.
    """
    return workflow_steps(release_workflow_text())


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


@pytest.mark.unit
class TestNothingLeaksARunnerVirtualEnvIntoTheReleaseContainer:
    """CI-121 -- the release job runs in two places, and one of them poisoned the other.

    The ``0.1.0`` release run died in ``build_command`` at ``uv build``::

        error: Failed to build `/github/workspace`
          Caused by: Failed to inspect Python interpreter from active virtual environment at
                     `.venv/bin/python3`
          Caused by: Broken symlink at `.venv/bin/python3`, was the underlying Python interpreter
                     removed?

    The chain, reproduced end to end in the PSR action's own base image (``python:3.13-bookworm``)
    rather than argued:

    1. ``astral-sh/setup-uv@v5`` with a ``python-version`` input runs ``uv venv`` -- so the
       workspace ``.venv`` is created by *that* action, not by the ``uv sync`` step that followed
       it -- and exports ``VIRTUAL_ENV`` through ``$GITHUB_ENV``, making it job-scoped.
    2. The next step is a Docker container action. The runner translates the value to
       ``/github/workspace/.venv``, and the mounted venv's interpreter symlink points into the
       runner's toolcache, which does not exist inside the image.
    3. PSR passes ``VIRTUAL_ENV`` straight through to ``build_command`` (see
       ``PSR_WHITELISTED_ENV``), and uv treats a broken **active** environment as fatal.

    ⚠ The measurement that matters is the negative one, because the obvious diagnosis is wrong: a
    broken ``.venv`` **alone does not fail**. Same workspace, same image, same command --

        ``VIRTUAL_ENV`` set   -> exit 2, zero distributions
        ``VIRTUAL_ENV`` unset -> exit 0, two distributions

    -- and the ``warning: Ignoring existing virtual environment ...`` line appears in *both*, so
    the warning in the failure log is not the cause it looks like. The variable is the mechanism;
    the directory is a bystander. Both ends of that are asserted here, because the fix has two
    halves that protect different things: removing the producer keeps the runner clean, and
    ``unset VIRTUAL_ENV`` keeps the build correct if a producer ever comes back.

    ⚠ Like the class above, none of this is reachable by running it -- a release run publishes to
    PyPI and claims a name. These assertions and that container harness are the only checks the
    release path has.
    """

    def test_the_build_command_clears_the_variable_before_it_invokes_uv(self) -> None:
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        cleared = first_step_unsetting(steps, POISONED_ENV)
        uses = first_step_starting_with(steps, 'uv')
        # Anti-vacuity (CI-083): a build_command that never invokes uv would pass an ordering
        # comparison between two Nones while asserting nothing at all.
        assert uses is not None, (
            f'{PYPROJECT_NAME}: `build_command` never invokes `uv`, so this guard is comparing '
            f'nothing. If the build genuinely moved off uv, delete this class with it.'
        )
        assert cleared is not None, (
            f'{PYPROJECT_NAME}: `build_command` invokes uv without clearing `{POISONED_ENV}`:\n  '
            + '\n  '.join(steps)
            + f'\nPSR hands this command a fixed WHITELIST rather than os.environ, and '
            f'`{POISONED_ENV}` is the one name on it that a step on the runner can set. When it '
            f'points at a venv built on the runner, uv inside the container sees a broken ACTIVE '
            f'environment and `uv build` fails hard -- which is exactly how the 0.1.0 release '
            f'aborted, after the version had already been computed. Add `unset {POISONED_ENV}`.'
        )
        assert cleared < uses, (
            f'{PYPROJECT_NAME}: `build_command` clears `{POISONED_ENV}` at step {cleared} but '
            f'already invoked uv at step {uses}:\n  '
            + '\n  '.join(steps)
            + '\nToo late; the failing call is the earlier one.'
        )

    def test_clearing_it_never_displaces_the_error_trap(self) -> None:
        # `set -e` must stay first no matter what gets prepended for CI-121's sake. Asserted here
        # as well as in the class above because THIS is the change that made the first line movable
        # -- and PSR runs the whole block as one `sh -c`, so a lost `set -e` means a failed re-lock
        # exits 0 and publishes a stale uv.lock silently.
        steps = build_command_steps(str(pyproject_table('tool', 'semantic_release').get('build_command', '')))
        cleared = first_step_unsetting(steps, POISONED_ENV)
        assert steps and steps[0] == 'set -e' and cleared != 0, (
            f'{PYPROJECT_NAME}: `build_command` begins with {steps[0] if steps else "nothing"!r}. '
            f'`unset {POISONED_ENV}` belongs immediately AFTER `set -e`, never before it.'
        )

    def test_the_release_workflow_puts_no_uv_on_the_runner(self) -> None:
        assert not uses_action(RELEASE_WORKFLOW.read_text(encoding='utf-8'), SETUP_UV_ACTION), (
            f'{RELEASE_NAME} uses {SETUP_UV_ACTION} again. With a `python-version` input that '
            f'action creates the workspace `.venv` AND exports `{POISONED_ENV}` job-wide, which is '
            f'what aborted the 0.1.0 release (CI-121).\n'
            f'Nothing in this job consumes it: the job has no `run:` steps, PSR runs in its own '
            f'container and `build_command` installs its own dependencies from the `build` extra, '
            f'`create-github-app-token` is node20, and `gh-action-pypi-publish` discovers a Python '
            f'on the runner and installs one itself if it finds none.\n'
            f'If some future step genuinely needs uv on the runner, it must not carry '
            f'`python-version` -- and change this guard deliberately, with a measurement, not to '
            f'make a red test go away.'
        )

    def test_psr_really_does_forward_the_variable_this_class_is_about(self) -> None:
        # The two assertions above are only worth having because PSR passes this name through. If
        # it ever stops, they are guarding a channel that no longer exists -- and the whitelist is
        # recorded in this file precisely so that is checkable rather than remembered.
        assert POISONED_ENV in PSR_WHITELISTED_ENV, (
            f'PSR_WHITELISTED_ENV no longer lists `{POISONED_ENV}`, so `build_command` cannot '
            f'inherit it and this whole class is moot. Re-read `build_distributions()` in the '
            f'pinned PSR before deleting it -- or before trusting that it still matters.'
        )

    def test_these_guards_can_still_see_the_original_bug(self) -> None:
        """Positive control: prove the assertions above go red on the config CI-121 replaced.

        Each asserts an absence or an ordering, which is the shape that passes loudly forever once
        the reader underneath it quietly stops reading. So the pre-CI-121 ``build_command`` and the
        pre-CI-121 workflow step are both pushed back through the same helpers (CI-072).
        """
        # 1. The build_command as it stood when the release failed: uv invoked, nothing cleared.
        original = build_command_steps(
            "set -e\npython -m pip install -e '.[build]'\nuv lock --upgrade-package cast-iron\n"
            'git add uv.lock\nuv build\n'
        )
        assert first_step_starting_with(original, 'uv') is not None, (
            'the control cannot even see `uv lock` as a uv call, so the ordering assertion it controls proves nothing.'
        )
        assert first_step_unsetting(original, POISONED_ENV) is None, (
            f'first_step_unsetting claims the pre-CI-121 build_command cleared `{POISONED_ENV}`. '
            f'It did not -- that is why the release failed -- so '
            f'test_the_build_command_clears_the_variable_before_it_invokes_uv would have passed on '
            f'the exact config that broke the release.'
        )
        # A near-miss that must NOT count as clearing: naming the variable is not unsetting it.
        assert first_step_unsetting(build_command_steps(f'export {POISONED_ENV}=/tmp/v'), POISONED_ENV) is None
        assert first_step_unsetting(build_command_steps(f'unset {POISONED_ENV}'), POISONED_ENV) == 0

        # 2. The removed workflow step must still be visible to the absence check.
        restored = '\n'.join(
            [
                '      - name: Install uv',
                f'        uses: {SETUP_UV_ACTION}@v5',
                '        with:',
                "          python-version: '3.12'",
            ]
        )
        assert uses_action(restored, SETUP_UV_ACTION), (
            f'uses_action cannot see a plainly restored `uses: {SETUP_UV_ACTION}@v5` step, so '
            f'test_the_release_workflow_puts_no_uv_on_the_runner would stay green with the trap '
            f'back in place -- theatre (CI-072).'
        )
        # 3. ...and the comment block that now explains the removal must NOT be read as a use.
        #    This is the real file's shape, not a hypothetical: release.yml names the action, its
        #    `python-version` input and the error it caused, in prose, right where it used to sit.
        commented_out = '\n'.join(
            [
                f'      # `{SETUP_UV_ACTION}@v5` with `python-version` created the .venv AND',
                f'      # exported {POISONED_ENV}. Do not restore:',
                f'      #   uses: {SETUP_UV_ACTION}@v5',
            ]
        )
        assert not uses_action(commented_out, SETUP_UV_ACTION), (
            f'uses_action reads a COMMENT naming {SETUP_UV_ACTION} as an invocation. '
            f'{RELEASE_NAME} carries exactly that comment, so the guard would fail permanently on '
            f'the very documentation that explains why it exists -- and the only way to make it '
            f'green again would be to delete the explanation.'
        )


@pytest.mark.unit
class TestTheChangelogExclusionsSuppressTypesTheParserKnows:
    """CI-122 -- the release body has a size limit, and the config that respects it can go inert.

    GitHub caps a Release body at **125,000 characters**. Undocumented in the REST reference;
    established by POSTing the oversized body as a *draft*, which answered
    ``{"field":"body","message":"body is too long (maximum is 125000 characters)"}`` and created
    nothing. The 0.1.0 run hit it at ``github.create_release`` -- *after* bumping the version,
    writing the changelog, committing and pushing the tag ``v0.1.0``, leaving a half-released repo
    to clean up by hand.

    It is a **first-release** problem. With no prior tag the whole history renders under one
    ``## v0.1.0`` heading; every later release covers only the commits since the previous tag.
    Measured against ``1197a10`` under PSR 9.21.2 -- the version ``@v9`` resolves to -- capturing
    the literal ``release_notes`` argument passed to ``Github.create_release``: **169,012 chars**
    with nothing excluded (over by 44,012, reproducing the 422) against **87,592** with the
    patterns below (29.9% under). Both compute ``0.1.0``: the exclusions are presentation-only.

    ⚠ **What is asserted here is not the size.** It cannot be: rendering the notes needs PSR 9.21.2,
    while ``uv.lock`` resolves **10.6.1**, whose ``mask_initial_release`` defaults to ``True`` and
    would render ``- Initial Release`` -- ~56 characters, passing vacuously and proving nothing
    about the v9 the action runs. Fetching 9.21.2 instead would put the network in a suite
    ``TestTheOfflineSuiteIsOfflineByConstruction`` exists to keep offline, and walking the git
    history would pass vacuously in CI anyway, where ``actions/checkout@v4`` carries no
    ``fetch-depth`` and clones one commit deep.

    So what is asserted is the failure mode a reader cannot see: that each pattern names a type the
    **parser** knows. ``^chores`` or ``^doc`` matches nothing, suppresses nothing, and looks
    entirely correct -- the CI-093 / CI-097 shape, config that reads load-bearing while doing
    nothing, with a 422 at the end of it.
    """

    def changelog_patterns(self) -> list[str]:
        """Return the configured ``exclude_commit_patterns``.

        Returns:
            The raw pattern strings, in file order.
        """
        return pyproject_string_list(
            pyproject_table('tool', 'semantic_release', 'changelog'), 'exclude_commit_patterns'
        )

    def test_the_non_user_facing_types_are_excluded_from_the_changelog(self) -> None:
        patterns = self.changelog_patterns()
        assert patterns, (
            f'{PYPROJECT_NAME}: `[tool.semantic_release.changelog].exclude_commit_patterns` is '
            f'empty or absent, so the FIRST release renders every commit in the history into one '
            f'GitHub Release body. Measured on this repo that is 169,012 characters against a '
            f'125,000 cap -- a 422 from `github.create_release`, raised only after the version '
            f'bump, the changelog, the commit and the tag push have already happened.'
        )

    def test_every_pattern_names_a_type_the_commit_parser_knows(self) -> None:
        """The load-bearing one: a pattern that matches nothing suppresses nothing, and looks fine.

        ``exclude_commit_patterns`` is regex, so PSR accepts ``^chores`` without complaint and
        silently changes nothing -- the config would read as a fix while the next release body
        stayed exactly as oversized as the one that 422'd.
        """
        parser_options = pyproject_table('tool', 'semantic_release', 'commit_parser_options')
        allowed = set(pyproject_string_list(parser_options, 'allowed_tags'))
        assert allowed, (
            f'{PYPROJECT_NAME}: `[tool.semantic_release.commit_parser_options].allowed_tags` is '
            f'empty or absent, so this test has nothing to compare the exclusions against and the '
            f'assertion below would pass on any pattern at all.'
        )
        for pattern in self.changelog_patterns():
            match = ANCHORED_COMMIT_TYPE.fullmatch(pattern)
            assert match is not None, (
                f'{PYPROJECT_NAME}: exclusion pattern `{pattern}` is not a plain `^<type>`. That '
                f'may well be deliberate, but this guard can no longer tell whether it suppresses '
                f'anything -- teach it the new shape rather than deleting the check.'
            )
            assert match.group(1) in allowed, (
                f'{PYPROJECT_NAME}: exclusion pattern `{pattern}` names `{match.group(1)}`, which '
                f'is not in `allowed_tags` {sorted(allowed)}. It therefore matches no commit and '
                f'suppresses nothing, while reading exactly like a pattern that works. The release '
                f'body stays oversized and the next release 422s.'
            )

    def test_no_excluded_type_is_one_that_cuts_a_release(self) -> None:
        """Excluding a bump driver would empty the changelog of the reason the release happened.

        PSR half-protects against this itself -- ``release_history.py:175`` skips an excluded commit
        only when its bump level is ``NO_RELEASE``, so a ``ci!:`` carrying a ``BREAKING CHANGE:``
        footer survives the filter (verified: it bumped to 1.0.0 and reappeared under its own
        heading). That carve-out covers the breaking case, not the ordinary one: ``^fix`` here would
        drop every non-breaking fix from a release those fixes caused.
        """
        parser_options = pyproject_table('tool', 'semantic_release', 'commit_parser_options')
        bump_drivers = set(pyproject_string_list(parser_options, 'minor_tags')) | set(
            pyproject_string_list(parser_options, 'patch_tags')
        )
        assert bump_drivers, (
            f'{PYPROJECT_NAME}: neither `minor_tags` nor `patch_tags` is set under '
            f'`commit_parser_options`, so this test compares the exclusions against an empty set '
            f'and cannot fail.'
        )
        overlap = excluded_commit_types(self.changelog_patterns()) & bump_drivers
        assert not overlap, (
            f'{PYPROJECT_NAME}: {sorted(overlap)} both cuts a release and is excluded from the '
            f'changelog, so the release notes would omit the commits that caused the release.'
        )

    def test_the_types_left_in_the_changelog_are_the_ones_that_were_ruled(self) -> None:
        """Pins the CI-122 ruling, so a new commit type forces a decision instead of drifting.

        ``mask_initial_release = true`` would also have fit (it collapses the body to
        ``- Initial Release``) and was rejected: Hard Rule #1 says the changelog *is* the commit
        history, so the feat/fix narrative stays and only the non-user-facing types go.
        """
        parser_options = pyproject_table('tool', 'semantic_release', 'commit_parser_options')
        allowed = set(pyproject_string_list(parser_options, 'allowed_tags'))
        included = allowed - excluded_commit_types(self.changelog_patterns())
        assert included == set(USER_FACING_COMMIT_TYPES), (
            f'{PYPROJECT_NAME}: the changelog would carry {sorted(included)}, not '
            f'{sorted(USER_FACING_COMMIT_TYPES)}. A type added to `allowed_tags` lands in the '
            f'release body by default and every commit of it enlarges a body that has already '
            f'overflowed once -- decide here whether it is user-facing, and update this test to '
            f'record the decision.'
        )

    def test_these_guards_can_still_see_the_original_bug(self) -> None:
        """Positive control (CI-072): every assertion above is about an absence or a membership.

        That is the shape that passes loudly forever once the reader underneath it stops reading,
        so the helpers get pushed the configurations that must be rejected.
        """
        # 1. The config as it stood when the release 422'd: no exclusions at all.
        assert not pyproject_string_list({}, 'exclude_commit_patterns'), (
            'pyproject_string_list invents patterns for a table that has none, so the emptiness '
            'assertion would have passed on the exact config that overflowed the release body.'
        )
        # 2. A typo'd pattern must not be read as suppressing anything -- `^chores` is valid regex
        #    and matches no commit in a conventional history.
        assert excluded_commit_types(['^chores', '^doc']) == {'chores', 'doc'}, (
            'excluded_commit_types silently normalises a typo to the type it resembles, so a '
            'pattern that suppresses nothing would compare equal to one that works.'
        )
        assert not excluded_commit_types(['^chores', '^doc']) & {'chore', 'docs'}
        # 3. Shapes the type-extractor must refuse rather than guess at, so
        #    test_every_pattern_names_a_type_the_commit_parser_knows fails loudly on them.
        assert excluded_commit_types(['chore', '.*', '^chore|^ci', '']) == set()
        # 4. ...and the real, working spelling must still be read, or every assertion above is
        #    comparing empty sets and cannot fail.
        assert excluded_commit_types(['^chore', '^ci']) == {'chore', 'ci'}


@pytest.mark.unit
class TestTheStepParserReadsStepsNotComments:
    """``workflow_steps`` is the load-bearing half of the CI-123 guard, so it is tested directly.

    Mirrors ``TestTheWorkflowParserReadsCommandsNotLabels``. Each case is a way the real
    ``release.yml`` could make a naive parser lie: it carries a comment block that names
    ``astral-sh/setup-uv@v5`` and quotes its ``python-version:`` input, and after CI-123 it also
    quotes ``github.event.inputs``, ``$GITHUB_ENV`` and a bare ``if: !inputs.rehearse`` in prose --
    every one of them a string some assertion below forbids. A parser that read comments would fail
    permanently on the documentation that explains why the guards exist.
    """

    def test_a_commented_out_step_is_not_a_step(self) -> None:
        document = '\n'.join(
            [
                '      # - name: Publish to TestPyPI',
                '      #   if: ${{ inputs.rehearse }}',
                '      #   uses: pypa/gh-action-pypi-publish@release/v1',
                '      #   with:',
                '      #     repository-url: https://test.pypi.org/legacy/',
            ]
        )
        assert workflow_steps(document) == []

    def test_a_block_scalar_run_is_captured_whole_and_the_next_step_still_parses(self) -> None:
        document = '\n'.join(
            [
                '      - name: Build',
                '        if: ${{ inputs.rehearse }}',
                '        run: |',
                '          pipx run --spec uv~=0.11.32 uv build',
                '          echo done',
                '      - name: After',
                '        uses: pypa/gh-action-pypi-publish@release/v1',
            ]
        )
        steps = workflow_steps(document)
        assert len(steps) == 2
        assert steps[0]['run'] == 'pipx run --spec uv~=0.11.32 uv build\necho done'
        assert steps[1] == {'name': 'After', 'uses': 'pypa/gh-action-pypi-publish@release/v1'}

    def test_with_children_are_namespaced_and_do_not_leak_into_the_next_step(self) -> None:
        document = '\n'.join(
            [
                '      - name: Publish to TestPyPI',
                '        uses: pypa/gh-action-pypi-publish@release/v1',
                '        with:',
                '          repository-url: https://test.pypi.org/legacy/',
                '          print-hash: true',
                '      - name: Publish to PyPI',
                '        uses: pypa/gh-action-pypi-publish@release/v1',
                '        with:',
                '          packages-dir: dist/',
            ]
        )
        steps = workflow_steps(document)
        # The URL keeps its own colon: the value is everything after the FIRST one.
        assert steps[0]['with.repository-url'] == 'https://test.pypi.org/legacy/'
        assert steps[0]['with.print-hash'] == 'true'
        assert repository_url(steps[1]) is None, (
            'a `with:` child leaked from one step into the next, so the real publish step would '
            "inherit the rehearsal step's index and the guard that keeps a real release off "
            'TestPyPI could never fail.'
        )

    def test_a_step_with_no_condition_normalizes_to_the_empty_string(self) -> None:
        # An ungated step runs in BOTH modes, so it must fail every mode assertion rather than
        # returning something a substring test would accidentally accept.
        step = workflow_steps('      - name: Ungated\n        uses: actions/checkout@v4\n')[0]
        assert normalized_condition(step) == ''
        assert not excludes_a_rehearsal(step)
        assert not excludes_a_real_release(step)

    def test_a_condition_is_normalized_free_of_its_expression_wrapper(self) -> None:
        step = workflow_steps(
            "      - name: Publish\n        if: ${{ !inputs.rehearse && steps.release.outputs.released == 'true' }}\n"
        )[0]
        assert normalized_condition(step) == "!inputs.rehearse&&steps.release.outputs.released=='true'"

    def test_an_input_declaration_is_read_and_a_commented_one_is_not(self) -> None:
        document = '\n'.join(
            [
                'on:',
                '  workflow_dispatch:',
                '    inputs:',
                '      # rehearse:',
                '      #   default: true',
                '      rehearse:',
                "        description: 'Rehearse against TestPyPI: no bump, no tag.'",
                '        type: boolean',
                '        default: false',
                'permissions:',
                '  contents: read',
            ]
        )
        declared = workflow_dispatch_input(document, REHEARSE_INPUT)
        assert declared['type'] == 'boolean'
        assert declared['default'] == 'false'
        # The description keeps its own colon, and `permissions:` is outside the block.
        assert declared['description'] == 'Rehearse against TestPyPI: no bump, no tag.'
        assert 'contents' not in declared

    def test_an_absent_input_yields_nothing(self) -> None:
        assert workflow_dispatch_input('on:\n  workflow_dispatch:\n', REHEARSE_INPUT) == {}

    def test_the_real_release_workflow_parses_to_the_steps_these_guards_iterate(self) -> None:
        """Anti-vacuity (CI-083) for every test in the next class -- they all iterate these sets.

        Asserted as exact counts rather than "at least one": a parse that returned an empty list,
        or one that split the comment blocks into phantom steps, would leave the mode assertions
        passing over the wrong number of things while reading as green.
        """
        steps = release_steps()
        assert len(steps) == RELEASE_STEP_COUNT, (
            f'{RELEASE_NAME} parses to {len(steps)} steps, not {RELEASE_STEP_COUNT}: '
            f'{[step.get("name") or step.get("uses") for step in steps]}.\n'
            f'Either a step was added or removed -- in which case decide which MODE it belongs to '
            f'and update RELEASE_STEP_COUNT deliberately -- or workflow_steps stopped reading the '
            f'file, which would make every assertion in '
            f'TestTheRehearsalCannotFireOnARealRelease pass over an empty set.'
        )
        assert len(steps_using(steps, PSR_ACTION)) == 1
        assert len(steps_using(steps, PSR_PUBLISH_ACTION)) == 1
        assert len(steps_using(steps, PYPI_PUBLISH_ACTION)) == 2, (
            f'{RELEASE_NAME} no longer has exactly two {PYPI_PUBLISH_ACTION} steps (real PyPI and '
            f'TestPyPI). That count is why these guards parse steps instead of using uses_action, '
            f'which returns on the first match.'
        )
        assert len(steps_running_a_command(steps)) == 1, (
            f'{RELEASE_NAME} has {len(steps_running_a_command(steps))} `run:` steps, not 1. The '
            f'rehearsal build is the only one -- and "the release job has no run: steps" was part '
            f'of the CI-121 reasoning, so a new one needs to be checked against it.'
        )


@pytest.mark.unit
class TestTheRehearsalCannotFireOnARealRelease:
    """CI-123 -- one workflow, two modes, and no way to run either one in a test.

    The rehearsal exists because two steps of this workflow have NEVER EXECUTED (``Publish to
    PyPI``, ``Upload artifacts to the GitHub Release``): both are gated on
    ``released == 'true'``, and both release runs died upstream of them -- CI-121 in
    ``build_command``, CI-122 at ``github.create_release``. A third failure at the publish step
    would land *after* the version bump, the tag and the Release already exist.

    So a ``rehearse`` input makes the same file either cut a real release or upload the in-tree
    ``0.0.0`` to TestPyPI. The two failure modes that creates are both silent:

    * a rehearsal that reaches python-semantic-release recreates the half-released state (bumped
      version, pushed tag, created Release) that two sessions were spent unwinding, and
    * a real release whose publish step names the test index reports success while publishing
      nothing anyone can install.

    Neither is visible at rest, and neither can be caught by running the workflow. Hence a
    structural guard, over a step-level parse. ⚠ The three ways it could quietly stop meaning
    anything -- an empty parse, the ``inputs.rehearse`` / ``!inputs.rehearse`` substring relation,
    and a comment being read as configuration -- are controlled explicitly, above and below.
    """

    def test_the_rehearse_input_is_a_boolean_that_defaults_to_false(self) -> None:
        declared = workflow_dispatch_input(release_workflow_text(), REHEARSE_INPUT)
        assert declared, (
            f'{RELEASE_NAME} declares no `{REHEARSE_INPUT}` workflow_dispatch input. Every mode '
            f'assertion below is then about conditions on an input that does not exist -- under a '
            f'dispatch the context would be empty, so the real path would run and the rehearsal '
            f'steps would silently never fire.'
        )
        assert declared.get('type') == 'boolean', (
            f'{RELEASE_NAME}: the `{REHEARSE_INPUT}` input is `type: {declared.get("type")}`, not '
            f'`boolean`. `type: boolean` is what makes `inputs.{REHEARSE_INPUT}` a real boolean; '
            f"a string input reopens the truthiness trap, where the value `'false'` is TRUTHY "
            f'(GitHub coerces only `false, 0, -0, "", \'\', null`) and every rehearsal gate fires '
            f'on a real release.'
        )
        assert declared.get('default') == 'false', (
            f'{RELEASE_NAME}: the `{REHEARSE_INPUT}` input defaults to '
            f'{declared.get("default")!r}. A `true` default turns the plain "Run workflow" button '
            f'-- the way the first release gets cut -- into a TestPyPI upload that reports success '
            f'and releases NOTHING, with no error anywhere to say so.'
        )

    def test_every_real_release_step_is_excluded_from_a_rehearsal(self) -> None:
        steps = release_steps()
        publishes = steps_using(steps, PYPI_PUBLISH_ACTION)
        real = (
            steps_using(steps, PSR_ACTION)
            + [step for step in publishes if repository_url(step) is None]
            + steps_using(steps, PSR_PUBLISH_ACTION)
        )
        # Anti-vacuity (CI-083): identified by role and by index, never by name -- a rename must
        # not silently empty this list, and a `repository-url` added to the real publish step must
        # not silently remove it from the set that has to be gated.
        assert len(real) == 3, (
            f'expected 3 real-release steps in {RELEASE_NAME} (semantic-release, publish to PyPI, '
            f'upload to the GitHub Release); found {len(real)}. If a `repository-url` appeared on '
            f'the real publish step it is no longer counted here -- see '
            f'test_the_publish_steps_point_at_the_indexes_they_claim_to.'
        )
        for step in real:
            assert excludes_a_rehearsal(step), (
                f'{RELEASE_NAME}: the real-release step '
                f'{step.get("name") or step.get("uses")!r} has the condition '
                f'{step.get("if", "")!r}, which does not exclude a rehearsal.\n'
                f'A rehearsal must not start python-semantic-release AT ALL: it would bump the '
                f'version, write CHANGELOG.md, commit, push the tag and create a GitHub Release -- '
                f'the exact half-released state the 0.1.0 runs left behind and that had to be '
                f'unwound by hand (remote tag deleted, main force-pushed).\n'
                f'Add `!inputs.{REHEARSE_INPUT}` to the condition, and keep the `${{{{ }}}}` '
                f'wrapper: a bare `if: !inputs.{REHEARSE_INPUT}` is a YAML TAG, not a condition.'
            )

    def test_every_rehearsal_step_is_excluded_from_a_real_release(self) -> None:
        steps = release_steps()
        rehearsal = steps_running_a_command(steps) + [
            step for step in steps_using(steps, PYPI_PUBLISH_ACTION) if repository_url(step) == TEST_PYPI_URL
        ]
        assert len(rehearsal) == 2, (
            f'expected 2 rehearsal-only steps in {RELEASE_NAME} (the build, and the upload to '
            f'{TEST_PYPI_URL}); found {len(rehearsal)}.'
        )
        for step in rehearsal:
            assert excludes_a_real_release(step), (
                f'{RELEASE_NAME}: the rehearsal-only step '
                f'{step.get("name") or step.get("uses")!r} has the condition '
                f'{step.get("if", "")!r}, which does not exclude a real release.\n'
                f'On the real path that either wastes a build or -- worse -- uploads a real '
                f'release to the TEST index, where nobody can install it and the version can never '
                f'be re-uploaded to the real one.\n'
                f'The condition must be `${{{{ inputs.{REHEARSE_INPUT} }}}}` and must NOT be the '
                f'negation: after normalization `!inputs.{REHEARSE_INPUT}` contains '
                f'`inputs.{REHEARSE_INPUT}`, so both modes would read as satisfied.'
            )

    def test_no_condition_reads_the_string_flavoured_spelling_of_the_input(self) -> None:
        offenders = [
            line.strip() for line in non_comment_lines(release_workflow_text()) if STRING_FLAVOURED_INPUTS in line
        ]
        assert not offenders, (
            f'{RELEASE_NAME} reads `{STRING_FLAVOURED_INPUTS}`:\n  ' + '\n  '.join(offenders) + '\n'
            f'Use the `inputs` context instead. GitHub: "the `inputs` context preserves Boolean '
            f'values as Booleans instead of converting them to strings", and the falsy set is '
            f"`false, 0, -0, \"\", '', null` -- the STRING `'false'` is not on it. So a bare "
            f'`{STRING_FLAVOURED_INPUTS}.{REHEARSE_INPUT}` is ALWAYS true and comparing it '
            f'`== true` is ALWAYS false. Both spellings silently invert a mode, in opposite '
            f'directions, and neither reports anything.'
        )

    def test_every_negated_condition_is_wrapped_in_the_expression_syntax(self) -> None:
        conditions = [
            line.strip().lstrip('-').strip().partition(':')[2].strip()
            for line in non_comment_lines(release_workflow_text())
            if line.strip().lstrip('-').strip().startswith('if:')
        ]
        negations = [condition for condition in conditions if '!inputs' in condition]
        # Anti-vacuity (CI-083): with no negated condition this loop asserts nothing, and a file
        # with no negations is itself the failure this class exists to catch.
        assert negations, (
            f'{RELEASE_NAME} has no negated `if:` at all, so nothing keeps python-semantic-release '
            f'out of a rehearsal. See test_every_real_release_step_is_excluded_from_a_rehearsal.'
        )
        for condition in negations:
            assert condition.startswith('${{'), (
                f'{RELEASE_NAME} has the condition `if: {condition}`, which is not wrapped in '
                f'`${{{{ }}}}`.\n'
                f'Measured: a leading `!` opens a YAML TAG, so `if: !inputs.{REHEARSE_INPUT}` is '
                f'not a false condition -- it is a parse error, `ConstructorError: could not '
                f"determine a constructor for the tag '!inputs.{REHEARSE_INPUT}'`. Quoting it "
                f'parses but leaves two spellings of the same idea in one file. Write '
                f'`if: ${{{{ !inputs.{REHEARSE_INPUT} }}}}`.'
            )

    def test_the_publish_steps_point_at_the_indexes_they_claim_to(self) -> None:
        publishes = steps_using(release_steps(), PYPI_PUBLISH_ACTION)
        assert len(publishes) == 2, (
            f'{RELEASE_NAME} has {len(publishes)} {PYPI_PUBLISH_ACTION} steps, not 2 (real PyPI and TestPyPI).'
        )
        named = [step for step in publishes if repository_url(step) is not None]
        assert len(named) == 1 and named[0]['with.repository-url'] == TEST_PYPI_URL, (
            f'{RELEASE_NAME}: exactly one publish step must name an index, and it must be '
            f'`{TEST_PYPI_URL}`; found {[repository_url(step) for step in publishes]}.\n'
            f'⚠ UNSET IS REAL PyPI. `repository-url` carries no default and the deprecated '
            f'`repository_url` alias defaults to `https://upload.pypi.org/legacy/`, with the action '
            f'passing `${{{{ inputs.repository-url || inputs.repository_url }}}}`. So a value on '
            f'the REAL step redirects a real release to another index -- a run that goes green '
            f'while publishing nothing users can install -- and a missing value on the REHEARSAL '
            f'step uploads the rehearsal to real PyPI, spending the name this row exists to '
            f'protect.'
        )

    def test_the_rehearsal_cannot_report_success_without_uploading_anything(self) -> None:
        """Captain ruling CI-123-Q2: the rehearsal is ONE-SHOT, and that is the point.

        TestPyPI is permanent per ``(name, version)``, so ``cast-iron 0.0.0`` can be uploaded
        exactly once. ``skip-existing: true`` would make a second run green -- and would make a
        *successful* rehearsal and a rehearsal that uploaded **nothing** indistinguishable, which
        is the false-green class this project keeps getting bitten by. The failure on a second run
        ("File already exists") is honest and wanted.
        """
        for step in steps_using(release_steps(), PYPI_PUBLISH_ACTION):
            for key in SKIP_EXISTING_KEYS:
                assert key not in step, (
                    f'{RELEASE_NAME}: the step {step.get("name")!r} sets '
                    f'`{key.partition(".")[2]}: {step[key]}`. Ruled against (CI-123-Q2): it makes '
                    f'a rehearsal that uploaded nothing report exactly the same green as one that '
                    f'worked, so the second run proves less than the first while looking '
                    f'identical. If a re-runnable rehearsal is genuinely wanted, that is a captain '
                    f'call and this test is where the decision gets recorded.'
                )

    def test_the_rehearsal_installs_uv_without_touching_the_job_environment(self) -> None:
        text = release_workflow_text()
        builds = steps_running_a_command(workflow_steps(text))
        assert len(builds) == 1, f'expected exactly 1 `run:` step in {RELEASE_NAME}; found {len(builds)}.'
        build = builds[0]
        assert 'uses' not in build, (
            f'{RELEASE_NAME}: the rehearsal build step now invokes the action '
            f'{build["uses"]!r}. It must stay a plain `run:` -- an action can export variables '
            f'job-wide, which is precisely the CI-121 mechanism.'
        )
        writers = [line.strip() for line in non_comment_lines(text) if JOB_SCOPED_ENV_FILE in line]
        assert not writers, (
            f'{RELEASE_NAME} writes `${JOB_SCOPED_ENV_FILE}`:\n  ' + '\n  '.join(writers) + '\n'
            f'That file is JOB-scoped: every later step inherits it, including the '
            f'python-semantic-release CONTAINER action, whose `build_command` receives a fixed '
            f'whitelist that includes `{POISONED_ENV}`. That is exactly how the 0.1.0 release died '
            f'(CI-121) -- `{SETUP_UV_ACTION}` exported a venv path that only resolves on the '
            f'runner. The guard is about the CHANNEL, not about that one action.'
        )
        requirement = flag_argument([build['run']], '--spec')
        declared = [
            specifier
            for specifier in pyproject_string_list(pyproject_table('project', 'optional-dependencies'), BUILD_EXTRA)
            if specifier.startswith('uv')
        ]
        assert requirement is not None, (
            f'{RELEASE_NAME}: the rehearsal build runs `{build["run"]}`, which passes no `--spec`, '
            f'so nothing pins the uv it builds with. Use '
            f"`pipx run --spec '<requirement>' uv build`."
        )
        assert len(declared) == 1, (
            f'{PYPROJECT_NAME}: `[project.optional-dependencies].{BUILD_EXTRA}` declares '
            f'{declared} for uv; this guard needs exactly one requirement to bind the workflow to.'
        )
        assert requirement == declared[0], (
            f'{RELEASE_NAME} builds the rehearsal with `{requirement}` while {PYPROJECT_NAME} '
            f'releases with `{declared[0]}`.\n'
            f'These are the two declarations of "the uv this project releases with" and they must '
            f'move together. The `{BUILD_EXTRA}` extra pins the uv that WROTE {LOCK_NAME} (0.11.x: '
            f'`version = 1`, `revision = 3`), not the newest uv -- so a rehearsal on a different '
            f'uv would be rehearsing a build the real release never performs.'
        )
        assert re.search(r'[=<>~!]', requirement), (
            f'{RELEASE_NAME}: the rehearsal build requirement `{requirement}` is unpinned, so the '
            f'rehearsal would silently drift onto whatever uv shipped this morning while the real '
            f'release stays on {declared[0]}.'
        )

    def test_these_guards_can_still_see_the_shapes_they_forbid(self) -> None:
        """Positive control (CI-072): every assertion above is an absence, a count or a substring.

        That is the shape that passes loudly forever once the reader underneath it stops reading,
        so each forbidden shape is pushed back through the same helpers and asserted to be SEEN.
        """
        # 1. An ungated step must fail both mode predicates -- it runs in both modes.
        ungated = workflow_steps('      - name: Publish\n        uses: pypa/gh-action-pypi-publish@release/v1\n')[0]
        assert not excludes_a_rehearsal(ungated) and not excludes_a_real_release(ungated), (
            'an ungated step satisfies a mode predicate, so both mode assertions would pass on a '
            'workflow with no conditions at all.'
        )
        # 2. The string-flavoured spelling must be visible on a non-comment line, and invisible on
        #    a commented one -- release.yml documents the trap in prose, right where it matters.
        live = f'        if: ${{{{ {STRING_FLAVOURED_INPUTS}.{REHEARSE_INPUT} }}}}\n'
        assert [line for line in non_comment_lines(live) if STRING_FLAVOURED_INPUTS in line], (
            'the string-flavoured spelling is invisible to non_comment_lines on a plain condition '
            'line, so that guard would stay green with the trap in the file.'
        )
        assert not [line for line in non_comment_lines(f'      # {live.strip()}\n') if STRING_FLAVOURED_INPUTS in line]
        # 3. A real publish step carrying an index must be visible to the index check.
        redirected = workflow_steps(
            '\n'.join(
                [
                    '      - name: Publish to PyPI',
                    '        uses: pypa/gh-action-pypi-publish@release/v1',
                    '        with:',
                    f'          repository-url: {TEST_PYPI_URL}',
                ]
            )
        )[0]
        assert repository_url(redirected) == TEST_PYPI_URL
        # ...including the deprecated alias, which is the spelling that carries a real default.
        aliased = workflow_steps(
            '      - uses: pypa/gh-action-pypi-publish@release/v1\n'
            '        with:\n'
            '          repository_url: https://upload.pypi.org/legacy/\n'
        )[0]
        assert repository_url(aliased) == 'https://upload.pypi.org/legacy/', (
            'repository_url reads only the kebab-case spelling, so the deprecated `repository_url` '
            'alias could redirect a real release with the guard none the wiser.'
        )
        # 4. `skip-existing` must be visible in both spellings (CI-123-Q2).
        for spelling in ('skip-existing', 'skip_existing'):
            skipping = workflow_steps(
                f'      - uses: pypa/gh-action-pypi-publish@release/v1\n        with:\n          {spelling}: true\n'
            )[0]
            assert f'with.{spelling}' in skipping, (
                f'`{spelling}: true` is invisible to the parser, so the one-shot ruling could be '
                f'reversed in the file without reversing it in the test.'
            )
        # 5. THE SUBSTRING TRAP, explicitly. After normalization `!inputs.rehearse` CONTAINS
        #    `inputs.rehearse`. If both predicates accepted both spellings, the two central mode
        #    assertions above would be one tautology that no mutation could ever redden.
        real_only = workflow_steps(f'      - name: R\n        if: ${{{{ !inputs.{REHEARSE_INPUT} }}}}\n')[0]
        rehearsal_only = workflow_steps(f'      - name: H\n        if: ${{{{ inputs.{REHEARSE_INPUT} }}}}\n')[0]
        assert excludes_a_rehearsal(real_only) and not excludes_a_real_release(real_only), (
            f'`!inputs.{REHEARSE_INPUT}` satisfies the REHEARSAL-only predicate. The negation is a '
            f'superstring of the plain form, so the two mode assertions accept each other and '
            f'neither can fail -- theatre (CI-072).'
        )
        assert excludes_a_real_release(rehearsal_only) and not excludes_a_rehearsal(rehearsal_only), (
            f'`inputs.{REHEARSE_INPUT}` satisfies the REAL-only predicate, so a rehearsal-only '
            f'step would read as correctly gated for a real release.'
        )
        # 6. ...and a commented-out step must not be read as configuration at all.
        assert workflow_steps(f'      # - name: R\n      #   if: ${{{{ !inputs.{REHEARSE_INPUT} }}}}\n') == []


class TestThePreCommitRuffHookIsNotTheLegacyAlias:
    """CI-108 -- the lint hook must be declared as ``ruff-check``, upstream's current id.

    Nothing is broken today: ``v0.16.0`` still ships ``ruff`` as an alias with an identical
    ``entry``, so both spellings lint the same files with the same ruff. This is asserted because
    the failure it prevents is *deferred and misattributed*. The alias is labelled "Legacy" in
    upstream's own hook manifest; when it goes, pre-commit fails with "hook id `ruff` not found in
    repo" -- at **pre-push**, on whichever PR next bumps ``rev``, whose author changed nothing to
    do with it. That is the same shape as CI-105: a lint-stage trap armed at rest, disarmed only by
    someone who happens to read this file.
    """

    def test_the_lint_hook_uses_the_current_id_not_the_legacy_alias(self) -> None:
        ids = hosted_hook_ids(PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), RUFF_HOOK_REPO)
        # Anti-vacuity (CI-083): an empty list satisfies "the alias is absent" while meaning
        # nothing lints at all, which is a strictly worse state than the one under test.
        assert ids, (
            f'{PRE_COMMIT_NAME} declares no hooks under {RUFF_HOOK_REPO}. Either the stanza was '
            f'removed -- in which case nothing lints at pre-push -- or the repo URL changed and '
            f'RUFF_HOOK_REPO needs to change with it.'
        )
        assert RUFF_LINT_HOOK_ID in ids, (
            f'{PRE_COMMIT_NAME}: the ruff stanza declares {ids} but not `{RUFF_LINT_HOOK_ID}`, so '
            f'nothing runs `ruff check` at pre-push. `make lint` would still catch a violation '
            f'locally; the push would not.'
        )
        assert RUFF_LEGACY_HOOK_ID not in ids, (
            f'{PRE_COMMIT_NAME}: the ruff stanza declares the deprecated `{RUFF_LEGACY_HOOK_ID}` '
            f'id. Upstream keeps it only as an alias (its .pre-commit-hooks.yaml files it under '
            f'"# Legacy alias") and both ids run `ruff check --force-exclude` today, so this is a '
            f'pure rename -- but when upstream drops it, pre-push fails with "hook id '
            f'`{RUFF_LEGACY_HOOK_ID}` not found" on an unrelated PR.\n'
            f'Fix: `- id: {RUFF_LINT_HOOK_ID}`.'
        )

    def test_the_stanza_still_declares_the_formatter_too(self) -> None:
        """The linter and the formatter are separate hooks; renaming one must not drop the other."""
        ids = hosted_hook_ids(PRE_COMMIT_CONFIG.read_text(encoding='utf-8'), RUFF_HOOK_REPO)
        assert RUFF_FORMAT_HOOK_ID in ids, (
            f'{PRE_COMMIT_NAME}: no `{RUFF_FORMAT_HOOK_ID}` hook under {RUFF_HOOK_REPO}. `ruff '
            f'check` does not reformat, so without it `make format` is the only thing keeping the '
            f'tree formatted and a push can land unformatted code.'
        )

    def test_a_comment_naming_the_legacy_alias_is_not_read_as_a_hook_id(self) -> None:
        """The parser must read configuration, not the prose explaining it (CI-072).

        This is not hypothetical: the real stanza carries a comment that names both ids in order to
        explain the rename. A parser that matched any line containing an id would report the alias
        from that comment and redden the assertion above on a file that is already correct.
        """
        stanza = '\n'.join(
            [
                f'  - repo: {RUFF_HOOK_REPO}',
                '    rev: v0.16.0',
                '    hooks:',
                f'      # `{RUFF_LINT_HOOK_ID}`, not `{RUFF_LEGACY_HOOK_ID}`: upstream renamed it.',
                f'      # - id: {RUFF_LEGACY_HOOK_ID}',
                f'      - id: {RUFF_LINT_HOOK_ID}',
            ]
        )
        assert hosted_hook_ids(stanza, RUFF_HOOK_REPO) == [RUFF_LINT_HOOK_ID]

    def test_this_guard_can_still_see_the_legacy_alias(self) -> None:
        """Positive control (CI-072): prove the assertion can go red.

        ``RUFF_LEGACY_HOOK_ID not in ids`` is an absence, the shape that passes loudest once the
        parser underneath it stops parsing. The pre-CI-108 stanza is pushed back through the same
        helper and the alias asserted VISIBLE, so a helper that silently returned ``[]`` -- a
        renamed repo, a restructured file -- fails here instead of passing forever.
        """
        pre_fix = '\n'.join(
            [
                f'  - repo: {RUFF_HOOK_REPO}',
                '    rev: v0.16.0',
                '    hooks:',
                f'      - id: {RUFF_LEGACY_HOOK_ID}',
                '        stages: [pre-push]',
                f'      - id: {RUFF_FORMAT_HOOK_ID}',
                '        stages: [pre-push]',
            ]
        )
        assert hosted_hook_ids(pre_fix, RUFF_HOOK_REPO) == [RUFF_LEGACY_HOOK_ID, RUFF_FORMAT_HOOK_ID]

    def test_another_repos_hooks_are_never_returned(self) -> None:
        """Ids are attributed to the stanza that declares them, not to whatever preceded them."""
        text = '\n'.join(
            [
                '  - repo: https://github.com/pre-commit/pre-commit-hooks',
                '    rev: v5.0.0',
                '    hooks:',
                '      - id: check-yaml',
                f'  - repo: {RUFF_HOOK_REPO}',
                '    rev: v0.16.0',
                '    hooks:',
                f'      - id: {RUFF_LINT_HOOK_ID}',
            ]
        )
        assert hosted_hook_ids(text, RUFF_HOOK_REPO) == [RUFF_LINT_HOOK_ID]
        assert hosted_hook_ids(text, 'https://github.com/pre-commit/pre-commit-hooks') == ['check-yaml']


class TestTheVultureAllowlistIsExactlyWhatSrcNeeds:
    """CI-107 -- ``uv run vulture src/`` must be able to exit 0, and for auditable reasons.

    Before ``[tool.vulture]`` existed this command -- named in ``CLAUDE.md`` as the project's
    dead-code check and wired up as ``make vulture`` -- exited **3** with 22 findings, every one a
    false positive. Exit 3 is vulture's ``DeadCode``, meaning it ran correctly and found something;
    the distinction matters because exit 2 (``InvalidCmdlineArguments``) would have meant the
    documented command was itself malformed, a different bug with a different fix.

    A check that cannot pass is worse than an absent one: it trains its readers to expect red, so
    the first *genuine* finding reads as more of the same. The allowlist restores a meaningful
    baseline -- and these tests keep that baseline honest, because an allowlist is only as good as
    its narrowness. ``ignore_names`` suppresses a bare name **everywhere in the project**, so a
    stale entry is a permanent blind spot that no one would ever see reported.
    """

    def allowlist(self) -> list[str]:
        """Return the configured ``ignore_names``."""
        return pyproject_string_list(pyproject_table('tool', 'vulture'), 'ignore_names')

    def min_confidence(self) -> int:
        """Return the configured ``min_confidence``, defaulting to vulture's own default of 0."""
        value = pyproject_table('tool', 'vulture').get('min_confidence', 0)
        return value if isinstance(value, int) else 0

    def test_the_allowlist_suppresses_every_finding_and_nothing_more(self) -> None:
        """Set equality, not containment -- it asserts sufficiency and minimality at once.

        Containment in one direction would let the allowlist rot: delete ``get_alias`` and its
        entry lingers, silently muting any future ``get_alias`` anywhere in the package. Containment
        in the other would let a new finding go unlisted. Equality is the only relation that keeps
        ``make vulture`` green *and* keeps this file an accurate inventory of what is suppressed.
        """
        reported = {name for name, _ in vulture_findings([str(SRC_DIR)], [], self.min_confidence())}
        configured = self.allowlist()
        # Anti-vacuity (CI-083): equality between two empty sets would pass this test while meaning
        # the scanner found nothing at all -- a moved src/, an unreadable tree.
        assert reported, (
            f'vulture reports nothing at all under {SRC_DIR}, so this comparison is vacuous. The '
            f'scan path is probably wrong (SRC_DIR) rather than the source being pristine.'
        )
        stale = sorted(set(configured) - set(reported))
        unlisted = sorted(set(reported) - set(configured))
        assert not stale, (
            f'{PYPROJECT_NAME} [tool.vulture] ignore_names lists {stale}, which vulture no longer '
            f'reports. The symbol was probably deleted or given a real caller in src/. Remove the '
            f'entry: `ignore_names` matches a bare name ANYWHERE, so a stale one silently hides a '
            f'future symbol that happens to share the name.'
        )
        assert not unlisted, (
            f'vulture reports {unlisted}, which {PYPROJECT_NAME} [tool.vulture] does not allowlist, '
            f'so `make vulture` now exits 3.\n'
            f'Do NOT reflexively add them here. Decide first: genuinely dead code should be '
            f'DELETED, which is strictly better than allowlisting it. Allowlist only what vulture '
            f'cannot see -- a field read reflectively by `Schema.as_dict()`, an attribute the '
            f'stdlib reads back, or public API used by downstream code and not by src/ -- and say '
            f'which of those it is in the comment above the entry.'
        )

    def test_no_entry_is_a_glob(self) -> None:
        """``ignore_names`` accepts wildcards; this repo forbids them.

        A pattern like ``EXIT_*`` or ``get_*`` would suppress names nobody has written yet, so the
        allowlist would stop being reviewable -- and the equality test above would stop being able
        to detect a stale entry, since a glob keeps matching long after its symbol is gone.
        """
        globbed = sorted(name for name in self.allowlist() if set(name) & set('*?[]'))
        assert not globbed, (
            f'{PYPROJECT_NAME} [tool.vulture] ignore_names contains glob patterns {globbed}. Spell '
            f'every suppressed name out in full: a glob silently covers symbols that do not exist '
            f'yet, and it can never be detected as stale.'
        )

    def test_min_confidence_is_not_raised_past_the_findings_it_must_catch(self) -> None:
        """The tempting wrong fix, blocked.

        Every one of the 22 original findings sat at exactly 60% -- and so does every unused
        function, class, method and attribute vulture will ever report. ``min_confidence = 61``
        would therefore turn all 22 green in one line while blinding the tool to nearly everything
        it exists to find, leaving a check that runs, passes, and means nothing.
        """
        assert self.min_confidence() <= 60, (
            f'{PYPROJECT_NAME} [tool.vulture] sets min_confidence = {self.min_confidence()}. '
            f'Unused functions, classes, methods and attributes are ALL reported at 60% '
            f'confidence, so anything above 60 suppresses them wholesale -- `make vulture` would '
            f'pass by refusing to look rather than by being clean. Allowlist specific names '
            f'instead, or delete the dead code.'
        )

    def test_this_guard_can_still_see_dead_code_the_allowlist_does_not_cover(self, tmp_path: Path) -> None:
        """Positive control (CI-072): prove the configured allowlist still lets a finding through.

        Every assertion above is an absence, and they share one dependency -- ``vulture_findings``
        actually reporting something. A module with an unmistakably dead function is scanned
        through the **real** ``ignore_names``, so a configuration that had grown broad enough to
        mute everything (a stray glob, a raised floor, an ``exclude`` swallowing the tree) fails
        here rather than reporting a clean ``src/`` forever.
        """
        (tmp_path / 'dead.py').write_text('def a_function_no_one_calls():\n    return 1\n', encoding='utf-8')
        reported = {name for name, _ in vulture_findings([str(tmp_path)], self.allowlist(), self.min_confidence())}
        assert 'a_function_no_one_calls' in reported, (
            f'vulture did not report an obviously dead function while running under the configured '
            f'ignore_names/min_confidence, so those settings suppress more than the '
            f'{len(self.allowlist())} names they list and `make vulture` is now decoration. '
            f'Reported: {reported}.'
        )

    def test_every_suppressed_file_is_named_in_the_comment(self) -> None:
        """The block comment must account for every file the allowlist covers.

        Asserted per **file** rather than per name, deliberately. Requiring each of the 19 names to
        be repeated in the prose would only force the comment to restate the array underneath it,
        and a comment that duplicates its data decays into noise nobody reads. The file is the unit
        that carries the *reason*: ``ir/models.py`` is suppressed because ``Schema.as_dict()``
        reads its fields reflectively, ``utils/logging.py`` because the stdlib reads the attributes
        back. A finding appearing in some sixth file means a new reason exists and is unwritten.
        """
        text = PYPROJECT.read_text(encoding='utf-8')
        block = text[text.index('[tool.vulture]') : text.index('[tool.pytest.ini_options]')]
        prose = '\n'.join(line for line in text[: text.index('[tool.vulture]')].splitlines()[-40:] if '#' in line)
        suppressed_files = sorted({location for _, location in vulture_findings([str(SRC_DIR)], [], 0)})
        assert suppressed_files, f'no findings under {SRC_DIR}; this comparison would be vacuous.'
        undocumented = [path for path in suppressed_files if path.removeprefix('src/castiron/') not in prose + block]
        assert not undocumented, (
            f'{PYPROJECT_NAME} [tool.vulture]: {undocumented} contain allowlisted findings but are '
            f'not named in the comment. Say which of the four blind spots applies there -- '
            f'reflective read, stdlib consumer, published API, or ported ahead of its caller -- so '
            f'the next reader can tell a deliberate suppression from an abandoned one.'
        )
