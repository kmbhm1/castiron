import ast
import dataclasses
import random
import sys
from pathlib import Path

import pytest

import castiron.emitters
from castiron.emitters import EmittedFile, Emitter, EmitterConfig
from castiron.emitters.base import STDLIB_MODULES, render_import_block, section_comment
from castiron.types import PYDANTIC_TYPE_MAP

#: Every import line the Pydantic emitter can put in a module from its own literals. Kept next to
#: the type map (which supplies the rest) so the two halves of the vocabulary are asserted
#: together -- `test_the_emitter_literals_are_still_the_ones_in_the_emitter` keeps this honest.
EMITTER_LITERAL_IMPORTS = (
    'from __future__ import annotations',
    'from enum import Enum',
    'from pydantic import BaseModel',
    'from pydantic import ConfigDict',
    'from pydantic import Field',
    'from pydantic import StringConstraints',
    'from typing import Annotated',
)

#: Modules castiron deliberately treats as third-party. Declared rather than inferred so that a
#: new type-map entry importing from an unclassified package fails a test instead of silently
#: landing in the third-party block.
KNOWN_THIRD_PARTY_MODULES = frozenset({'pydantic'})


def is_import_statement(text: str) -> bool:
    """Whether ``text`` is exactly one ``import``/``from`` statement.

    ⚠ Parsed, not prefix-matched. ``base.py`` carries the bare literals ``'from '`` and
    ``'import '`` for :meth:`str.removeprefix`, and a ``startswith`` filter collects those too --
    yielding a module named ``''`` and a guard failure that says nothing useful.

    ⚠ ``ValueError`` is caught alongside ``SyntaxError`` because they are **the same condition on
    different interpreters**: the emitter carries NUL-bearing literals for CI-009, and
    ``ast.parse('\\x00')`` raises ``ValueError: source code string cannot contain null bytes`` on
    **py3.10** where later versions raise ``SyntaxError``. Caught by ``make test-matrix`` on the
    3.10 leg and nowhere else -- the CI-081/CI-082 shape exactly.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    # `ast.Import | ast.ImportFrom`, not `(ast.Import, ast.ImportFrom)`: the pre-push hook pins
    # ruff 0.6.9, whose UP038 rejects the tuple form, while the resolved 0.16.0 dropped that rule
    # and accepts both. The same CI-105 pinned-vs-resolved ruff skew -- and it means
    # a green `make validate` does not imply a green `git push`. PEP 604 isinstance is 3.10+, and
    # 3.10 is the floor.
    return len(tree.body) == 1 and isinstance(tree.body[0], ast.Import | ast.ImportFrom)


def import_literals_in(source: str) -> set[str]:
    """Return every string constant in ``source`` that is itself a single import statement."""
    return {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and is_import_statement(node.value)
    }


def emittable_import_lines() -> set[str]:
    """Every import line reachable from the type map plus **every** emitter's own literals.

    ⚠ Enumerated by walking `src/castiron/emitters/`, not from a hand-written list scoped to the
    Pydantic emitter. `render_import_block` and `STDLIB_MODULES` are shared with CI-012
    (SQLAlchemy) and CI-030 (the typed client), and a new emitter's `import uuid` or
    `from collections.abc import Sequence` would land in the third-party block and silently
    re-open I001 for its users. Deriving from the package means the classification guard covers a
    new emitter the moment its module exists -- nobody has to remember to widen this (CI-072).
    """
    lines: set[str] = set()
    for path in sorted(Path(castiron.emitters.__file__).parent.rglob('*.py')):
        lines |= import_literals_in(path.read_text(encoding='utf-8'))
    for resolution in PYDANTIC_TYPE_MAP.values():
        lines.update(resolution.imports)
    return lines


def module_of(line: str) -> str:
    """Return the module an import line names."""
    if line.startswith('from '):
        return line.removeprefix('from ').partition(' import ')[0]
    return line.removeprefix('import ').strip()


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
    """The isort-compatible contract (CI-094 / `CI94-Q3`).

    ⚠ The pre-CI-094 version of this class used modules ``a`` and ``b``, which both land in the
    third-party section -- so it exercised **no grouping at all** and would have stayed green
    through the entire change. Every assertion below names a module whose section is decided by
    :data:`castiron.emitters.base.STDLIB_MODULES`.
    """

    def test_dedupes_and_sorts(self) -> None:
        block = render_import_block(['from b import y', 'from a import x', 'from b import y'])
        assert block == 'from a import x\nfrom b import y'

    def test_empty(self) -> None:
        assert render_import_block([]) == ''

    def test_the_three_sections_are_ordered_and_separated_by_one_blank_line(self) -> None:
        block = render_import_block(
            ['from pydantic import BaseModel', 'from decimal import Decimal', 'from __future__ import annotations']
        )
        assert (
            block
            == 'from __future__ import annotations\n\nfrom decimal import Decimal\n\nfrom pydantic import BaseModel'
        )

    def test_a_plain_import_precedes_every_from_import_in_its_section(self) -> None:
        # isort's `force-sort-within-sections = false` default. A naive string sort puts
        # `import datetime` LAST, which is how the old renderer produced I001 on every module.
        block = render_import_block(['from typing import Any', 'import datetime', 'from decimal import Decimal'])
        assert block.splitlines() == ['import datetime', 'from decimal import Decimal', 'from typing import Any']

    def test_same_module_from_imports_are_merged_onto_one_line(self) -> None:
        block = render_import_block(
            ['from ipaddress import IPv4Network, IPv6Network', 'from ipaddress import IPv4Address, IPv6Address']
        )
        assert block == 'from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network'

    def test_names_are_ordered_constant_then_class_then_the_rest(self) -> None:
        # `order-by-type`: 'UUID4'.isupper() is True, so it is a CONSTANT and sorts before the
        # classes. Plain alphabetical order leaves the line I001-dirty -- this is the single rule
        # a from-first-principles implementation is most likely to miss.
        block = render_import_block(
            [
                'from pydantic import BaseModel',
                'from pydantic import UUID4',
                'from pydantic import Json',
                'from pydantic import quirk',
                'from pydantic import ConfigDict',
            ]
        )
        assert block == 'from pydantic import UUID4, BaseModel, ConfigDict, Json, quirk'

    def test_module_ordering_is_case_insensitive(self) -> None:
        block = render_import_block(['from Zeta import a', 'from alpha import b'])
        assert block.splitlines() == ['from alpha import b', 'from Zeta import a']

    def test_a_submodule_is_classified_by_its_top_level_package(self) -> None:
        block = render_import_block(['import datetime.timezone', 'from vendor.pkg import Thing'])
        assert block == 'import datetime.timezone\n\nfrom vendor.pkg import Thing'

    def test_an_unknown_module_falls_back_to_third_party(self) -> None:
        # The safe direction: a misclassification shows up as a wrongly-grouped line in a golden
        # diff, never as a green suite on three interpreters and a red one on the fourth.
        block = render_import_block(['from decimal import Decimal', 'from unheard_of import Thing'])
        assert block == 'from decimal import Decimal\n\nfrom unheard_of import Thing'

    def test_a_plain_and_a_from_import_of_one_module_coexist(self) -> None:
        block = render_import_block(['from datetime import timezone', 'import datetime'])
        assert block == 'import datetime\nfrom datetime import timezone'

    def test_a_single_character_name_is_not_a_constant(self) -> None:
        # `order-by-type` classifies CONSTANT as all-upper AND longer than one character, so `T`
        # ranks as a variable and sorts LAST. Measured against ruff: `T, Ab, ITEM2` -> `ITEM2, Ab,
        # T`. `name.isupper()` alone puts `T` first and leaves the line I001-dirty.
        assert render_import_block(['from typing import T, Ab, IO']) == 'from typing import IO, Ab, T'

    def test_names_are_ordered_naturally_not_lexicographically(self) -> None:
        # isort compares digit runs numerically: Item2 before Item10, where sorted() gives the
        # reverse. Measured against ruff, on both the CONSTANT and the CLASS rank.
        block = render_import_block(['from typing import Item2, Item10, ITEM2, ITEM10'])
        assert block == 'from typing import ITEM2, ITEM10, Item2, Item10'

    def test_modules_are_ordered_naturally_too(self) -> None:
        block = render_import_block(['from mod10 import a', 'from mod2 import b', 'import pkg10', 'import pkg2'])
        assert block.splitlines() == ['import pkg2', 'import pkg10', 'from mod2 import b', 'from mod10 import a']


@pytest.mark.unit
class TestDivergencesFromRealIsort:
    """The three shapes where this renderer is **not** isort, and the proof they are unreachable.

    ``render_import_block`` is deliberately not a general isort implementation -- it is exact for
    the import lines castiron's emitters actually produce, which is verified exhaustively over the
    complete power set of that vocabulary. These tests pin the gap in both directions: what the
    renderer does today (so the divergence is a recorded decision, not a lurking surprise), and
    that **nothing castiron can emit reaches it** (so the fidelity claim stays true).

    ⚠ The second half is the load-bearing one. ``render_import_block`` and :data:`STDLIB_MODULES`
    are shared with CI-012 and CI-030, so the day a new emitter emits ``import uuid`` or a
    relative import, the unreachability assertions go red *here* rather than as ``I001`` in a
    user's repository.
    """

    def test_an_unlisted_stdlib_module_lands_in_third_party(self) -> None:
        # ruff would group `uuid` with `decimal`; castiron groups it with `pydantic`. The fix is
        # to extend STDLIB_MODULES, which is what the unreachability test below forces.
        block = render_import_block(['import uuid', 'from decimal import Decimal'])
        assert block == 'from decimal import Decimal\n\nimport uuid'

    def test_a_relative_import_gets_no_localfolder_section(self) -> None:
        # ruff emits a fourth block, after third party. castiron sorts it into third party.
        block = render_import_block(['from .local import Thing', 'from pydantic import BaseModel'])
        assert block == 'from .local import Thing\nfrom pydantic import BaseModel'

    def test_an_aliased_name_is_merged_where_ruff_would_split_it(self) -> None:
        # ruff writes `from m import a as b` on its own statement.
        block = render_import_block(['from m import a as b', 'from m import c'])
        assert block == 'from m import a as b, c'

    def test_no_emittable_import_reaches_any_of_those_three_shapes(self) -> None:
        lines = emittable_import_lines()
        relative = sorted(line for line in lines if line.startswith('from .'))
        aliased = sorted(line for line in lines if ' as ' in line)
        unlisted = sorted(
            line for line in lines if module_of(line) not in STDLIB_MODULES | KNOWN_THIRD_PARTY_MODULES | {'__future__'}
        )
        assert (relative, aliased, unlisted) == ([], [], []), (
            f'castiron can now emit an import shape where render_import_block is NOT isort: '
            f'relative={relative}, aliased={aliased}, unclassified={unlisted}. The fidelity claim '
            f"in render_import_block's docstring no longer holds -- either extend the renderer "
            f'(LOCALFOLDER section / alias splitting) or STDLIB_MODULES, and narrow the docstring.'
        )


@pytest.mark.unit
class TestRenderImportBlockIsOrderInsensitive:
    """Hard Rule #9, at the exact place grouping could break it.

    ``PydanticEmitter._imports`` hands this function a ``set``, whose iteration order varies with
    ``PYTHONHASHSEED``. The old renderer was one total ``sorted()`` over raw strings; grouping
    replaces that with three nested orderings, and any one left partial re-opens the hazard
    silently -- correct on the author's machine, different on CI. ``CI-065`` is the precedent: a
    ``sorted(..., key=len)`` that shipped.
    """

    VOCABULARY = (
        'from __future__ import annotations',
        'import datetime',
        'from decimal import Decimal',
        'from enum import Enum',
        'from ipaddress import IPv4Address, IPv6Address',
        'from ipaddress import IPv4Network, IPv6Network',
        'from typing import Annotated',
        'from typing import Any',
        'from pydantic import BaseModel',
        'from pydantic import ConfigDict',
        'from pydantic import Field',
        'from pydantic import Json',
        'from pydantic import StringConstraints',
        'from pydantic import UUID4',
    )

    def test_a_thousand_shuffles_render_exactly_one_output(self) -> None:
        rng = random.Random(20260803)
        outputs = set()
        for _ in range(1000):
            shuffled = list(self.VOCABULARY)
            rng.shuffle(shuffled)
            outputs.add(render_import_block(shuffled))
        assert len(outputs) == 1, f'{len(outputs)} distinct renderings from one set of import lines'

    def test_every_container_type_and_duplicates_render_identically(self) -> None:
        expected = render_import_block(self.VOCABULARY)
        assert render_import_block(list(self.VOCABULARY)) == expected
        assert render_import_block(set(self.VOCABULARY)) == expected
        assert render_import_block(tuple(reversed(self.VOCABULARY))) == expected
        assert render_import_block([*self.VOCABULARY, *self.VOCABULARY, *self.VOCABULARY]) == expected
        assert render_import_block(iter(self.VOCABULARY)) == expected

    def test_the_full_vocabulary_renders_the_derived_target(self) -> None:
        # Derived by running `ruff check --isolated --select I --fix`, not from the isort docs.
        assert render_import_block(self.VOCABULARY) == (
            'from __future__ import annotations\n'
            '\n'
            'import datetime\n'
            'from decimal import Decimal\n'
            'from enum import Enum\n'
            'from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network\n'
            'from typing import Annotated, Any\n'
            '\n'
            'from pydantic import UUID4, BaseModel, ConfigDict, Field, Json, StringConstraints'
        )


@pytest.mark.unit
class TestStdlibClassificationTable:
    """Two guards on :data:`STDLIB_MODULES`, because it is a hand-maintained table (`CI94-D8`).

    It is hand-maintained on purpose: :data:`sys.stdlib_module_names` differs between 3.10 and
    3.13, so classifying with it would make the emitted bytes a function of the running
    interpreter. These two tests buy back what the automatic version would have given.
    """

    def test_the_emitter_literals_are_still_the_ones_in_the_emitter(self) -> None:
        # EMITTER_LITERAL_IMPORTS is hand-written, and half the vocabulary rides on it. Read the
        # emitter's own source rather than trusting the copy: an import added to `_imports` and
        # not added here would silently leave a whole section of the vocabulary unchecked.
        import castiron.emitters.pydantic.emitter as emitter_module

        in_source = import_literals_in(Path(emitter_module.__file__).read_text(encoding='utf-8'))
        assert in_source == set(EMITTER_LITERAL_IMPORTS), (
            f'the emitter emits {sorted(in_source)} but this module declares '
            f'{sorted(EMITTER_LITERAL_IMPORTS)}. Keep them equal, or the vocabulary guards below '
            f'silently stop covering the difference.'
        )

    def test_every_emittable_import_names_a_classified_module(self) -> None:
        # Enumerated from PYDANTIC_TYPE_MAP, so a new entry importing from an unclassified
        # package fails here rather than silently landing in the third-party block (CI-072).
        classified = STDLIB_MODULES | KNOWN_THIRD_PARTY_MODULES | {'__future__'}
        unclassified = sorted({module_of(line) for line in emittable_import_lines()} - classified)
        assert unclassified == [], (
            f'{unclassified} is imported by castiron but classified by neither STDLIB_MODULES nor '
            f'KNOWN_THIRD_PARTY_MODULES. Decide which section it belongs to and say so in both '
            f'places -- an unlisted module silently renders as third-party.'
        )

    def test_the_table_agrees_with_the_running_interpreter(self) -> None:
        # Runs on all four legs of the matrix, so a wrong entry is caught without ever letting
        # the interpreter decide the emitted bytes.
        assert STDLIB_MODULES <= sys.stdlib_module_names
        assert not (KNOWN_THIRD_PARTY_MODULES & sys.stdlib_module_names)

    def test_no_emittable_import_line_is_long_enough_for_ruff_to_wrap_it(self) -> None:
        # I001 compares against ruff's formatted output, which wraps a `from ... import ...` line
        # past `line-length` (default 88) into a parenthesized block. castiron emits one line, so
        # a merged line at 89+ characters would be I001-dirty for a reason nothing else catches.
        merged: dict[str, set[str]] = {}
        for line in emittable_import_lines():
            if line.startswith('from '):
                module, _, names = line.removeprefix('from ').partition(' import ')
                merged.setdefault(module, set()).update(name.strip() for name in names.split(','))
        widest = max(
            (len(render_import_block([f'from {m} import {n}' for n in names])), m) for m, names in merged.items()
        )
        assert widest[0] <= 88, (
            f'the merged import line for {widest[1]!r} is {widest[0]} characters. ruff wraps past '
            f'88 (its default line-length), so this module would report I001 no matter how it is '
            f'ordered. Splitting the line is a design change, not a sort fix.'
        )


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
