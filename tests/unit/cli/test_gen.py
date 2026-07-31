"""``castiron gen`` end to end.

The whole file runs offline. CI-005's hard fetch/parse split (CI5-D3) means a local
``--from ./openapi.json`` exercises the entire pipeline -- source, IR, emitter, write path,
summary -- with **zero HTTP mocking**; only the four tests that are explicitly about the
network patch ``urlopen``.
"""

import json
import sys
from pathlib import Path
from types import ModuleType, TracebackType
from typing import Any
from urllib.error import URLError
from urllib.request import Request

import pytest
from click.testing import CliRunner, Result

from castiron.cli import cli
from castiron.cli.gen import format_size
from castiron.ir import Schema
from castiron.sources import SourceFetchError, SourceParseError, build_schema_from_document

SECRET = 'eyJhbGciOiJIUzI1NiJ9-SUPERSECRET'


def run(runner: CliRunner, *args: str, **kwargs: Any) -> Result:
    return runner.invoke(cli, ['gen', *args], **kwargs)


class FakeResponse:
    """The minimal ``urlopen`` return value ``fetch_openapi_document`` uses."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        return False


# ---------------------------------------------------------------------------
# The Phase-0 exit criterion, executed offline.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExitCriterion:
    def test_it_writes_the_committed_golden_byte_for_byte(
        self, runner: CliRunner, project: Path, openapi_golden_text: str
    ) -> None:
        # The golden is exactly PydanticEmitter(EmitterConfig()).emit(...) with all defaults,
        # so this is a byte-level proof that the CLI's write path does not alter emitter output.
        result = run(runner, '--from', 'openapi.json', '--emit', 'pydantic', '--output', 'out')
        assert result.exit_code == 0, result.output
        assert (project / 'out' / 'schema.py').read_bytes() == openapi_golden_text.encode('utf-8')

    def test_the_summary_names_the_counts_and_the_file(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out')
        assert 'castiron: read 6 tables, 1 enum and 4 functions from openapi.json' in result.stdout
        assert f'castiron: wrote {Path("out") / "schema.py"} (' in result.stdout

    def test_running_twice_produces_identical_bytes(self, runner: CliRunner, project: Path) -> None:
        first = run(runner, '--from', 'openapi.json', '--output', 'out')
        written = (project / 'out' / 'schema.py').read_bytes()
        second = run(runner, '--from', 'openapi.json', '--output', 'out')
        assert (first.exit_code, second.exit_code) == (0, 0)
        assert (project / 'out' / 'schema.py').read_bytes() == written

    def test_the_written_module_is_valid_python_and_instantiable(self, runner: CliRunner, project: Path) -> None:
        assert run(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 0
        source = (project / 'out' / 'schema.py').read_text(encoding='utf-8')
        module = ModuleType('castiron_cli_generated')
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, '<castiron-generated>', 'exec'), module.__dict__)
            product = module.__dict__['ProductsBaseSchema'](id=1, name='Anvil')
            assert product.name == 'Anvil'
        finally:
            del sys.modules[module.__name__]

    def test_the_output_directory_is_created_when_missing(self, runner: CliRunner, project: Path) -> None:
        assert run(runner, '--from', 'openapi.json', '--output', 'deep/nested').exit_code == 0
        assert (project / 'deep' / 'nested' / 'schema.py').is_file()

    def test_the_default_output_directory_is_the_cwd(self, runner: CliRunner, project: Path) -> None:
        assert run(runner, '--from', 'openapi.json').exit_code == 0
        assert (project / 'schema.py').is_file()


# ---------------------------------------------------------------------------
# Source dispatch: URL vs. local document.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSourceDispatch:
    def test_the_local_path_never_touches_the_fetcher(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError('the offline path must not open a socket')

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', explode)
        assert run(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 0

    def test_a_url_is_handed_to_the_openapi_source_with_every_parsed_option(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_document: dict[str, Any],
    ) -> None:
        seen: dict[str, Any] = {}

        def fake_load(url: str, **kwargs: Any) -> Schema:
            seen['url'] = url
            seen.update(kwargs)
            return build_schema_from_document(openapi_fixture_document)

        monkeypatch.setattr('castiron.cli.gen.load_openapi_schema', fake_load)
        result = run(
            runner,
            '--from',
            'https://abcdefgh.supabase.co',
            '--key',
            SECRET,
            '--schema',
            'billing',
            '--timeout',
            '7.5',
            '--infer-generated-primary-keys',
            '--output',
            'out',
        )
        assert result.exit_code == 0, result.output
        assert seen == {
            'url': 'https://abcdefgh.supabase.co',
            'key': SECRET,
            'schema': 'billing',
            'timeout': 7.5,
            'infer_generated_primary_keys': True,
            'disable_model_prefix_protection': False,
        }

    def test_the_summary_reports_the_normalized_rest_root(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_document: dict[str, Any],
    ) -> None:
        monkeypatch.setattr(
            'castiron.cli.gen.load_openapi_schema',
            lambda url, **kwargs: build_schema_from_document(openapi_fixture_document),
        )
        result = run(runner, '--from', 'https://abcdefgh.supabase.co', '--output', 'out')
        assert 'from https://abcdefgh.supabase.co/rest/v1/' in result.stdout

    def test_the_key_reaches_the_fetcher_from_the_environment(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_path: Path,
    ) -> None:
        # The only test that walks env -> CLI -> source -> fetcher.
        captured: list[Request] = []

        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            captured.append(request)
            return FakeResponse(openapi_fixture_path.read_bytes())

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        monkeypatch.setenv('CASTIRON_KEY', SECRET)
        result = run(runner, '--from', 'https://abcdefgh.supabase.co', '--output', 'out')
        assert result.exit_code == 0, result.output
        assert captured[0].full_url == 'https://abcdefgh.supabase.co/rest/v1/'
        assert captured[0].get_header('Apikey') == SECRET
        assert captured[0].get_header('Authorization') == f'Bearer {SECRET}'

    def test_the_supabase_key_variable_is_the_fallback(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_path: Path,
    ) -> None:
        captured: list[Request] = []

        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            captured.append(request)
            return FakeResponse(openapi_fixture_path.read_bytes())

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        monkeypatch.setenv('SUPABASE_KEY', SECRET)
        monkeypatch.setenv('SUPABASE_URL', 'https://abcdefgh.supabase.co')
        assert run(runner, '--output', 'out').exit_code == 0
        assert captured[0].get_header('Apikey') == SECRET

    def test_castiron_key_wins_over_supabase_key(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_path: Path,
    ) -> None:
        captured: list[Request] = []

        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            captured.append(request)
            return FakeResponse(openapi_fixture_path.read_bytes())

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        monkeypatch.setenv('CASTIRON_KEY', SECRET)
        monkeypatch.setenv('SUPABASE_KEY', 'the-other-project-key')
        assert run(runner, '--from', 'https://abcdefgh.supabase.co', '--output', 'out').exit_code == 0
        assert captured[0].get_header('Apikey') == SECRET

    def test_a_local_document_that_is_not_json_fails_with_exit_one(self, runner: CliRunner, project: Path) -> None:
        (project / 'broken.json').write_text('{not json', encoding='utf-8')
        result = run(runner, '--from', 'broken.json')
        assert result.exit_code == 1
        assert 'not valid JSON' in result.output

    def test_a_local_document_that_is_not_an_object_fails_with_exit_one(self, runner: CliRunner, project: Path) -> None:
        (project / 'list.json').write_text('[1, 2]', encoding='utf-8')
        result = run(runner, '--from', 'list.json')
        assert result.exit_code == 1
        assert 'not an object' in result.output

    def test_a_local_document_that_cannot_be_decoded_fails_with_exit_one(
        self, runner: CliRunner, project: Path
    ) -> None:
        (project / 'binary.json').write_bytes(b'\xff\xfe\x00')
        result = run(runner, '--from', 'binary.json')
        assert result.exit_code == 1
        assert 'Could not read' in result.output


# ---------------------------------------------------------------------------
# Usage errors (exit 2).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestUsageErrors:
    def test_a_missing_source_names_all_three_ways_to_supply_one(self, runner: CliRunner, project: Path) -> None:
        result = run(runner)
        assert result.exit_code == 2
        for mention in ('--from', 'CASTIRON_FROM', '[tool.castiron]'):
            assert mention in result.output

    def test_a_source_that_is_neither_a_url_nor_a_file_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'nope.json')
        assert result.exit_code == 2
        assert 'neither a URL nor an existing file' in result.output
        # Deliberately not silently prepended with https://.
        assert 'https://' in result.output

    def test_an_unknown_emitter_lists_the_registered_ones(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--emit', 'nope')
        assert result.exit_code == 2
        assert 'pydantic' in result.output

    def test_filename_with_more_than_one_emitter_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--emit', 'pydantic', '--emit', 'pydantic', '--filename', 'x.py')
        assert result.exit_code == 2
        assert '--filename applies to a single-emitter run' in result.output

    def test_an_empty_emitter_list_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        (project / 'pyproject.toml').write_text('[tool.castiron]\nemit = []\n', encoding='utf-8')
        result = run(runner, '--from', 'openapi.json')
        assert result.exit_code == 2
        assert 'No emitters selected' in result.output

    def test_a_nonexistent_config_file_is_a_usage_error(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--config', 'nope.toml', '--from', 'openapi.json')
        assert result.exit_code == 2


# ---------------------------------------------------------------------------
# Failure mapping (exit 1 / 70).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFailureMapping:
    @pytest.mark.parametrize('error', [SourceFetchError, SourceParseError])
    def test_a_source_failure_exits_one(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
    ) -> None:
        def fail(url: str, **kwargs: Any) -> Schema:
            raise error('the source said no')

        monkeypatch.setattr('castiron.cli.gen.load_openapi_schema', fail)
        result = run(runner, '--from', 'https://abcdefgh.supabase.co')
        assert result.exit_code == 1
        assert 'the source said no' in result.output

    def test_a_real_network_failure_exits_one_with_the_hint_worthy_message(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            raise URLError('nodename nor servname provided')

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        result = run(runner, '--from', 'https://typo.supabase.co')
        assert result.exit_code == 1
        assert 'Could not reach https://typo.supabase.co/rest/v1/' in result.output

    def test_an_unexpected_exception_exits_seventy_without_a_traceback(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(url: str, **kwargs: Any) -> Schema:
            raise RuntimeError('a castiron bug')

        monkeypatch.setattr('castiron.cli.gen.load_openapi_schema', boom)
        result = run(runner, '--from', 'https://abcdefgh.supabase.co')
        assert result.exit_code == 70
        assert 'internal error (RuntimeError' in result.output
        assert 'This is a bug' in result.output
        assert 'Traceback' not in result.output

    def test_debug_lets_the_exception_escape_so_python_prints_the_traceback(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(url: str, **kwargs: Any) -> Schema:
            raise RuntimeError('a castiron bug')

        monkeypatch.setattr('castiron.cli.gen.load_openapi_schema', boom)
        with pytest.raises(RuntimeError, match='a castiron bug'):
            run(runner, '--from', 'https://abcdefgh.supabase.co', '--debug', catch_exceptions=False)

    def test_no_overwrite_with_an_existing_target_exits_one(self, runner: CliRunner, project: Path) -> None:
        (project / 'schema.py').write_text('mine\n', encoding='utf-8')
        result = run(runner, '--from', 'openapi.json', '--no-overwrite')
        assert result.exit_code == 1
        assert 'already exists' in result.output
        assert (project / 'schema.py').read_text(encoding='utf-8') == 'mine\n'

    def test_two_emitters_writing_the_same_file_exits_one(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--emit', 'pydantic', '--emit', 'pydantic')
        assert result.exit_code == 1
        assert 'same path' in result.output

    def test_a_traversing_filename_exits_one(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--filename', '../escape.py', '--output', 'out')
        assert result.exit_code == 1
        assert not (project / 'escape.py').exists()


# ---------------------------------------------------------------------------
# Secrets.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSecrets:
    def test_a_query_string_key_is_redacted_out_of_an_error(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # normalize_postgrest_url preserves the query string and the source embeds the target
        # in its error message, so this is a genuine, reachable leak path.
        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            raise URLError('down')

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        result = run(runner, '--from', f'https://x.supabase.co/rest/v1/?apikey={SECRET}')
        assert result.exit_code == 1
        assert SECRET not in result.output
        assert 'apikey=***' in result.output

    def test_a_query_string_key_is_redacted_out_of_the_summary(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_path: Path,
    ) -> None:
        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            return FakeResponse(openapi_fixture_path.read_bytes())

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        result = run(runner, '--from', f'https://x.supabase.co/rest/v1/?apikey={SECRET}', '--output', 'out')
        assert result.exit_code == 0, result.output
        assert SECRET not in result.output
        assert 'apikey=***' in result.stdout

    def test_the_literal_key_value_never_reaches_the_terminal(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fail(url: str, **kwargs: Any) -> Schema:
            raise SourceFetchError(f'{url} returned HTTP 401 while presenting {kwargs["key"]}')

        monkeypatch.setattr('castiron.cli.gen.load_openapi_schema', fail)
        result = run(runner, '--from', 'https://x.supabase.co', '--key', SECRET)
        assert result.exit_code == 1
        assert SECRET not in result.output
        assert 'HTTP 401' in result.output

    def test_the_key_never_reaches_the_generated_file(
        self,
        runner: CliRunner,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
        openapi_fixture_path: Path,
    ) -> None:
        def fake_urlopen(request: Request, timeout: float | None = None) -> FakeResponse:
            return FakeResponse(openapi_fixture_path.read_bytes())

        monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', fake_urlopen)
        result = run(runner, '--from', 'https://x.supabase.co', '--key', SECRET, '--output', 'out')
        assert result.exit_code == 0, result.output
        assert SECRET not in (project / 'out' / 'schema.py').read_text(encoding='utf-8')

    def test_help_names_the_environment_variables_but_never_a_value(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('CASTIRON_KEY', SECRET)
        result = run(runner, '--help')
        assert 'CASTIRON_KEY' in result.output
        assert 'SUPABASE_KEY' in result.output
        assert SECRET not in result.output


# ---------------------------------------------------------------------------
# The emitter toggles: every EmitterConfig field is reachable and does something.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmitterToggles:
    @pytest.mark.parametrize(
        ('flag', 'marker', 'present_by_default'),
        [
            ('--no-crud-models', 'class UsersInsert', True),
            ('--no-enums', 'class PublicOrderStatusEnum', True),
            ('--no-foreign-keys', '# Foreign Keys', True),
            ('--singular-names', 'class Product(', False),
            ('--null-parent-classes', 'Parent', False),
        ],
    )
    def test_each_flag_changes_the_emitted_output(
        self, runner: CliRunner, project: Path, flag: str, marker: str, present_by_default: bool
    ) -> None:
        assert run(runner, '--from', 'openapi.json', '--output', 'before').exit_code == 0
        assert run(runner, '--from', 'openapi.json', '--output', 'after', flag).exit_code == 0
        before = (project / 'before' / 'schema.py').read_text(encoding='utf-8')
        after = (project / 'after' / 'schema.py').read_text(encoding='utf-8')
        assert (marker in before) is present_by_default
        assert (marker in after) is not present_by_default

    def test_model_prefix_protection_can_be_disabled(
        self, runner: CliRunner, project: Path, openapi_fixture_document: dict[str, Any]
    ) -> None:
        # The committed fixture has no `model_` column, so the document is extended in place
        # rather than a second fixture being committed.
        document = json.loads(json.dumps(openapi_fixture_document))
        document['definitions']['users']['properties']['model_name'] = {'format': 'text', 'type': 'string'}
        (project / 'prefixed.json').write_text(json.dumps(document), encoding='utf-8')

        assert run(runner, '--from', 'prefixed.json', '--output', 'protected').exit_code == 0
        assert run(runner, '--from', 'prefixed.json', '--output', 'open', '--no-model-prefix-protection').exit_code == 0
        assert 'protected_namespaces' not in (project / 'protected' / 'schema.py').read_text(encoding='utf-8')
        assert 'protected_namespaces' in (project / 'open' / 'schema.py').read_text(encoding='utf-8')

    def test_filename_overrides_the_emitter_default(self, runner: CliRunner, project: Path) -> None:
        assert run(runner, '--from', 'openapi.json', '--output', 'out', '--filename', 'models.py').exit_code == 0
        assert (project / 'out' / 'models.py').is_file()
        assert not (project / 'out' / 'schema.py').exists()


# ---------------------------------------------------------------------------
# Reporting: --dry-run, -q, -v.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReporting:
    def test_dry_run_writes_nothing_and_says_so(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out', '--dry-run')
        assert result.exit_code == 0
        assert not (project / 'out').exists()
        assert 'would write' in result.stdout
        assert '[dry run, nothing written]' in result.stdout

    def test_dry_run_reports_the_size_a_real_run_would_write(self, runner: CliRunner, project: Path) -> None:
        dry = run(runner, '--from', 'openapi.json', '--output', 'out', '--dry-run')
        real = run(runner, '--from', 'openapi.json', '--output', 'out')
        size = dry.stdout.split('(')[-1].split(')')[0]
        assert f'({size})' in real.stdout

    def test_quiet_suppresses_the_summary(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out', '-q')
        assert result.exit_code == 0
        assert result.stdout == ''
        assert (project / 'out' / 'schema.py').is_file()

    def test_quiet_does_not_suppress_errors(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'nope.json', '-q')
        assert result.exit_code == 2
        assert 'neither a URL nor an existing file' in result.output

    def test_the_identity_pk_warning_is_on_by_default(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out')
        assert 'integer primary key with no visible default' in result.stderr
        assert '--infer-generated-primary-keys' in result.stderr

    def test_the_identity_pk_warning_is_silenced_by_the_flag(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out', '--infer-generated-primary-keys')
        assert 'integer primary key with no visible default' not in result.stderr

    def test_verbose_shows_the_openapi_fidelity_note(self, runner: CliRunner, project: Path) -> None:
        quiet_run = run(runner, '--from', 'openapi.json', '--output', 'a')
        loud_run = run(runner, '--from', 'openapi.json', '--output', 'b', '-v')
        assert 'no database connection' not in quiet_run.stderr
        assert 'no database connection' in loud_run.stderr

    def test_double_verbose_adds_debug_provenance(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out', '-vv')
        assert 'DEBUG castiron.cli.gen' in result.stderr

    def test_debug_implies_debug_logging(self, runner: CliRunner, project: Path) -> None:
        result = run(runner, '--from', 'openapi.json', '--output', 'out', '--debug')
        assert 'DEBUG castiron.cli.gen' in result.stderr

    @pytest.mark.parametrize(('size', 'expected'), [(0, '0 B'), (999, '999 B'), (1000, '1.0 kB'), (14200, '14.2 kB')])
    def test_the_size_is_rendered_in_bytes_below_a_kilobyte(self, size: int, expected: str) -> None:
        assert format_size(size) == expected

    def test_repeated_invocations_do_not_stack_log_handlers(self, runner: CliRunner, project: Path) -> None:
        first = run(runner, '--from', 'openapi.json', '--output', 'out')
        second = run(runner, '--from', 'openapi.json', '--output', 'out')
        assert first.stderr.count('integer primary key') == 1
        assert second.stderr.count('integer primary key') == 1
