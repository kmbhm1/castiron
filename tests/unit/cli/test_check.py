"""``castiron check`` end to end.

Like ``test_gen.py``, the whole file runs offline: CI-005's hard fetch/parse split means a local
``--from ./openapi.json`` exercises the entire pipeline with zero HTTP mocking.

Two properties get more attention here than anywhere else in the suite, because they are what a
user's CI job depends on:

* **``check`` never writes.** ``test_check_never_writes_anything`` snapshots the whole output tree
  (names *and* bytes) around a clean run and a drifted run and asserts it is unchanged, and asserts
  a nonexistent ``--output`` is not created. A drift guard that quietly repaired the file it was
  asked to inspect would make every green build meaningless.
* **Exit 3 means "the comparison ran and the answer is not identical"; exit 1 means "castiron could
  not compare at all."** That single rule is what makes a missing file 3 and an unreadable file 1,
  and every exit-code test here is a case of it.
"""

import os
import stat
import sys
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result

import castiron
from castiron.cli import cli
from castiron.cli.check import FileComparison, render_report
from castiron.emitters.base import parse_header_version
from tests.unit.cli.conftest import write_config

SECRET = 'eyJhbGciOiJIUzI1NiJ9.SUPERSECRETVALUE'


def run_check(runner: CliRunner, *args: str, **kwargs: Any) -> Result:
    return runner.invoke(cli, ['check', *args], **kwargs)


def run_gen(runner: CliRunner, *args: str, **kwargs: Any) -> Result:
    return runner.invoke(cli, ['gen', *args], **kwargs)


def snapshot(root: Path) -> dict[str, bytes]:
    """Every file under ``root``, by relative path, with its exact bytes."""
    if not root.exists():
        return {}
    return {str(path.relative_to(root)): path.read_bytes() for path in sorted(root.rglob('*')) if path.is_file()}


def generate(runner: CliRunner, *args: str) -> Result:
    """Run ``gen`` into ``out/`` and assert it succeeded (the precondition of most tests here)."""
    result = run_gen(runner, '--from', 'openapi.json', '--output', 'out', '-q', *args)
    assert result.exit_code == 0, result.output
    return result


