"""The column-identifier oracle — enumerate character classes, then EXECUTE the result.

**Why this module exists, stated plainly.** ``CI-085`` is the column half of the defect class
``CI-080`` was the enum half of. That row (PR #17) took **four** fix rounds, and its own closing
note is unambiguous about why:

    *"every round was an **oracle** problem, not a logic problem. ``.isidentifier()`` was the
    wrong oracle; then the corpus was the wrong oracle; then the alphabet was; then the class
    name the oracle executed under was."*

So this module does not assert about emitted **text**. A predicate over text is the shape that
failed four times. It enumerates the **character classes that can produce a hazard**, runs them
through the **real** :func:`~castiron.ir.build.column_identifiers` and the **real**
:class:`~castiron.emitters.pydantic.PydanticEmitter`, and then:

1. :func:`compile` s the module — which catches ``2fast`` (``SyntaxError``); and
2. :func:`exec` s it — which is the only step that catches a **leading underscore**. Pydantic
   raises ``NameError: Fields must not use names with leading underscores`` when the class body
   runs, and ``compile()`` / :mod:`ast` see nothing wrong with it at all. **This step is the
   whole point of the file.**
3. Constructs the model **by wire name** and asserts ``model_dump(by_alias=True) == payload``.
   One line proves that every wire name survived, that none was merged, that each carries its
   own value, and that the alias round-trips.

⚠ **Two things the enum oracle does that this one deliberately does NOT copy** (spec §5.4):

* It does **not** assert warning-silence. ``_exec_enum_clean`` does, because on py3.10 a
  ``DeprecationWarning`` is the only signal of enum name-mangling. There is no analogue here,
  and pydantic emits a legitimate ``UserWarning: Field name "copy" ... shadows an attribute in
  parent "BaseModel"`` for an ordinary column named ``copy``/``json``/``dict``/``schema`` — a
  wart that is explicitly out of scope, and asserting silence would produce false reds.
* It does **not** hard-code the emitted class name. That was ``CI-113``'s blind spot on the enum
  side. The class name here is derived from the emitter's own naming and cross-checked against
  the emitted text, so a naming change cannot make the oracle silently stop finding its class.
"""

import builtins
import keyword
import os
import subprocess
import sys
import unicodedata
from itertools import product
from typing import Any

import pytest

from castiron.emitters import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
from castiron.ir import ColumnInfo, Schema, TableInfo
from castiron.ir.build import (
    add_constraints_to_table_details,
    add_foreign_key_info_to_table_details,
    column_identifiers,
    column_name_is_reserved,
    column_name_reserved_exceptions,
    get_alias,
    get_table_details_from_columns,
    identifier_characters,
    resolved_column_name,
    standardize_column_name,
    update_columns_with_constraints,
)
from castiron.utils.naming import to_pascal_case

#: ``tests/unit/ir`` → ``tests/unit`` → ``tests`` → repo root. Used by the subprocess probe.
REPO_ROOT = __import__('pathlib').Path(__file__).resolve().parents[3]

#: FULLWIDTH LOW LINE. ``unicodedata.normalize('NFKC', '＿') == '_'``, and it is an
#: identifier-**continue** character, so :func:`identifier_characters` keeps it (``CI94-D2``).
NFKC_UNDERSCORE = '＿'

#: LATIN SMALL LIGATURE FI. ``NFKC`` folds it to ``fi``, and ``'ﬁ'.isidentifier()`` is ``True``.
NFKC_LIGATURE = 'ﬁ'

#: 🔴 **Every character is here because a specific mutation needs it** — see
#: :meth:`TestTheAlphabet.test_the_alphabet_covers_the_classes_that_matter`, which states the
#: mapping. Enumerating character *classes* rather than *shapes* is what lets a hazard nobody has
#: named yet fail here (``CI-072``).
COLUMN_ALPHABET = ('_', NFKC_UNDERSCORE, 'a', '2', ' ', NFKC_LIGATURE)

#: ⚠ **Pinned, and pinned deliberately.** ``CI-094``'s fix-round-3 ``[LOW]`` finding was that an
#: *unpinned* generator default let a sibling sweep silently narrow by 98 % while still printing
#: green. 6 + 6² + 6³ = 258.
MAX_LENGTH = 3
EXPECTED_CORPUS_SIZE = 258

#: Measured at ``max_length=4``: 1554 names, a 283 KB module, and it passes — headroom, not
#: budget. ``CI-095`` records that the gate cost is grudged, so ``max_length=3`` ships and the
#: length-4 size is asserted (cheaply, without emitting it) so raising the bound is a known
#: quantity rather than a guess.
LENGTH_FOUR_CORPUS_SIZE = 1554

