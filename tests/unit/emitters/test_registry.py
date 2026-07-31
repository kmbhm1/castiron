"""The emitter registry: the one-line seam CI-012/030/031 register through."""

import pytest

from castiron.emitters import EMITTERS, EmitterConfig, EmitterSpec, PydanticEmitter, get_emitter_spec


@pytest.mark.unit
class TestRegistry:
    def test_pydantic_is_registered(self) -> None:
        spec = get_emitter_spec('pydantic')
        assert isinstance(spec, EmitterSpec)
        assert spec.name == 'pydantic'
        assert spec.default_filename == 'schema.py'

    def test_the_factory_builds_the_emitter_from_a_config(self) -> None:
        emitter = get_emitter_spec('pydantic').build(EmitterConfig(output_filename='models.py'))
        assert isinstance(emitter, PydanticEmitter)
        assert emitter.config.output_filename == 'models.py'

    def test_an_unregistered_name_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            get_emitter_spec('nope')

    def test_every_entry_is_keyed_by_its_own_name(self) -> None:
        assert all(name == spec.name for name, spec in EMITTERS.items())

    def test_the_spec_is_frozen(self) -> None:
        spec = get_emitter_spec('pydantic')
        with pytest.raises(AttributeError):
            spec.name = 'other'  # type: ignore[misc] - proving the dataclass is frozen
