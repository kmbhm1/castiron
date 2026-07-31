"""The ``[tool.castiron]`` project config: discovery, validation, and the precedence chain.

The precedence assertions run through the real command rather than inspecting
``ctx.default_map``, because the property under test is click's own resolution order
(command line → environment → default map → default) and only an end-to-end run exercises it.
"""

import shutil
from pathlib import Path

import click
import pytest
from click.testing import CliRunner, Result

from castiron.cli import cli
from castiron.cli.config import (
    CONFIG_KEYS,
    ConfigError,
    anchor_path,
    canonical_key,
    config_option_callback,
    discover_config_file,
    load_config_table,
    resolve_config,
    valid_config_keys,
)
from castiron.emitters import EmitterConfig
from tests.unit.cli.conftest import write_config


def run(runner: CliRunner, *args: str) -> Result:
    return runner.invoke(cli, ['gen', *args])


def emitted(project: Path, directory: str = 'out') -> str:
    return (project / directory / 'schema.py').read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# Discovery.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDiscovery:
    def test_a_pyproject_in_the_start_directory_is_found(self, tmp_path: Path) -> None:
        expected = write_config(tmp_path, '[tool.castiron]\n')
        assert discover_config_file(tmp_path) == expected

    def test_a_pyproject_two_directories_up_is_found(self, tmp_path: Path) -> None:
        expected = write_config(tmp_path, '[tool.castiron]\n')
        nested = tmp_path / 'src' / 'app'
        nested.mkdir(parents=True)
        assert discover_config_file(nested) == expected

    def test_the_nearest_pyproject_wins(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[tool.castiron]\noutput = "far"\n')
        nested = tmp_path / 'package'
        nested.mkdir()
        nearest = write_config(nested, '[tool.castiron]\noutput = "near"\n')
        assert discover_config_file(nested) == nearest

    def test_the_walk_stops_at_the_first_hit_even_without_the_table(self, tmp_path: Path) -> None:
        # One pyproject.toml defines the project; continuing the walk could silently inherit a
        # parent monorepo's settings.
        write_config(tmp_path, '[tool.castiron]\noutput = "parent"\n')
        nested = tmp_path / 'package'
        nested.mkdir()
        nearest = write_config(nested, '[project]\nname = "child"\n')
        assert discover_config_file(nested) == nearest
        assert resolve_config(None, start=nested) == (nearest, {})

    def test_no_pyproject_anywhere_is_an_empty_config(self, tmp_path: Path) -> None:
        assert discover_config_file(tmp_path) is None
        assert resolve_config(None, start=tmp_path) == (None, {})

    def test_a_pyproject_without_the_table_is_an_empty_config(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[project]\nname = "x"\n')
        assert load_config_table(path, explicit=False) == {}

    def test_the_config_is_read_from_the_cwd_by_default(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "discovered"\n')
        assert run(runner).exit_code == 0
        assert (project / 'discovered' / 'schema.py').is_file()


# ---------------------------------------------------------------------------
# The explicit --config path.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestExplicitConfig:
    def test_a_standalone_file_uses_the_same_table_name(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "standalone"\n', 'castiron.toml')
        assert run(runner, '--config', 'castiron.toml').exit_code == 0
        assert (project / 'standalone' / 'schema.py').is_file()

    def test_an_explicit_file_without_the_table_is_an_error(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[project]\nname = "x"\n', 'castiron.toml')
        result = run(runner, '--config', 'castiron.toml', '--from', 'openapi.json')
        assert result.exit_code == 1
        assert 'castiron.toml' in result.output
        assert 'no [tool.castiron] table' in result.output

    def test_it_can_come_from_the_environment(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "from-env"\n', 'castiron.toml')
        monkeypatch.setenv('CASTIRON_CONFIG', str(project / 'castiron.toml'))
        assert run(runner).exit_code == 0
        assert (project / 'from-env' / 'schema.py').is_file()

    def test_it_beats_the_discovered_pyproject(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "discovered"\n')
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "explicit"\n', 'castiron.toml')
        assert run(runner, '--config', 'castiron.toml').exit_code == 0
        assert (project / 'explicit' / 'schema.py').is_file()
        assert not (project / 'discovered').exists()


# ---------------------------------------------------------------------------
# Precedence: command line > environment > config file > built-in default.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPrecedence:
    def test_the_built_in_default_applies_with_no_config(self, runner: CliRunner, project: Path) -> None:
        assert run(runner, '--from', 'openapi.json').exit_code == 0
        assert (project / 'schema.py').is_file()

    def test_the_config_beats_the_built_in_default(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "cfg"\n')
        assert run(runner).exit_code == 0
        assert (project / 'cfg' / 'schema.py').is_file()

    def test_the_command_line_beats_the_config(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "cfg"\n')
        assert run(runner, '--output', 'cli').exit_code == 0
        assert (project / 'cli' / 'schema.py').is_file()
        assert not (project / 'cfg').exists()

    def test_the_environment_beats_the_config(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (project / 'from-config.json').write_text('{"nope": true}', encoding='utf-8')
        write_config(project, '[tool.castiron]\nfrom = "from-config.json"\n')
        monkeypatch.setenv('CASTIRON_FROM', 'openapi.json')
        assert run(runner, '--output', 'out').exit_code == 0
        assert (project / 'out' / 'schema.py').is_file()

    def test_the_command_line_beats_the_environment(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv('CASTIRON_FROM', 'does-not-exist.json')
        assert run(runner, '--from', 'openapi.json', '--output', 'out').exit_code == 0
        assert (project / 'out' / 'schema.py').is_file()

    def test_a_string_option_resolves_through_the_config(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\nschema = "billing"\noutput = "out"\n')
        assert run(runner).exit_code == 0
        assert 'class BillingOrderStatusEnum' in emitted(project)

    def test_the_command_line_beats_the_config_for_a_string_option(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\nschema = "billing"\noutput = "out"\n')
        assert run(runner, '--schema', 'shipping').exit_code == 0
        assert 'class ShippingOrderStatusEnum' in emitted(project)

    def test_a_boolean_from_the_config_applies(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\ncrud-models = false\n')
        assert run(runner).exit_code == 0
        assert 'class UsersInsert' not in emitted(project)

    def test_a_positive_flag_overrides_a_false_config_value(self, runner: CliRunner, project: Path) -> None:
        # The both-directions property that justifies declaring every boolean as --x/--no-x:
        # with a bare `--crud-models` flag there would be no way to turn a config `false` back on.
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\ncrud-models = false\n')
        assert run(runner, '--crud-models').exit_code == 0
        assert 'class UsersInsert' in emitted(project)

    def test_a_negative_flag_overrides_a_true_config_value(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\ncrud-models = true\n')
        assert run(runner, '--no-crud-models').exit_code == 0
        assert 'class UsersInsert' not in emitted(project)

    def test_emit_replaces_and_never_merges(self, runner: CliRunner, project: Path) -> None:
        # Two pydantic emitters resolve to one path, so the merged reading would exit 1.
        write_config(
            project, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\nemit = ["pydantic", "pydantic"]\n'
        )
        assert run(runner).exit_code == 1
        assert run(runner, '--emit', 'pydantic').exit_code == 0

    def test_a_float_option_resolves_through_the_config(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\ntimeout = 5\n')
        assert load_config_table(path, explicit=False) == {'timeout': 5.0}


# ---------------------------------------------------------------------------
# Key spelling and the full key surface.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeySurface:
    @pytest.mark.parametrize('spelling', ['crud-models', 'crud_models'])
    def test_dashes_and_underscores_are_interchangeable(self, tmp_path: Path, spelling: str) -> None:
        path = write_config(tmp_path, f'[tool.castiron]\n{spelling} = false\n')
        assert load_config_table(path, explicit=False) == {'crud_models': False}

    def test_from_is_the_documented_alias_for_the_source_parameter(self, tmp_path: Path) -> None:
        # The value is also anchored to the config file's directory (CI6-D5a).
        path = write_config(tmp_path, '[tool.castiron]\nfrom = "x.json"\n')
        assert load_config_table(path, explicit=False) == {'source': str(tmp_path / 'x.json')}

    def test_every_emitter_config_toggle_has_a_config_key(self) -> None:
        # CI4-D4 says every behavioral toggle lives on EmitterConfig and "CLI wiring lands in
        # CI-006"; this is the assertion that none of them was forgotten.
        params = {param for param, _ in CONFIG_KEYS.values()}
        assert {
            'crud_models',
            'enums',
            'foreign_keys',
            'null_parent_classes',
            'singular_names',
            'model_prefix_protection',
            'filename',
        } <= params
        assert len(EmitterConfig.__dataclass_fields__) == 7

    def test_the_valid_key_list_is_documented_in_dashes(self) -> None:
        keys = valid_config_keys()
        assert 'infer-generated-primary-keys' in keys
        assert 'check' in keys
        assert not any('_' in key for key in keys)

    def test_canonicalization_is_idempotent(self) -> None:
        assert canonical_key(canonical_key('crud-models')) == 'crud_models'


# ---------------------------------------------------------------------------
# Validation: fail loudly, name the file and the key.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestValidation:
    def test_an_unknown_key_is_rejected_with_a_suggestion(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\ncrud-modles = false\n')
        result = run(runner)
        assert result.exit_code == 1
        assert "unknown key 'crud-modles'" in result.output
        assert "Did you mean 'crud-models'?" in result.output
        assert 'pyproject.toml' in result.output

    def test_an_unknown_key_with_no_near_match_still_lists_the_valid_ones(
        self, runner: CliRunner, project: Path
    ) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\nzzzzzzzz = 1\n')
        result = run(runner)
        assert result.exit_code == 1
        assert 'Did you mean' not in result.output
        assert 'Valid keys:' in result.output
        assert 'singular-names' in result.output

    @pytest.mark.parametrize(
        ('body', 'key', 'expected', 'actual'),
        [
            ('output = 3', 'output', 'a string', 'an integer'),
            ('emit = "pydantic"', 'emit', 'an array of strings', 'a string'),
            ('timeout = "fast"', 'timeout', 'a number', 'a string'),
            ('crud-models = "yes"', 'crud-models', 'a boolean', 'a string'),
            ('emit = [1, 2]', 'emit', 'an array of strings', 'an array'),
            # `true` must be reported as a boolean, not an integer: bool is an int subclass,
            # so the type-name check has to test bool first or every flag typo reads wrong.
            ('timeout = true', 'timeout', 'a number', 'a boolean'),
            ('output = 1.5', 'output', 'a string', 'a float'),
            ('output = { a = 1 }', 'output', 'a string', 'a table'),
            ('output = 1979-05-27T07:32:00Z', 'output', 'a string', 'a datetime'),
        ],
    )
    def test_a_wrong_type_names_the_key_and_both_types(
        self, runner: CliRunner, project: Path, body: str, key: str, expected: str, actual: str
    ) -> None:
        write_config(project, f'[tool.castiron]\nfrom = "openapi.json"\n{body}\n')
        result = run(runner)
        assert result.exit_code == 1
        assert f"'{key}' must be {expected}, but it is {actual}." in result.output
        assert 'pyproject.toml' in result.output

    def test_malformed_toml_names_the_file(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron\nfrom = ')
        result = run(runner, '--from', 'openapi.json')
        assert result.exit_code == 1
        assert 'pyproject.toml' in result.output
        assert 'not valid TOML' in result.output

    def test_an_unreadable_config_file_is_an_error(self, tmp_path: Path) -> None:
        directory = tmp_path / 'pyproject.toml'
        directory.mkdir()
        with pytest.raises(ConfigError, match='Could not read'):
            load_config_table(directory, explicit=True)

    def test_a_scalar_castiron_table_is_an_error(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool]\ncastiron = 1\n')
        with pytest.raises(ConfigError, match=r'\[tool.castiron\] must be a table'):
            load_config_table(path, explicit=False)


# ---------------------------------------------------------------------------
# The reserved [tool.castiron.check] table (CI-021).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestReservedCheckTable:
    def test_it_is_accepted_and_ignored(self, runner: CliRunner, project: Path) -> None:
        write_config(
            project,
            '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\n\n[tool.castiron.check]\nfail-on = "any"\n',
        )
        assert run(runner).exit_code == 0
        assert (project / 'out' / 'schema.py').is_file()

    def test_it_contributes_nothing_to_the_default_map(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\n\n[tool.castiron.check]\nfail-on = "any"\n')
        assert load_config_table(path, explicit=False) == {}

    def test_a_scalar_check_is_an_error(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\ncheck = 1\n')
        result = run(runner)
        assert result.exit_code == 1
        assert "'check' must be a table" in result.output
        assert 'castiron check' in result.output


# ---------------------------------------------------------------------------
# The API key is never readable from a committed file.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestKeyIsForbidden:
    def test_a_key_entry_is_rejected(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\nkey = "eyJhbGciOi-SUPERSECRET"\n')
        result = run(runner)
        assert result.exit_code == 1
        assert 'CASTIRON_KEY' in result.output
        assert 'SUPERSECRET' not in result.output

    def test_it_is_rejected_in_a_standalone_file_too(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nkey = "x"\n', 'castiron.toml')
        with pytest.raises(ConfigError, match='must not contain'):
            load_config_table(path, explicit=True)

    def test_the_underscore_spelling_is_rejected_the_same_way(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nkey = "x"\n')
        with pytest.raises(ConfigError, match='CASTIRON_KEY'):
            load_config_table(path, explicit=False)

    def test_key_is_not_in_the_valid_key_list(self) -> None:
        assert 'key' not in valid_config_keys()


# ---------------------------------------------------------------------------
# The eager callback itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigCallback:
    def test_shell_completion_never_reads_a_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # Under resilient parsing (shell completion) click tolerates half-parsed input; reading
        # -- and rejecting -- a config file there would break completion in a broken project.
        write_config(tmp_path, '[tool.castiron]\nnonsense = 1\n')
        monkeypatch.chdir(tmp_path)
        ctx = click.Context(cli, resilient_parsing=True)
        param = click.Option(['--config'])
        assert config_option_callback(ctx, param, None) is None
        assert ctx.default_map is None

    def test_it_merges_into_an_existing_default_map(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        write_config(tmp_path, '[tool.castiron]\noutput = "cfg"\n')
        monkeypatch.chdir(tmp_path)
        ctx = click.Context(cli, default_map={'schema': 'kept'})
        used = config_option_callback(ctx, click.Option(['--config']), None)
        assert used == tmp_path / 'pyproject.toml'
        assert ctx.default_map == {'schema': 'kept', 'output': str(tmp_path / 'cfg')}


# ---------------------------------------------------------------------------
# CI6-D5a: config-file paths are relative to the CONFIG FILE, not the cwd.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConfigRelativePaths:
    def test_a_relative_from_is_anchored_to_the_config_file(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nfrom = "./openapi.json"\n')
        assert load_config_table(path, explicit=False) == {'source': str(tmp_path / 'openapi.json')}

    def test_a_relative_output_is_anchored_to_the_config_file(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\noutput = "src/myapp/models"\n')
        assert load_config_table(path, explicit=False) == {'output': str(tmp_path / 'src' / 'myapp' / 'models')}

    @pytest.mark.parametrize('value', ['/opt/models', '/opt/models/', '/opt/./models'])
    def test_an_absolute_value_is_returned_exactly_as_written(self, tmp_path: Path, value: str) -> None:
        # Exact-string, not just "same directory": joining an absolute path onto a base is a
        # no-op on POSIX (pathlib discards the left operand), so an equality check against a
        # plain '/opt/models' would pass with the early return deleted. The trailing-slash and
        # './' forms are what actually pin the contract -- an absolute config value is passed
        # through verbatim rather than silently normalized in the summary.
        assert anchor_path(tmp_path, value) == value

    def test_an_absolute_output_reaches_the_default_map_unchanged(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\noutput = "/opt/models/"\n')
        assert load_config_table(path, explicit=False) == {'output': '/opt/models/'}

    def test_a_url_from_is_never_anchored(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nfrom = "https://abcdefgh.supabase.co"\n')
        assert load_config_table(path, explicit=False) == {'source': 'https://abcdefgh.supabase.co'}

    def test_filename_is_not_a_path_key(self, tmp_path: Path) -> None:
        # It names a file *under* --output, so anchoring it would make it absolute and the
        # traversal guard would (correctly) refuse it.
        path = write_config(tmp_path, '[tool.castiron]\nfilename = "models.py"\n')
        assert load_config_table(path, explicit=False) == {'filename': 'models.py'}

    def test_a_run_from_a_subdirectory_reads_and_writes_the_same_tree(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The reproduction: before CI6-D5a this exited 2 ("./openapi.json is neither a URL nor
        # an existing file"), and with an absolute `from` it silently wrote into the WRONG tree
        # at exit 0 -- which would make CI-021's `check` give a cwd-dependent answer.
        write_config(project, '[tool.castiron]\nfrom = "./openapi.json"\noutput = "src/myapp/models"\n')
        deeper = project / 'sub' / 'deeper'
        deeper.mkdir(parents=True)
        monkeypatch.chdir(deeper)
        result = run(runner)
        assert result.exit_code == 0, result.output
        assert (project / 'src' / 'myapp' / 'models' / 'schema.py').is_file()
        assert not (deeper / 'src').exists()

    def test_the_command_line_still_wins_and_stays_cwd_relative(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A path typed on the command line is relative to where you are standing, as always.
        write_config(project, '[tool.castiron]\nfrom = "./openapi.json"\noutput = "from-config"\n')
        deeper = project / 'sub'
        deeper.mkdir()
        monkeypatch.chdir(deeper)
        assert run(runner, '--output', 'from-cli').exit_code == 0
        assert (deeper / 'from-cli' / 'schema.py').is_file()

    def test_an_explicit_relative_config_anchors_to_its_own_directory(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = project / 'conf'
        nested.mkdir()
        shutil.copy(project / 'openapi.json', nested / 'openapi.json')
        write_config(nested, '[tool.castiron]\nfrom = "openapi.json"\noutput = "out"\n', 'castiron.toml')
        assert run(runner, '--config', 'conf/castiron.toml').exit_code == 0
        assert (nested / 'out' / 'schema.py').is_file()

    def test_the_summary_stays_short_when_the_target_is_under_the_cwd(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "./openapi.json"\noutput = "out"\n')
        result = run(runner)
        assert f'wrote {Path("out") / "schema.py"} (' in result.stdout

    def test_the_summary_shows_the_full_path_when_the_target_is_elsewhere(
        self, runner: CliRunner, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        write_config(project, '[tool.castiron]\nfrom = "./openapi.json"\noutput = "out"\n')
        deeper = project / 'sub'
        deeper.mkdir()
        monkeypatch.chdir(deeper)
        result = run(runner)
        assert str(project / 'out' / 'schema.py') in result.stdout


# ---------------------------------------------------------------------------
# `emit` values are validated where the file can be named (CI6-D6).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEmitValidation:
    def test_an_unregistered_emitter_names_the_config_file(self, runner: CliRunner, project: Path) -> None:
        write_config(project, '[tool.castiron]\nfrom = "openapi.json"\nemit = ["pydantik"]\n')
        result = run(runner)
        assert result.exit_code == 1
        assert 'pyproject.toml' in result.output
        assert "'pydantik'" in result.output
        assert 'Registered emitters: pydantic' in result.output

    def test_a_registered_emitter_is_accepted(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nemit = ["pydantic"]\n')
        assert load_config_table(path, explicit=False) == {'emit': ['pydantic']}

    def test_every_unknown_name_is_reported(self, tmp_path: Path) -> None:
        path = write_config(tmp_path, '[tool.castiron]\nemit = ["a", "pydantic", "b"]\n')
        with pytest.raises(ConfigError, match="'a', 'b'"):
            load_config_table(path, explicit=False)


@pytest.mark.unit
class TestArticles:
    @pytest.mark.parametrize(
        ('body', 'expected'),
        [
            ('[tool]\ncastiron = 1\n', 'but it is an integer'),
            ('[tool]\ncastiron = "x"\n', 'but it is a string'),
            ('[tool.castiron]\ncheck = 1\n', 'but it is an integer'),
            ('[tool.castiron]\ncheck = "x"\n', 'but it is a string'),
        ],
    )
    def test_the_article_agrees_with_the_type_name(self, tmp_path: Path, body: str, expected: str) -> None:
        path = write_config(tmp_path, body)
        with pytest.raises(ConfigError, match=expected):
            load_config_table(path, explicit=False)