#: The curated exceptions ``column_name_reserved_exceptions`` exempts, restated here so the
#: no-change sweep names them rather than importing the list it is checking.
CURATED_EXCEPTIONS = ('id', 'credits', 'copyright', 'license', 'help', 'property', 'sum')

#: ⚠ **The spec's "192 names" does NOT reproduce, and one pinned number would be wrong on three
#: of the four gate legs.** ``dir(builtins)`` grows between minor versions (3.11 added
#: ``BaseExceptionGroup``/``ExceptionGroup``; 3.13 added ``PythonFinalizationError`` and
#: ``_IncompleteInputError``), so the union is measured at **187 / 189 / 189 / 191** on
#: 3.10 / 3.11 / 3.12 / 3.13. Pinned per version, with a floor for anything unlisted, because a
#: count that cannot narrow is the point and a count that is simply wrong is worse than none.
EXPECTED_RESERVED_COUNT: dict[tuple[int, int], int] = {(3, 10): 187, (3, 11): 189, (3, 12): 189, (3, 13): 191}
RESERVED_COUNT_FLOOR = 187


def generated_column_names(max_length: int = MAX_LENGTH) -> list[str]:
    """Return every string over :data:`COLUMN_ALPHABET` from length 1 to ``max_length``.

    Args:
        max_length: The longest name to generate. Pinned by
            :meth:`TestTheAlphabet.test_the_alphabet_covers_the_classes_that_matter`.

    Returns:
        The generated names, shortest first, in deterministic product order.
    """
    names: list[str] = []
    for length in range(1, max_length + 1):
        names.extend(''.join(combination) for combination in product(COLUMN_ALPHABET, repeat=length))
    return names


def build_table(source_names: list[str], disable_model_prefix_protection: bool = False) -> TableInfo:
    """Build a one-table :class:`~castiron.ir.TableInfo` from wire column names.

    Routes through the **real** :func:`~castiron.ir.build.column_identifiers`, so the oracle
    exercises the shipped algorithm rather than a restatement of it.

    Args:
        source_names: The wire column names, in ``attnum`` order.
        disable_model_prefix_protection: Passed straight through.

    Returns:
        A ``BASE TABLE`` whose columns are all nullable ``text``.
    """
    table = TableInfo(name='hostile', schema='public', table_type='BASE TABLE')
    for identifier in column_identifiers(source_names, disable_model_prefix_protection):
        table.add_column(ColumnInfo(name=identifier.name, raw_type='text', alias=identifier.alias, is_nullable=True))
    return table


def emit(table: TableInfo, config: EmitterConfig | None = None) -> str:
    """Emit the Pydantic module for a one-table schema with the real emitter."""
    return PydanticEmitter(config or EmitterConfig()).emit(Schema(tables=[table]))[0].content


def base_class_name(table: TableInfo) -> str:
    """The Base class name the emitter writes for ``table``.

    ⚠ Derived from the emitter's own naming helpers, never hard-coded. ``CI-113`` was exactly
    the failure of an oracle that ran under a stand-in class name and was therefore structurally
    blind to an axis; :meth:`TestTheOracle.test_the_oracle_reads_the_class_name_out_of_the_module`
    additionally cross-checks this against the emitted text.
    """
    return f'{to_pascal_case(table.name)}BaseSchema'


def exec_module(module: str, class_name: str) -> Any:
    """Compile **and execute** ``module``, returning the class named ``class_name``.

    :func:`compile` alone is not enough and that is the thesis of this file: a field named
    ``_private`` compiles cleanly and raises ``NameError`` when pydantic builds the class.

    Args:
        module: The emitted module text.
        class_name: The class to pull out of the executed namespace.

    Returns:
        The executed model class.
    """
    code = compile(module, '<castiron-oracle>', 'exec')
    namespace: dict[str, Any] = {}
    exec(code, namespace)  # noqa: S102 -- executing castiron's own output IS the oracle
    return namespace[class_name]


