import dataclasses

import pytest

from castiron.emitters import EmittedFile, Emitter, EmitterConfig
from castiron.emitters.base import render_import_block, section_comment


@pytest.mark.unit
class TestEmittedFile:
    def test_is_frozen_value(self) -> None:
        f = EmittedFile(path='schema.py', content='x')
        assert (f.path, f.content) == ('schema.py', 'x')
        with pytest.raises(dataclasses.FrozenInstanceError):
            f.path = 'other.py'  # type: ignore[misc]


@pytest.mark.unit
class TestEmitterAbstract:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            Emitter()  # type: ignore[abstract]

    def test_base_emit_raises_not_implemented(self) -> None:
        from castiron.ir import Schema

        class _Concrete(Emitter):
            def emit(self, schema: Schema) -> list[EmittedFile]:
                return super().emit(schema)

        with pytest.raises(NotImplementedError):
            _Concrete().emit(Schema())


@pytest.mark.unit
class TestRenderImportBlock:
    def test_dedupes_and_sorts(self) -> None:
        block = render_import_block(['from b import y', 'from a import x', 'from b import y'])
        assert block == 'from a import x\nfrom b import y'

    def test_empty(self) -> None:
        assert render_import_block([]) == ''


@pytest.mark.unit
class TestSectionComment:
    def test_title_uppercased(self) -> None:
        assert section_comment('base classes') == '# BASE CLASSES'

    def test_notes_wrapped(self) -> None:
        result = section_comment('T', ['a short note'])
        assert result == '# T\n# a short note'

    def test_long_note_wraps_across_lines(self) -> None:
        note = 'word ' * 30
        lines = section_comment('T', [note.strip()]).splitlines()
        assert lines[0] == '# T'
        assert len(lines) > 2
        assert all(line.startswith('# ') for line in lines)


@pytest.mark.unit
class TestEmitterConfigDefaults:
    def test_defaults_reproduce_sp_shape(self) -> None:
        c = EmitterConfig()
        assert c.generate_crud_models is True
        assert c.generate_enums is True
        assert c.include_foreign_keys is True
        assert c.add_null_parent_classes is False
        assert c.disable_model_prefix_protection is False
        assert c.singular_names is False
        assert c.output_filename == 'schema.py'

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            EmitterConfig().generate_enums = False  # type: ignore[misc]
