"""The write path -- where Hard Rule #9 (byte stability) lives or dies."""

from pathlib import Path

import pytest

from castiron.cli.output import OutputError, WriteResult, resolve_output_path, write_emitted_files
from castiron.emitters import EmittedFile


def emitted(path: str = 'schema.py', content: str = 'x = 1\n') -> EmittedFile:
    return EmittedFile(path, content)


@pytest.mark.unit
class TestResolveOutputPath:
    def test_a_relative_path_joins_under_the_output_directory(self, tmp_path: Path) -> None:
        assert resolve_output_path(tmp_path, emitted('schema.py')) == tmp_path / 'schema.py'

    def test_a_nested_relative_path_is_preserved(self, tmp_path: Path) -> None:
        assert resolve_output_path(tmp_path, emitted('models/schema.py')) == tmp_path / 'models' / 'schema.py'

    def test_an_absolute_path_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match='must be relative'):
            resolve_output_path(tmp_path, emitted('/etc/passwd'))

    def test_a_traversing_path_is_refused(self, tmp_path: Path) -> None:
        # Reachable from user input today: --filename and the config file both feed this.
        with pytest.raises(OutputError, match=r"must not contain '\.\.'"):
            resolve_output_path(tmp_path, emitted('../escape.py'))

    def test_a_traversing_segment_in_the_middle_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError):
            resolve_output_path(tmp_path, emitted('models/../../escape.py'))