@pytest.mark.unit
class TestTheAlphabet:
    """The corpus's own guard: a sweep that silently narrows is worse than no sweep."""

    def test_the_alphabet_covers_the_classes_that_matter(self) -> None:
        # Each entry names the mutation it catches. If one is dropped, the mutant it covers
        # survives and this file quietly stops being an oracle.
        #
        #   'a'  -- the control: an ordinary identifier character must survive untouched.
        #   '2'  -- drop the leading-digit/empty guard and '2' is not an identifier.
        #   ' '  -- neuter identifier_characters and a space reaches the field line.
        #   '_'  -- drop the leading-underscore guard and pydantic raises NameError at import.
        #   '＿' -- drop NFKC from the GUARDS ('＿a' is '_a' to the compiler) and from the
        #           UNIQUENESS KEY ('field_＿a' and 'field__a' are one binding).
        #   'ﬁ'  -- drop NFKC from the ALIAS rule and the wire name 'ﬁ' is lost from the dump.
        assert 'a' in COLUMN_ALPHABET
        assert any(c.isdigit() for c in COLUMN_ALPHABET)
        assert ' ' in COLUMN_ALPHABET
        assert '_' in COLUMN_ALPHABET
        assert NFKC_UNDERSCORE in COLUMN_ALPHABET
        assert unicodedata.normalize('NFKC', NFKC_UNDERSCORE) == '_'
        assert NFKC_LIGATURE in COLUMN_ALPHABET
        assert unicodedata.normalize('NFKC', NFKC_LIGATURE) == 'fi'
        assert NFKC_LIGATURE.isidentifier(), 'the ligature must be identifier-LEGAL and NFKC-active'
        assert len(generated_column_names()) == EXPECTED_CORPUS_SIZE
        assert len(generated_column_names(4)) == LENGTH_FOUR_CORPUS_SIZE

    def test_the_corpus_actually_contains_a_collision(self) -> None:
        # The collision loop is only covered if the enumerated corpus reaches it. 'a_', 'a ' and
        # 'a＿' all resolve to the same NFKC key, so it does -- asserted rather than assumed.
        names = generated_column_names()
        assert {'a_', 'a ', f'a{NFKC_UNDERSCORE}'} <= set(names)
        resolved = [identifier.name for identifier in column_identifiers(names)]
        assert len(set(resolved)) == len(resolved)
        assert any(name.endswith(('_2', '_3')) for name in resolved), 'the collision loop was never entered'


@pytest.mark.unit
class TestTheOracle:
    """🔴 The executing oracle. Nothing here asserts on emitted text."""

    def test_the_oracle_reads_the_class_name_out_of_the_module(self) -> None:
        # CI-113's lesson: an oracle that executes under a stand-in class name is blind to an
        # axis. The name is derived from the emitter's helpers AND confirmed against its output.
        table = build_table(['a'])
        assert f'class {base_class_name(table)}(' in emit(table)

    def test_every_generated_column_name_survives_into_a_real_pydantic_model(self) -> None:
        names = generated_column_names()
        table = build_table(names)
        module = emit(table)

        # 1+2. compile() catches `2fast`; exec() catches `_private`, which compile() cannot see.
        model = exec_module(module, base_class_name(table))

        # 3. Nothing dropped and nothing merged.
        assert len(model.model_fields) == len(names), (
            f'{len(names) - len(model.model_fields)} column(s) did not become fields. Two names '
            f'collapsed into one binding (NFKC) or the collision rule dropped a variant.'
        )

        # 4+5. Construct BY WIRE NAME -- what a user of a PostgREST response actually has -- and
        # assert the exact by-alias round-trip. This single line proves every wire name survived,
        # that none was merged, that each carries its own value, and that the alias round-trips.
        payload = {name: f'v{index}' for index, name in enumerate(names)}
        assert len(payload) == len(names), 'the generated corpus must contain no duplicate names'
        assert model(**payload).model_dump(by_alias=True) == payload

    def test_no_generated_name_is_left_unusable_as_a_field(self) -> None:
        # The same property stated against the rule rather than the interpreter, so a failure
        # names WHICH name is broken instead of only "the module did not import".
        offenders = [
            (identifier.source, identifier.name)
            for identifier in column_identifiers(generated_column_names())
            if not unicodedata.normalize('NFKC', identifier.name).isidentifier()
            or unicodedata.normalize('NFKC', identifier.name).startswith('_')
        ]
        assert offenders == [], offenders

    def test_the_generated_names_are_unique_under_nfkc(self) -> None:
        keys = [
            unicodedata.normalize('NFKC', identifier.name)
            for identifier in column_identifiers(generated_column_names())
        ]
        assert len(set(keys)) == len(keys), 'two columns would collapse to one field at import'

    def test_the_oracle_is_deterministic_across_hash_seeds(self) -> None:
        """Hard Rule #9, probed in **subprocesses** over six seeds.

        ⚠ Calling :func:`column_identifiers` five times in one process **cannot fail**:
        ``PYTHONHASHSEED`` is fixed for the life of an interpreter. That was a real ``CI-094``
        finding (fix round 3), where the in-process version stayed green under a literal
        ``for source in set(source_names)`` mutation. ``CI-065`` is the standing precedent that a
        non-total ordering really does flip castiron's output under a different seed.
        """
        script = (
            'import sys; sys.path.insert(0, ".");'
            'from castiron.ir.build import column_identifiers;'
            'from tests.unit.ir.test_column_identifiers import generated_column_names;'
            'names = generated_column_names();'
            'print("|".join(i.name for i in column_identifiers(names)))'
        )
        outputs = set()
        for seed in ('0', '1', '42', '4294967295', 'random', 'random'):
            result = subprocess.run(
                [sys.executable, '-c', script],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, 'PYTHONHASHSEED': seed},
                cwd=REPO_ROOT,
            )
            outputs.add(result.stdout)
        assert len(outputs) == 1, f'{len(outputs)} distinct renderings across hash seeds'


