"""Guards for repository-configuration traps whose failure mode is silence.

**CI-093** -- a bare ``MANIFEST`` line in ``.gitignore`` also matched a ``manifest/``
**directory**, because git compares ignore patterns case-insensitively when ``core.ignorecase``
is set (it is, on macOS). Its contents then vanish from ``git add -A`` *and* from ``git status``,
with no error -- while on Linux CI the same pattern matches nothing. That is silent
cross-platform divergence in the file set itself. It cost the CI-007 corpus dispatch four files
before anyone noticed.

⚠ Anchoring the pattern (``/MANIFEST``) does **not** fix this: anchoring constrains *where* a
pattern may match, not *how case is compared*. Measured, both directions, git 2.55.0 on APFS:

    rule                          root manifest/ (macOS)   root manifest/ (Linux)
    /MANIFEST                     IGNORED                  tracked
    (line deleted)                tracked                  tracked

The line was deleted rather than anchored: castiron builds with **hatchling**, has no
``setup.py`` and no ``setup.cfg``, and therefore cannot produce the setuptools/distutils sdist
artifact the line existed for.

This follows the precedent of
``tests/unit/corpus/test_goldens.py::TestTheToolingActuallyProtectsTheseBytes``: repository
configuration that something else depends on is asserted, because "remember to update the
config" is not a mechanism.
"""

import subprocess
from pathlib import Path

import pytest

#: ``tests/unit`` -> ``tests`` -> the repository root.
REPO_ROOT = Path(__file__).parents[2]

#: Paths that must stay trackable. The first is the minimal reproduction; the second is the real
#: one -- what the CI-007 corpus dispatch tried to commit before renaming the directory to
#: ``fingerprints/`` to dodge the collision. Neither exists: ``git check-ignore`` answers for
#: hypothetical paths, which is exactly what makes this assertion cheap.
SHADOWED_PATHS = ('manifest/f.txt', 'tests/unit/corpus/manifest/rows.json')


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
