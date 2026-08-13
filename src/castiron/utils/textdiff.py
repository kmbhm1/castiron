"""Rendering the difference between two texts so a reader can act on it.

``castiron check`` and the golden corpus have the same job: two large texts differ, and the
message about it is the whole product. A bare "they are not equal" on a 36 KB module is not a
result, it is a wall — and the cheapest response to an unreadable diff is to regenerate, which is
precisely how a drift guard stops guarding anything.

These renderers were proved inside ``tests/unit/corpus/compare.py`` before they moved here, and the
corpus now imports them rather than keeping a second copy (Hard Rule #6). The one that justifies
the module on its own is :func:`whitespace_only_lines`: a whitespace-only difference
renders in a unified diff as **two identical-looking lines**, and a reader who sees that concludes
the tool is broken. ``repr()`` is the only honest rendering of it.

Everything here is pure: no I/O, no pytest, no Python-specific parsing. Deliberately **no**
structural counters — counting ``class`` / ``import`` / indented ``name: annotation`` lines is a
Python heuristic, and a comparator a future TS/Zod emitter will use must not carry a Python bias.
The hunks are what name the drifted region.
"""

import difflib
import hashlib
from collections.abc import Sequence

#: How many hunks (or whitespace differences) to show before summarizing the rest. Three is enough
#: to see the shape of a change; what was suppressed is always counted, so a reader is never misled
#: into thinking they have seen the whole diff.
MAX_HUNKS = 3

#: Context lines per unified-diff hunk.
DIFF_CONTEXT = 3

#: Characters that occupy no visible width, so a difference made only of them renders as two
#: identical-looking lines. The BOM (``\ufeff``) is the one that actually occurs in the wild: an
#: editor "helpfully" saving a generated module as UTF-8-with-BOM is real drift that a unified
#: diff shows as an unchanged line above an unchanged line. The zero-width space, non-joiner,
#: joiner and word joiner are here because they have exactly the same property, not because they
#: have been observed.
INVISIBLE_CHARACTERS = '\ufeff\u200b\u200c\u200d\u2060'

_INVISIBLE_TABLE = str.maketrans('', '', INVISIBLE_CHARACTERS)


def sha256_text(text: str) -> str:
    """Return the sha256 of ``text`` encoded as UTF-8.

    Two 16-character digest prefixes are the cheapest "these are genuinely different files"
    signal there is, and unlike a size they cannot coincide by accident.

    Args:
        text: The text to digest.

    Returns:
        The lowercase hex digest.
    """
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def changed_line_counts(expected: Sequence[str], actual: Sequence[str]) -> tuple[int, int]:
    """Count how many lines were added and removed between two line sequences.

    ``+4 / -1`` is the one number that says *how big* a difference is before the reader has read
    any of it, and it is what tells "one character moved" apart from "the whole file was
    regenerated".

    Computed from :class:`difflib.SequenceMatcher` opcodes with ``autojunk=False`` rather than by
    counting ``ndiff``'s ``'+ '`` / ``'- '`` prefixes, which is what the golden corpus used to do.
    Two reasons, and the first is the one that matters:

    1. **It must describe the same diff the hunks below it show.** :func:`_unified_diff` disables
       ``autojunk``; ``ndiff`` does not. A ``lines: +492 / -491`` header above a two-line hunk is
       not a summary, it is a contradiction — and that pair is measured, not hypothetical: it is
       what the two produce on the committed ``testbed-public/default.py.txt`` golden (1 373
       lines) with one line replaced and one inserted.
    2. ``ndiff`` runs an intraline ``_fancy_replace`` pass over every replaced block, looking
       *inside* lines to produce ``?`` guide rows this count then throws away. On a degenerate
       2 000-line input it did not finish in 120 s, where this returns immediately. ``check``
       renders it on the **drift** path, which is the CI path users wait on.

    Args:
        expected: The reference side's lines.
        actual: The other side's lines.

    Returns:
        ``(added, removed)`` — lines present only in ``actual``, and only in ``expected``.
    """
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, expected, actual, autojunk=False).get_opcodes():
        if tag in ('replace', 'delete'):
            removed += i2 - i1
        if tag in ('replace', 'insert'):
            added += j2 - j1
    return added, removed


def whitespace_only_lines(
    expected: Sequence[str],
    actual: Sequence[str],
    *,
    expected_label: str,
    actual_label: str,
    max_lines: int = MAX_HUNKS,
) -> list[str]:
    """Render a ``repr()``-based report when two texts differ only *invisibly*.

    "Invisibly" means whitespace **or** one of :data:`INVISIBLE_CHARACTERS` — a UTF-8 BOM is the
    case that occurs, and it has exactly the property that makes whitespace worth special-casing:
    a unified diff renders it as an unchanged line above an unchanged line.

    Returns ``[]`` when the difference is a real content change, which is the caller's signal to
    fall back to :func:`unified_hunks`.

    The returned lines are unindented; a caller that indents its report indents these uniformly.
    The two labels are padded to a common width so the ``repr()`` values line up under each other,
    which is the entire point of the rendering.

    Args:
        expected: The reference side's lines, as ``splitlines(keepends=True)`` produced them.
        actual: The other side's lines.
        expected_label: What to call the reference side (``'on disk'``, ``'committed'``).
        actual_label: What to call the other side (``'from the schema'``, ``'produced'``).
        max_lines: How many differing lines to spell out before summarizing.

    Returns:
        The report lines, or ``[]`` when the difference is not whitespace-only.
    """
    if [_visible(line) for line in expected] != [_visible(line) for line in actual]:
        return []

    width = max(len(expected_label), len(actual_label))
    report = [
        '⚠ WHITESPACE-ONLY difference (or a zero-width character) -- shown as repr() because it is otherwise invisible:'
    ]
    shown = 0
    for index, (before, after) in enumerate(zip(expected, actual)):
        if before == after:
            continue
        if shown >= max_lines:
            report.append(f'  ... and more (only the first {max_lines} whitespace differences are shown)')
            break
        report.append(f'  line {index + 1}: {expected_label:<{width}} {before!r}')
        report.append(f'  line {index + 1}: {actual_label:<{width}} {after!r}')
        shown += 1
    return report