@pytest.mark.unit
class TestThePublicMapping:
    """The `field_` contract, pinned by name (spec §3.2/§4.5).

    ⚠ **This becomes a permanent public contract the moment ``0.1.0`` publishes** — a user will
    write ``row.field_2fast``. ``CI85-Q1``, confirmed by the captain: it is the *shipped*
    ``class`` → ``field_class`` contract widened, not a new one invented.
    """

    @pytest.mark.parametrize(
        ('source', 'expected_name', 'expected_alias'),
        [
            # --- currently valid, and must not move (the no-change half) ---
            ('id', 'id', None),
            ('ok_column', 'ok_column', None),
            ('email', 'email', None),
            ('credits', 'credits', None),
            ('copyright', 'copyright', None),
            ('sum', 'sum', None),
            # `CI94-D2`: Unicode identifiers are KEPT, never folded to ASCII. Both of these are
            # REAL column names in the live testbed's `edge.identifier_torture` capture, and both
            # are already legal Python -- so they are the load-bearing no-change controls for a
            # schema the corpus deliberately quarantines.
            ('Ünïcödé', 'Ünïcödé', None),
            ('trailing_underscore_', 'trailing_underscore_', None),
            # --- shipped reserved-word behaviour, unchanged ---
            ('class', 'field_class', 'class'),
            ('model_config', 'field_model_config', 'model_config'),
            # --- the repair (new) ---
            ('2fast', 'field_2fast', '2fast'),
            ('space name', 'space_name', 'space name'),
            ('kebab-case', 'kebab_case', 'kebab-case'),
            ('_private', 'field__private', '_private'),
            (' x', 'field__x', ' x'),
            ('', 'field_', ''),
            (NFKC_LIGATURE, NFKC_LIGATURE, NFKC_LIGATURE),
        ],
    )
    def test_the_pinned_mapping(self, source: str, expected_name: str, expected_alias: str | None) -> None:
        assert standardize_column_name(source) == expected_name
        assert get_alias(source) == expected_alias

    def test_the_model_prefix_flag_still_disables_the_model_rename(self) -> None:
        # Shipped behaviour, unchanged: --no-model-prefix-protection leaves `model_config` alone.
        assert standardize_column_name('model_config', True) == 'model_config'
        assert get_alias('model_config', True) is None

    def test_the_two_real_capture_names_reach_a_working_model_untouched(self) -> None:
        # The stronger form of the two no-change controls above: not merely "the string does not
        # move" but "the emitted model still binds them and round-trips them with no alias".
        names = ['id', 'Ünïcödé', 'trailing_underscore_']
        table = build_table(names)
        assert [c.alias for c in table.columns] == [None, None, None]
        model = exec_module(emit(table), base_class_name(table))
        payload = {name: f'v{index}' for index, name in enumerate(names)}
        assert model(**payload).model_dump(by_alias=True) == payload