@pytest.mark.unit
class TestWriteEmittedFiles:
    def test_it_writes_the_content_verbatim(self, tmp_path: Path) -> None:
        results = write_emitted_files([emitted(content='a = 1\nb = 2\n')], tmp_path)
        assert results == [WriteResult(tmp_path / 'schema.py', 12, True)]
        assert (tmp_path / 'schema.py').read_text(encoding='utf-8') == 'a = 1\nb = 2\n'

    def test_it_creates_a_missing_output_directory(self, tmp_path: Path) -> None:
        target = tmp_path / 'deeply' / 'nested'
        write_emitted_files([emitted()], target)
        assert (target / 'schema.py').is_file()

    def test_it_reuses_an_existing_output_directory(self, tmp_path: Path) -> None:
        (tmp_path / 'out').mkdir()
        (tmp_path / 'out' / 'keep.txt').write_text('keep', encoding='utf-8')
        write_emitted_files([emitted()], tmp_path / 'out')
        assert (tmp_path / 'out' / 'keep.txt').read_text(encoding='utf-8') == 'keep'

    def test_it_creates_parents_for_a_nested_emitted_path(self, tmp_path: Path) -> None:
        write_emitted_files([emitted('models/deep/schema.py')], tmp_path)
        assert (tmp_path / 'models' / 'deep' / 'schema.py').is_file()

    def test_it_overwrites_by_default(self, tmp_path: Path) -> None:
        (tmp_path / 'schema.py').write_text('stale\n', encoding='utf-8')
        write_emitted_files([emitted(content='fresh\n')], tmp_path)
        assert (tmp_path / 'schema.py').read_text(encoding='utf-8') == 'fresh\n'

    def test_no_overwrite_refuses_an_existing_target(self, tmp_path: Path) -> None:
        (tmp_path / 'schema.py').write_text('stale\n', encoding='utf-8')
        with pytest.raises(OutputError, match='already exists'):
            write_emitted_files([emitted()], tmp_path, overwrite=False)
        assert (tmp_path / 'schema.py').read_text(encoding='utf-8') == 'stale\n'

    def test_no_overwrite_is_all_or_nothing(self, tmp_path: Path) -> None:
        # The pre-flight must check EVERY target before writing ANY of them: a half-generated
        # output tree is worse than none. `first.py` is checked second, so an inline check
        # would already have written it by the time `schema.py` failed.
        (tmp_path / 'schema.py').write_text('stale\n', encoding='utf-8')
        files = [emitted('first.py'), emitted('schema.py')]
        with pytest.raises(OutputError):
            write_emitted_files(files, tmp_path, overwrite=False)
        assert not (tmp_path / 'first.py').exists()

    def test_two_files_resolving_to_the_same_path_are_refused(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError, match='same path'):
            write_emitted_files([emitted(), emitted()], tmp_path)

    def test_the_collision_guard_fires_before_anything_is_written(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError):
            write_emitted_files([emitted('a.py'), emitted('b.py'), emitted('a.py')], tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_a_traversing_path_is_refused_before_anything_is_written(self, tmp_path: Path) -> None:
        with pytest.raises(OutputError):
            write_emitted_files([emitted('a.py'), emitted('../escape.py')], tmp_path)
        assert list(tmp_path.iterdir()) == []

    def test_an_unwritable_target_raises_output_error(self, tmp_path: Path) -> None:
        # A directory where the file should go: the write fails with OSError, which must not
        # escape as a traceback (exit 70) -- it is an actionable user error (exit 1).
        (tmp_path / 'schema.py').mkdir()
        with pytest.raises(OutputError, match='Could not write'):
            write_emitted_files([emitted()], tmp_path)


@pytest.mark.unit
class TestByteFidelity:
    def test_the_write_pins_the_newline_translation_off(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The load-bearing assertion, and it has to be made on the *call*: with newline=None
        # Python's text layer rewrites \n -> \r\n only on Windows, so reading the file back on
        # POSIX would pass whether or not the parameter is there -- a guard that cannot fail is
        # not a guard. Dropping newline='\n' makes CI-021's `check` report permanent drift for
        # every Windows user, and this is the only portable way to catch it.
        seen: dict[str, object] = {}
        real_write_text = Path.write_text

        def spy(self: Path, data: str, **kwargs: object) -> int:
            seen.update(kwargs)
            return real_write_text(self, data, **kwargs)  # type: ignore[arg-type] - passthrough spy

        monkeypatch.setattr(Path, 'write_text', spy)
        write_emitted_files([emitted(content='line one\n')], tmp_path)
        assert seen == {'encoding': 'utf-8', 'newline': '\n'}

    def test_newlines_are_never_translated(self, tmp_path: Path) -> None:
        write_emitted_files([emitted(content='line one\nline two\n')], tmp_path)
        assert (tmp_path / 'schema.py').read_bytes() == b'line one\nline two\n'

    def test_existing_carriage_returns_are_preserved_verbatim(self, tmp_path: Path) -> None:
        write_emitted_files([emitted(content='a\r\nb\n')], tmp_path)
        assert (tmp_path / 'schema.py').read_bytes() == b'a\r\nb\n'

    def test_non_ascii_content_round_trips_as_utf8(self, tmp_path: Path) -> None:
        write_emitted_files([emitted(content='# schema→typed-code ✓\n')], tmp_path)
        assert (tmp_path / 'schema.py').read_bytes() == '# schema→typed-code ✓\n'.encode()

    def test_the_reported_size_is_the_utf8_byte_length(self, tmp_path: Path) -> None:
        results = write_emitted_files([emitted(content='→\n')], tmp_path)
        assert results[0].size == 4
        assert (tmp_path / 'schema.py').stat().st_size == 4


@pytest.mark.unit
class TestDryRun:
    def test_it_writes_nothing_and_creates_no_directory(self, tmp_path: Path) -> None:
        target = tmp_path / 'out'
        results = write_emitted_files([emitted('models/schema.py')], target, dry_run=True)
        assert not target.exists()
        assert results[0].written is False

    def test_it_reports_the_same_paths_and_sizes_as_a_real_run(self, tmp_path: Path) -> None:
        files = [emitted('a.py', 'x = 1\n'), emitted('b.py', '→ = 2\n')]
        dry = write_emitted_files(files, tmp_path / 'out', dry_run=True)
        real = write_emitted_files(files, tmp_path / 'out')
        assert [(r.path, r.size) for r in dry] == [(r.path, r.size) for r in real]
        assert [r.written for r in real] == [True, True]

    def test_it_still_enforces_the_no_overwrite_pre_flight(self, tmp_path: Path) -> None:
        (tmp_path / 'schema.py').write_text('stale\n', encoding='utf-8')
        with pytest.raises(OutputError):
            write_emitted_files([emitted()], tmp_path, overwrite=False, dry_run=True)