def _visible(line: str) -> str:
    """Return ``line`` with every zero-width character deleted and its surrounding whitespace gone."""
    return line.translate(_INVISIBLE_TABLE).strip()


def unified_hunks(
    expected: Sequence[str],
    actual: Sequence[str],
    *,
    fromfile: str,
    tofile: str,
    max_hunks: int = MAX_HUNKS,
    context: int = DIFF_CONTEXT,
) -> list[str]:
    """Render the first ``max_hunks`` unified-diff hunks, counting any remainder.

    The count of suppressed hunks is always printed. Silently truncating a diff is worse than
    truncating it loudly: a reader who does not know there is more will fix the part they can see
    and be surprised twice.

    The returned lines are unindented except for the diff body, which is indented two spaces
    under its own ``showing N of M hunk(s):`` header.

    Args:
        expected: The reference side's lines, as ``splitlines(keepends=True)`` produced them.
        actual: The other side's lines.
        fromfile: The ``---`` label.
        tofile: The ``+++`` label.
        max_hunks: How many hunks to show.
        context: Context lines per hunk.

    Returns:
        The report lines. When the two sequences are equal there are no hunks, and a single
        explanatory line is returned rather than an empty list — an empty report inside a
        failure message reads as a broken renderer.
    """
    diff = _unified_diff(expected, actual, fromfile=fromfile, tofile=tofile, context=context)
    hunk_starts = [index for index, line in enumerate(diff) if line.startswith('@@')]
    if not hunk_starts:
        return ['(no unified-diff hunks; the texts differ only in a way the diff cannot show)']

    cutoff = hunk_starts[max_hunks] if len(hunk_starts) > max_hunks else len(diff)
    report = [f'showing {min(len(hunk_starts), max_hunks)} of {len(hunk_starts)} hunk(s):']
    report.extend('  ' + line.rstrip('\n') for line in diff[:cutoff])
    if len(hunk_starts) > max_hunks:
        report.append(f'  ... {len(hunk_starts) - max_hunks} further hunk(s) suppressed.')
    return report


def _unified_diff(
    expected: Sequence[str],
    actual: Sequence[str],
    *,
    fromfile: str,
    tofile: str,
    context: int,
) -> list[str]:
    r"""Produce ``difflib.unified_diff``'s output with ``autojunk`` **off**.

    🔴 This is not a reimplementation for its own sake, and it is the reason ``check``'s report is
    readable at all. ``difflib.unified_diff`` builds its ``SequenceMatcher`` with the default
    ``autojunk=True``, which — on any sequence of 200 elements or more — discards every element
    occurring in more than 1% of the second sequence *as junk*, so those lines can never anchor a
    match. A generated module is exactly the input that heuristic was not written for: it is long,
    and its most common lines (a bare ``\n``, a docstring terminator, a closing ``    )``) are
    structural.

    Measured on the committed ``testbed-public/default.py.txt`` golden (1 373 lines) with two lines
    changed — one replaced at 373, one inserted at 993:

    * ``autojunk=True``  → **7 hunks, 1 258 diff lines** (and ``ndiff`` calls it ``+492 / -491``)
    * ``autojunk=False`` → **2 hunks, 17 diff lines**

    A drift guard whose whole product is "the diff names the drifted region" cannot print a
    1 258-line wall for a two-line drift. Everything else is byte-for-byte CPython's
    ``unified_diff`` — same ``---``/``+++``/``@@`` grammar, same range formatting — so the output
    stays an ordinary unified diff that ``diff``-aware tooling and human eyes both read.

    Args:
        expected: The reference side's lines.
        actual: The other side's lines.
        fromfile: The ``---`` label.
        tofile: The ``+++`` label.
        context: Context lines per hunk.

    Returns:
        The diff lines, each ending in a newline where its source line did.
    """
    before, after = list(expected), list(actual)
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    diff: list[str] = []
    for group in matcher.get_grouped_opcodes(context):
        if not diff:
            diff.append(f'--- {fromfile}\n')
            diff.append(f'+++ {tofile}\n')
        first, last = group[0], group[-1]
        diff.append(f'@@ -{_range(first[1], last[2])} +{_range(first[3], last[4])} @@\n')
        for tag, i1, i2, j1, j2 in group:
            if tag == 'equal':
                diff.extend(f' {line}' for line in before[i1:i2])
                continue
            if tag in ('replace', 'delete'):
                diff.extend(f'-{line}' for line in before[i1:i2])
            if tag in ('replace', 'insert'):
                diff.extend(f'+{line}' for line in after[j1:j2])
    return diff


def _range(start: int, stop: int) -> str:
    """Format one side of a ``@@`` header the way :mod:`difflib` does (1-based, length elided at 1)."""
    length = stop - start
    if length == 1:
        return str(start + 1)
    return f'{start if length == 0 else start + 1},{length}'