@pytest.mark.unit
class TestCollisionFamilies:
    """Spec §4.5's collision table. The rule is per **table**, in ``attnum`` order.

    That order is contractual (``pipeline.py:169-172``: ``properties`` order *is* pg ``attnum``),
    and it is the Hard Rule #9 answer — the collision rule depends on column order, that order is
    a pure function of committed input, and no set or dict iteration reaches it.
    """

    @pytest.mark.parametrize(
        ('sources', 'expected'),
        [
            (['space name', 'space-name', 'space_name'], ['space_name', 'space_name_2', 'space_name_3']),
            (['class', 'field_class'], ['field_class', 'field_class_2']),
            # ⚠ Note the asymmetry: the column LITERALLY named `field_class` keeps its name and
            # the RENAMED `class` takes the suffix, because `field_class` came first in attnum
            # order. Deterministic, and the alias on each disambiguates them for a reader.
            (['field_class', 'class'], ['field_class', 'field_class_2']),
            ([NFKC_LIGATURE, 'fi'], [NFKC_LIGATURE, 'fi_2']),
            (['a', 'a', 'a_2'], ['a', 'a_2', 'a_2_2']),
            ([' x', '_x', 'x'], ['field__x', 'field__x_2', 'x']),
            (['', ' ', '  '], ['field_', 'field__', 'field___']),
            # ⚠ The row to keep: `field_＿private` NFKC-normalizes to `field__private`, so the
            # NORMALIZED uniqueness key is what catches it. A raw-string check emits two fields
            # that collapse into one at import.
            (['_private', f'{NFKC_UNDERSCORE}private'], ['field__private', f'field_{NFKC_UNDERSCORE}private_2']),
        ],
    )
    def test_the_collision_families(self, sources: list[str], expected: list[str]) -> None:
        assert [identifier.name for identifier in column_identifiers(sources)] == expected

    @pytest.mark.parametrize(
        'sources',
        [
            ['space name', 'space-name', 'space_name'],
            ['class', 'field_class'],
            [NFKC_LIGATURE, 'fi'],
            [' x', '_x', 'x'],
            ['', ' ', '  '],
            ['_private', f'{NFKC_UNDERSCORE}private'],
        ],
    )
    def test_every_collision_family_reaches_a_working_model(self, sources: list[str]) -> None:
        # `CI94-Q1`'s single non-negotiable -- NEVER silently drop a variant -- asserted by
        # execution rather than by inspecting names.
        table = build_table(sources)
        model = exec_module(emit(table), base_class_name(table))
        assert len(model.model_fields) == len(sources)
        payload = {name: f'v{index}' for index, name in enumerate(sources)}
        assert model(**payload).model_dump(by_alias=True) == payload

    def test_the_source_order_is_authoritative(self) -> None:
        # Reversing the input reverses which variant keeps the bare name -- the property that
        # makes `attnum` order load-bearing rather than incidental.
        assert [i.name for i in column_identifiers(['a b', 'a_b'])] == ['a_b', 'a_b_2']
        assert [i.name for i in column_identifiers(['a_b', 'a b'])] == ['a_b', 'a_b_2']
        assert [i.source for i in column_identifiers(['a b', 'a_b'])] == ['a b', 'a_b']


@pytest.mark.unit
class TestNothingCurrentlyValidMoves:
    """Spec §3.4 instrument 2 — the reserved axis, **enumerated** rather than sampled.

    This is the axis most likely to regress and it is small enough to enumerate exhaustively, so
    it is enumerated (``CI-072``). Every Python keyword, every name in ``dir(builtins)``, plus the
    seven curated exceptions, against **both** values of ``disable_model_prefix_protection``.
    """

    @staticmethod
    def _reserved_corpus() -> list[str]:
        return sorted(set(keyword.kwlist) | set(dir(builtins)) | set(CURATED_EXCEPTIONS))

    def test_the_reserved_corpus_cannot_silently_narrow(self) -> None:
        names = self._reserved_corpus()
        expected = EXPECTED_RESERVED_COUNT.get(sys.version_info[:2])
        if expected is None:  # pragma: no cover -- only on an interpreter outside the support window
            assert len(names) >= RESERVED_COUNT_FLOOR, (
                f"{sys.version_info[:2]} is outside castiron's >=3.10,<3.14 window and is not in "
                f'EXPECTED_RESERVED_COUNT. Measure it and add the row.'
            )
        else:
            assert len(names) == expected
        assert {'class', 'import', 'None', 'type', 'list', *CURATED_EXCEPTIONS} <= set(names)

    def test_no_keyword_or_builtin_column_name_moves(self) -> None:
        # The expected value is the SHIPPED rule restated literally, so the assertion is
        # independent of the widened implementation it is checking.
        moved: list[tuple[str, bool, str, str]] = []
        for name in self._reserved_corpus():
            for dmpp in (False, True):
                renamed = column_name_is_reserved(name, dmpp) and not column_name_reserved_exceptions(name)
                expected_name = f'field_{name}' if renamed else name
                expected_alias = name if renamed else None
                actual_name = standardize_column_name(name, dmpp)
                actual_alias = get_alias(name, dmpp)
                if (actual_name, actual_alias) != (expected_name, expected_alias):
                    moved.append((name, dmpp, str(actual_name), str(expected_name)))
        assert moved == [], f'{len(moved)} reserved name(s) moved: {moved[:10]}'

    def test_the_curated_exceptions_are_the_identity(self) -> None:
        # Asserted by name, not by argument (spec §3.4): they are valid identifiers that do not
        # start with `_`, so the new identifier guard is the identity on them.
        for name in CURATED_EXCEPTIONS:
            assert standardize_column_name(name) == name
            assert get_alias(name) is None

    def test_a_valid_identifier_column_is_returned_unchanged(self) -> None:
        # Over the generated corpus: every name that is ALREADY usable comes back byte-identical.
        for name in generated_column_names():
            normalized = unicodedata.normalize('NFKC', name)
            already_usable = (
                normalized.isidentifier()
                and not normalized.startswith('_')
                and not (column_name_is_reserved(normalized) and not column_name_reserved_exceptions(normalized))
            )
            if already_usable:
                assert standardize_column_name(name) == name, name

    def test_identifier_characters_is_the_identity_on_a_legal_name(self) -> None:
        for name in ('id', 'ok_column', 'Ünïcödé', 'trailing_underscore_', '_private', NFKC_LIGATURE):
            assert identifier_characters(name) == name