# ---------------------------------------------------------------------------
# The two answers: clean and drifted.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheTwoAnswers:
    def test_a_freshly_generated_tree_checks_clean(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 0, result.output
        assert 'is up to date' in result.stdout
        assert 'drift' not in result.stdout

    def test_the_clean_run_reports_what_it_read(self, runner: CliRunner, project: Path) -> None:
        # The same counts line `gen` prints, from the same renderer: "read 2 tables" when you
        # expected 20 is the RLS signal, and it is worth as much to `check` as to `gen`.
        generate(runner)
        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert 'castiron: read 6 tables, 1 enum and 4 functions from openapi.json' in result.stdout

    def test_a_hand_edited_file_is_drift(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_text(target.read_text(encoding='utf-8').replace('    id: int', '    id: str', 1), encoding='utf-8')

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert 'drift detected in 1 of 1 generated file(s).' in result.stdout
        assert str(Path('out') / 'schema.py') in result.stdout
        # The hunk is the whole product: the report must NAME the drifted region.
        assert '-    id: str' in result.stdout
        assert '+    id: int' in result.stdout
        assert 'run `castiron gen` to regenerate.' in result.stdout

    def test_a_schema_change_is_drift(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        document = (project / 'openapi.json').read_text(encoding='utf-8')
        (project / 'openapi.json').write_text(document.replace('"products"', '"produce"'), encoding='utf-8')

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert 'drift detected' in result.stdout

    def test_a_missing_output_file_is_drift_not_an_error(self, runner: CliRunner, project: Path) -> None:
        # The rule: the comparison RAN and the answer is "not identical", so it is 3. The user's
        # next action ("run gen") is identical to any other drift.
        generate(runner)
        (project / 'out' / 'schema.py').unlink()

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert f'{Path("out") / "schema.py"} does not exist.' in result.stdout
        # The accepted cost of that rule is a typo'd --output also exiting 3, so the mitigation is
        # in the message: it names the resolved path AND the flag that produced it.
        assert 'resolved from --output out' in result.stdout

    def test_a_missing_output_directory_is_drift_not_an_error(self, runner: CliRunner, project: Path) -> None:
        result = run_check(runner, '--from', 'openapi.json', '--output', 'nowhere')
        assert result.exit_code == 3, result.output
        assert 'does not exist.' in result.stdout

    @pytest.mark.skipif(os.geteuid() == 0, reason='root can read a 0o000 file, so the failure cannot be provoked')
    @pytest.mark.skipif(sys.platform == 'win32', reason='POSIX permission bits')
    def test_an_unreadable_output_file_is_an_error_not_drift(self, runner: CliRunner, project: Path) -> None:
        # The other half of the rule: a permission error is not an ANSWER to "has this drifted?",
        # so castiron could not perform the comparison at all -- exit 1.
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.chmod(0o000)
        try:
            result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        finally:
            target.chmod(stat.S_IRUSR | stat.S_IWUSR)
        assert result.exit_code == 1, result.output
        assert 'Could not read' in result.output

    def test_every_drifted_file_is_reported_not_just_the_first(self, runner: CliRunner, project: Path) -> None:
        # Two emitted files, produced by two single-emitter `gen` runs with different --filename.
        # Short-circuiting on the first would cost a second CI round trip to find the second.
        generate(runner, '--filename', 'first.py')
        generate(runner, '--filename', 'second.py')
        for name in ('first.py', 'second.py'):
            target = project / 'out' / name
            target.write_text(f'# hand written {name}\n', encoding='utf-8')

        first = run_check(runner, '--from', 'openapi.json', '--output', 'out', '--filename', 'first.py')
        second = run_check(runner, '--from', 'openapi.json', '--output', 'out', '--filename', 'second.py')
        assert (first.exit_code, second.exit_code) == (3, 3)
        assert 'first.py' in first.stdout
        assert 'second.py' in second.stdout

    def test_one_drifted_file_among_several_is_counted_and_the_rest_named_clean(self) -> None:
        # Exercised at the renderer, because the only registered emitter produces one file today
        # and a stub emitter would prove less than the shape itself does.
        comparisons = [
            FileComparison(
                path=Path('out/schema.py'),
                status='differs',
                expected='a: int\n',
                actual='a: str\n',
                recorded_version='0.5.0',
            ),
            FileComparison(
                path=Path('out/other.py'), status='match', expected='same\n', actual='same\n', recorded_version='0.5.0'
            ),
        ]
        report = render_report(comparisons, output_dir=Path('out'), running_version='0.5.0', quiet=False)
        assert 'drift detected in 1 of 2 generated file(s).' in report
        assert 'other.py is up to date.' in report


# ---------------------------------------------------------------------------
# The one thing `check` must never do.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCheckNeverWrites:
    def test_check_never_writes_anything(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        out = project / 'out'

        before = snapshot(out)
        assert run_check(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 0
        assert snapshot(out) == before, 'a clean check modified the output tree'

        target = out / 'schema.py'
        target.write_text(target.read_text(encoding='utf-8') + '\n# hand edit\n', encoding='utf-8')
        drifted = snapshot(out)
        assert run_check(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 3
        assert snapshot(out) == drifted, 'a drifted check modified the output tree'

    def test_it_does_not_create_a_missing_output_directory(self, runner: CliRunner, project: Path) -> None:
        assert run_check(runner, '--from', 'openapi.json', '--output', 'deep/nested').exit_code == 3
        assert not (project / 'deep').exists()

    def test_it_has_no_write_or_fix_flag(self, runner: CliRunner, project: Path) -> None:
        # There is no `--fix`: `gen` is the fix. This pins the absence so nobody adds one by
        # accident while "improving" the report.
        for flag in ('--write', '--fix'):
            result = run_check(runner, '--from', 'openapi.json', flag)
            assert result.exit_code == 2, flag


# ---------------------------------------------------------------------------
# Encodings and line endings.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEncodingsAndLineEndings:
    def test_crlf_on_disk_is_not_reported_as_drift(self, runner: CliRunner, project: Path) -> None:
        # A Windows contributor with core.autocrlf=true gets CRLF in the working tree. A byte-exact
        # check would report permanent, unfixable drift on every one of their CI runs -- the exact
        # failure `newline='\n'` exists to prevent, arriving through a different door.
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_bytes(target.read_text(encoding='utf-8').replace('\n', '\r\n').encode('utf-8'))

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 0, result.output
        assert b'\r\n' in target.read_bytes(), 'the fixture did not actually produce a CRLF file'

    def test_a_lone_carriage_return_is_also_normalized(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_bytes(target.read_text(encoding='utf-8').replace('\n', '\r').encode('utf-8'))
        assert run_check(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 0

    def test_a_utf8_bom_is_reported_as_drift_and_rendered_readably(self, runner: CliRunner, project: Path) -> None:
        # A BOM is real drift -- it changes the bytes of a committed file -- and it is invisible in
        # a normal diff, which is precisely what the repr() renderer is for.
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_bytes(b'\xef\xbb\xbf' + target.read_bytes())

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert 'WHITESPACE-ONLY' in result.stdout
        assert '\\ufeff' in result.stdout

    def test_a_whitespace_only_difference_is_rendered_with_repr(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_text(
            target.read_text(encoding='utf-8').replace('    id: int\n', '    id: int \n', 1), encoding='utf-8'
        )

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert 'WHITESPACE-ONLY' in result.stdout
        assert "'    id: int \\n'" in result.stdout
        assert "'    id: int\\n'" in result.stdout


# ---------------------------------------------------------------------------
# The version-aware diagnostic (spec §5.5) — three rows, and they are exhaustive.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTheVersionAwareDiagnostic:
    def _comparison(self, recorded: str | None) -> list[FileComparison]:
        return [
            FileComparison(
                path=Path('out/schema.py'),
                status='differs',
                expected='a: int\n',
                actual='a: str\n',
                recorded_version=recorded,
            )
        ]

    def test_the_same_version_on_both_sides_blames_the_schema_or_a_hand_edit(self) -> None:
        report = render_report(self._comparison('0.5.0'), output_dir=Path('out'), running_version='0.5.0', quiet=False)
        assert 'generated by castiron 0.5.0, and you are running 0.5.0' in report
        assert 'your schema or a hand edit' in report

    def test_a_different_version_says_castirons_own_output_may_have_moved(self) -> None:
        report = render_report(self._comparison('0.4.0'), output_dir=Path('out'), running_version='0.5.0', quiet=False)
        assert 'generated by castiron 0.4.0; you are running 0.5.0' in report
        # The honest limit: castiron cannot attribute individual hunks to the version change, so
        # the message must keep saying "some or all".
        assert 'Some or all of this difference' in report
        assert 'adopt the current output' in report

    def test_a_headerless_file_says_it_records_no_version(self) -> None:
        report = render_report(self._comparison(None), output_dir=Path('out'), running_version='0.5.0', quiet=False)
        assert 'records no castiron version' in report
        assert 'predates castiron 0.5.0' in report

    def test_the_diagnostic_reads_the_header_the_generated_file_actually_carries(
        self, runner: CliRunner, project: Path
    ) -> None:
        # End to end, so the parse is proved against real emitted bytes rather than a literal.
        generate(runner)
        target = project / 'out' / 'schema.py'
        text = target.read_text(encoding='utf-8')
        assert parse_header_version(text) == castiron.__version__
        target.write_text(
            text.replace(f'castiron {castiron.__version__}', 'castiron 0.0.1-ancient', 1), encoding='utf-8'
        )

        result = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert result.exit_code == 3, result.output
        assert f'generated by castiron 0.0.1-ancient; you are running {castiron.__version__}' in result.stdout


# ---------------------------------------------------------------------------
# Errors, usage errors, and the redaction contract.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorsAndUsage:
    def test_an_unreachable_source_exits_1_with_a_hint(self, runner: CliRunner, project: Path) -> None:
        # Proves `check` reuses `cli_error_handling` verbatim rather than growing its own boundary.
        def fail(*args: Any, **kwargs: Any) -> None:
            from castiron.sources import SourceFetchError

            raise SourceFetchError('https://abcdefgh.supabase.co/rest/v1/ returned HTTP 401')

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr('castiron.cli.pipeline.load_openapi_schema', fail)
            result = run_check(runner, '--from', 'https://abcdefgh.supabase.co', '--output', 'out')
        assert result.exit_code == 1, result.output
        assert 'HTTP 401' in result.output
        assert 'Hint:' in result.output

    def test_missing_from_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        result = run_check(runner, '--output', 'out')
        assert result.exit_code == 2
        assert 'No schema source' in result.output

    def test_filename_with_two_emitters_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        result = run_check(
            runner, '--from', 'openapi.json', '--emit', 'pydantic', '--emit', 'pydantic', '--filename', 'x.py'
        )
        assert result.exit_code == 2
        assert '--filename applies to a single-emitter run' in result.output

    def test_overwrite_and_dry_run_are_not_check_options(self, runner: CliRunner, project: Path) -> None:
        # Dropping them is not an omission -- `check` never writes, so both would be meaningless.
        # click's usage error is the correct answer and it costs no code.
        for flag in ('--overwrite', '--no-overwrite', '--dry-run'):
            result = run_check(runner, '--from', 'openapi.json', flag)
            assert result.exit_code == 2, flag
            assert 'No such option' in result.output, flag

    def test_a_userinfo_url_is_refused_before_anything_runs(self, runner: CliRunner, project: Path) -> None:
        result = run_check(runner, '--from', f'https://user:{SECRET}@abcdefgh.supabase.co', '--output', 'out')
        assert result.exit_code == 2
        assert 'carries credentials in its userinfo' in result.output
        assert SECRET not in result.output

    def test_the_key_is_redacted_from_check_output(self, runner: CliRunner, project: Path) -> None:
        # CI6-D7 applies to EVERY printed string, and `check` is a new set of printed strings.
        generate(runner)
        result = run_check(runner, '--from', 'openapi.json', '--key', SECRET, '--output', 'out', '-vv')
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output

    def test_a_key_in_the_source_is_redacted_from_the_summary(self, runner: CliRunner, project: Path) -> None:
        generate(runner)
        result = run_check(runner, '--from', f'openapi.json?apikey={SECRET}', '--output', 'out')
        # The path does not exist with the query string attached, so this is the usage-error path;
        # what matters is that the echoed value is masked.
        assert SECRET not in result.output


# ---------------------------------------------------------------------------
# Quiet, determinism, and the config file.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQuietDeterminismAndConfig:
    def test_quiet_suppresses_the_clean_summary_but_not_the_drift_report(
        self, runner: CliRunner, project: Path
    ) -> None:
        generate(runner)
        clean = run_check(runner, '--from', 'openapi.json', '--output', 'out', '-q')
        assert clean.exit_code == 0
        assert clean.stdout == ''

        target = project / 'out' / 'schema.py'
        target.write_text(target.read_text(encoding='utf-8').replace('    id: int', '    id: str', 1), encoding='utf-8')
        drifted = run_check(runner, '--from', 'openapi.json', '--output', 'out', '-q')
        assert drifted.exit_code == 3
        # The report is the payload, not a summary: suppressing it would leave a CI log saying
        # only "exit 3", which sends someone back to run the command again.
        assert 'drift detected' in drifted.stdout
        assert 'read 6 tables' not in drifted.stdout

    def test_quiet_drops_the_up_to_date_lines_from_a_drift_report(self) -> None:
        comparisons = [
            FileComparison(
                path=Path('out/a.py'), status='differs', expected='a\n', actual='b\n', recorded_version='0.5.0'
            ),
            FileComparison(path=Path('out/b.py'), status='match', expected='s\n', actual='s\n', recorded_version=None),
        ]
        loud = render_report(comparisons, output_dir=Path('out'), running_version='0.5.0', quiet=False)
        quiet = render_report(comparisons, output_dir=Path('out'), running_version='0.5.0', quiet=True)
        assert 'b.py is up to date.' in loud
        assert 'b.py is up to date.' not in quiet
        assert 'drift detected in 1 of 2' in quiet

    def test_the_report_is_byte_identical_across_two_runs(self, runner: CliRunner, project: Path) -> None:
        # `check`'s own determinism. Byte-stability is the product here: a report that varied
        # between runs would make the drift guard unreviewable.
        generate(runner)
        target = project / 'out' / 'schema.py'
        target.write_text(target.read_text(encoding='utf-8').replace('    id: int', '    id: str', 1), encoding='utf-8')

        first = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        second = run_check(runner, '--from', 'openapi.json', '--output', 'out')
        assert (first.exit_code, second.exit_code) == (3, 3)
        assert first.stdout == second.stdout

    def test_check_reads_the_same_tool_castiron_table_as_gen(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\nemit = ["pydantic"]\n')
        assert run_gen(runner, '-q').exit_code == 0
        result = run_check(runner)
        assert result.exit_code == 0, result.output
        assert 'is up to date' in result.stdout

    def test_an_output_path_in_the_config_is_anchored_to_the_config_file(
        self, runner: CliRunner, project: Path
    ) -> None:
        # The cwd-independence guarantee `cli/config.py` names `check` for: a config-file `output`
        # is anchored to the config file's directory, so running from a subdirectory must not make
        # `check` compare against a different (nonexistent) path.
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\n')
        assert run_gen(runner, '-q').exit_code == 0

        nested = project / 'sub' / 'dir'
        nested.mkdir(parents=True)
        with pytest.MonkeyPatch.context() as patch:
            patch.chdir(nested)
            result = run_check(runner, '--config', str(project / 'pyproject.toml'))
        assert result.exit_code == 0, result.output

    def test_a_gen_only_config_key_does_not_break_check(self, runner: CliRunner, project: Path) -> None:
        # click consults `default_map` per parameter, so a key `check` has no parameter for is
        # simply never looked up. No code change was needed for this; the test is what pins it.
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\noverwrite = true\n')
        assert run_gen(runner, '-q').exit_code == 0
        assert run_check(runner).exit_code == 0

    def test_a_reserved_check_table_is_still_parsed_and_ignored(self, runner: CliRunner, project: Path) -> None:
        # CI21-D4: `[tool.castiron.check]` gains no keys in this row and its contents stay
        # unvalidated. Today `foo = 1` inside it is accepted, and rejecting it would be a
        # behaviour change with no benefit while the table has no keys.
        write_config(
            project,
            '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\n\n[tool.castiron.check]\nfoo = 1\n',
        )
        assert run_gen(runner, '-q').exit_code == 0
        assert run_check(runner).exit_code == 0
