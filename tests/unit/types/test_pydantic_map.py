import ast
import re

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
        # ⚠ `point` used to be on this list and is NOT any more -- see the test below. This
        # comment is the only place a reader learns which entries are ports and which are not.
        assert PYDANTIC_TYPE_MAP['float'] == TypeResolution('Decimal', ('from decimal import Decimal',))
        assert PYDANTIC_TYPE_MAP['money'].python_type == 'Decimal'
        assert PYDANTIC_TYPE_MAP['line'].python_type == 'Any'
        assert PYDANTIC_TYPE_MAP['uuid'].python_type == 'UUID4'
        assert PYDANTIC_TYPE_MAP['json'] == PYDANTIC_TYPE_MAP['jsonb']

    def test_the_two_deliberate_divergences_from_upstream(self) -> None:
        """CI-092: the only two entries castiron does **not** carry verbatim, and why.

        Both were putting a ruff finding into every user's repository. Neither changes the shape
        castiron reports -- ``point`` is still a 2-tuple of floats and ``cidr`` is still an
        ``IPv4Network`` -- only the spelling and the import.
        """
        # `Tuple[float, float]` + `from typing import Tuple` -> UP035 once and UP006 per column.
        # The builtin needs no import at all: PEP 585 generics are valid RUNTIME expressions on
        # >=3.9, which matters because castiron emits `from __future__ import annotations` only
        # when the schema has foreign keys.
        assert PYDANTIC_TYPE_MAP['point'] == TypeResolution('tuple[float, float]')
        assert PYDANTIC_TYPE_MAP['point'].imports == ()
        # The import used to name `IPv6Network`, which the resolution never references -> F401.
        assert PYDANTIC_TYPE_MAP['cidr'] == TypeResolution('IPv4Network', ('from ipaddress import IPv4Network',))
        # The counter-check: `inet` imports two names and USES both. It is correct as it stands
        # and narrowing it would break the emitted module.
        assert PYDANTIC_TYPE_MAP['inet'] == TypeResolution(
            'IPv4Address | IPv6Address', ('from ipaddress import IPv4Address, IPv6Address',)
        )

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


#: ``typing`` names with a PEP 585 / PEP 604 equivalent, and what to write instead. Declared as
#: **data** so a map entry that reintroduces one fails without anyone remembering to look. The
#: list is deliberately wider than the two rules `make lint` selects (`UP006`, `UP007`): a guard
#: that only knows about the findings castiron has already hit is a guard about the past.
DEPRECATED_TYPING_ALIASES = {
    'Tuple': 'tuple',
    'List': 'list',
    'Dict': 'dict',
    'Set': 'set',
    'FrozenSet': 'frozenset',
    'Type': 'type',
    'Deque': 'collections.deque',
    'DefaultDict': 'collections.defaultdict',
    'OrderedDict': 'collections.OrderedDict',
    'Counter': 'collections.Counter',
    'ChainMap': 'collections.ChainMap',
    'Text': 'str',
    'Optional': 'X | None',
    'Union': 'X | Y',
    'Callable': 'collections.abc.Callable',
    'Iterable': 'collections.abc.Iterable',
    'Iterator': 'collections.abc.Iterator',
    'Generator': 'collections.abc.Generator',
    'Sequence': 'collections.abc.Sequence',
    'Mapping': 'collections.abc.Mapping',
    'MutableMapping': 'collections.abc.MutableMapping',
    'AbstractSet': 'collections.abc.Set',
}


def _bound_names(import_line: str) -> tuple[str, ...]:
    """Every name an import statement binds, parsed rather than string-matched."""
    statement = ast.parse(import_line).body[0]
    assert isinstance(statement, ast.Import | ast.ImportFrom), import_line
    return tuple(alias.asname or alias.name for alias in statement.names)


@pytest.mark.unit
class TestTheMapCannotReintroduceCi092:
    """A structural invariant over **all** 65 entries, derived from the map itself.

    ``CI-092`` shipped because nothing checked the map's *shape*: two entries carried an import of
    a name they never used and a ``typing`` alias with a builtin equivalent, and castiron wrote
    both into every user's repository. Fixing those two entries closes today's finding; this class
    is what stops the 66th entry reopening it.

    ⚠ Enumerated from ``PYDANTIC_TYPE_MAP`` itself, never a hand-written list of keys (``CI-072``,
    and exactly what ``cases.config_axes()`` does for the config sweep). A new entry is covered
    the moment it exists.
    """

    def test_the_sweep_actually_covers_the_whole_map(self) -> None:
        # A guard that silently narrows is worse than none. 65 entries as of CI-092; this is a
        # floor, not an equality, so adding a type does not fail for the wrong reason.
        assert len(PYDANTIC_TYPE_MAP) >= 65

    @pytest.mark.parametrize('key', sorted(PYDANTIC_TYPE_MAP))
    def test_no_entry_imports_a_name_it_does_not_use(self, key: str) -> None:
        """Shape (a): the ``F401`` shape. ``cidr`` imported an ``IPv6Network`` it never resolved to."""
        resolution = PYDANTIC_TYPE_MAP[key]
        tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', resolution.python_type))
        for line in resolution.imports:
            statement = ast.parse(line).body[0]
            for name in _bound_names(line):
                if isinstance(statement, ast.Import):
                    # `import datetime` is used via attribute access: `datetime.datetime`.
                    assert resolution.python_type.startswith(f'{name}.') or name.split('.')[0] in tokens, (
                        f'{key!r} runs `{line}` but never mentions {name!r} in '
                        f'{resolution.python_type!r} -- that is an F401 in the emitted module.'
                    )
                else:
                    assert name in tokens, (
                        f'{key!r} imports {name!r} from `{line}` but resolves to '
                        f'{resolution.python_type!r}, which never mentions it -- an F401 in the '
                        f'emitted module. Either use it or narrow the import.'
                    )

    @pytest.mark.parametrize('key', sorted(PYDANTIC_TYPE_MAP))
    def test_no_entry_uses_a_deprecated_typing_alias(self, key: str) -> None:
        """Shape (b): the ``UP006``/``UP035`` shape. ``point`` resolved to ``Tuple[float, float]``."""
        resolution = PYDANTIC_TYPE_MAP[key]
        tokens = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]*', resolution.python_type))
        for alias, replacement in DEPRECATED_TYPING_ALIASES.items():
            assert alias not in tokens, (
                f'{key!r} resolves to {resolution.python_type!r}, which uses the deprecated '
                f'typing.{alias} -- write {replacement!r} instead (UP006/UP007).'
            )
        for line in resolution.imports:
            statement = ast.parse(line).body[0]
            if isinstance(statement, ast.ImportFrom) and statement.module == 'typing':
                offenders = sorted(set(_bound_names(line)) & set(DEPRECATED_TYPING_ALIASES))
                assert offenders == [], (
                    f'{key!r} runs `{line}`, and {offenders} are deprecated typing aliases '
                    f'(UP035). The emitted module carries that import into a user repository.'
                )

    @pytest.mark.parametrize('key', sorted(PYDANTIC_TYPE_MAP))
    def test_every_import_line_is_a_real_import_statement(self, key: str) -> None:
        # The premise the two invariants above rest on: `imports` holds parseable import
        # statements, one per line. A blob with an embedded newline would slip past both.
        for line in PYDANTIC_TYPE_MAP[key].imports:
            assert '\n' not in line, f'{key!r}: {line!r} is more than one statement'
            assert _bound_names(line), f'{key!r}: {line!r} binds nothing'