def fullwidth(text: str) -> str:
    """Respell ASCII letters and digits with their FULLWIDTH equivalents.

    ``'class'`` becomes ``'ｃｌａｓｓ'``: a different string, a **valid identifier**, not a keyword
    by inspection — and ``NFKC``-identical to ``class``, which is what the compiler actually sees.
    """
    return ''.join(chr(ord(c) - 0x20 + 0xFF00) if c.isascii() and c.isalnum() else c for c in text)


@pytest.mark.unit
class TestTheGuardsReadTheNormalizedForm:
    """🔴 The NFKC axis of the guards, enumerated — and the one the generated corpus CANNOT reach.

    ⚠ **Recorded because the spec got this wrong and the mutation harness caught it.** Spec §5.6
    predicted that mutant 4 (*"guards read ``name`` instead of ``NFKC(name)``"*) would be caught by
    the oracle *"on ``＿a``"*. It is **not**: ``'＿a'.isidentifier()`` is already ``False``, because
    U+FF3F is an identifier-**continue** character and not a **start** one, so the raw check catches
    it with or without the normalization. Measured: mutant 4 **survived all 51 oracle tests**.

    The reason is structural, and it is worth stating rather than patching around. On the
    *identifier-shape* axis the normalization is defensive: after
    :func:`~castiron.ir.build.identifier_characters` every character is XID_Continue, XID is closed
    under NFKC, and exactly **one** XID_Start codepoint (U+005F, i.e. ``_`` itself) folds to a
    leading underscore — so ``NFKC(name)`` and ``name`` cannot disagree there. The normalization is
    behaviourally load-bearing on the **reserved** axis only, and a six-character alphabet
    provably cannot *spell a keyword*, so no enumeration over it could ever fail.

    So this axis is enumerated over the corpus that *can* reach it: every reserved name, respelled
    in fullwidth. That is the same "enumerate, do not sample" discipline (``CI-072``) pointed at
    the axis that actually carries the property — which is precisely the correction ``CI-094``'s
    fix round 2 had to make when its alphabet could not contain an NFKC-active character.
    """

    def test_the_fullwidth_respelling_really_is_nfkc_equivalent(self) -> None:
        # The corpus's own guard: if `fullwidth` stopped folding, every assertion below would
        # pass for the wrong reason.
        assert fullwidth('class') == 'ｃｌａｓｓ'
        assert fullwidth('class') != 'class'
        assert unicodedata.normalize('NFKC', fullwidth('class')) == 'class'
        assert fullwidth('class').isidentifier()
        assert not keyword.iskeyword(fullwidth('class'))

    def test_every_reserved_name_is_still_repaired_when_spelled_in_fullwidth(self) -> None:
        missed: list[str] = []
        for name in sorted(set(keyword.kwlist) | set(dir(builtins))):
            if column_name_reserved_exceptions(name):
                continue
            spelled = fullwidth(name)
            if spelled == name:  # pragma: no cover -- every reserved name is ASCII alnum or '_'
                continue
            if standardize_column_name(spelled) == spelled:
                missed.append(name)
        assert missed == [], (
            f'{len(missed)} reserved name(s) escape the guard when respelled in fullwidth, e.g. '
            f'{missed[:5]}. The guard is reading the RAW name; the compiler reads the NFKC form, '
            f'so `ｃｌａｓｓ` would bind a pydantic field literally named `class`.'
        )

    def test_no_repaired_name_normalizes_to_a_keyword_or_builtin(self) -> None:
        # The property stated over the whole generated corpus AND the fullwidth reserved corpus,
        # so it is total over both alphabets rather than over the one that happens to be enumerated.
        sources = generated_column_names() + [fullwidth(n) for n in sorted(set(keyword.kwlist) | set(dir(builtins)))]
        offenders = [
            (identifier.source, identifier.name)
            for identifier in column_identifiers(sources)
            if column_name_is_reserved(unicodedata.normalize('NFKC', identifier.name))
            and not column_name_reserved_exceptions(unicodedata.normalize('NFKC', identifier.name))
        ]
        assert offenders == [], offenders

    def test_the_fullwidth_keyword_column_reaches_a_typeable_attribute(self) -> None:
        # §2.3's witness, executed rather than argued: without the normalization in the guard, a
        # column named `clａss` emits a field whose *bound attribute* is literally `class` --
        # syntactically unreachable, addressable only through `getattr`.
        names = ['id', 'clａss', fullwidth('import')]
        table = build_table(names)
        model = exec_module(emit(table), base_class_name(table))
        for field_name in model.model_fields:
            assert not keyword.iskeyword(field_name), (
                f'the emitted model binds an attribute named {field_name!r}, which is a Python '
                f'keyword -- a user cannot type `row.{field_name}`.'
            )
        payload = {name: f'v{index}' for index, name in enumerate(names)}
        assert model(**payload).model_dump(by_alias=True) == payload


