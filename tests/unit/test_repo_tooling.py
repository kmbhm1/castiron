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
path** (CI-115, CI-121, CI-122). Nothing there can be checked by running it -- a release run
publishes to PyPI -- and each of those rows is a live failure that surfaced only mid-release, after
the version bump and the tag push had already happened. These assertions are the only automated
check that path has.

The sharpest case is what gates the two steps that actually publish. Both wait on
``steps.release.outputs.released == 'true'``, and both failure modes are silent: a step that lost
that guard uploads the *previous* version's artifacts under a version PyPI has already accepted
(refused permanently, after the tag and the GitHub Release exist), and a publish step that names an
index at all redirects a real release somewhere nobody can install it while the run goes green. So
those conditions are asserted structurally, from a step-level parse of the workflow.

⚠ That parse outlived what it was written for. It arrived with CI-123, a ``rehearse`` input that
made the same file either cut a real release or upload to TestPyPI; the captain removed that mode
on 2026-08-08, once the rehearsal and two real releases had all run green. What remains is the half
that was never about the rehearsal.

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

  Fixing the check did not make anything *run* it, though, so the row had a second half (captain's
  ruling, 2026-08-08): ``make validate`` and ``make validate-fast`` now invoke it, and
  ``TestTheDeadCodeCheckIsPartOfThePrePushGate`` asserts they still do -- including that the target
  they depend on is not a hollow one, since a prerequisite naming a target whose recipe checks
  nothing passes just as green as one that works.

  ⚠ That ruling also aged the CI-086 guard above, which is **CI-136**. The offline invariant was
  asserted of every *pytest* invocation -- correct while pytest was the whole gate, and scoped to a
  tool rather than to the property from the moment vulture became the first non-pytest member.
  Nothing then stopped a prerequisite from opening a socket, and a ``make validate`` that quietly
  needs the network is a gate that passes on the machine it was written on and fails on a fresh
  contributor's. ``TestEveryStepOfThePrePushGateIsOffline`` asserts it over ``validate``'s own
  prerequisites, transitively, program by program, and fails CLOSED on a name it does not
  recognise -- so adding a network-capable step is a decision rather than an accident.

* **CI-108** -- the lint hook was declared as ``ruff``, which upstream retains only as a deprecated
  alias of ``ruff-check``. Nothing was broken; both ids run ``ruff check --force-exclude`` at
  ``v0.16.0``. It is asserted because of *when* it would break: on the alias's removal, at
  pre-push, on whichever PR next moves ``rev`` -- the same deferred, misattributed failure as
  CI-105, in the same stanza.

* **CI-144** -- ``actionlint`` and ``zizmor`` are scoped to ``^\\.github/workflows/``, so as hooks
  they run only on a commit that edits one: every other commit leaves the workflows -- and the
  action pins CI-143 put in them -- audited by nobody. They now run in CI as well, where those
  files are present on every run, and ``TestEveryWorkflowLinterInThePreCommitConfigAlsoRunsInCi``
  asserts the two lists stay the same list. It also forbids CI from reaching either tool any way
  but through pre-commit, which is CI-105's lesson applied before the drift rather than after it.

**CI-102** is this module turned on itself: five ways the guards above were weaker than they read.
Two of them are the defect this file exists to catch, committed by the file itself.

* The readers were **single-physical-line**, and ``Makefile:88-93`` wraps the ``test-matrix`` legs
  across a shell continuation -- so ``--cov-fail-under=90`` sat on a line whose first token is not
  ``pytest`` and **no guard here saw the matrix coverage floor at all**. Measured: deleting the
  floor from both legs -- undoing ``CI-089``/``CI-088`` entirely -- left all 103 assertions green.
  The same defect ran backwards on the workflow side, where re-formatting CI's ``run:`` into a
  ``run: |`` block reported a **correct** workflow as missing ``--cov``. ``join_continuations`` is
  now shared by both readers, and ``TestTheCoverageFloorIsOnEveryLegOfTheGate`` asserts the floor
  that nothing had ever asserted.

