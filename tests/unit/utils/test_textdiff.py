"""Tests for the shared diff renderers ``castiron check`` and the golden corpus both use.

Most of this file moved out of ``tests/unit/corpus/test_compare.py`` with the renderers themselves
(CI-021b). CI-072's second correction is the reason it moved rather than being left behind: "in
every case the harness's *own correctness* was the weak link, not the mutants." That argument gets
stronger, not weaker, when the renderer stops being harness code and becomes the text a user reads
when their CI build goes red.

Two properties here are load-bearing beyond the rendering:

* :func:`castiron.utils.textdiff._unified_diff` must agree with :func:`difflib.unified_diff`
  wherever ``difflib``'s ``autojunk`` heuristic is inactive, and must **not** agree where it is
  active — that divergence is the whole reason the function exists.
* A whitespace-only difference must render as ``repr()``. Without it the report shows two
  identical-looking lines and the reader concludes castiron is broken.
"""

import difflib
import random

import pytest

from castiron.utils.textdiff import (
    DIFF_CONTEXT,
    MAX_HUNKS,
    _unified_diff,
    changed_line_counts,
    sha256_text,
    unified_hunks,
    whitespace_only_lines,
)


def _lines(*values: str) -> list[str]:
    """Build a keepends line list from bare strings."""
    return [f'{value}\n' for value in values]


@pytest.mark.unit
class TestTheDigest:
    def test_it_is_the_sha256_of_the_utf8_encoding(self) -> None:
        # Pinned against hashlib directly rather than against a literal: a literal would only
        # prove the function is stable, not that it is sha256 of the UTF-8 bytes.
        import hashlib

        text = 'naïve: str = "café"\n'
        assert sha256_text(text) == hashlib.sha256(text.encode('utf-8')).hexdigest()

    def test_two_texts_differing_by_one_character_get_different_digests(self) -> None:
        assert sha256_text('a: int\n') != sha256_text('a: str\n')


@pytest.mark.unit
class TestChangedLineCounts:
    def test_it_counts_additions_and_removals_separately(self) -> None:
        before = _lines('a', 'b', 'c')
        after = _lines('a', 'B', 'c', 'd')
        assert changed_line_counts(before, after) == (2, 1)

    def test_identical_sequences_report_no_change(self) -> None:
        assert changed_line_counts(_lines('a', 'b'), _lines('a', 'b')) == (0, 0)

    def test_it_always_describes_the_same_diff_the_hunks_show(self) -> None:
        """The invariant that decides how this is computed, not merely a sanity check.

        The ``lines: +N / -M`` row sits directly above the hunks in ``check``'s report. If the two
        were derived from different matchers -- which is exactly what happens when the count comes
        from ``difflib.ndiff`` (``autojunk`` on) and the hunks from :func:`_unified_diff`
        (``autojunk`` off) -- the header contradicts the body it introduces.
        """
        rng = random.Random(84021)
        for _ in range(200):
            before = [f'{rng.randint(0, 5)}\n' for _ in range(rng.randint(0, 40))]
            after = list(before)
            for _ in range(rng.randint(0, 5)):
                if not after:
                    after.append('z\n')
                    continue
                index = rng.randrange(len(after))
                operation = rng.choice(('insert', 'delete', 'replace'))
                if operation == 'insert':
                    after.insert(index, f'{rng.randint(0, 5)}\n')
                elif operation == 'delete':
                    del after[index]
                else:
                    after[index] = f'{rng.randint(0, 5)}\n'
            diff = _unified_diff(before, after, fromfile='a', tofile='b', context=0)
            body = [line for line in diff if not line.startswith(('---', '+++', '@@'))]
            from_the_diff = (
                sum(1 for line in body if line.startswith('+')),
                sum(1 for line in body if line.startswith('-')),
            )
            assert changed_line_counts(before, after) == from_the_diff, (before, after)


@pytest.mark.unit
class TestWhitespaceOnlyDifferencesAreVisible:
    """The CI-063 lesson applied to a diff renderer."""

    def test_a_trailing_space_is_reported_as_repr_not_as_an_invisible_diff(self) -> None:
        report = whitespace_only_lines(
            ['a: int\n', 'b: int\n'],
            ['a: int \n', 'b: int\n'],
            expected_label='on disk',
            actual_label='from the schema',
        )
        assert report, 'a trailing-space difference was not recognized as whitespace-only'
        rendered = '\n'.join(report)
        # Without repr() these two lines print identically and the reader concludes the tool lies.
        assert "'a: int \\n'" in rendered
        assert "'a: int\\n'" in rendered
        assert 'WHITESPACE-ONLY' in rendered

    def test_a_real_content_change_is_not_treated_as_whitespace_only(self) -> None:
        assert (
            whitespace_only_lines(['a: int\n'], ['a: str\n'], expected_label='on disk', actual_label='from the schema')
            == []
        )

    def test_it_caps_how_many_whitespace_differences_it_prints(self) -> None:
        before = [f'line{index}\n' for index in range(10)]
        after = [f'line{index} \n' for index in range(10)]
        rendered = '\n'.join(whitespace_only_lines(before, after, expected_label='committed', actual_label='produced'))
        assert 'and more' in rendered
        assert rendered.count('committed ') == MAX_HUNKS

    def test_a_utf8_bom_counts_as_an_invisible_difference(self) -> None:
        # The case that actually occurs: an editor saves a generated module as UTF-8-with-BOM.
        # `str.strip()` alone does not remove it (a BOM is not whitespace to Python), so without
        # INVISIBLE_CHARACTERS this falls through to a unified diff that shows two identical-
        # looking lines -- exactly the failure this renderer exists to prevent.
        report = whitespace_only_lines(
            ['\ufeffimport datetime\n'],
            ['import datetime\n'],
            expected_label='on disk',
            actual_label='from the schema',
        )
        assert report, 'a BOM was not recognized as an invisible difference'
        assert '\\ufeff' in '\n'.join(report)

    def test_the_labels_are_padded_so_the_reprs_line_up(self) -> None:
        # The alignment is the point: two repr()s at different columns are harder to compare than
        # the invisible diff this renderer replaces.
        report = whitespace_only_lines(
            ['a: int\n'], ['a: int \n'], expected_label='on disk', actual_label='from the schema'
        )
        columns = {line.index("'") for line in report if "'" in line}
        assert len(columns) == 1, report