@pytest.mark.unit
class TestTheSharedCharacterMap:
    """``CI94-D2``, carried across the move into ``ir.build`` unchanged."""

    def test_unicode_identifier_characters_are_kept_not_folded(self) -> None:
        assert identifier_characters('Ünïcödé') == 'Ünïcödé'

    def test_there_is_no_run_collapsing_and_no_stripping(self) -> None:
        # 'a  b' and 'a b' must stay DISTINGUISHABLE ATTEMPTS; the collision rule -- not the
        # character map -- is what resolves them when they are not.
        assert identifier_characters('a  b') == 'a__b'
        assert identifier_characters('a b') == 'a_b'
        assert identifier_characters(' x ') == '_x_'

    def test_it_is_one_character_out_per_character_in(self) -> None:
        for name in generated_column_names():
            assert len(identifier_characters(name)) == len(name)


def column_row(table: str, column: str, *, nullable: str = 'YES') -> tuple[object, ...]:
    """One 12-tuple column row for :func:`get_table_details_from_columns`."""
    return ('public', table, column, None, nullable, 'text', None, 'BASE TABLE', None, 'text', None, None)


@pytest.mark.unit
class TestTheFourCallSitesAgree:
    """``standardize_column_name`` has **four** callers and a mismatch is silent.

    ``ir/build.py``'s own docstrings say so in as many words: a disagreement between the column
    marshaler and the FK/constraint marshalers makes the FK "silently stop matching any column",
    leaves ``primary``/``is_unique``/``is_foreign_key`` unset, and makes
    ``TableInfo.primary_key()`` return a phantom name. Nothing raises. So the agreement is
    asserted here rather than argued in a comment.
    """

    def test_the_single_pass_refactor_equals_the_per_table_function(self) -> None:
        # ⚠ Spec §4.6: `get_table_details_from_columns` was a single row loop and the collision
        # rule needs the whole table. The refactor must be EQUIVALENT to running
        # `column_identifiers` over the table's wire names in row order -- asserted, not trusted.
        wire = ['id', 'space name', 'space-name', 'space_name', 'class', '_private', '2fast']
        tables = get_table_details_from_columns([column_row('t', name) for name in wire])
        columns = tables[('public', 't')].columns
        expected = column_identifiers(wire)
        assert [c.name for c in columns] == [i.name for i in expected]
        assert [c.alias for c in columns] == [i.alias for i in expected]

    def test_two_tables_get_independent_collision_scopes(self) -> None:
        # The rule is per TABLE. If `used` leaked across tables, the second table's `space name`
        # would come out as `space_name_2` -- a name that means nothing to its own reader.
        rows = [column_row('a', 'space name'), column_row('b', 'space name'), column_row('a', 'space-name')]
        tables = get_table_details_from_columns(rows)
        assert [c.name for c in tables[('public', 'a')].columns] == ['space_name', 'space_name_2']
        assert [c.name for c in tables[('public', 'b')].columns] == ['space_name']

    def test_a_foreign_key_resolves_to_the_name_the_table_actually_uses(self) -> None:
        rows = [
            column_row('child', 'id', nullable='NO'),
            column_row('child', 'space name'),
            column_row('child', 'space-name'),
            column_row('parent', '2fast', nullable='NO'),
        ]
        tables = get_table_details_from_columns(rows)
        add_foreign_key_info_to_table_details(
            tables, [('public', 'child', 'space-name', 'public', 'parent', '2fast', 'child_fk', False)]
        )
        fk = tables[('public', 'child')].foreign_keys[0]
        # `space-name` is the SECOND collider, so a per-name recomputation would say `space_name`
        # and the FK would point at the wrong column. The lookup says `space_name_2`.
        assert fk.column_name == 'space_name_2'
        assert fk.foreign_column_name == 'field_2fast'
        assert [c.name for c in tables[('public', 'child')].columns] == ['id', 'space_name', 'space_name_2']

    def test_a_constraint_resolves_to_the_name_the_table_actually_uses(self) -> None:
        rows = [column_row('t', '2fast', nullable='NO'), column_row('t', 'space name'), column_row('t', 'space-name')]
        tables = get_table_details_from_columns(rows)
        add_constraints_to_table_details(
            tables, 'public', [('t_pkey', 't', ['2fast', 'space-name'], 'p', 'PRIMARY KEY (2fast, space-name)', False)]
        )
        assert tables[('public', 't')].constraints[0].columns == ['field_2fast', 'space_name_2']

    def test_the_constraint_flags_actually_land_on_the_column(self) -> None:
        # The end-to-end consequence, and the reason the agreement matters: `update_columns_with
        # _constraints` matches `ConstraintInfo.columns` against `ColumnInfo.name` by equality.
        rows = [column_row('t', '2fast', nullable='NO')]
        tables = get_table_details_from_columns(rows)
        add_constraints_to_table_details(
            tables, 'public', [('t_pkey', 't', ['2fast'], 'p', 'PRIMARY KEY (2fast)', False)]
        )
        update_columns_with_constraints(tables)
        assert tables[('public', 't')].columns[0].primary is True
        assert tables[('public', 't')].primary_key() == ['field_2fast']

    @pytest.mark.parametrize(
        ('source', 'expected'),
        [('class', 'field_class'), ('2fast', 'field_2fast'), ('ok_column', 'ok_column'), ('id', 'id')],
    )
    def test_the_lookup_falls_back_to_the_per_name_repair_not_the_raw_name(self, source: str, expected: str) -> None:
        # ⚠ Unreachable from the OpenAPI source (an FK row always names a column the table has),
        # but a live-DB LEFT JOIN can produce one. Falling back to the RAW name would change
        # SHIPPED behaviour for `class`, which `standardize_column_name` renames at that site
        # today -- so the fallback is the repair, and this pins it by name.
        table = TableInfo(name='t', schema='public')
        table.add_column(ColumnInfo(name='ok_column', raw_type='text'))
        assert resolved_column_name(table, source) == expected

    def test_the_lookup_reconstructs_the_wire_name_through_the_alias(self) -> None:
        table = build_table(['2fast', 'ok_column', NFKC_LIGATURE])
        assert resolved_column_name(table, '2fast') == 'field_2fast'
        assert resolved_column_name(table, 'ok_column') == 'ok_column'
        assert resolved_column_name(table, NFKC_LIGATURE) == NFKC_LIGATURE