* ``WHOLE_SUITE_TARGETS`` was a hard-coded list of three names, so the guard written against
  ``CI-086`` **reproduced CI-086's own defect** -- it held by enumeration rather than by
  construction, while the CI half of the same class already did discovery ("every pytest
  invocation in ``ci.yml``"). Measured: appending a ``smoke:`` target that runs the whole suite
  with no marker left all seven of those guards green. Targets are now **discovered**, with
  ``test-unit`` / ``test-integration`` as named exceptions that must themselves be real.

* Comment handling was **asymmetric**: the workflow reader skipped ``#`` lines and the Makefile
  reader did not, so a tab-indented comment mentioning pytest -- in a file this comment-dense --
  reddened a guard on prose. Both sides now go through the same two functions.

* This is also the first family here to need a ``.git`` directory, which the hatchling sdist does
  not ship; see ``TestGitignoreDoesNotShadowADirectory`` for what that costs and what now runs in
  its place.
"""

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterable
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
UNIT_TEST_DIR = REPO_ROOT / 'tests' / 'unit'

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

#: The pre-commit repos whose hooks lint ``.github/`` configuration (CI-144) -- the workflows for
#: actionlint, and the workflows plus ``dependabot.yml`` and any ``action.yml`` for zizmor. Each
#: carries an upstream ``files:`` filter, so locally they fire only on a commit that touches one of
#: those files -- measured, not inferred: committing an unrelated file reports
#: ``Lint GitHub Actions workflow files...(no files to check)Skipped``. That is correct for an
#: incremental hook and wrong as the *only* enforcement, because what they check is a property of
#: those files rather than of the diff: a workflow pinned to a compromised action is not audited by
#: a PR that edits Python. CI sees the files on every run, which is why they also run there.
#:
#: Named as REPOS rather than as hook ids, so the ids come out of ``.pre-commit-config.yaml``
#: instead of being restated here: a stanza that grows a second hook is then covered without anyone
#: remembering to extend a list, and a repo that is renamed or dropped empties its id list and
#: trips the anti-vacuity assertion rather than passing silently on nothing.
WORKFLOW_LINT_HOOK_REPOS = (
    'https://github.com/rhysd/actionlint',
    'https://github.com/zizmorcore/zizmor-pre-commit',
)

#: The two linters' own executables. Asserted **never** to appear in command position in CI, which
#: is what makes the guard above mean "CI runs THESE hooks" rather than "CI runs something with the
#: same name": a ``uvx zizmor`` step would satisfy neither the pinned ``rev`` nor the ``args:`` that
#: carry ``--config .github/zizmor.yml``, and a second declaration of a tool's version is the exact
#: shape CI-105 cost two red pushes on (see ``TestTheRuffHookAndTheRuffGateAreTheSameRuff``).
#: Spelled as the binaries rather than derived from the hook ids they currently coincide with --
#: deriving them would make this assertion silently narrow if an id and a program name diverged.
WORKFLOW_LINT_PROGRAMS = frozenset({'actionlint', 'zizmor'})

#: How CI must reach them: ``pre-commit run <hook-id>``, at most one id per invocation (upstream's
#: CLI takes a single positional), which is why the workflow carries one step per linter.
PRE_COMMIT_PROGRAM = 'pre-commit'
PRE_COMMIT_RUN = 'run'

#: ``pre-commit run`` flags whose value is the NEXT token, skipped when locating the hook id for
#: the same reason ``UV_RUN_VALUE_FLAGS`` exists: unskipped, ``--hook-stage pre-push`` would report
#: ``pre-push`` as a hook id. ``--files`` is listed although it takes *many* values, so a step that
#: used it would lose its hook id and redden this guard -- the fail-closed direction, and a prompt
#: to teach the parser rather than a hole that passes.
PRE_COMMIT_VALUE_FLAGS = frozenset(
    {'-c', '--config', '--color', '--hook-stage', '--from-ref', '--source', '--to-ref', '--origin', '--files'}
)

#: The Make target the pre-push mypy hook must invoke. Not compared as a literal string: the
#: assertion is that whatever target the hook runs is one ``make validate`` *also* runs, which is
#: what makes "the gate and the hook cannot disagree" structural rather than remembered.
VALIDATE_TARGET = 'validate'

#: The iterating variant of the gate, and the dead-code target both must run (CI-107). Named as
#: constants for the same reason ``VALIDATE_TARGET`` is: the assertions are about the *relation*
#: between these targets, so a rename should break one lookup here rather than five string
#: literals scattered through the guards.
VALIDATE_FAST_TARGET = 'validate-fast'
VULTURE_TARGET = 'vulture'

#: The convenience target whose ``-m`` expression decides what "a unit test" means (CI-134). Named
#: here because the marker is NOT hard-coded below: the guard reads it back out of this target, so
#: renaming the marker in one place cannot leave the other silently selecting nothing.
UNIT_TARGET = 'test-unit'

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

#: The coverage half of the invariant above, DERIVED rather than restated (CI-102). It is
#: asserted separately because its scope is different: every whole-suite target must carry the
#: marker, but ``coverage`` deliberately omits the floor (it renders HTML rather than gating), so
#: the floor is asserted over the two GATE targets instead. Deriving it means the ``90`` in
#: Hard Rule #8 has exactly one spelling in this module.
COVERAGE_FLOOR_FLAGS = {flag: value for flag, value in LOAD_BEARING_FLAGS.items() if flag.startswith('--cov')}

#: A Make rule line: a target name at column 0, followed by ``:``. ``(?!=)`` rejects a ``FOO :=``
#: variable assignment, and requiring an alphanumeric first character rejects ``.PHONY`` and its
#: siblings -- neither is a target with a recipe. Recipe lines are tab-indented and so cannot match.
MAKE_RULE = re.compile(r'^(?P<target>[A-Za-z0-9][A-Za-z0-9_.-]*)\s*:(?!=)')

#: Make targets that deliberately select ONE half of the suite, and are therefore exempt from the
#: whole-suite marker assertion. The ONLY hard-coded target names in that guard, and they are
#: exceptions rather than scope -- see ``TestTheOfflineSuiteIsOfflineByConstruction`` for why that
#: distinction is the whole of CI-102's second finding. Each is asserted to be a real pytest-running
#: target, so a rename empties the exemption loudly instead of silently widening it.
MARKER_SCOPED_TARGETS = ('test-unit', 'test-integration')

#: A FLOOR for that discovery, never its scope. Enumeration used as a lower bound can only
#: fail closed -- it catches a reader that has gone blind -- whereas enumeration used as the scope
#: is precisely the CI-086 defect the guard was written against. A target added tomorrow is covered
#: without appearing here; a target that stops being found trips this.
DISCOVERY_FLOOR = frozenset({'test', 'coverage', 'test-matrix'})

#: How a workflow would reach the suite indirectly. Named because the failure message for "CI runs
#: no pytest" has to be able to tell "the step was renamed" from "the step now says `make test`" --
#: the alternative PR #14's audit deliberately rejected, and the one a reader is most likely to
#: have just introduced (CI-102, F8).
MAKE_PROGRAM = 'make'

#: The two gate targets whose every step must be offline (CI-136). Both, not just ``validate``:
#: they are meant to differ along ONE axis (how many interpreters), so a step that could reach the
#: network in one and not the other would be a second, undocumented difference -- the same argument
#: ``TestTheDeadCodeCheckIsPartOfThePrePushGate`` already makes about vulture's membership.
GATE_TARGETS = (VALIDATE_TARGET, VALIDATE_FAST_TARGET)

#: Programs the gate may run that are not pytest (CI-136). Each reads local files and emits a
#: verdict: ruff parses ``[tool.ruff]`` and the source, mypy the same plus its cache, vulture is a
#: static AST scan. None takes a URL, a host or a DSN in any invocation this repository makes.
#:
#: Kept EXACTLY as wide as the gate needs, on the ``[tool.vulture] ignore_names`` precedent: a
#: stale entry here would silently pre-authorise a tool nothing runs yet, which is how an allowlist
#: stops being one. ``test_the_allowlist_is_exactly_what_the_gate_runs`` asserts the equality.
OFFLINE_GATE_TOOLS = frozenset({'mypy', 'ruff', 'vulture'})

#: The one program on the gate that CAN reach a live source, and is therefore allowed only with
#: the marker. Named rather than spelled inline so the two halves of the CI-136 guard -- "is this
#: program allowlisted" and "does this pytest carry ``-m``" -- cannot drift onto different spellings.
GATE_PYTEST = 'pytest'

#: Shell builtins the gate's recipes use for sequencing and banners. Separated from
#: ``OFFLINE_GATE_TOOLS`` because they are inert by CONSTRUCTION rather than by audit -- none of
#: them can open a socket in any invocation -- so this set is deliberately allowed to be wider than
#: what the Makefile happens to use today, and is not asserted for exactness.
INERT_SHELL_BUILTINS = frozenset({'[', 'cd', 'echo', 'false', 'printf', 'set', 'test', 'true'})

#: The wrapper every tool in this Makefile is invoked through. Peeled before the program is
#: compared against the allowlist: unpeeled, ``uv`` is a single entry that covers everything uv can
#: be asked to run, which is not an allowlist at all.
UV_RUNNER = ('uv', 'run')

#: ``uv run`` flags whose value is the NEXT token, so both must be skipped when locating the
#: program. Missing one leaves the flag's value in the program position and the guard fails closed
#: (see ``unwrap_uv_run``), which is why an incomplete list costs a red test rather than a hole.
UV_RUN_VALUE_FLAGS = frozenset(
    {'-p', '--python', '--group', '--with', '--with-requirements', '--extra', '--project', '--directory'}
)

#: Characters a POSIX shell reads as command separators. ``(`` and ``)`` are deliberately EXCLUDED
#: from ``shlex``'s punctuation set: including them splits ``$(MAKEFILE_LIST)`` into four tokens and
#: promotes the variable name into command position, which would redden this guard on a Make
#: idiom that runs nothing. A real subshell instead survives as one unsplit token, is not on any
#: allowlist, and so still fails -- the safe direction.
SHELL_SEPARATOR_CHARS = ';&|'

#: Words a shell reads as syntax rather than as a program, and after which a fresh command begins.
SHELL_KEYWORDS = frozenset({'!', '{', '}', 'do', 'done', 'elif', 'else', 'esac', 'fi', 'if', 'then', 'until', 'while'})

#: Compound-command openers followed by a WORD LIST rather than by a command -- ``for V in 3.10
#: 3.11 3.13 3.12`` names four interpreters, not four programs. Everything up to the next separator
#: is skipped after one of these. ``while``/``until`` are keywords above, not here: what follows
#: them really is a command.
SHELL_WORD_LIST_OPENERS = frozenset({'case', 'for', 'select'})

#: Make's recipe-line prefixes: ``@`` suppresses the echo, ``-`` ignores a failing command, ``+``
#: runs the line even under ``make -n``. Stripped before tokenizing, so ``@uv`` is read as ``uv``.
MAKE_RECIPE_PREFIXES = '@-+'

#: Assignment-prefix form (``FOO=bar cmd``), which occupies command position without being one.
ENV_ASSIGNMENT = re.compile(r'[A-Za-z_][A-Za-z0-9_]*=')

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

#: The same trap from the other direction (CI-119), and the reason it needs its own entry: these
#: keys are **real**, just not at this level. A bare ``changelog_file = "CHANGELOG.md"`` under
#: ``[tool.semantic_release]`` sat directly beneath ``version_toml`` and ``version_variables``,
#: which really do decide where the version is written, and read exactly like the line that names
#: the changelog. It named nothing: ``'changelog_file' in RawConfig.model_fields`` is ``False`` on
#: **both** PSR majors this repo touches -- 9.21.2 (what the ``@v9`` action runs) and 10.6.1 (what
#: ``uv.lock`` resolves) -- so ``extra="ignore"`` dropped it, silently, through two shipped
#: releases that wrote ``CHANGELOG.md`` anyway.
#:
#: Mapped to the **live** table rather than merely listed, because "move it there" is the obvious
#: fix and the obvious target is the wrong one: ``[tool.semantic_release.changelog].changelog_file``
#: exists but is deprecated in 9.21.2's own source ("Deprecated! Moved to
#: 'default_templates.changelog_file'", with a ``field_validator`` that logs "compatibility will
#: break in v10" on every load). The live spelling is one table deeper, where the default is
#: already ``CHANGELOG.md``, so the key was deleted instead of moved.
MISPLACED_SEMANTIC_RELEASE_KEYS = {'changelog_file': 'tool.semantic_release.changelog.default_templates'}

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

#: The upload action. Assertions about it go through ``workflow_steps`` rather than
#: ``uses_action`` / ``pinned_action_major``, which answer only "is this action used, and at what
#: major": neither can read a step's ``if:`` or its ``with:`` children, and both return on the
#: FIRST match -- so a second step using this action (the CI-123 rehearsal was exactly that shape)
#: would be answered about silently by whichever one came first.
PYPI_PUBLISH_ACTION = 'pypa/gh-action-pypi-publish'

#: The action that attaches ``dist/*`` to the GitHub Release. A real-release step, and one that
#: has never executed -- see CI-116 for why it is this action and not ``upload-to-gh-release``.
PSR_PUBLISH_ACTION = 'python-semantic-release/publish-action'

#: The condition both publishing steps carry, normalized (``normalized_condition`` strips the
#: ``${{ }}`` wrapper and all whitespace). ``released`` is ``'true'`` only when
#: python-semantic-release actually cut a release; a no-op run sets it to ``'false'`` and still
#: succeeds, so a step that lost this guard would publish on a run that released nothing.
RELEASED_GUARD = "steps.release.outputs.released=='true'"

#: Both spellings of the publish action's index input, asserted **absent** on the real step.
#: Read from the action's own ``action.yml``: the canonical kebab-case ``repository-url`` carries
#: NO default, the deprecated ``repository_url`` alias defaults to
#: ``https://upload.pypi.org/legacy/``, and the composite passes
#: ``${{ inputs.repository-url || inputs.repository_url }}``. So **unset is real PyPI**, and any
#: value on the real step silently redirects a real release to another index.
REPOSITORY_URL_KEYS = ('with.repository-url', 'with.repository_url')

#: The file whose name IS the CI-121 mechanism. ``$GITHUB_ENV`` is job-scoped, so anything written
#: to it inherits into every later step, including the container action -- which is how
#: ``VIRTUAL_ENV`` reached ``build_command``. ``setup-uv`` was one producer, not the category, so
#: the assertion is about the channel rather than about that action.
JOB_SCOPED_ENV_FILE = 'GITHUB_ENV'

#: The number of steps the release job has: app token, checkout, PSR, publish to PyPI, upload to
#: the GitHub Release. Asserted so that a parse which silently returns fewer (or none) fails HERE
#: rather than making every loop below pass vacuously (CI-083).
RELEASE_STEP_COUNT = 5

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


def flags_from_tokens(tokens: list[str]) -> dict[str, str]:
    """Map flag name to value for one already-tokenized argv.

    Handles both spellings pytest accepts: ``--cov=X`` (joined) and ``-m X`` (separate). A flag
    with no value maps to the empty string.

    Takes tokens rather than a string because the CI-136 guard reads flags off ONE invocation
    picked out of a line that holds several, and re-joining those tokens with spaces would destroy
    the very value under test: ``-m 'not integration'`` survives tokenization as a single token and
    would come back from a round-trip as ``-m not``.

    Args:
        tokens: One argv, as ``command_tokens`` or ``shell_invocations`` produces it.

    Returns:
        The flags found, e.g. ``{'-m': 'not integration', '--cov': 'src/castiron'}``.
    """
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


def workflow_run_commands(text: str) -> list[str]:
    """Return every logical shell command a workflow's ``run:`` steps execute.

    Only ``run:`` values are considered -- a ``run:`` key with the command inline, or the body of
    a ``run: |`` block scalar. **A step's ``name:`` is never a command**, which matters more than
    it looks: relying on tokenization to exclude it works only for names whose punctuation
    happens to fuse the word, and ``- name: Run pytest`` is an entirely ordinary rename. That
    would otherwise be read as an invocation with no flags, and every assertion below would
    report a missing marker on a command that does not exist.

    Comment lines are dropped and backslash continuations are joined (CI-102), which is what makes
    this the mirror image of ``joined_recipe`` on the Makefile side rather than a second, subtly
    different reader. Both halves of that mattered: the Makefile reader skipped no comments, and
    NEITHER joined continuations -- so re-formatting the CI step below into a ``run: |`` block
    whose command wraps reported a **correct** workflow as missing ``--cov``, measured.

    Parsed textually rather than with PyYAML, matching the reasoning already recorded in
    ``test_goldens.py``: pyyaml is only a *transitive* dependency here (pre-commit pulls it in),
    and a test that silently depends on someone else's requirement is one ``uv sync`` away from
    an unexplained collection error.

    Args:
        text: The whole workflow file.

    Returns:
        The commands, stripped, in file order.
    """
    commands: list[str] = []
    block: list[str] = []
    block_indent: int | None = None
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if block_indent is not None:
            if not stripped or stripped.startswith('#'):
                continue
            if indent > block_indent:
                block.append(stripped)
                continue
            commands.extend(join_continuations(block))
            block, block_indent = [], None  # the block ended; re-read this line as ordinary YAML
        if not stripped or stripped.startswith('#'):
            continue
        key, separator, remainder = stripped.partition('run:')
        if not separator or key.strip().strip('-').strip():
            continue
        if remainder.strip() in BLOCK_SCALAR_HEADERS:
            block_indent = indent
            continue
        commands.append(remainder.strip())
    commands.extend(join_continuations(block))
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


def make_recipe(text: str, target: str) -> list[str]:
    """Return the command lines in one Make target's recipe.

    A recipe is the tab-indented block under a rule line, so the scan starts at the rule and stops
    at the first line that is not tab-indented -- which is what keeps the *next* target's commands
    out of this one's. Leading ``@`` (Make's echo suppression) is left on, since it is part of the
    line a reader sees.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'vulture'``.

    Returns:
        The recipe lines, stripped and comment-free. Empty when the target does not exist, or is a
        pure aggregate like ``validate`` whose work is all in its prerequisites.
    """
    recipe: list[str] = []
    inside = False
    for line in text.splitlines():
        if re.match(rf'^{re.escape(target)}:', line):
            inside = True
            continue
        if inside:
            if not line.startswith('\t'):
                break
            if line.strip() and not line.strip().startswith('#'):
                recipe.append(line.strip())
    return recipe


def transitive_prerequisites(text: str, target: str) -> list[str]:
    """Return every target one Make target depends on, directly or through another target.

    Transitive rather than direct because ``make validate`` runs the whole tree, not the top row:
    a prerequisite that is itself an aggregate would otherwise hide everything beneath it, and the
    CI-136 guard would report on a name instead of on what Make executes.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'validate'``.

    Returns:
        The dependency targets in breadth-first order, each once. Empty when the target does not
        exist or has no prerequisites. A cycle terminates rather than recursing forever -- Make
        itself tolerates one, so a guard that hung on it would be the more surprising failure.
    """
    ordered: list[str] = []
    pending = list(make_prerequisites(text, target))
    while pending:
        name = pending.pop(0)
        if name in ordered:
            continue
        ordered.append(name)
        pending.extend(make_prerequisites(text, name))
    return ordered


def join_continuations(lines: Iterable[str]) -> list[str]:
    """Join backslash-continued physical lines into whole logical commands.

    Shared by both readers on purpose (CI-102), because the defect it fixes was symmetric. Read
    line by line, ``Makefile``'s ``test-matrix`` recipe ends a ``pytest`` invocation mid-flight and
    *starts* the next line with ``--cov=src/castiron``: every guard here saw the legs' ``-m``
    marker and NONE saw their ``--cov-fail-under=90``, so deleting the coverage floor from both
    legs -- undoing ``CI-089``/``CI-088`` entirely -- was measured to leave the whole module green.
    Running backwards it is worse than a hole: a ``run: |`` block in a workflow whose command wraps
    is a **correct** step that a line-at-a-time reader reports as missing its flags, and a guard
    that cries wolf gets edited until it stops.

    Args:
        lines: Physical lines, already stripped of indentation and of comments.

    Returns:
        The logical command lines, in order. A trailing continuation with nothing after it is
        emitted rather than dropped, so a truncated recipe reaches an assertion instead of
        disappearing from the parse.
    """
    joined: list[str] = []
    pending = ''
    for line in lines:
        if line.endswith('\\'):
            pending += line[:-1].rstrip() + ' '
            continue
        joined.append((pending + line).strip())
        pending = ''
    if pending:
        joined.append(pending.strip())
    return joined


def joined_recipe(text: str, target: str) -> list[str]:
    """Return one target's recipe with backslash-continued lines joined into whole commands.

    Load-bearing, not cosmetic. ``make_recipe`` returns physical lines, and ``test-matrix``'s recipe
    is a single shell loop spread over ten of them: read line by line, its fourth line ends mid
    ``pytest`` invocation and its fifth *starts* with ``--cov=src/castiron``. A reader that treats
    each line as a command would then report ``--cov=src/castiron`` as the program being run, and
    the CI-136 allowlist -- which fails closed on an unknown program -- would go red on a
    continuation. Joining first is what makes "one command, one verdict" true.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'test-matrix'``.

    Returns:
        The logical command lines, in recipe order. Empty for a target with no recipe.
    """
    return join_continuations(make_recipe(text, target))


def shell_tokens(command: str) -> list[str]:
    """Tokenize one shell command, keeping ``;``, ``&`` and ``|`` as tokens of their own.

    Distinct from ``command_tokens``, which uses ``shlex.split`` and so fuses a separator onto the
    word beside it (``3.12;`` rather than ``3.12``, ``;``). Command position cannot be tracked
    through that, and command position is the whole of ``shell_invocations``.

    Args:
        command: One logical shell command line.

    Returns:
        The tokens, or ``[]`` when the line is not tokenizable on its own (an unbalanced quote).
        Callers guard an empty result with an explicit anti-vacuity assertion.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_SEPARATOR_CHARS)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return []


def unwrap_uv_run(invocation: list[str]) -> list[str]:
    """Return the argv of the program a ``uv run`` invocation actually executes.

    Every tool in this Makefile is invoked through ``uv run``, so an unpeeled argv reports ``uv``
    for the linter, the type checker, the dead-code scan and pytest alike -- one allowlist entry
    covering everything uv can be asked to run, which is not an allowlist.

    Args:
        invocation: One argv from ``shell_invocations``.

    Returns:
        The inner argv. ``invocation`` unchanged when it is not a ``uv run``, and also when no
        program can be located -- an unrecognised value-taking flag leaves ``uv`` in the program
        position, so the guard fails closed instead of reporting the flag's value as a program.
    """
    if tuple(invocation[: len(UV_RUNNER)]) != UV_RUNNER:
        return invocation
    index = len(UV_RUNNER)
    while index < len(invocation):
        token = invocation[index]
        if not token.startswith('-'):
            return invocation[index:]
        index += 2 if token in UV_RUN_VALUE_FLAGS else 1
    return invocation


def shell_invocations(command: str) -> list[list[str]]:
    """Return the argv of every program one shell command line runs.

    A command line in this Makefile is rarely one command: ``test-matrix`` is a ``for`` loop around
    an ``if``/``else`` holding two separate ``pytest`` calls. They are split apart because the
    CI-136 assertions are per-invocation -- a whole-line flag scan would let one leg's
    ``-m "not integration"`` stand in for the other's, which is the exact hole ``CI-089`` closed in
    the Makefile itself.

    Shell syntax is skipped rather than misread as programs: keywords (``if``, ``then``, ``do``),
    the word list of a ``for``, and an ``FOO=bar`` assignment prefix all leave command position
    where it was. ``uv run`` is peeled (see ``unwrap_uv_run``). Make's ``@``/``-``/``+`` recipe
    prefixes are stripped first.

    Args:
        command: One logical command line, continuations already joined.

    Returns:
        One argv per program, in the order a shell would reach them, each already unwrapped.
    """
    invocations: list[list[str]] = []
    current: list[str] | None = None
    in_word_list = False
    for token in shell_tokens(command.lstrip(MAKE_RECIPE_PREFIXES + ' ')):
        if token and not set(token) - set(SHELL_SEPARATOR_CHARS):
            current, in_word_list = None, False
            continue
        if in_word_list:
            continue
        if current is not None:
            current.append(token)
            continue
        if token in SHELL_KEYWORDS:
            continue
        if token in SHELL_WORD_LIST_OPENERS:
            in_word_list = True
            continue
        if ENV_ASSIGNMENT.match(token):
            continue
        current = [token]
        invocations.append(current)
    return [unwrap_uv_run(invocation) for invocation in invocations]


