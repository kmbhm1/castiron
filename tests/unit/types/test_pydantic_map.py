import pytest

from castiron.types import PYDANTIC_TYPE_MAP, TypeResolution


@pytest.mark.unit
class TestPydanticMap:
    def test_both_pg_vocabularies_present(self) -> None:
        # Internal forms and information_schema/format_type forms both exist.
        for internal, canonical in [
            ('int4', 'integer'),
            ('int8', 'bigint'),
            ('timestamptz', 'timestamp with time zone'),
            ('varchar', 'character varying'),
        ]:
            assert internal in PYDANTIC_TYPE_MAP
            assert canonical in PYDANTIC_TYPE_MAP
            assert PYDANTIC_TYPE_MAP[internal].python_type == PYDANTIC_TYPE_MAP[canonical].python_type

    def test_fidelity_choices_ported_verbatim(self) -> None:
        # These are supabase-pydantic's deliberate choices, carried (not "fixed") in CI-004.
        assert PYDANTIC_TYPE_MAP['float'] == TypeResolution('Decimal', ('from decimal import Decimal',))
        assert PYDANTIC_TYPE_MAP['money'].python_type == 'Decimal'
        assert PYDANTIC_TYPE_MAP['point'].python_type == 'Tuple[float, float]'
        assert PYDANTIC_TYPE_MAP['line'].python_type == 'Any'
        assert PYDANTIC_TYPE_MAP['uuid'].python_type == 'UUID4'
        assert PYDANTIC_TYPE_MAP['json'] == PYDANTIC_TYPE_MAP['jsonb']

    def test_character_resolves_to_str(self) -> None:
        # CI-005 gap-fill: ``char(n)`` reports as ``character`` through
        # information_schema.data_type, format_type() and PostgREST's ``format`` alike,
        # yet the ported map had only ``char``/``char(n)``/``character(n)``.
        assert PYDANTIC_TYPE_MAP['character'] == TypeResolution('str')
        for token in ('char', 'char(n)', 'character(n)', 'character varying'):
            assert PYDANTIC_TYPE_MAP[token].python_type == PYDANTIC_TYPE_MAP['character'].python_type

    def test_values_are_type_resolutions(self) -> None:
        assert all(isinstance(v, TypeResolution) for v in PYDANTIC_TYPE_MAP.values())

    def test_map_is_import_clean_of_pydantic(self) -> None:
        """The map module defines type strings only; it must not import pydantic at runtime."""
        import castiron.types.pydantic_map as module

        assert not hasattr(module, 'BaseModel')