@pytest.mark.unit
class TestCi085ResidualTheIrNameIsNotTheRuntimeAttribute:
    """🔴 Spec §8.2, **pinned as present** — a known residual, not asserted as correct.

    ``ColumnInfo.name`` records ``ﬁ`` while the attribute Python actually binds is ``fi``. It is
    inert for every ASCII name (NFKC is the identity there) and the emitted module is **lossless**,
    because the alias rule carries the wire name. It is kept rather than normalized because
    :func:`~castiron.utils.naming.python_member_names` already ships the same convention, and two
    conventions for one concept would be worse than one imperfect one.

    Pinned in the house pattern (``CI-085`` shipped ``compiles=False``; ``CI-113`` ships
    ``TestCi113``): the assertions below are written so that **fixing** the residual turns them
    red, which is what keeps the gap visible instead of forgotten.
    """

    def test_the_recorded_name_differs_from_the_bound_attribute(self) -> None:
        table = build_table([NFKC_LIGATURE])
        assert table.columns[0].name == NFKC_LIGATURE
        model = exec_module(emit(table), base_class_name(table))
        assert list(model.model_fields) == ['fi'], 'the residual is closed -- update spec §8.2 and this test'
        assert table.columns[0].name not in model.model_fields

    def test_the_wire_name_is_still_lossless_despite_the_residual(self) -> None:
        # Why the residual is acceptable: the alias carries the truth even though `name` does not.
        table = build_table([NFKC_LIGATURE])
        assert table.columns[0].alias == NFKC_LIGATURE
        model = exec_module(emit(table), base_class_name(table))
        assert model(**{NFKC_LIGATURE: 'v'}).model_dump(by_alias=True) == {NFKC_LIGATURE: 'v'}