def pytest_invocations(commands: Iterable[str]) -> list[list[str]]:
    """Return the argv of every pytest run among some already-logical command lines.

    Per invocation rather than per line, because one line here is often several commands:
    ``test-matrix``'s recipe is a ``for`` loop around an ``if``/``else`` holding two separate
    ``pytest`` calls, and a whole-line flag scan would let one leg's ``-m "not integration"`` stand
    in for the other's -- the exact hole ``CI-089`` closed in the Makefile itself.

    Command position, not substring. ``test-matrix`` prints ``printf '\\n=== pytest on py%s ===\\n'``
    and CI names a step ``Test (pytest, offline suite, 90% floor)``; a substring check calls both of
    those an invocation and then reports a missing marker on a command that does not exist.
    ``shell_invocations`` peels the ``uv run`` wrapper first, so ``uv run pytest`` reads as pytest.

    ⚠ A future ``python -m pytest`` would report as ``python`` and be counted as no pytest at all.
    That is the FAIL-CLOSED direction and it is deliberate: every caller guards its result with an
    anti-vacuity assertion (``CI-083``), so a reader that has gone blind is a red test naming the
    file, not a green one covering nothing. The fix would be to teach this function, never to
    loosen the guards.

    Args:
        commands: Logical command lines, continuations already joined.

    Returns:
        One argv per pytest invocation, in the order a shell would reach them.
    """
    return [
        invocation
        for command in commands
        for invocation in shell_invocations(command)
        if invocation and invocation[0] == GATE_PYTEST
    ]


def workflow_pytest_commands(text: str) -> list[list[str]]:
    """Return the argv of every pytest invocation in a workflow document.

    Args:
        text: The whole workflow file.

    Returns:
        One argv per pytest invocation, in file order.
    """
    return pytest_invocations(workflow_run_commands(text))


def make_targets(text: str) -> list[str]:
    """Return every target the ``Makefile`` declares, in file order.

    Args:
        text: The whole ``Makefile``.

    Returns:
        The target names. ``.PHONY`` and variable assignments are not targets and are excluded by
        ``MAKE_RULE``; a target declared twice appears once.
    """
    found: list[str] = []
    for line in text.splitlines():
        match = MAKE_RULE.match(line)
        if match and match.group('target') not in found:
            found.append(match.group('target'))
    return found


def make_targets_running_pytest(text: str) -> list[str]:
    """Return every Make target whose recipe runs pytest -- by DISCOVERY, not by a list.

    The point of CI-102's second finding. The guard this feeds was written against ``CI-086``, and
    it reproduced ``CI-086``'s own defect: it held by enumerating three target names while the CI
    half of the same class already discovered ("every pytest invocation in ``ci.yml``"). Measured,
    appending a ``smoke:`` target that ran the whole suite with no marker left all seven of those
    assertions green -- a whitelist cannot report what nobody remembered to add to it.

    Args:
        text: The whole ``Makefile``.

    Returns:
        The target names, in file order. A pure aggregate like ``validate`` has no recipe of its
        own and so is absent -- what it *reaches* is the subject of ``gate_invocations``.
    """
    return [target for target in make_targets(text) if pytest_invocations(joined_recipe(text, target))]


def workflow_make_delegations(workflow: str, makefile: str) -> list[str]:
    """Return the pytest-running Make targets a workflow reaches through ``make``.

    Exists for a failure MESSAGE rather than for an assertion (CI-102, F8). ``ci.yml`` open-codes
    its pytest step deliberately -- PR #14's audit rejected ``run: make test``, because a workflow
    should state the contract it enforces and ``make test`` carries developer-iteration flags
    (``-vv``) that must not reach CI by inheritance. So switching to ``make test`` is a real
    regression, and it happens to be the single most likely reason a reader is looking at "CI
    invokes no pytest" at all. Detecting it lets the message name what happened instead of
    guessing "did the step move or get renamed?", which points at the wrong file.

    Args:
        workflow: The whole workflow file.
        makefile: The whole ``Makefile``.

    Returns:
        The target names the workflow delegates to, in file order. Empty is the good answer.
    """
    runners = set(make_targets_running_pytest(makefile))
    delegated: list[str] = []
    for command in workflow_run_commands(workflow):
        for invocation in shell_invocations(command):
            if invocation and invocation[0] == MAKE_PROGRAM:
                delegated.extend(token for token in invocation[1:] if token in runners)
    return delegated


def missing_git_reason() -> str | None:
    """Return why git cannot be run at all, or None when it can.

    Returns:
        A reason naming the problem, or None.
    """
    return None if shutil.which('git') else 'the `git` executable is not on PATH'


