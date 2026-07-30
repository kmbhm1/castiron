import pytest

from castiron.ir import ColumnInfo
from castiron.types import (
    DEFAULT_RESOLUTION,
    PYDANTIC_TYPE_MAP,
    TypeResolution,
    resolve,
    resolve_column_type,
)


def _col(raw_type: str, array_element_type: str | None = None) -> ColumnInfo:
    return ColumnInfo(name='c', raw_type=raw_type, array_element_type=array_element_type)


@pytest.mark.unit
class TestResolveScalar:
    @pytest.mark.parametrize(
        ('raw', 'py_type', 'imports'),
        [
            ('integer', 'int', ()),
            ('int4', 'int', ()),
            ('bigint', 'int', ()),
            ('text', 'str', ()),
            ('character varying', 'str', ()),
            ('boolean', 'bool', ()),
            ('numeric', 'Decimal', ('from decimal import Decimal',)),
            ('uuid', 'UUID4', ('from pydantic import UUID4',)),
            ('timestamp with time zone', 'datetime.datetime', ('import datetime',)),
            ('timestamptz', 'datetime.datetime', ('import datetime',)),
            ('date', 'datetime.date', ('import datetime',)),
            ('bytea', 'bytes', ()),
        ],
    )
    def test_scalar_types(self, raw: str, py_type: str, imports: tuple[str, ...]) -> None:
        result = resolve(raw, PYDANTIC_TYPE_MAP)
        assert result == TypeResolution(py_type, imports)

    def test_jsonb_union(self) -> None:
        result = resolve('jsonb', PYDANTIC_TYPE_MAP)
        assert result.python_type == 'dict | list[dict] | list[Any] | Json'
        assert result.imports == ('from typing import Any', 'from pydantic import Json')

    def test_case_insensitive(self) -> None:
        assert resolve('INTEGER', PYDANTIC_TYPE_MAP).python_type == 'int'

    def test_unknown_falls_back_to_default(self) -> None:
        assert resolve('nonexistent', PYDANTIC_TYPE_MAP) == DEFAULT_RESOLUTION
        assert resolve('USER-DEFINED', PYDANTIC_TYPE_MAP) == DEFAULT_RESOLUTION

    def test_custom_default(self) -> None:
        custom = TypeResolution('str', ())
        assert resolve('nope', PYDANTIC_TYPE_MAP, default=custom) == custom

    def test_bracket_suffix_wraps_list(self) -> None:
        assert resolve('integer[]', PYDANTIC_TYPE_MAP).python_type == 'list[int]'


@pytest.mark.unit
class TestResolveColumnType:
    def test_scalar_column(self) -> None:
        assert resolve_column_type(_col('integer'), PYDANTIC_TYPE_MAP).python_type == 'int'

    def test_user_defined_scalar_is_any(self) -> None:
        # Enum scalar columns resolve to Any from the map; the emitter overlays the enum.
        assert resolve_column_type(_col('USER-DEFINED'), PYDANTIC_TYPE_MAP) == DEFAULT_RESOLUTION

    def test_array_via_element_type(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='integer'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[int]'

    def test_array_bracket_raw_type_without_element(self) -> None:
        result = resolve_column_type(_col('integer[]'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[int]'

    def test_array_element_underscore_stripped(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='_int4'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[int]'

    def test_array_element_quotes_stripped(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='"FourthType"'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[Any]'
        assert result.imports == ('from typing import Any',)

    def test_array_element_trailing_brackets_stripped(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='text[]'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[str]'

    def test_unknown_array_element_is_list_any(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='mystery'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[Any]'

    def test_array_without_element_is_list_any(self) -> None:
        result = resolve_column_type(_col('array'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[Any]'

    def test_timestamp_array_carries_datetime_import(self) -> None:
        result = resolve_column_type(_col('ARRAY', array_element_type='timestamp with time zone'), PYDANTIC_TYPE_MAP)
        assert result.python_type == 'list[datetime.datetime]'
        assert result.imports == ('import datetime',)

    @pytest.mark.parametrize(
        ('vocab_a', 'vocab_b'),
        [
            ('integer', 'int4'),
            ('text', '_text'),
            ('timestamp with time zone', 'timestamptz'),
            ('character varying', 'varchar'),
        ],
    )
    def test_array_element_vocabulary_parity(self, vocab_a: str, vocab_b: str) -> None:
        """CI4-Q3: array-element resolution is type-equivalent across pg vocabularies."""
        a = resolve_column_type(_col('ARRAY', array_element_type=vocab_a), PYDANTIC_TYPE_MAP)
        b = resolve_column_type(_col('ARRAY', array_element_type=vocab_b), PYDANTIC_TYPE_MAP)
        assert a == b


@pytest.mark.unit
def test_imports_are_single_lines() -> None:
    """No import carries an embedded newline (they must be individually dedupable)."""
    for resolution in PYDANTIC_TYPE_MAP.values():
        for line in resolution.imports:
            assert '\n' not in line