@pytest.mark.unit
class TestUnifiedHunks:
    def test_it_names_the_two_sides_and_shows_the_change(self) -> None:
        report = '\n'.join(
            unified_hunks(
                _lines('a', 'b', 'c'),
                _lines('a', 'B', 'c'),
                fromfile='on disk',
                tofile='produced from the schema',
            )
        )
        assert 'showing 1 of 1 hunk(s):' in report
        assert '--- on disk' in report
        assert '+++ produced from the schema' in report
        assert '-b' in report
        assert '+B' in report

    def test_it_suppresses_extra_hunks_but_says_how_many(self) -> None:
        before = [f'line{index}\n' for index in range(100)]
        after = [f'line{index}{"!" if index % 10 == 0 else ""}\n' for index in range(100)]
        report = '\n'.join(unified_hunks(before, after, fromfile='a', tofile='b'))
        assert f'showing {MAX_HUNKS} of 10 hunk(s)' in report
        assert f'... {10 - MAX_HUNKS} further hunk(s) suppressed.' in report

    def test_identical_texts_return_an_explanation_not_an_empty_report(self) -> None:
        # An empty list spliced into a failure message reads as a broken renderer, which is worse
        # than a line saying there is nothing to show.
        report = unified_hunks(_lines('a'), _lines('a'), fromfile='a', tofile='b')
        assert report == ['(no unified-diff hunks; the texts differ only in a way the diff cannot show)']

    def test_the_hunk_body_is_indented_under_its_header(self) -> None:
        report = unified_hunks(_lines('a', 'b'), _lines('a', 'B'), fromfile='a', tofile='b')
        assert not report[0].startswith(' ')
        assert all(line.startswith('  ') for line in report[1:])


@pytest.mark.unit
class TestTheDiffAgreesWithDifflibExceptWhereAutojunkFires:
    """``_unified_diff`` is CPython's ``unified_diff`` with one flag flipped, and it must stay that way."""

    def test_it_matches_difflib_exactly_on_sequences_autojunk_never_touches(self) -> None:
        # difflib only applies autojunk at 200+ elements, so every case here must agree byte for
        # byte. Randomized because the interesting inputs are the boundary ones -- empty sides,
        # changes at index 0 and at the end, adjacent hunks merging at a given context width.
        rng = random.Random(20260812)
        for _ in range(400):
            before = [f'{rng.randint(0, 6)}\n' for _ in range(rng.randint(0, 30))]
            after = list(before)
            for _ in range(rng.randint(0, 4)):
                if not after:
                    after.append('z\n')
                    continue
                index = rng.randrange(len(after))
                operation = rng.choice(('insert', 'delete', 'replace'))
                if operation == 'insert':
                    after.insert(index, f'{rng.randint(0, 6)}\n')
                elif operation == 'delete':
                    del after[index]
                else:
                    after[index] = f'{rng.randint(0, 6)}\n'
            for context in (0, 1, DIFF_CONTEXT):
                assert _unified_diff(before, after, fromfile='x', tofile='y', context=context) == list(
                    difflib.unified_diff(before, after, fromfile='x', tofile='y', n=context)
                ), (before, after, context)

    def test_it_diverges_from_difflib_exactly_where_autojunk_fires(self) -> None:
        """The mechanism, exhibited deterministically rather than hoped for.

        ``difflib`` treats any line occurring in more than 1% of a sequence of 200 or more as
        *junk*, so it can never anchor a match. Below, one line differs out of 256 -- and with the
        heuristic on, the whole file is rendered as one enormous replace because the padding can no
        longer be matched. That is not a contrived shape: a generated module is long, and its
        commonest lines (blank lines, docstring terminators, closing parentheses) are structural.
        """
        before = ['    padding: int\n'] * 5 + ['    changed: int\n'] + ['    padding: int\n'] * 250
        after = ['    padding: int\n'] * 5 + ['    changed: str\n'] + ['    padding: int\n'] * 250

        ours = _unified_diff(before, after, fromfile='a', tofile='b', context=DIFF_CONTEXT)
        theirs = list(difflib.unified_diff(before, after, fromfile='a', tofile='b', n=DIFF_CONTEXT))

        assert len(ours) == 11, ours
        assert len(theirs) > 500, (
            'difflib.unified_diff no longer shatters this input, so the reason _unified_diff '
            'exists may have gone away. Re-measure before deleting it.'
        )