def outside_git_checkout_reason(path: Path) -> str | None:
    """Return why ``path`` cannot answer a ``git check-ignore`` question, or None when it can.

    ``GIT_CEILING_DIRECTORIES`` is set to ``path`` itself so the upward search cannot escape it.
    Without that this function answers about whatever repository happens to CONTAIN ``path``,
    which would make the control in ``TestGitignoreDoesNotShadowADirectory`` depend on where the
    machine puts its temporary directories.

    Args:
        path: The directory the question would be asked in.

    Returns:
        A reason naming the problem, or None when ``path`` is a git work tree.
    """
    reason = missing_git_reason()
    if reason:
        return reason
    if not path.is_dir():
        return f'{path} does not exist'
    result = subprocess.run(
        ['git', 'rev-parse', '--git-dir'],
        cwd=path,
        env={**os.environ, 'GIT_CEILING_DIRECTORIES': str(path)},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f'{path} is not a git work tree ({result.stderr.strip()})'
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

    Token-level for the reason ``pytest_invocations`` is: a substring test for ``uv`` matches the word
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


def normalized_condition(step: dict[str, str]) -> str:
    """Return a step's ``if`` free of its expression wrapper and of all whitespace.

    Whitespace-insensitive on purpose: a condition is compared for what it *requires*, so
    reformatting one must not redden a guard while dropping a conjunct from one must.

    Args:
        step: One step from ``workflow_steps``.

    Returns:
        The condition, e.g. ``"steps.release.outputs.released=='true'"``. The empty string for a
        step with no condition -- a real state here, not a sentinel: the version step is
        deliberately ungated.
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


def workflow_pre_commit_hook_ids(text: str) -> set[str]:
    """Return every pre-commit hook id a workflow runs through ``pre-commit run``.

    Read from the *command*, never from a step's ``name`` -- a step called "Audit workflows
    (zizmor)" that runs nothing would otherwise satisfy the guard that CI audits the workflows,
    which is the failure this whole family exists to make impossible.

    ``shell_invocations`` peels the ``uv run`` wrapper, so the invocation is compared as
    ``pre-commit run ...`` rather than as ``uv ...``.

    Continuations are joined first (CI-102). This reader had the same single-physical-line defect
    the pytest readers did, and here it fails closed rather than open -- a wrapped
    ``pre-commit run \\`` + ``--all-files zizmor`` loses its hook id and reports CI as not running
    the hook at all. Closed is the better direction and still the wrong verdict.

    Args:
        text: The whole workflow file.

    Returns:
        The hook ids, e.g. ``{'actionlint', 'zizmor'}``. Empty when no step runs pre-commit.
    """
    hook_ids: set[str] = set()
    for step in steps_running_a_command(workflow_steps(text)):
        for line in join_continuations(step['run'].splitlines()):
            for invocation in shell_invocations(line):
                if invocation[:2] != [PRE_COMMIT_PROGRAM, PRE_COMMIT_RUN]:
                    continue
                hook_ids.update(positional_arguments(invocation[2:], PRE_COMMIT_VALUE_FLAGS))
    return hook_ids


def positional_arguments(tokens: list[str], value_flags: frozenset[str]) -> list[str]:
    """Return the non-flag arguments of one argv tail.

    Args:
        tokens: The tokens after the sub-command, e.g. ``['--all-files', 'zizmor']``.
        value_flags: Flags whose value is the next token, and which therefore consume it.

    Returns:
        The positional arguments, in order. A joined ``--flag=value`` is consumed as a flag.
    """
    positionals: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token.startswith('-'):
            skip = token in value_flags
            continue
        positionals.append(token)
    return positionals


def workflow_tools_outside_pre_commit(text: str, tools: frozenset[str]) -> set[str]:
    """Return every named tool a workflow reaches other than through ``pre-commit run``.

    Deliberately NOT a check of command position (CI-144). ``uvx zizmor``, ``pipx run actionlint``
    and ``./actionlint`` all put a *wrapper* or a path in command position, so a guard that read
    only ``invocation[0]`` would answer "no direct invocation" for the three spellings a second
    copy is most likely to arrive in. Every token of a non-pre-commit invocation is considered
    instead, reduced to its basename and stripped of any ``@version`` suffix.

    ``uses:`` is scanned as well, because the same second copy can arrive as an action
    (``zizmorcore/zizmor-action``, ``raven-actions/actionlint``) rather than as a command.

    A ``pre-commit run`` invocation is skipped whole: that is the sanctioned path, and the hook id
    it names is precisely the token this function otherwise reports.

    Args:
        text: The whole workflow file.
        tools: The tool names to look for, e.g. ``{'actionlint', 'zizmor'}``.

    Returns:
        The subset of ``tools`` the workflow reaches some other way. Empty is the good answer.
    """
    found: set[str] = set()
    for step in workflow_steps(text):
        action = step.get('uses', '').partition('@')[0]
        found |= {tool for tool in tools if tool in action}
        for line in join_continuations(step.get('run', '').splitlines()):
            for invocation in shell_invocations(line):
                if invocation[:2] == [PRE_COMMIT_PROGRAM, PRE_COMMIT_RUN]:
                    continue
                found |= tools & {token.rpartition('/')[2].partition('@')[0] for token in invocation}
    return found


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


def makefile_text() -> str:
    """Return the repository's ``Makefile``, whole.

    Returns:
        The contents of ``Makefile``.
    """
    return MAKEFILE.read_text(encoding='utf-8')


def ci_workflow_text() -> str:
    """Return the repository's CI workflow, whole.

    Returns:
        The contents of ``.github/workflows/ci.yml``.
    """
    return CI_WORKFLOW.read_text(encoding='utf-8')


def ci_pytest_commands() -> list[list[str]]:
    """Return the argv of every pytest invocation in the repository's CI workflow.

    Returns:
        One argv per invocation found in ``.github/workflows/ci.yml``.
    """
    return workflow_pytest_commands(ci_workflow_text())


def make_pytest_commands(text: str, target: str) -> list[list[str]]:
    """Return the argv of every pytest invocation in one Make target's recipe.

    Takes the ``Makefile`` text rather than reading it (CI-102) so the reader can be exercised on
    a synthetic Makefile. Every parser in this module that could only be pointed at the real file
    was, in practice, tested only through the file it guards -- which is how a reader acquires a
    blind spot nobody can write a control for.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'test'``.

    Returns:
        One argv per pytest invocation, in recipe order. Empty if the target does not run pytest.
    """
    return pytest_invocations(joined_recipe(text, target))


def selected_marker(text: str, target: str) -> str | None:
    """Return the marker expression one Make target selects with ``-m``.

    Args:
        text: The whole ``Makefile``.
        target: The target name, e.g. ``'test-unit'``.

    Returns:
        The ``-m`` value of that target's pytest invocation, or ``None`` when the target runs no
        pytest or passes no ``-m`` -- both of which mean the target no longer selects by marker,
        which the caller asserts on rather than papering over with a default.
    """
    commands = make_pytest_commands(text, target)
    if not commands:
        return None
    return flags_from_tokens(commands[0]).get('-m')


def gate_invocations(target: str) -> list[tuple[str, list[str]]]:
    """Return ``(prerequisite, argv)`` for every program one gate target's prerequisites run.

    Module-level rather than a method (CI-102) because two guard classes now ask this question:
    ``TestEveryStepOfThePrePushGateIsOffline`` asks whether each program can open a socket, and
    ``TestTheCoverageFloorIsOnEveryLegOfTheGate`` asks whether each pytest carries the floor. Two
    copies of the walk would be two chances for one of them to stop following the tree.

    Args:
        target: A gate target, i.e. ``validate`` or ``validate-fast``.

    Returns:
        One ``(prerequisite, argv)`` pair per program, in prerequisite order.
    """
    text = makefile_text()
    prerequisites = transitive_prerequisites(text, target)
    # Anti-vacuity (CI-083), twice over: no prerequisites, or a prerequisite whose recipe
    # parses to nothing, would make every loop below pass having inspected zero programs.
    assert prerequisites, f'the `{target}` target of {MAKE_NAME} has no prerequisites -- was it renamed or removed?'
    found: list[tuple[str, list[str]]] = []
    for name in prerequisites:
        invocations = [invocation for command in joined_recipe(text, name) for invocation in shell_invocations(command)]
        assert invocations, (
            f'the `{name}` prerequisite of `make {target}` parses to no program at all. Either '
            f'its recipe is empty -- in which case `make {target}` depends on a target that '
            f'does nothing and passes, the hollow-target trap `TestTheDeadCodeCheckIsPartOf'
            f'ThePrePushGate` already guards vulture against -- or `shell_invocations` can no '
            f'longer read its shape, and every assertion in this class has stopped covering it.'
        )
        found.extend((name, invocation) for invocation in invocations)
    return found


def unmarked_test_objects(source: str, marker: str) -> list[tuple[str, int]]:
    """Return the top-level test classes and functions in one module that lack a marker.

    Reads the source rather than importing it: an import would run module-level code in every test
    file in the tree, and the property being checked is a property of the text a reviewer sees.

    Only module-level definitions are inspected. That is what pytest collects here -- no test class
    in this repository nests another -- and it keeps the scan from descending into the helper
    classes that live inside test bodies.

    Args:
        source: The module's source text.
        marker: The bare marker name, e.g. ``'unit'``.

    Returns:
        ``(name, line number)`` for each unmarked definition, in file order.
    """
    wanted = f'pytest.mark.{marker}'
    unmarked: list[tuple[str, int]] = []
    for node in ast.parse(source).body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.lower().startswith('test'):
            continue
        applied = {ast.unparse(getattr(decorator, 'func', decorator)) for decorator in node.decorator_list}
        if wanted not in applied:
            unmarked.append((node.name, node.lineno))
    return unmarked


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

    ⚠ **This family needs a git checkout, and one supported environment does not have one**
    (CI-102's fourth finding). hatchling's sdist ships ``tests/``, ``Makefile``, ``.github/`` and
    ``.gitignore`` -- verified by unpacking one -- but never ``.git/``, so ``pytest`` from an
    unpacked sdist produced **2 hard failures** where the suite had previously run clean. The
    failures were honest and actionable, but they were failures in a valid environment, and a red
    test that means "you are not in a checkout" teaches a reader that red is negotiable.

    The ruling was **not** to swap a failure for a skip and lose the coverage. There are two
    different questions here, and only one of them needs the repository:

    * *Would these paths vanish on THIS machine?* -- the whole-machine question, which folds in
      ``.git/info/exclude`` (where this repo's harness is excluded) and any nested ``.gitignore``.
      Only a real checkout can answer it, so it is skipped -- narrowly, on
      ``outside_git_checkout_reason``, never on a bare non-zero exit -- outside one.
    * *Does the TRACKED ``.gitignore`` shadow them?* -- the CI-093 bug as it actually shipped, in
      the one file the sdist does carry. ``test_the_shipped_gitignore_shadows_nothing`` answers it
      in a scratch repository and therefore runs everywhere, including from an unpacked sdist.

    So the sdist keeps the assertion that its own contents can support, the checkout keeps both,
    and the positive control below -- the thing that could rot silently -- never skips at all,
    because it builds its own repository. Net: the guard got wider, not narrower.
    """

    @pytest.mark.parametrize('path', SHADOWED_PATHS)
    def test_no_ignore_rule_hides_a_manifest_directory(self, path: str) -> None:
        reason = outside_git_checkout_reason(REPO_ROOT)
        if reason:
            pytest.skip(
                f'this asks git what THIS repository ignores, and {reason}. Expected from an '
                f'unpacked sdist, which ships tests/ and .gitignore but not .git/ -- '
                f'test_the_shipped_gitignore_shadows_nothing covers the tracked file there.'
            )
        result = subprocess.run(
            ['git', '-c', 'core.ignorecase=true', 'check-ignore', '-v', path],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        # 0 = ignored, 1 = not ignored, anything else = git itself failed. Distinguishing 128
        # from 1 matters: an error would otherwise read as "not ignored" and pass vacuously.
        # The skip above handles only "no repository"; any OTHER git failure still fails here.
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

    @pytest.mark.parametrize('path', SHADOWED_PATHS)
    def test_the_shipped_gitignore_shadows_nothing(self, tmp_path: Path, path: str) -> None:
        """The same question about the TRACKED file alone, so it survives outside a checkout.

        Narrower than the test above on purpose, and the difference is worth stating: a rule added
        to ``.git/info/exclude`` would shadow a path on one developer's machine and be invisible
        here. That is why this does not replace the checkout test -- it is the half that an sdist
        can still answer, and the half the CI-093 bug actually lived in.
        """
        reason = missing_git_reason()
        if reason:
            pytest.skip(f'this reconstructs a repository to ask git a question, and {reason}.')
        subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, capture_output=True)
        (tmp_path / '.gitignore').write_text((REPO_ROOT / '.gitignore').read_text(encoding='utf-8'), encoding='utf-8')
        result = subprocess.run(
            ['git', '-c', 'core.ignorecase=true', 'check-ignore', '-v', path],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, (
            f'the tracked .gitignore ignores {path} under case-insensitive matching (git said '
            f'{result.returncode}). The rule doing it:\n  {result.stdout.strip()}\n'
            f'That is CI-093: on macOS the path vanishes from `git add -A` and from `git status` '
            f'with no error, while Linux CI tracks it normally.'
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

        It builds its own repository, so unlike the two guards above it needs no checkout and does
        NOT skip from an unpacked sdist. That asymmetry is deliberate (CI-102): the assertion that
        can rot silently is the one that must never be conditional.
        """
        reason = missing_git_reason()
        if reason:
            pytest.skip(f'this control builds a repository to reproduce the trap, and {reason}.')
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

    def test_the_skip_condition_tells_a_checkout_from_a_bare_directory(self, tmp_path: Path) -> None:
        """A skip is only safe while it can tell the two apart -- so that is asserted, not assumed.

        The failure this forbids is the expensive one: a condition that answered "not a checkout"
        everywhere would skip the CI-093 guard on all four CI legs and report nothing but a green
        run. ``GIT_CEILING_DIRECTORIES`` is what makes the negative half deterministic -- without
        it, a temporary directory that happens to sit under some repository answers "checkout".
        """
        reason = missing_git_reason()
        if reason:
            pytest.skip(f'this control asks git about two directories, and {reason}.')
        assert outside_git_checkout_reason(tmp_path) is not None, (
            f'{tmp_path} is not a git work tree, but the skip condition says it is. Every guard '
            f'in this class would then run outside a checkout and fail with git`s exit 128 -- the '
            f'CI-102 finding, unfixed.'
        )
        checkout = tmp_path / 'checkout'
        checkout.mkdir()
        subprocess.run(['git', 'init', '-q', str(checkout)], check=True, capture_output=True)
        assert outside_git_checkout_reason(checkout) is None, (
            f'{checkout} is a git work tree this test just created, and the skip condition does '
            f'not recognise it. It would then fire everywhere -- skipping the CI-093 guard on all '
            f'four CI legs and reporting nothing but a green run, which is worse than the failure '
            f'it replaced because it is silent.'
        )
        assert outside_git_checkout_reason(tmp_path / 'nope') is not None, (
            'a path that does not exist must yield a reason rather than raising out of the helper.'
        )
        # And the same discrimination against the environment this actually runs in -- guarded by
        # the environment rather than asserted of it, because an unpacked sdist has no `.git` and
        # asserting REPO_ROOT is a checkout would re-create the very failure CI-102 removed.
        if (REPO_ROOT / '.git').exists():
            assert outside_git_checkout_reason(REPO_ROOT) is None, (
                f'{REPO_ROOT} carries a .git entry, so it IS a checkout, but the skip condition '
                f'says otherwise -- the guards above are skipping right here and asserting nothing.'
            )


@pytest.mark.unit
class TestTheWorkflowParserReadsCommandsNotLabels:
    """The parser is the load-bearing half of every CI assertion below, so it is tested directly.

    Each case here is a regression measured against a previous implementation. The first family
    came from the PR #14 reviewer: a parser that partitioned on ``run:`` and fell back to the whole
    line read a step *named* ``Run pytest`` as an invocation, and the resulting failure named a
    command that does not exist.

    The second family is CI-102's first finding, from the other direction -- the reader was
    single-physical-line, so a ``run: |`` block whose command wrapped across a backslash was
    reported as an invocation MISSING its flags. That is the dangerous direction for a guard: not a
    hole, a false alarm on correct configuration, which gets the guard edited until it stops.
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
        assert workflow_pytest_commands(document) == [['pytest', '-m', 'not integration']]

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
        assert workflow_pytest_commands(document) == [['pytest', '-m', 'not integration', '--cov-fail-under=90']]

    def test_a_wrapped_block_scalar_command_keeps_its_flags(self) -> None:
        """CI-102, F5 in reverse: this exact reformat was measured to redden two correct guards.

        The step below is byte-for-byte equivalent to the one ``ci.yml`` runs today; only its
        typography differs. A reader that stops at the backslash reports ``--cov`` as absent and
        sends someone to fix a workflow that is already right.
        """
        document = '\n'.join(
            [
                '      - name: Test (pytest, offline suite, 90% floor)',
                '        run: |',
                '          uv run pytest -m "not integration" \\',
                '            --cov=src/castiron --cov-report=term-missing --cov-fail-under=90',
            ]
        )
        assert workflow_pytest_commands(document) == [
            [
                'pytest',
                '-m',
                'not integration',
                '--cov=src/castiron',
                '--cov-report=term-missing',
                '--cov-fail-under=90',
            ]
        ]

    def test_a_comment_inside_a_block_scalar_is_not_a_command(self) -> None:
        document = '\n'.join(
            [
                '      - name: Test',
                '        run: |',
                '          # remember to run pytest -x locally when this fails',
                '          uv run pytest -m "not integration"',
            ]
        )
        assert workflow_pytest_commands(document) == [['pytest', '-m', 'not integration']]

    def test_a_block_scalar_at_end_of_file_is_still_read(self) -> None:
        # The block is flushed after the loop as well as when a dedent ends it; without that, the
        # LAST step in a workflow -- which is where the test step tends to live -- would vanish.
        document = '      - name: Test\n        run: |\n          uv run pytest -m "not integration"\n'
        assert workflow_pytest_commands(document) == [['pytest', '-m', 'not integration']]

    def test_two_commands_on_one_block_are_two_invocations(self) -> None:
        document = '      - run: |\n        uv run pytest -m unit && uv run pytest -m "not integration"\n'
        assert workflow_pytest_commands(document) == [['pytest', '-m', 'unit'], ['pytest', '-m', 'not integration']]

    def test_a_make_delegation_is_visible_to_the_failure_message(self) -> None:
        # F8: `run: make test` is the alternative PR #14 rejected, and the likeliest reason a
        # reader is looking at "CI invokes no pytest". The message names it because this can see it.
        makefile = 'test: ## Run all tests\n\t@uv run pytest -m "not integration"\n'
        workflow = '      - name: Test\n        run: make test\n'
        assert workflow_pytest_commands(workflow) == []
        assert workflow_make_delegations(workflow, makefile) == ['test']

    def test_a_make_target_that_runs_no_pytest_is_not_a_delegation(self) -> None:
        # Otherwise every `run: make build` in any workflow would be reported as the missing
        # test step, and the message would misdirect in the other direction.
        makefile = 'build: ## Build\n\t@uv build\n'
        assert workflow_make_delegations('      - run: make build\n', makefile) == []

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

    ⚠ The Makefile half of this used to hold by ENUMERATION: a three-name ``WHOLE_SUITE_TARGETS``
    tuple, against a CI half that already discovered every pytest invocation in ``ci.yml``. That
    is ``CI-086``'s own defect, committed by the guard written against it -- offline by absence of
    something rather than by construction -- and it was not theoretical: appending a ``smoke:``
    target that ran the whole suite with no marker was measured to leave all seven assertions in
    this class green. Targets are now **discovered** (``make_targets_running_pytest``), and the
    only names written down are ``MARKER_SCOPED_TARGETS`` -- exceptions, each of which must itself
    be a real pytest-running target -- and ``DISCOVERY_FLOOR``, which is a lower bound on the
    reader and can therefore only fail closed.
    """

    def test_ci_runs_the_offline_suite_with_the_floor(self) -> None:
        commands = ci_pytest_commands()
        # Anti-vacuity (CI-083): an empty list would pass every assertion below it. The message is
        # built from what the workflow actually does, because "did the test step move or get
        # renamed?" pointed at the wrong thing in the likeliest case -- CI-102, F8.
        delegated = workflow_make_delegations(ci_workflow_text(), makefile_text())
        cause = (
            f'It now reaches the suite through `make {delegated[0]}` instead. That is the '
            f'alternative PR #14 deliberately rejected: this workflow should state the contract it '
            f'enforces, and `make {delegated[0]}` carries developer-iteration flags (-vv) that '
            f'must not reach CI by inheritance. Delegating also makes this guard vacuous in the '
            f'one direction it cannot recover -- it compares CI against {MAKE_NAME}, and a '
            f'{MAKE_NAME} compared against itself agrees always. Restore the open-coded step.'
            if delegated
            else (
                'No `run:` step delegates to a pytest-running Make target either, so the step was '
                'renamed, moved, commented out, or is now spelled in a way `pytest_invocations` '
                'does not read as a pytest run (`python -m pytest` reads as `python`, by design -- '
                'see that function).'
            )
        )
        assert commands, f'{CI_NAME} invokes pytest nowhere. {cause}'
        for command in commands:
            flags = flags_from_tokens(command)
            for flag, expected in LOAD_BEARING_FLAGS.items():
                assert flags.get(flag) == expected, (
                    f'{CI_NAME} runs pytest without `{flag} {expected}`, found {flags.get(flag)!r}:\n'
                    f'  {" ".join(command)}\n'
                    f'`-m "not integration"` is what keeps CI offline BY CONSTRUCTION rather than by '
                    f'`CASTIRON_TEST_POSTGREST_URL` happening to be unset in the runner; `--cov` and '
                    f'`--cov-fail-under` are the 90% floor (Hard Rule #8). Restore it in {CI_NAME}, '
                    f'or update LOAD_BEARING_FLAGS here and in {MAKE_NAME} deliberately.'
                )

    def test_ci_and_make_test_agree_on_the_load_bearing_flags(self) -> None:
        ci_commands = ci_pytest_commands()
        make_commands = make_pytest_commands(makefile_text(), 'test')
        assert ci_commands and make_commands, (
            f'expected a pytest invocation in both {CI_NAME} and the `test` target of {MAKE_NAME}; '
            f'found {len(ci_commands)} and {len(make_commands)}'
        )
        ci_flags = flags_from_tokens(ci_commands[0])
        make_flags = flags_from_tokens(make_commands[0])
        for flag in LOAD_BEARING_FLAGS:
            assert ci_flags.get(flag) == make_flags.get(flag), (
                f'{CI_NAME} and the `test` target of {MAKE_NAME} disagree on `{flag}`: '
                f'{ci_flags.get(flag)!r} in CI vs {make_flags.get(flag)!r} in {MAKE_NAME}. '
                f'These two encode ONE invariant -- the offline suite with the 90% floor -- and '
                f'whichever one you just changed, change the other. Only these flags are compared; '
                f'the rest of each command line is free to differ (CI has no `-vv`, by design).'
            )

    def test_every_make_target_that_runs_the_whole_suite_excludes_the_live_source_suite(self) -> None:
        text = makefile_text()
        discovered = make_targets_running_pytest(text)
        # Anti-vacuity (CI-083). DISCOVERY_FLOOR is a lower bound on the READER, never the scope
        # of the guard: it goes red when the parse stops finding targets it has always found, and
        # it is silent about a target added tomorrow -- which the loop below covers regardless.
        assert DISCOVERY_FLOOR <= set(discovered), (
            f'`make_targets_running_pytest` found {discovered}, which is missing '
            f'{sorted(DISCOVERY_FLOOR - set(discovered))}. Those targets do run pytest, so the '
            f'reader has gone blind and this guard is now covering less than it reports. Fix the '
            f'reader; do not shrink DISCOVERY_FLOOR to match it.'
        )
        for target in discovered:
            if target in MARKER_SCOPED_TARGETS:
                continue
            for command in make_pytest_commands(text, target):
                assert flags_from_tokens(command).get('-m') == LOAD_BEARING_FLAGS['-m'], (
                    f'the `{target}` target of {MAKE_NAME} runs the whole suite without '
                    f'`-m "{LOAD_BEARING_FLAGS["-m"]}"`:\n  {" ".join(command)}\n'
                    f'A developer with CASTIRON_TEST_POSTGREST_URL exported would then get a '
                    f'`make validate` that opens sockets, falsifying the offline guarantee this file, '
                    f'CONTRIBUTING.md, tests/integration/README.md and tests/integration/conftest.py '
                    f'all sell. A target that deliberately selects ONE half of the suite belongs in '
                    f'MARKER_SCOPED_TARGETS -- and that is a decision, which is the whole point of '
                    f'discovering targets rather than listing them.'
                )

    def test_every_marker_scoped_exception_is_a_real_pytest_target(self) -> None:
        """A stale exemption is invisible: it excuses a target that no longer exists, silently.

        The failure it prevents is a rename. If ``test-unit`` became ``unit``, the exemption would
        go on excusing a name nothing matches while the real target fell into the whole-suite loop
        above and failed there instead -- a red test pointing at the wrong line. Asserting the
        exemptions are live turns that into a message about the exemption.
        """
        discovered = set(make_targets_running_pytest(makefile_text()))
        stale = [target for target in MARKER_SCOPED_TARGETS if target not in discovered]
        assert not stale, (
            f'MARKER_SCOPED_TARGETS excuses {stale} from the whole-suite marker assertion, but no '
            f'such target of {MAKE_NAME} runs pytest. An exemption for a target that does not '
            f'exist is not enforcement of anything -- it is a name that will silently excuse '
            f'whatever is created with it next. Rename it here, or delete it.'
        )

    def test_the_discovery_can_still_see_a_target_the_old_whitelist_missed(self) -> None:
        """Positive control (CI-072) for CI-102's second finding, using the exact repro.

        ``WHOLE_SUITE_TARGETS = ('test', 'coverage', 'test-matrix')`` was measured against a real
        ``smoke:`` target appended to the real ``Makefile``: all seven guards in this class stayed
        green, because a whitelist cannot report a name nobody added to it. The synthetic Makefile
        below is that regression, and it is asserted to be *found* -- otherwise the loop above has
        merely swapped one enumeration for another.
        """
        makefile = '\n'.join(
            [
                'test: ## Run all tests',
                '\t@uv run pytest -m "not integration" --cov=src/castiron --cov-fail-under=90',
                '',
                'test-unit: ## Run only unit tests',
                '\t@uv run pytest -m unit tests/unit/',
                '',
                'smoke: ## Quick whole-suite smoke run',
                '\t@uv run pytest -q --cov=src/castiron',
                '',
                'build: ## Build sdist + wheel',
                '\t@uv build',
                '',
            ]
        )
        assert make_targets_running_pytest(makefile) == ['test', 'test-unit', 'smoke'], (
            f'discovery returned {make_targets_running_pytest(makefile)}. `smoke` is the CI-102 '
            f'regression reproduced verbatim; if it is absent this guard has gone back to holding '
            f'by enumeration, and `build` being present would mean the reader calls any recipe a '
            f'pytest run.'
        )
        unmarked = [
            target
            for target in make_targets_running_pytest(makefile)
            if target not in MARKER_SCOPED_TARGETS
            for command in make_pytest_commands(makefile, target)
            if flags_from_tokens(command).get('-m') != LOAD_BEARING_FLAGS['-m']
        ]
        assert unmarked == ['smoke'], (
            f'the whole-suite assertion would report {unmarked} on the trapped Makefile. It must '
            f'report exactly `smoke`: `test` carries the marker, and `test-unit` is exempt by '
            f'MARKER_SCOPED_TARGETS rather than by having gone unnoticed.'
        )

    def test_a_recipe_comment_that_mentions_pytest_is_not_an_invocation(self) -> None:
        """CI-102's third finding: the two readers handled comments differently, and it showed.

        ``workflow_run_commands`` skipped ``#`` lines; the Makefile reader did not. This Makefile
        is comment-dense by design -- the ``test-matrix`` target alone carries twenty lines of
        rationale -- and a tab-indented comment inside a recipe was measured to turn two guards in
        this class red on prose, reporting that ``make test`` runs the whole suite without the
        marker when the recipe is exactly correct. A guard that cries wolf gets edited until it
        stops, which is the more expensive failure.
        """
        makefile = '\n'.join(
            [
                'test: ## Run all tests',
                '\t# when debugging one file, run pytest -x on it directly instead',
                '\t@uv run pytest -m "not integration" --cov=src/castiron',
                '',
            ]
        )
        assert make_pytest_commands(makefile, 'test') == [['pytest', '-m', 'not integration', '--cov=src/castiron']], (
            f'a recipe comment mentioning pytest was read as an invocation: {make_pytest_commands(makefile, "test")}'
        )


@pytest.mark.unit
class TestTheCoverageFloorIsOnEveryLegOfTheGate:
    """CI-102's first finding: ``CI-089``/``CI-088`` had no guard at all, and nobody could tell.

    ``Makefile`` argues at length that ``--cov-fail-under`` on every matrix leg is the only thing
    that can tell "everything passed" from "almost nothing ran" -- ``CI-083``'s partial-deselection
    hole, where ``184 passed, 1236 deselected`` exits 0 and *reads* like success. The flag sits on
    a shell **continuation line** of the ``test-matrix`` recipe, and every reader in this module
    was single-physical-line, so the guards saw the legs' ``-m`` marker and none of them ever saw
    the floor. Measured: deleting ``--cov-fail-under=90`` from both legs -- undoing ``CI-089``
    entirely, and returning three of the four gate legs to being blind to partial deselection --
    left all 103 assertions in this file green.

    Which makes this the module's own thesis turned on itself. The file exists because "remember to
    update the config" is not a mechanism; the argument for the floor being on every leg was
    written into the ``Makefile`` as a comment, and a comment is not a mechanism either.

    Asserted over the GATE targets rather than over every whole-suite target, because that is the
    real scope: ``coverage`` renders an HTML report and deliberately carries no floor. And walked
    through ``gate_invocations`` -- transitively, per invocation -- so it is a claim about what
    ``make validate`` executes rather than about a target name.
    """

    @pytest.mark.parametrize('target', GATE_TARGETS)
    def test_every_pytest_the_gate_runs_carries_the_coverage_floor(self, target: str) -> None:
        assert COVERAGE_FLOOR_FLAGS, (
            'COVERAGE_FLOOR_FLAGS is empty, so the loop below asserts nothing. It is derived from '
            'LOAD_BEARING_FLAGS by a `--cov` prefix; if those flags were renamed, rename this.'
        )
        pytests = [pair for pair in gate_invocations(target) if pair[1][0] == GATE_PYTEST]
        assert pytests, (
            f'`make {target}` runs no pytest at all, so this assertion is vacuous. Did the test leg leave the gate?'
        )
        for prerequisite, invocation in pytests:
            flags = flags_from_tokens(invocation)
            for flag, expected in COVERAGE_FLOOR_FLAGS.items():
                assert flags.get(flag) == expected, (
                    f'`make {target}` runs pytest without `{flag}={expected}` through its '
                    f'`{prerequisite}` prerequisite, found {flags.get(flag)!r}:\n'
                    f'  {" ".join(invocation)}\n'
                    f'The 90% floor is Hard Rule #8, and on the matrix it is also the only thing '
                    f'that can tell "everything passed" from "almost nothing ran": partial '
                    f'deselection leaves too few tests to trip pytest`s exit 5, so the leg reports '
                    f'"184 passed, 1236 deselected" and exits 0 (CI-083, measured). CI already runs '
                    f'the floor on all four legs -- dropping it here makes the gate weaker than the '
                    f'CI after it, which is CI-081.'
                )

    def test_the_matrix_target_is_still_two_legs_to_this_walk(self) -> None:
        """Anti-vacuity with teeth: the guard above must be seen to inspect BOTH matrix legs.

        ``CI-089``'s whole subject is that the floor was on the final 3.12 leg only, so a walk that
        reached one leg would pass on exactly the configuration the row exists to forbid.
        """
        legs = [pair for pair in gate_invocations(VALIDATE_TARGET) if pair[1][0] == GATE_PYTEST]
        assert len(legs) == 2, (
            f'`make {VALIDATE_TARGET}` parses to {len(legs)} pytest invocation(s), not the two legs '
            f'of the `test-matrix` if/else. With one, the floor could be missing from the other leg '
            f'and this guard would report nothing -- which is `CI-089` exactly.'
        )
        assert {leg[0] for leg in legs} == {'test-matrix'}, (
            f'the pytest legs came from {sorted({leg[0] for leg in legs})}, not from `test-matrix`.'
        )

    def test_this_guard_can_still_see_a_leg_that_lost_the_floor(self) -> None:
        """Positive control (CI-072), reproducing the pre-CI-089 Makefile exactly.

        Two legs, the floor on the 3.12 one only, split across a continuation -- the shape that
        was measured to leave this whole module green. The control asserts both that the reader
        JOINS the continuation (otherwise it sees no floor anywhere and would ``fail`` for the
        wrong reason) and that the unfloored leg is the one reported.
        """
        makefile = '\n'.join(
            [
                'test-matrix: ## Run the suite on every CI interpreter',
                '\t@set -e; for V in 3.10 3.12; do \\',
                '\t\tif [ "$$V" = "3.12" ]; then \\',
                '\t\t\tuv run --python "$$V" pytest -q -m "not integration" \\',
                '\t\t\t\t--cov=src/castiron --cov-fail-under=90; \\',
                '\t\telse \\',
                '\t\t\tuv run --python "$$V" pytest -q -m "not integration" \\',
                '\t\t\t\t--cov=src/castiron; \\',
                '\t\tfi; \\',
                '\tdone',
                '',
            ]
        )
        legs = make_pytest_commands(makefile, 'test-matrix')
        assert len(legs) == 2, f'the control parsed {len(legs)} leg(s), not two: {legs}'
        floored = [flags_from_tokens(leg).get('--cov-fail-under') for leg in legs]
        assert floored == ['90', None], (
            f'the control read the floors as {floored}, expected the 3.12 leg to carry `90` and '
            f'the other to carry none. `[90, 90]` would mean the reader is inventing the flag; '
            f'`[None, None]` would mean it still cannot follow a continuation, and this guard '
            f'would then be red on the CORRECT Makefile rather than on the broken one.'
        )


@pytest.mark.unit
class TestTheRecipeParserReadsProgramsNotSyntax:
    """The parser is the load-bearing half of the CI-136 guard, so it is tested on its own.

    Every case here is a shape the real ``Makefile`` already contains, and each is a way a naive
    reader lies about what the gate runs: a ``for`` header that looks like four programs, an
    ``if``/``else`` that hides one of two ``pytest`` calls behind the other, a line continuation
    that turns ``--cov=src/castiron`` into a program name, and a ``uv run`` wrapper that answers
    ``uv`` for every tool in the tree.
    """

    def test_the_uv_wrapper_is_peeled_off_the_program(self) -> None:
        assert shell_invocations('@uv run ruff check .') == [['ruff', 'check', '.']]

    def test_a_value_taking_uv_flag_does_not_become_the_program(self) -> None:
        # `uv run --python 3.13 pytest` runs pytest, not 3.13 -- and `3.13` on the allowlist is a
        # hole shaped exactly like the tool it stands in front of.
        assert shell_invocations('uv run --python 3.13 pytest -q') == [['pytest', '-q']]

    def test_a_uv_run_with_no_program_fails_closed(self) -> None:
        # No program to find, so the argv is returned whole and `uv` -- which is on no allowlist --
        # is what gets reported. The alternative, guessing, is what an allowlist exists to prevent.
        assert shell_invocations('uv run --python') == [['uv', 'run', '--python']]

    def test_a_for_header_is_a_word_list_not_four_programs(self) -> None:
        command = 'for V in 3.10 3.11 3.13 3.12; do uv run mypy src --python-version "$$V"; done'
        assert shell_invocations(command) == [['mypy', 'src', '--python-version', '$$V']]

    def test_both_branches_of_an_if_are_separate_invocations(self) -> None:
        # The reason the guard is per-invocation: with one flag scan over the whole line, the
        # `then` leg's marker would answer for the `else` leg that lost it.
        command = 'if [ "$$V" = "3.12" ]; then uv run pytest -m "not integration"; else uv run pytest -q; fi'
        assert shell_invocations(command) == [
            ['[', '$$V', '=', '3.12', ']'],
            ['pytest', '-m', 'not integration'],
            ['pytest', '-q'],
        ]

    def test_an_assignment_prefix_is_not_a_program(self) -> None:
        assert shell_invocations('CASTIRON_TEST_POSTGREST_URL=x uv run pytest') == [['pytest']]

    def test_a_make_variable_is_not_split_into_a_program(self) -> None:
        # `$(MAKEFILE_LIST)` is one token only because `(` and `)` are kept OUT of the separator
        # set; with shlex's default punctuation it becomes `$`, `(`, `MAKEFILE_LIST`, `)` and the
        # variable name lands in command position.
        assert shell_invocations('@grep -E x $(MAKEFILE_LIST)') == [['grep', '-E', 'x', '$(MAKEFILE_LIST)']]

    def test_a_continued_line_is_one_command_not_two(self) -> None:
        makefile = 't:\n\t@uv run pytest -m "not integration" \\\n\t\t--cov=src/castiron --cov-fail-under=90\n'
        assert joined_recipe(makefile, 't') == [
            '@uv run pytest -m "not integration" --cov=src/castiron --cov-fail-under=90'
        ]

    def test_prerequisites_are_followed_through_an_intermediate_target(self) -> None:
        makefile = 'validate: lint stage-two\n\nstage-two: fetch\n\nfetch:\n\t@curl -sSf https://example.test\n'
        assert transitive_prerequisites(makefile, 'validate') == ['lint', 'stage-two', 'fetch']

    def test_a_prerequisite_cycle_terminates(self) -> None:
        assert transitive_prerequisites('a: b\n\nb: a\n', 'a') == ['b', 'a']

    def test_the_real_matrix_target_runs_exactly_two_pytests(self) -> None:
        # Anti-vacuity for the class below: it iterates these invocations, and a parser that found
        # one (or none) would quietly narrow every assertion that follows.
        text = MAKEFILE.read_text(encoding='utf-8')
        programs = [
            invocation[0] for command in joined_recipe(text, 'test-matrix') for invocation in shell_invocations(command)
        ]
        assert programs.count(GATE_PYTEST) == 2, (
            f'the `test-matrix` target of {MAKE_NAME} parses to {programs}, which is not the two '
            f'`{GATE_PYTEST}` legs its if/else holds. Did the recipe change shape, or did the parser stop '
            f'following it?'
        )


@pytest.mark.unit
class TestEveryStepOfThePrePushGateIsOffline:
    """CI-136 -- the offline invariant asserted over what the GATE runs, not over pytest alone.

    ``TestTheOfflineSuiteIsOfflineByConstruction`` above covers every *pytest* invocation, which
    was the whole of the gate right up until vulture joined it (CI-107, captain's ruling
    2026-08-08). ``vulture`` is harmless; being the FIRST non-pytest member is not, because it
    proved the guard was scoped to a tool rather than to the property. From that point the guard
    covered strictly less than the thing it is about -- the CI-081 shape, on the axis of "what
    counts as the gate" -- and nothing structural stopped the next prerequisite from opening a
    socket. A ``make validate`` that quietly needs the network is a gate that fails on an aeroplane
    and on a fresh contributor's machine, and passes on the one where it was written.

    So the property is asserted over ``make validate``'s own prerequisites, transitively, program
    by program: each is either ``pytest`` carrying ``-m "not integration"``, or a name on an
    explicit allowlist. It fails CLOSED -- an unrecognised program is a failure, not a shrug --
    which is what makes adding one a decision rather than an accident.
    """

    @pytest.mark.parametrize('target', GATE_TARGETS)
    def test_every_program_the_gate_runs_is_offline_by_construction(self, target: str) -> None:
        allowed = OFFLINE_GATE_TOOLS | INERT_SHELL_BUILTINS | {GATE_PYTEST}
        for prerequisite, invocation in gate_invocations(target):
            assert invocation[0] in allowed, (
                f'`make {target}` reaches `{invocation[0]}` through its `{prerequisite}` '
                f'prerequisite:\n  {" ".join(invocation)}\n'
                f'which is on none of this module`s offline allowlists (OFFLINE_GATE_TOOLS, '
                f'INERT_SHELL_BUILTINS, GATE_PYTEST). '
                f'The pre-push gate must run offline BY CONSTRUCTION -- CONTRIBUTING.md, '
                f'tests/integration/README.md and {MAKE_NAME} all promise a network-free '
                f'`make {target}`, and a gate that needs a socket fails on a fresh contributor and '
                f'passes on the machine it was written on. If this program genuinely cannot reach '
                f'a network, add it to OFFLINE_GATE_TOOLS with a note saying why; if it can, it '
                f'does not belong in the gate.'
            )

    @pytest.mark.parametrize('target', GATE_TARGETS)
    def test_every_pytest_the_gate_runs_excludes_the_live_source_suite(self, target: str) -> None:
        pytests = [pair for pair in gate_invocations(target) if pair[1][0] == GATE_PYTEST]
        assert pytests, (
            f'`make {target}` runs no pytest at all, so this assertion is vacuous. Did the test leg leave the gate?'
        )
        for prerequisite, invocation in pytests:
            assert flags_from_tokens(invocation).get('-m') == LOAD_BEARING_FLAGS['-m'], (
                f'`make {target}` runs pytest without `-m "{LOAD_BEARING_FLAGS["-m"]}"` through its '
                f'`{prerequisite}` prerequisite:\n  {" ".join(invocation)}\n'
                f'The live-source suite under tests/integration/ skips itself when '
                f'CASTIRON_TEST_POSTGREST_URL is unset, so without the marker the gate is offline '
                f'by ABSENCE OF CONFIGURATION -- a developer who exports it gets a `make {target}` '
                f'that opens sockets, and nothing says so. Note this is checked per INVOCATION: '
                f'`test-matrix` holds two, and the marker on one leg must not answer for the other.'
            )

    def test_the_allowlist_is_exactly_what_the_gate_runs(self) -> None:
        """A stale entry pre-authorises a tool nothing runs yet, which is how an allowlist rots.

        The ``[tool.vulture] ignore_names`` precedent, in the other direction: that allowlist is
        asserted to be no WIDER than today's findings for exactly this reason. An entry added
        speculatively -- or left behind by a tool that has since left the gate -- means the next
        prerequisite to invoke that name passes silently, and the guard reports nothing at the one
        moment it was written for.
        """
        run = {invocation[0] for _, invocation in gate_invocations(VALIDATE_TARGET)}
        assert OFFLINE_GATE_TOOLS <= run, (
            f'OFFLINE_GATE_TOOLS allows {sorted(OFFLINE_GATE_TOOLS - run)}, which `make '
            f'{VALIDATE_TARGET}` does not run. Delete the entry, or -- if the tool was just removed '
            f'from the gate -- say so in the commit, because until then it is a name any future '
            f'prerequisite may invoke unchallenged.'
        )

    def test_these_guards_can_still_see_a_prerequisite_that_opens_a_socket(self) -> None:
        """Positive control (CI-072): every assertion above is a membership over a parsed list.

        That shape passes loudly when the parser beneath it stops working -- a
        ``shell_invocations`` that returned ``[]`` would satisfy every loop above forever, and a
        ``transitive_prerequisites`` that stopped following the tree would satisfy them while
        inspecting one target of four. So the failure is reproduced here against a Makefile that
        really does reach the network, THROUGH an intermediate target, and shown to be detected.
        """
        trapped = '\n'.join(
            [
                'validate: lint refresh-corpus ## The pre-push gate',
                '',
                'lint: ## Sort imports + lint with ruff',
                '\t@uv run ruff check .',
                '',
                'refresh-corpus: fetch-schema ## rebuild the corpus',
                '\t@uv run python -m tests.unit.corpus.regenerate',
                '',
                'fetch-schema: ## pull the live schema',
                '\t@uv run --python 3.12 curl -sSf "$$CASTIRON_TEST_POSTGREST_URL" -o schema.json',
            ]
        )
        reached = transitive_prerequisites(trapped, VALIDATE_TARGET)
        assert reached == ['lint', 'refresh-corpus', 'fetch-schema'], (
            f'the control walked to {reached}, so the network-capable target is not even reached '
            f'and test_every_program_the_gate_runs_is_offline_by_construction cannot be shown to '
            f'fail. The whole point of following prerequisites transitively is that `fetch-schema` '
            f'is two hops from the gate.'
        )
        programs = {
            invocation[0]
            for name in reached
            for command in joined_recipe(trapped, name)
            for invocation in shell_invocations(command)
        }
        allowed = OFFLINE_GATE_TOOLS | INERT_SHELL_BUILTINS | {GATE_PYTEST}
        assert 'curl' in programs and not programs <= allowed, (
            f'the control parsed {sorted(programs)} and did not surface a program outside the '
            f'allowlist, so the guard above cannot be shown to fail. Most likely `unwrap_uv_run` '
            f'stopped peeling `uv run --python 3.12 curl` and every tool now reports as `uv`.'
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

    def test_a_recipe_stops_before_the_next_target(self) -> None:
        # The hazard `make_recipe` exists to avoid: reading on past the blank line and crediting
        # `vulture` with the *next* target's commands, so a hollowed-out target still looks alive.
        makefile = '\n'.join(
            [
                'vulture: ## Find unused code',
                '\t@uv run vulture src/',
                '',
                'typecheck: ## Type-check',
                '\t@uv run mypy src',
            ]
        )
        assert make_recipe(makefile, 'vulture') == ['@uv run vulture src/']
        assert make_recipe(makefile, 'typecheck') == ['@uv run mypy src']

    def test_an_aggregate_target_has_an_empty_recipe(self) -> None:
        # `validate` is prerequisites only. An empty recipe must not read as an absent target,
        # which is why the gate guards below assert on prerequisites and recipe separately.
        assert make_recipe('validate: lint vulture\n\nvulture:\n\t@uv run vulture src/\n', 'validate') == []
        assert make_recipe('vulture:\n\t@uv run vulture src/\n', 'nonexistent') == []


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

    @pytest.mark.parametrize(('key', 'live_table'), sorted(MISPLACED_SEMANTIC_RELEASE_KEYS.items()))
    def test_no_bare_key_belongs_to_a_sub_table_instead(self, key: str, live_table: str) -> None:
        table = pyproject_table('tool', 'semantic_release')
        assert key not in table, (
            f'{PYPROJECT_NAME}: `[tool.semantic_release].{key}` is not a field of `RawConfig` at '
            f'this level -- on either PSR major this repo runs -- so `extra="ignore"` drops it '
            f'without an error or a warning. It is worse than a stale key: it sits next to '
            f'`version_toml` and `version_variables`, which ARE read, so it reads as the line that '
            f'configures the changelog while configuring nothing.\n'
            f'The live spelling is `[{live_table}].{key}`, whose default is already the value this '
            f'line was setting -- which is why CI-119 deleted it rather than moving it. Moving it '
            f'one table up instead, to `[tool.semantic_release.changelog].{key}`, would land on a '
            f'field PSR itself marks deprecated and slates for removal.'
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
        # 4. And a restored MISPLACED key -- same mechanism, different species (CI-119). Spelled
        #    out separately because this one is a bare key sharing its name with a real field in a
        #    nested table, which is exactly the shape a lookup that walked into sub-tables would
        #    report as "present" no matter where it sat.
        for key in MISPLACED_SEMANTIC_RELEASE_KEYS:
            misplaced = tomllib.loads(f'[tool.semantic_release]\n{key} = "CHANGELOG.md"\n')
            assert key in misplaced['tool']['semantic_release'], (
                f'a table whose only key is `{key}` does not read as containing it, so '
                f'test_no_bare_key_belongs_to_a_sub_table_instead is theatre.'
            )
            nested = tomllib.loads(f'[tool.semantic_release.changelog]\n{key} = "CHANGELOG.md"\n')
            assert key not in nested['tool']['semantic_release'], (
                f'`{key}` inside `[tool.semantic_release.changelog]` reads as a bare key of '
                f'`[tool.semantic_release]`, so the guard would go red on config that is fine.'
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

    def test_nothing_in_the_release_workflow_writes_the_job_scoped_env_file(self) -> None:
        """The CHANNEL, not the producer -- ``setup-uv`` was one writer of ``$GITHUB_ENV``, not the category.

        Carried over from the CI-123 guards when the TestPyPI rehearsal was removed (2026-08-08):
        the rehearsal is gone, but the mechanism this asserts is CI-121's, and it outlives the mode
        it happened to be written under. Anything writing that file -- another action, or a
        ``run:`` step with an ``echo ... >> "$GITHUB_ENV"`` -- inherits into every later step,
        including the python-semantic-release container, whose ``build_command`` receives a fixed
        whitelist that includes ``VIRTUAL_ENV``.
        """
        writers = [line.strip() for line in non_comment_lines(release_workflow_text()) if JOB_SCOPED_ENV_FILE in line]
        assert not writers, (
            f'{RELEASE_NAME} writes `${JOB_SCOPED_ENV_FILE}`:\n  ' + '\n  '.join(writers) + '\n'
            f'That file is JOB-scoped: every later step inherits it, including the '
            f'python-semantic-release CONTAINER action. That is exactly how the 0.1.0 release died '
            f'(CI-121) -- `{SETUP_UV_ACTION}` exported a venv path through it that resolves only '
            f'on the runner.'
        )
        # Anti-vacuity (CI-072), inline because the assertion is an absence over a filter: the
        # filter must SEE a plain write, and must NOT see the prose. release.yml names
        # `$GITHUB_ENV` in the comment block that explains CI-121, so both halves are live.
        write = f'        run: echo X >> "${JOB_SCOPED_ENV_FILE}"'
        assert [line for line in non_comment_lines(f'{write}\n') if JOB_SCOPED_ENV_FILE in line], (
            f'non_comment_lines cannot see `{write.strip()}`, so this guard would stay green with '
            f'the CI-121 channel reopened.'
        )
        assert not [line for line in non_comment_lines(f'      # {write.strip()}\n') if JOB_SCOPED_ENV_FILE in line], (
            f'a COMMENT naming `${JOB_SCOPED_ENV_FILE}` is read as a write, so this guard would '
            f'fail permanently on the documentation that explains why it exists.'
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
    """``workflow_steps`` is the load-bearing half of the release-path guards, so it is tested directly.

    Mirrors ``TestTheWorkflowParserReadsCommandsNotLabels``. Each case is a way the real
    ``release.yml`` could make a naive parser lie: it carries comment blocks that name
    ``astral-sh/setup-uv@v5``, quote its ``python-version:`` input and quote the publish action's
    own ``${{ inputs.repository-url || inputs.repository_url }}`` -- every one of them a string
    some assertion here or in the next class reads. A parser that read comments would fail
    permanently on the documentation that explains why the guards exist.

    ⚠ Two of the counts the next class depends on are now **zero or one** (one publish action, no
    ``run:`` steps), and an absence proves nothing about a reader that stopped reading. So the
    cases below show the parser *seeing* each of those shapes on a synthetic document first.
    """

    def test_a_commented_out_step_is_not_a_step(self) -> None:
        document = '\n'.join(
            [
                '      # - name: Publish to PyPI',
                "      #   if: ${{ steps.release.outputs.released == 'true' }}",
                '      #   uses: pypa/gh-action-pypi-publish@release/v1',
                '      #   with:',
                '      #     repository-url: https://test.pypi.org/legacy/',
            ]
        )
        assert workflow_steps(document) == []

    def test_a_block_scalar_run_is_captured_whole_and_the_next_step_still_parses(self) -> None:
        # Anti-vacuity (CI-083) for `test_the_real_release_workflow_parses_to_the_steps_these_guards_iterate`,
        # which asserts the job has NO `run:` steps: the parser has to be shown reading one at all,
        # block-scalar body included, or that assertion is an empty list compared with an empty list.
        document = '\n'.join(
            [
                '      - name: Build',
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
        assert steps_running_a_command(steps) == [steps[0]]

    def test_with_children_are_namespaced_and_do_not_leak_into_the_next_step(self) -> None:
        document = '\n'.join(
            [
                '      - name: Publish somewhere else',
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
            'a `with:` child leaked from one step into the next, so a step that names an index '
            'would hand it to the step after it -- and the guard that keeps the real publish step '
            'pointed at real PyPI could never fail.'
        )

    def test_a_step_with_no_condition_normalizes_to_the_empty_string(self) -> None:
        # The version step is genuinely ungated, so the empty string is a value the next class
        # compares against rather than a sentinel for "not parsed".
        step = workflow_steps('      - name: Ungated\n        uses: actions/checkout@v4\n')[0]
        assert 'if' not in step
        assert normalized_condition(step) == ''

    def test_a_condition_is_normalized_free_of_its_expression_wrapper(self) -> None:
        step = workflow_steps("      - name: Publish\n        if: ${{ steps.release.outputs.released == 'true' }}\n")[0]
        assert normalized_condition(step) == RELEASED_GUARD

    def test_the_real_release_workflow_parses_to_the_steps_these_guards_iterate(self) -> None:
        """Anti-vacuity (CI-083) for every test in the next class -- they all iterate these sets.

        Asserted as exact counts rather than "at least one": a parse that returned an empty list,
        or one that split the comment blocks into phantom steps, would leave the assertions below
        passing over the wrong number of things while reading as green.
        """
        steps = release_steps()
        assert len(steps) == RELEASE_STEP_COUNT, (
            f'{RELEASE_NAME} parses to {len(steps)} steps, not {RELEASE_STEP_COUNT}: '
            f'{[step.get("name") or step.get("uses") for step in steps]}.\n'
            f'Either a step was added or removed -- in which case decide deliberately what gates '
            f'it and update RELEASE_STEP_COUNT -- or workflow_steps stopped reading the file, '
            f'which would make every assertion in the next class pass over an empty set.'
        )
        assert len(steps_using(steps, PSR_ACTION)) == 1
        assert len(steps_using(steps, PSR_PUBLISH_ACTION)) == 1
        assert len(steps_using(steps, PYPI_PUBLISH_ACTION)) == 1, (
            f'{RELEASE_NAME} no longer has exactly one {PYPI_PUBLISH_ACTION} step. A second one is '
            f'how a release quietly reaches another index -- the CI-123 TestPyPI rehearsal was '
            f'that shape -- and it is why these guards parse steps rather than call uses_action, '
            f'which returns on the first match.'
        )
        assert steps_running_a_command(steps) == [], (
            f'{RELEASE_NAME} has a `run:` step: '
            f'{[step.get("name") for step in steps_running_a_command(steps)]}.\n'
            f'"The release job has no `run:` steps" is part of the CI-121 reasoning -- a run step '
            f'is somewhere `${JOB_SCOPED_ENV_FILE}` can be written, and that file is job-scoped, so '
            f'it reaches the python-semantic-release container. Check a new one against that '
            f'class before adding it.'
        )


@pytest.mark.unit
class TestThePublishStepsFireOnlyOnARealRelease:
    """What survives the removal of the TestPyPI rehearsal (CI-123, removed 2026-08-08).

    The rehearsal added a ``rehearse`` input and an ``!inputs.rehearse`` conjunct to every real
    step. The captain removed the mode once it and two real releases (``v0.1.0``, ``v0.1.1``) had
    all run green -- but two invariants those conjuncts sat next to are not about the rehearsal at
    all, and both are still unreachable by running anything: a release run publishes to PyPI and
    spends a version number.

    * **Both publishing steps stay gated on a release having happened.** ``released`` is
      ``'true'`` only when python-semantic-release actually cut one; a no-op run sets it to
      ``'false'`` and the job still succeeds. A step that lost the guard would upload whatever
      ``dist/`` holds under a version PyPI has already accepted -- refused permanently, and only
      after the tag and the GitHub Release exist.
    * **The publish step names no index.** For ``pypa/gh-action-pypi-publish``, *unset is real
      PyPI*: ``repository-url`` carries no default and the deprecated ``repository_url`` alias
      defaults to the real endpoint, so any value there redirects a real release somewhere nobody
      can install it, with the run reporting success.

    ⚠ The version step is the one that must NOT be gated -- everything downstream reads its
    outputs, and a skipped step produces none.
    """

    def test_the_version_step_runs_on_every_dispatch(self) -> None:
        versions = steps_using(release_steps(), PSR_ACTION)
        assert len(versions) == 1, f'{RELEASE_NAME} has {len(versions)} {PSR_ACTION} steps, not 1.'
        assert normalized_condition(versions[0]) == '', (
            f'{RELEASE_NAME}: the {PSR_ACTION} step now carries `if: {versions[0].get("if", "")}`. '
            f'It is the step that DECIDES whether a release happens and every step after it reads '
            f'`steps.release.outputs.released` -- a skipped step produces no outputs, so that '
            f'output becomes the empty string, both publish steps skip, and the whole job reports '
            f'success having released nothing. Removing the rehearsal (CI-123) left this step '
            f'deliberately ungated; keep it that way.'
        )

    def test_both_publishing_steps_wait_for_a_release_to_have_happened(self) -> None:
        steps = release_steps()
        # Identified by action rather than by name, so renaming a step cannot silently empty the
        # set this iterates (CI-083).
        publishing = steps_using(steps, PYPI_PUBLISH_ACTION) + steps_using(steps, PSR_PUBLISH_ACTION)
        assert len(publishing) == 2, (
            f'expected 2 publishing steps in {RELEASE_NAME} (upload to PyPI, upload to the GitHub '
            f'Release); found {len(publishing)}.'
        )
        for step in publishing:
            assert RELEASED_GUARD in normalized_condition(step), (
                f'{RELEASE_NAME}: the publishing step {step.get("name") or step.get("uses")!r} has '
                f'the condition {step.get("if", "")!r}, which does not require '
                f"`steps.release.outputs.released == 'true'`.\n"
                f'python-semantic-release sets that output to `false` and EXITS ZERO when the '
                f'commits since the last tag cut no version. Without the guard the job would then '
                f'upload the artifacts already on PyPI under a version it has already accepted -- '
                f'which PyPI refuses permanently -- or attach them to a release that does not '
                f'exist.\n'
                f'If a conjunct is genuinely being added here, keep this one and update the test '
                f'deliberately.'
            )

    def test_the_publish_step_names_no_index_so_it_reaches_real_pypi(self) -> None:
        publishes = steps_using(release_steps(), PYPI_PUBLISH_ACTION)
        assert len(publishes) == 1, f'{RELEASE_NAME} has {len(publishes)} {PYPI_PUBLISH_ACTION} steps, not 1.'
        assert repository_url(publishes[0]) is None, (
            f'{RELEASE_NAME}: the publish step names the index '
            f'{repository_url(publishes[0])!r}.\n'
            f'⚠ UNSET IS REAL PyPI. The canonical kebab-case `repository-url` carries no default, '
            f'the deprecated `repository_url` alias defaults to `https://upload.pypi.org/legacy/`, '
            f'and the composite passes `${{{{ inputs.repository-url || inputs.repository_url }}}}`. '
            f'So ANY value here silently redirects a real release to another index -- a run that '
            f'goes green while publishing nothing anyone can install. That is not hypothetical: '
            f'the CI-123 rehearsal pointed exactly this action at test.pypi.org.'
        )

    def test_these_guards_can_still_see_the_shapes_they_forbid(self) -> None:
        """Positive control (CI-072): every assertion above is an absence, a count or a substring.

        That is the shape that passes loudly forever once the reader underneath it stops reading,
        so each forbidden shape is pushed back through the same helpers and asserted to be SEEN.
        """
        # 1. An ungated publish step -- the exact regression that dropping a conjunct could leave
        #    behind -- must be visible as ungated.
        ungated = workflow_steps('      - name: Publish\n        uses: pypa/gh-action-pypi-publish@release/v1\n')[0]
        assert RELEASED_GUARD not in normalized_condition(ungated), (
            'an ungated step reads as carrying the released guard, so the assertion that both '
            'publishing steps carry it could not fail.'
        )
        # 2. ...and a guarded one as guarded, or (1) is two empty strings agreeing with each other.
        guarded = workflow_steps(
            "      - name: Publish\n        if: ${{ steps.release.outputs.released == 'true' }}\n"
        )[0]
        assert RELEASED_GUARD in normalized_condition(guarded)
        # 3. A publish step carrying an index must be visible to the index check...
        redirected = workflow_steps(
            '\n'.join(
                [
                    '      - name: Publish to PyPI',
                    '        uses: pypa/gh-action-pypi-publish@release/v1',
                    '        with:',
                    '          repository-url: https://test.pypi.org/legacy/',
                ]
            )
        )[0]
        assert repository_url(redirected) == 'https://test.pypi.org/legacy/'
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
        # 4. ...and a commented-out step must not be read as configuration at all. release.yml
        #    quotes both spellings in prose immediately above the step this class guards.
        assert (
            workflow_steps(
                '      # - name: Publish to PyPI\n'
                '      #   uses: pypa/gh-action-pypi-publish@release/v1\n'
                '      #   with:\n'
                '      #     repository-url: https://test.pypi.org/legacy/\n'
            )
            == []
        )


@pytest.mark.unit
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


@pytest.mark.unit
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


@pytest.mark.unit
class TestTheDeadCodeCheckIsPartOfThePrePushGate:
    """CI-107, second half -- the allowlist made vulture *able* to pass; this makes it *run*.

    PR #31 fixed the check and stopped there, which left the CI-081 shape intact on a new axis:
    ``CLAUDE.md`` named ``uv run vulture src/`` as the project's dead-code check, ``make vulture``
    shipped it, and nothing executed it on the way to a push. A check that is documented but
    unenforced decays exactly like the check that could never pass -- silently, and only
    detectably by the finding it failed to report.

    The captain's ruling of 2026-08-08 put it in ``make validate``, and deliberately **nowhere
    else**: not in ``.pre-commit-config.yaml`` and not in ``.github/workflows/ci.yml``. That is a
    narrower coupling than the mypy hook's (see ``TestTheMypyHookAndTheMypyGateCannotDisagree``),
    so these assertions stay inside the Makefile rather than reaching across to the hooks.
    """

    def prerequisites(self, target: str) -> list[str]:
        """Return one gate target's prerequisites, asserting the target still exists."""
        found = make_prerequisites(MAKEFILE.read_text(encoding='utf-8'), target)
        # Anti-vacuity (CI-083): a renamed or deleted target yields [], and every membership
        # assertion below would then be checking `in []` -- red for the right reason, here.
        assert found, f'the `{target}` target of {MAKE_NAME} has no prerequisites -- was it renamed or removed?'
        return found

    def test_the_pre_push_gate_runs_the_dead_code_check(self) -> None:
        prerequisites = self.prerequisites(VALIDATE_TARGET)
        assert VULTURE_TARGET in prerequisites, (
            f'`make {VALIDATE_TARGET}` runs {prerequisites}, which does not include '
            f'`{VULTURE_TARGET}`. Dropping it returns the dead-code check to the state CI-107 was '
            f'filed about: named in the docs, shipped as a target, and executed by nothing on the '
            f'way to a push. If it is genuinely too noisy to gate on, that is a captain decision '
            f'and the docs claiming it must change with it -- do not just delete it here.'
        )

    def test_the_fast_gate_runs_it_too(self) -> None:
        """``validate-fast`` reduces ``validate`` along the interpreter axis and no other.

        vulture has no interpreter axis to reduce -- it is a static AST scan, so the
        single-interpreter version of it *is* the whole check (which is also why there is no
        ``vulture-matrix``). Omitting it here would make ``validate-fast`` differ from
        ``validate`` on the *check* axis as well, i.e. "fast" would quietly also mean "and does
        not look at dead code" -- and an iterating developer would first learn about a finding at
        push, which is the friction that gets a check deleted.
        """
        prerequisites = self.prerequisites(VALIDATE_FAST_TARGET)
        assert VULTURE_TARGET in prerequisites, (
            f'`make {VALIDATE_FAST_TARGET}` runs {prerequisites}, which does not include '
            f'`{VULTURE_TARGET}`, while `make {VALIDATE_TARGET}` does. The two targets are meant '
            f'to differ only in how many interpreters they cover; a check present in one and '
            f'absent from the other is a second difference, and an undocumented one.'
        )

    def test_it_runs_before_the_matrix_legs(self) -> None:
        """Make runs prerequisites left to right, so the cheap static checks must come first.

        This is asserted rather than left to the comment in the Makefile because it is the only
        part of that comment a reader cannot verify by eye at a glance -- and a sub-second check
        sequenced after a ~17s four-interpreter matrix reports its finding last, which is the same
        as reporting it late.
        """
        prerequisites = self.prerequisites(VALIDATE_TARGET)
        matrix_legs = [name for name in prerequisites if name.endswith('-matrix')]
        assert matrix_legs, (
            f'`make {VALIDATE_TARGET}` runs {prerequisites}, none of which is a `-matrix` leg, so '
            f'this ordering assertion is vacuous. Did the interpreter matrix leave the gate '
            f'(CI-082)?'
        )
        assert prerequisites.index(VULTURE_TARGET) < min(prerequisites.index(leg) for leg in matrix_legs), (
            f'`make {VALIDATE_TARGET}` runs {prerequisites}, sequencing `{VULTURE_TARGET}` after '
            f'{matrix_legs}. Make honours that order, so a dead-code finding would surface only '
            f'after the matrix -- put the sub-second static checks first.'
        )

    def test_the_gated_target_actually_invokes_vulture(self) -> None:
        """Membership in a prerequisite list is worth nothing if the target is hollow.

        ``validate: lint vulture ...`` stays green if ``vulture``'s recipe is emptied, commented
        out, or quietly re-pointed at some other tool -- Make would run a target that does
        nothing and report success. So the recipe is read, and ``vulture`` asserted as its own
        shell token (the ``pytest_invocations`` lesson: a substring check is satisfied by the help
        text and by the target's own name).
        """
        recipe = make_recipe(MAKEFILE.read_text(encoding='utf-8'), VULTURE_TARGET)
        assert recipe, (
            f'the `{VULTURE_TARGET}` target of {MAKE_NAME} has an empty recipe, so `make '
            f'{VALIDATE_TARGET}` now depends on a target that does nothing and passes.'
        )
        invocations = [line for line in recipe if 'vulture' in command_tokens(line)]
        assert invocations, (
            f'the `{VULTURE_TARGET}` target of {MAKE_NAME} runs {recipe}, none of which invokes '
            f'`vulture` as a command. The gate would still list it as a prerequisite and still '
            f'pass, having checked nothing.'
        )
        scanned = [
            line for line in invocations if any(token.rstrip('/').endswith('src') for token in command_tokens(line))
        ]
        assert scanned, (
            f'the `{VULTURE_TARGET}` target of {MAKE_NAME} runs {invocations}, which does not '
            f'point vulture at `src/`. Scanning a narrower path is how the check keeps passing '
            f'while covering less than the `[tool.vulture]` allowlist in {PYPROJECT_NAME} claims '
            f'it covers.'
        )

    def test_these_guards_can_still_see_a_gate_that_dropped_it(self) -> None:
        """Positive control (CI-072): every assertion above is a membership or a non-empty.

        Both shapes pass loudly when the parser underneath has stopped working -- a
        ``make_prerequisites`` that returned the help comment's words would satisfy every ``in``
        above forever. So the pre-CI-107 Makefile shape is re-parsed here and shown to fail.
        """
        before = '\n'.join(
            [
                'validate: lint typecheck-matrix test-matrix ## The pre-push gate',
                '',
                'vulture: ## Find unused code with vulture',
                '\t@uv run vulture src/',
            ]
        )
        assert VULTURE_TARGET not in make_prerequisites(before, VALIDATE_TARGET), (
            'the control could not reproduce a gate that omits vulture, so '
            'test_the_pre_push_gate_runs_the_dead_code_check cannot be shown to fail. Note the '
            'help comment above says "with vulture" -- a parser that did not strip `##` would '
            'find the word there and pass.'
        )
        hollow = 'validate: lint vulture ## gate\n\nvulture: ## Find unused code with vulture\n\t@echo skipping\n'
        assert not [line for line in make_recipe(hollow, VULTURE_TARGET) if 'vulture' in command_tokens(line)], (
            'the control could not reproduce a hollow `vulture` target, so '
            'test_the_gated_target_actually_invokes_vulture cannot be shown to fail.'
        )


@pytest.mark.unit
class TestEveryUnitTestIsSelectedByTheUnitTarget:
    """CI-134 -- two guard classes in this very file were invisible to ``make test-unit``.

    ``TestThePreCommitRuffHookIsNotTheLegacyAlias`` and
    ``TestTheVultureAllowlistIsExactlyWhatSrcNeeds`` shipped without ``@pytest.mark.unit`` while
    the other ten classes here carried it. Neither weakened the **gate** -- ``make test`` and
    ``make validate`` select ``-m "not integration"``, which an unmarked test satisfies -- so this
    is not the CI-081 shape. It is the quieter one: ``make test-unit`` is the target a developer
    reaches for while iterating, and there it reported success having never run them.

    Nothing announces that. A deselected test is not a failure, and the count it moves
    (``1471 passed`` -> ``1469 passed``) is not a number anyone reads. The same silence is what
    ``CI-083`` describes from the other end.

    ⚠ The marker is **read back out of the Makefile** rather than written here as a literal. A
    guard that hard-codes ``'unit'`` proves the files agree with this file; deriving it proves they
    agree with the target that actually selects them, which is the property that failed.
    """

    def test_the_unit_target_still_selects_by_marker(self) -> None:
        marker = selected_marker(makefile_text(), UNIT_TARGET)
        assert marker is not None, (
            f'the `{UNIT_TARGET}` target of {MAKE_NAME} no longer runs pytest with `-m`, so '
            f'"the marker" has no definition and the scan below would assert nothing. If the '
            f'target deliberately stopped selecting by marker, this guard should go with it.'
        )
        assert marker in {
            name.split(':', 1)[0]
            for name in pyproject_string_list(pyproject_table('tool', 'pytest', 'ini_options'), 'markers')
        }, (
            f'the `{UNIT_TARGET}` target selects `-m {marker}`, which is not registered in '
            f'`[tool.pytest.ini_options].markers` of {PYPROJECT_NAME}. An unregistered marker '
            f'selects nothing and pytest reports it as a warning, not an error.'
        )

    def test_no_test_under_tests_unit_escapes_that_marker(self) -> None:
        marker = selected_marker(makefile_text(), UNIT_TARGET)
        assert marker is not None
        modules = sorted(UNIT_TEST_DIR.rglob('test_*.py'))
        assert len(modules) > 1, (
            f'{UNIT_TEST_DIR} yielded {len(modules)} test modules, so this scan is close to '
            f'vacuous -- the glob, not the tree, is what changed.'
        )
        escaped = {
            str(module.relative_to(REPO_ROOT)): unmarked_test_objects(module.read_text(encoding='utf-8'), marker)
            for module in modules
        }
        offenders = {path: found for path, found in escaped.items() if found}
        assert not offenders, (
            f'these top-level test definitions under {UNIT_TEST_DIR.relative_to(REPO_ROOT)} carry '
            f'no `@pytest.mark.{marker}`, so `make {UNIT_TARGET}` silently skips them: '
            f'{offenders}. They still run under `make test` / `make validate`, which select '
            f'`-m "not integration"` -- which is exactly why nobody notices. Add the decorator.'
        )

    def test_this_scan_can_still_see_a_missing_marker(self) -> None:
        """Positive control (CI-072): both assertions above are "found nothing", the shape that
        passes loudest when the parser under it has stopped working.

        So the two real omissions are reconstructed verbatim and shown to be visible, and a
        correctly marked module is shown to be clean -- a scanner that reported *everything* as
        unmarked would satisfy the first half of this control while making the guard cry wolf.
        """
        marked = 'import pytest\n\n\n@pytest.mark.unit\nclass TestFine:\n    pass\n'
        assert unmarked_test_objects(marked, 'unit') == [], (
            'a class that plainly carries `@pytest.mark.unit` reads as unmarked, so '
            'test_no_test_under_tests_unit_escapes_that_marker would fail on a clean tree.'
        )
        bare = 'import pytest\n\n\nclass TestThePreCommitRuffHookIsNotTheLegacyAlias:\n    pass\n'
        assert unmarked_test_objects(bare, 'unit') == [('TestThePreCommitRuffHookIsNotTheLegacyAlias', 4)], (
            'the CI-134 omission, reproduced exactly as it shipped, does not read as unmarked -- '
            'so this guard could not have caught the bug it was written for.'
        )
        wrong = 'import pytest\n\n\n@pytest.mark.integration\ndef test_thing() -> None:\n    pass\n'
        assert unmarked_test_objects(wrong, 'unit') == [('test_thing', 5)], (
            'a module-level test function marked with a DIFFERENT marker reads as marked, so any '
            'decorator at all would satisfy the scan.'
        )
        # And the marker itself must come from the Makefile, not from this file's imagination.
        text = makefile_text()
        assert selected_marker(text, 'coverage') is not None and selected_marker(text, 'help') is None, (
            'selected_marker no longer distinguishes a target that selects by marker from one '
            'that runs no pytest at all, so reading the marker back out of the Makefile proves '
            'nothing.'
        )


@pytest.mark.unit
class TestEveryWorkflowLinterInThePreCommitConfigAlsoRunsInCi:
    """CI-144 -- the two workflow linters, asserted to run where the files they lint always exist.

    Same family as CI-107 and CI-108 above: not a check that is wrong, a check that quietly does
    not run. Both hooks carry an upstream ``files:`` filter -- ``^\\.github/workflows/`` for
    actionlint, and the workflows plus ``dependabot.yml`` plus any ``action.yml`` for zizmor -- so
    as pre-commit hooks they fire only on a commit that edits one of those. Measured: a commit of an
    unrelated file prints ``(no files to check)Skipped`` for both. Every other commit leaves the
    workflows audited by nobody, including the commits that matter most here: ``release.yml`` holds
    ``id-token: write`` for PyPI Trusted Publishing, and the action pins those workflows carry are a
    supply-chain surface that goes stale on its own, without a diff.

    ⚠ The assertions are about the RELATION between two files, not about either one's contents.
    Neither "the hooks exist" nor "CI has two steps" is worth asserting alone -- the first is
    already enforced by pre-commit and the second by CI going red. What nothing enforces is that
    the two stay the same set, which is why a hook added to that config tomorrow must show up here.

    They go red on: a workflow-lint hook declared in ``.pre-commit-config.yaml`` that no CI step
    runs; a renamed or deleted stanza (the id list empties, and the anti-vacuity assertion fires
    rather than a loop passing over nothing); and a CI step that invokes either linter directly
    instead of through pre-commit, which would re-open the CI-105 gap -- a second declaration of
    the version and the arguments, free to drift from the hook config.
    """

    def test_ci_runs_every_workflow_lint_hook_the_config_declares(self) -> None:
        config = PRE_COMMIT_CONFIG.read_text(encoding='utf-8')
        run_in_ci = workflow_pre_commit_hook_ids(CI_WORKFLOW.read_text(encoding='utf-8'))
        # Anti-vacuity (CI-083): with no pre-commit invocation parsed at all, every set difference
        # below would be reported against the full declared list -- but a parser that silently
        # returned nothing would look identical to a workflow that ran nothing, and the message
        # would send a reader to the wrong file.
        assert run_in_ci, (
            f'{CI_NAME} runs no `pre-commit run` at all. Either the workflow-lint job was removed '
            f'(then remove these guards with it and record why), or its steps are spelled in a '
            f'form workflow_pre_commit_hook_ids cannot read.'
        )
        for repo in WORKFLOW_LINT_HOOK_REPOS:
            declared = hosted_hook_ids(config, repo)
            assert declared, (
                f'{PRE_COMMIT_NAME} declares no hooks under {repo}. This guard reads the hook ids '
                f'from that stanza, so an empty list would make it pass while asserting nothing; '
                f'update WORKFLOW_LINT_HOOK_REPOS if the linter genuinely moved or was dropped.'
            )
            missing = set(declared) - run_in_ci
            assert not missing, (
                f'{PRE_COMMIT_NAME} declares {sorted(missing)} under {repo}, but {CI_NAME} never '
                f'runs it. Those hooks are scoped to `.github/workflows/`, so pre-commit skips '
                f'them on every commit that does not touch one -- CI is where they are guaranteed '
                f'to see the files. Add a step: `uv run pre-commit run --all-files <hook-id>`.'
            )

    def test_ci_reaches_them_through_pre_commit_rather_than_a_second_copy(self) -> None:
        direct = workflow_tools_outside_pre_commit(CI_WORKFLOW.read_text(encoding='utf-8'), WORKFLOW_LINT_PROGRAMS)
        assert not direct, (
            f'{CI_NAME} invokes {sorted(direct)} directly. Run it as '
            f'`uv run pre-commit run --all-files <hook-id>` instead: a direct call is a SECOND '
            f"declaration of that tool's version and arguments (zizmor's `--config "
            f'.github/zizmor.yml` among them), free to drift from {PRE_COMMIT_NAME}. That is the '
            f'CI-105 shape -- a gate covering something different from the check beside it -- '
            f'which cost this repo two red pushes on the ruff axis.'
        )

    def test_these_guards_can_still_see_the_shapes_they_forbid(self) -> None:
        # A step NAMED after a linter, running nothing, is the cheapest way to fake this guard.
        named_only = """
jobs:
  workflows:
    steps:
      - name: Audit workflows (zizmor)
        run: echo done
"""
        assert workflow_pre_commit_hook_ids(named_only) == set()

        # A value-taking flag's value is not a hook id, and `uv run` is peeled off the program.
        staged = """
jobs:
  workflows:
    steps:
      - run: uv run pre-commit run --all-files --hook-stage pre-push zizmor
"""
        assert workflow_pre_commit_hook_ids(staged) == {'zizmor'}

        # A block scalar holding one invocation per line is read as two, not as one.
        both = """
jobs:
  workflows:
    steps:
      - run: |
          uv run pre-commit run --all-files actionlint
          uv run pre-commit run --all-files zizmor
"""
        assert workflow_pre_commit_hook_ids(both) == {'actionlint', 'zizmor'}

        # A wrapped invocation keeps its hook id (CI-102). This reader carried the same
        # single-physical-line defect as the pytest readers, and here it fails CLOSED: the id lands
        # on the continuation, so a correct workflow reads as running no hook at all.
        wrapped = """
jobs:
  workflows:
    steps:
      - run: |
          uv run pre-commit run --all-files \\
            --config .pre-commit-config.yaml zizmor
"""
        assert workflow_pre_commit_hook_ids(wrapped) == {'zizmor'}

        # And dropping one of them is visible -- the shape the first assertion has to catch.
        assert workflow_pre_commit_hook_ids(
            both.replace('          uv run pre-commit run --all-files zizmor\n', '')
        ) == {'actionlint'}

        # A second copy is seen in each spelling it could realistically arrive in -- including the
        # three that leave a wrapper, not the tool, in command position.
        for command in ('uv run zizmor .', 'uvx zizmor .github/workflows', 'uvx zizmor@1.30 .', './zizmor .'):
            step = f'jobs:\n  j:\n    steps:\n      - run: {command}\n'
            assert workflow_tools_outside_pre_commit(step, WORKFLOW_LINT_PROGRAMS) == {'zizmor'}, command
        action = 'jobs:\n  j:\n    steps:\n      - uses: zizmorcore/zizmor-action@v1\n'
        assert workflow_tools_outside_pre_commit(action, WORKFLOW_LINT_PROGRAMS) == {'zizmor'}

        # While the sanctioned spelling is not -- there the tool name IS the hook id, and the
        # config path in zizmor's own args must not be read as a second copy either.
        sanctioned = (
            'jobs:\n  j:\n    steps:\n'
            '      - run: uv run pre-commit run --all-files --config .github/zizmor.yml zizmor\n'
        )
        assert workflow_tools_outside_pre_commit(sanctioned, WORKFLOW_LINT_PROGRAMS) == set()
        assert workflow_tools_outside_pre_commit(both, WORKFLOW_LINT_PROGRAMS) == set()
