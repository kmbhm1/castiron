"""Tests for the corpus's own comparator and counters.

``compare.py`` and ``pipeline.py``'s counters are **harness code**, and CI-072's second
correction is blunt about what that means: "in every case the harness's *own correctness* was the
weak link, not the mutants." An unreadable or wrong golden-failure message is worse than none,
because it is what pushes a reader toward `--write` instead of toward the cause.

So the message is exercised here on synthetic inputs, where the expected output is known — rather
than inferred from the one time a real golden happened to fail.
"""

from pathlib import Path

import pytest

from tests.unit.corpus.cases import REGENERATE_COMMAND
from tests.unit.corpus.compare import MAX_HUNKS, _render_failure, _whitespace_only_report, assert_golden
from tests.unit.corpus.pipeline import Counters, count_structure


@pytest.mark.unit
class TestStructuralCounters:
    def test_it_counts_classes_fields_and_imports_textually(self) -> None:
        text = (
            'from __future__ import annotations\n'
            'import datetime\n'
            '\n'
            '\n'
            '# SECTION\n'
            'class Thing(Base):\n'
            '    """Doc."""\n'
            '\n'
            '    # Columns\n'
            '    id: int\n'
            '    name: str | None = Field(default=None)\n'
        )
        assert count_structure(text) == Counters(lines=11, chars=len(text), classes=1, fields=2, imports=2)

    def test_a_comment_a_docstring_and_a_deeper_indent_are_not_fields(self) -> None:
        # The field rule must not drift into counting comments or nested code, or a counter delta
        # would report movement that is not there.
        text = '    # Columns: a note\n    """Doc: text."""\n        nested: int\n    real: int\n'
        assert count_structure(text).fields == 1

    def test_counters_work_on_a_module_that_does_not_parse(self) -> None:
        # Still load-bearing, for a reason that survived CI-085 fixing the one golden that used
        # to demonstrate it. Every committed golden parses today, but the counters are what makes
        # a REGRESSION legible: an ast-based counter would raise on the very diff a reader most
        # needs to read, and `characterized` cases are allowed to declare `compiles=False` again.
        # The text below is exactly what castiron emitted before CI-080 and CI-085 landed.
        text = 'class Broken(Base):\n    2fast: str\n    IN PROGRESS = "x"\n'
        assert count_structure(text).classes == 1
        assert count_structure(text).fields == 1

    def test_the_delta_names_only_what_moved(self) -> None:
        before = count_structure('class A(B):\n    x: int\n')
        after = count_structure('class A(B):\n    x: int\n    y: int\n')
        delta = before.delta(after)
        assert 'fields 1->2 (+1)' in delta
        assert 'classes' not in delta

    def test_the_delta_says_so_when_nothing_structural_moved(self) -> None:
        same = count_structure('class A(B):\n    x: int\n')
        assert 'no structural counter moved' in same.delta(same)


@pytest.mark.unit
class TestWhitespaceOnlyDifferencesAreVisible:
    """The CI-063 lesson applied to a diff renderer."""

    def test_a_trailing_space_is_reported_as_repr_not_as_an_invisible_diff(self) -> None:
        report = _whitespace_only_report(['a: int\n', 'b: int\n'], ['a: int \n', 'b: int\n'])
        assert report, 'a trailing-space difference was not recognized as whitespace-only'
        rendered = '\n'.join(report)
        # Without repr() these two lines print identically and the reader concludes the tool lies.
        assert "'a: int \\n'" in rendered
        assert "'a: int\\n'" in rendered
        assert 'WHITESPACE-ONLY' in rendered

    def test_a_real_content_change_is_not_treated_as_whitespace_only(self) -> None:
        assert _whitespace_only_report(['a: int\n'], ['a: str\n']) == []

    def test_it_caps_how_many_whitespace_differences_it_prints(self) -> None:
        before = [f'line{index}\n' for index in range(10)]
        after = [f'line{index} \n' for index in range(10)]
        rendered = '\n'.join(_whitespace_only_report(before, after))
        assert 'and more' in rendered
        assert rendered.count('committed ') == MAX_HUNKS


@pytest.mark.unit
class TestTheFailureMessageIsActionable:
    def test_it_names_the_case_the_sizes_the_counters_and_the_command(self, tmp_path: Path) -> None:
        golden = tmp_path / 'default.py.txt'
        expected = 'class A(B):\n    x: int\n'
        golden.write_text(expected, encoding='utf-8')
        message = _render_failure(
            'class A(B):\n    x: int\n    y: int\n', expected, golden, 'demo-case', 'emitted module'
        )

        assert message.startswith('demo-case: the emitted module does not match its committed golden.')
        assert 'chars committed ->' in message
        assert 'sha256:' in message
        assert 'fields 1->2 (+1)' in message
        assert f'{REGENERATE_COMMAND} --write' in message
        # The reader must be told the two reasons a golden moves, or "regenerate" looks like the
        # only available response.
        assert 'Behaviour changed ON PURPOSE' in message
        assert 'Hard Rule #9' in message

    def test_it_suppresses_extra_hunks_but_says_how_many(self) -> None:
        expected = ''.join(f'line{index}\n' for index in range(100))
        actual = ''.join(f'line{index}{"!" if index % 10 == 0 else ""}\n' for index in range(100))
        message = _render_failure(actual, expected, _AnyPath(), 'demo-case', 'emitted module')  # type: ignore[arg-type]
        assert f'showing {MAX_HUNKS} of 10 hunk(s)' in message
        assert f'... {10 - MAX_HUNKS} further hunk(s) suppressed.' in message

    def test_a_missing_golden_fails_with_the_regeneration_command(self, tmp_path: Path) -> None:
        # pytest.fail raises Failed, which derives from BaseException -- `pytest.raises(Exception)`
        # would NOT catch it, and the test would have reported a false failure about a real pass.
        with pytest.raises(pytest.fail.Exception, match='MISSING'):
            assert_golden('anything', tmp_path / 'absent.py.txt', case='demo-case', what='emitted module')

    def test_an_identical_golden_passes_silently(self, tmp_path: Path) -> None:
        golden = tmp_path / 'same.py.txt'
        golden.write_text('identical\n', encoding='utf-8')
        assert_golden('identical\n', golden, case='demo-case', what='emitted module')


class _AnyPath:
    """A stand-in path for message rendering, which only needs ``relative_to`` to fail."""

    def relative_to(self, _other: object) -> object:
        raise ValueError('not relative')

    def __str__(self) -> str:
        return '<synthetic>'
