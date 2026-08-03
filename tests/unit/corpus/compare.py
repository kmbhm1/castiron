"""Golden comparison with a failure message a reader can act on.

A bare ``assert actual == golden`` on a 36 KB string is not a test result, it is a wall. Worse,
pytest's assertion rewriting on two 1 392-line strings produces output that is *actively harder*
to read than nothing. And the cheapest response to an unreadable golden failure is
"regenerate it", which is precisely how a golden stops guarding anything.

So :func:`assert_golden` fails with: the case and artifact, both sizes, which structural counters
moved, how many lines changed, the first few unified-diff hunks (with the remainder counted, not
dropped silently), and the exact regeneration command. For a whitespace-only difference it prints
``repr()`` of the differing lines — otherwise the diff renders as two identical-looking lines,
which is the CI-063 lesson ("test against the encodings input really arrives in") applied to a
diff renderer.
"""

import difflib
from pathlib import Path

import pytest

from tests.unit.corpus.cases import REGENERATE_COMMAND
from tests.unit.corpus.pipeline import count_structure, sha256_text

#: How many unified-diff hunks to show before summarizing the rest. Three is enough to see the
#: shape of a change; the count of what was suppressed is always printed, so a reader is never
#: misled into thinking they have seen the whole diff.
MAX_HUNKS = 3

#: Context lines per hunk.
DIFF_CONTEXT = 3


def assert_golden(actual: str, golden: Path, *, case: str, what: str) -> None:
    """Assert ``actual`` equals the committed golden at ``golden``, byte for byte.

    Args:
        actual: The text this branch's code produces.
        golden: The committed golden file.
        case: The corpus case id, for the failure message.
        what: The artifact class (``'emitted module'``, ``'Schema IR'``, ``'manifest'``).

    Raises:
        Failed: The golden is missing or differs. ``pytrace=False`` keeps the message readable —
            a traceback through the comparator tells the reader nothing they need.
    """
    if not golden.is_file():
        pytest.fail(
            f'{case}: the committed {what} golden is MISSING at {_rel(golden)}.\n'
            f'The case table declares it, so either the file was lost or the case is wrong.\n'
            f'Regenerate with: {REGENERATE_COMMAND} --write',
            pytrace=False,
        )

    expected = golden.read_text(encoding='utf-8')
    if actual == expected:
        return

    pytest.fail(_render_failure(actual, expected, golden, case, what), pytrace=False)


def _render_failure(actual: str, expected: str, golden: Path, case: str, what: str) -> str:
    """Build the full mismatch report."""
    expected_lines = expected.splitlines(keepends=True)
    actual_lines = actual.splitlines(keepends=True)
    changed_added = sum(1 for line in difflib.ndiff(expected_lines, actual_lines) if line.startswith('+ '))
    changed_removed = sum(1 for line in difflib.ndiff(expected_lines, actual_lines) if line.startswith('- '))

    report = [
        f'{case}: the {what} does not match its committed golden.',
        '',
        f'  golden:   {_rel(golden)}',
        f'  size:     {len(expected)} chars committed -> {len(actual)} chars produced',
        f'  sha256:   {sha256_text(expected)[:16]} committed -> {sha256_text(actual)[:16]} produced',
        f'  lines:    +{changed_added} / -{changed_removed}',
        f'  counters: {count_structure(expected).delta(count_structure(actual))}',
        '',
    ]

    whitespace_only = _whitespace_only_report(expected_lines, actual_lines)
    if whitespace_only:
        report.extend(whitespace_only)
    else:
        report.extend(_hunk_report(expected_lines, actual_lines))

    report.extend(
        [
            '',
            'A golden moves for exactly one of two reasons:',
            '  1. Behaviour changed ON PURPOSE. Then the PR body needs a `## Golden delta`',
            '     section written BEFORE regenerating, predicting the cause, direction and',
            '     magnitude of this diff -- a description written afterwards is worth nothing.',
            '  2. Behaviour changed BY ACCIDENT. That is a Hard Rule #9 violation (emitter',
            '     output is byte-stable) and `castiron check` would now report drift to users',
            '     who changed nothing.',
            '',
            f'  inspect: {REGENERATE_COMMAND}          (writes nothing; diffs into dist/scratch/)',
            f'  accept:  {REGENERATE_COMMAND} --write  (rewrites the committed golden)',
        ]
    )
    return '\n'.join(report)


def _whitespace_only_report(expected_lines: list[str], actual_lines: list[str]) -> list[str]:
    """Return a ``repr()``-based report when the two texts differ only in whitespace.

    A whitespace-only diff is *invisible* in a normal unified diff: the two sides render as
    identical-looking lines and the reader concludes the tool is broken. ``repr()`` is the only
    honest rendering.

    Args:
        expected_lines: The committed golden's lines.
        actual_lines: The produced lines.

    Returns:
        The report lines, or ``[]`` when the difference is not whitespace-only.
    """
    if [line.strip() for line in expected_lines] != [line.strip() for line in actual_lines]:
        return []

    report = ['  ⚠ WHITESPACE-ONLY difference -- shown as repr() because it is otherwise invisible:']
    shown = 0
    for index, (before, after) in enumerate(zip(expected_lines, actual_lines)):
        if before == after:
            continue
        if shown >= MAX_HUNKS:
            report.append(f'    ... and more (only the first {MAX_HUNKS} whitespace differences are shown)')
            break
        report.append(f'    line {index + 1}: committed {before!r}')
        report.append(f'    line {index + 1}: produced  {after!r}')
        shown += 1
    return report


def _hunk_report(expected_lines: list[str], actual_lines: list[str]) -> list[str]:
    """Return the first :data:`MAX_HUNKS` unified-diff hunks, counting any remainder."""
    diff = list(
        difflib.unified_diff(
            expected_lines,
            actual_lines,
            fromfile='committed golden',
            tofile='produced by this branch',
            n=DIFF_CONTEXT,
        )
    )
    hunk_starts = [index for index, line in enumerate(diff) if line.startswith('@@')]
    if not hunk_starts:  # pragma: no cover - unreachable while the texts differ
        return ['  (no unified-diff hunks; the difference is in the trailing newline)']

    cutoff = hunk_starts[MAX_HUNKS] if len(hunk_starts) > MAX_HUNKS else len(diff)
    report = [f'  showing {min(len(hunk_starts), MAX_HUNKS)} of {len(hunk_starts)} hunk(s):']
    report.extend('    ' + line.rstrip('\n') for line in diff[:cutoff])
    if len(hunk_starts) > MAX_HUNKS:
        report.append(f'    ... {len(hunk_starts) - MAX_HUNKS} further hunk(s) suppressed.')
    return report


def _rel(path: Path) -> str:
    """Render ``path`` relative to the repository root when possible."""
    from tests.unit.corpus.cases import REPO_ROOT

    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:  # pragma: no cover - a golden always lives under the repo root
        return str(path)
