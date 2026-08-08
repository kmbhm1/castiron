import ast
import itertools
import keyword
import os
import subprocess
import sys
import unicodedata
import warnings
from pathlib import Path

import pytest

from castiron.ir import EnumInfo
from castiron.utils.naming import (
    EnumMember,
    _is_enum_reserved_shape,
    pluralize,
    python_class_name,
    python_identifier,
    python_member_names,
    singularize,
    to_pascal_case,
)

#: The repository root, so the subprocess determinism probe can import `tests.` and `src/`.
REPO_ROOT = Path(__file__).parents[3]


def _enum(*labels: str) -> EnumInfo:
    """The one ``EnumInfo`` the helpers below derive from -- member names AND class name alike.

    🔴 **The pair must come from a single ``EnumInfo``, and that is ``CI-113``'s whole lesson.**
    ``EnumMeta`` consults the **enclosing class name**, so member names derived from one enum and
    then executed under a stand-in (``class E``) are being checked against a class the emitter
    would never write -- an oracle that cannot observe the axis it is aimed at. Every
    ``_exec_enum*`` default below is :data:`FIXTURE_CLASS`, derived from *this* enum by
    :func:`python_class_name`, so the two cannot drift apart again.
    """
    return EnumInfo(name='t', values=list(labels), schema='public')


#: The class name the Pydantic emitter would really write for :func:`_enum` -- ``PublicTEnum``.
#: Executing member bodies under this rather than ``E`` is what keeps the class-name axis honest.
FIXTURE_CLASS = python_class_name(_enum())


def _names(*labels: str) -> list[str]:
    """The member names ``python_member_names`` derives for ``labels``, in order."""
    return [m.name for m in python_member_names(_enum(*labels))]


def _members(*labels: str) -> list[EnumMember]:
    return python_member_names(_enum(*labels))


def _exec_enum(pairs: list[tuple[str, str]], class_name: str = FIXTURE_CLASS) -> dict[str, object]:
    """Build and **execute** a real ``Enum`` class body from ``(member_name, value)`` pairs.

    ⚠ Execution, not ``compile()``. All three shapes CI-080's fix round 1 closed are invisible to
    a parse: ``_sunder_`` raises when the class body runs, ``__dunder__`` is silently swallowed by
    ``EnumMeta``, and a mangled name is rewritten by the compiler. A test that only compiles
    reports green on a module that cannot be imported.

    ⚠ **Warnings are captured and asserted on, not suppressed** -- see :func:`_exec_enum_clean`.
    On py3.10 a ``DeprecationWarning`` is the *only* signal that a name was name-mangled, because
    3.10 still creates the member (under ``_E__X``) rather than dropping it. Muting it would
    blind the one interpreter that reports the defect at all.

    🔴 **``class_name`` is a parameter, and its default is a REAL emitted class name rather than
    a hard-coded ``E``.** ``EnumMeta`` consults the **enclosing class name** (``_is_private``), so
    an oracle that always executes under ``class E`` is structurally blind to that axis -- it
    cannot observe ``CI-113`` no matter how many labels it is given. An oracle blind to an axis is
    the same defect this whole row has been about, so the default is :data:`FIXTURE_CLASS`, the
    name :func:`python_class_name` gives the very enum :func:`_members` derives from.
    """
    return _exec_enum_capturing(pairs, class_name)[0]


def _exec_enum_capturing(
    pairs: list[tuple[str, str]], class_name: str = FIXTURE_CLASS
) -> tuple[dict[str, object], list[warnings.WarningMessage]]:
    """As :func:`_exec_enum`, and also return whatever the class body warned about."""
    import enum as enum_module

    body = '\n'.join(f'    {name} = {value!r}' for name, value in pairs)
    source = f'class {class_name}(str, enum_module.Enum):\n{body}\n'
    namespace: dict[str, object] = {'enum_module': enum_module}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        exec(compile(source, '<enum-corpus>', 'exec'), namespace)  # noqa: S102 - executing IS the assertion
    return namespace, list(caught)


def _exec_enum_clean(pairs: list[tuple[str, str]], class_name: str = FIXTURE_CLASS) -> dict[str, object]:
    """Execute the class body and assert it warned about **nothing**.

    🔴 This is a py3.10-specific detector for the name-mangling defect, and it is the reason the
    gate runs the whole interpreter matrix. 3.11+ silently drop a mangled member, so the only
    evidence there is the member count; **3.10 keeps it** under a class-derived name and emits
    ``DeprecationWarning: private variables, such as '_E__2', will be normal attributes in 3.11``.
    Asserting silence turns that warning into a failing test on one leg instead of 46 lines of
    noise in the gate output on all four.
    """
    namespace, caught = _exec_enum_capturing(pairs, class_name)
    assert caught == [], [f'{w.category.__name__}: {w.message}' for w in caught]
    return namespace


def _exec_enum_survives(name: str, class_name: str) -> bool:
    """Whether the **interpreter** accepts ``name`` as a member of ``class_name``, verbatim.

    The authority the predicate is cross-checked against (``CI94-D8``): build a real class, look
    at what came out. "Survived" means the class body ran, warned about **nothing**, produced
    exactly one member, gave it the name the compiler derives from what we wrote, and round-tripped
    the value.

    🔴 **"Warned about nothing" is a load-bearing clause, not tidiness.** py3.10 *keeps* a
    class-private member (under the mangled name) and only emits ``DeprecationWarning: private
    variables ... will be normal attributes in 3.11``, while py3.11+ drop it outright. Judging
    survival on the member count alone therefore makes 3.10 disagree with the other three legs
    about the same name -- which would make :meth:`test_the_predicate_matches_what_the_INTERPRETER_actually_does`
    pass on ``main`` and **fail after the fix, on 3.10 only**. A name whose meaning depends on the
    running interpreter is not usable; it is the Hard Rule #9 half of ``CI-113``. Measured: with
    this clause the sweep disagrees on **8** names before the fix and **0** after, on all four
    legs; without it, 3.10 reports 0 before and 8 after.
    """
    try:
        namespace, caught = _exec_enum_capturing([(name, 'v')], class_name)
    except ValueError:
        return False
    enum_class = namespace[class_name]
    members = list(enum_class)  # type: ignore[call-overload]
    return (
        not caught
        and len(members) == 1
        and members[0].name == unicodedata.normalize('NFKC', name)
        and enum_class('v').value == 'v'  # type: ignore[operator]
    )


@pytest.mark.unit
class TestToPascalCase:
    @pytest.mark.parametrize(
        ('value', 'expected'),
        [
            ('order_status', 'OrderStatus'),
            ('user', 'User'),
            ('a_b_c', 'ABC'),
            ('', ''),
        ],
    )
    def test_pascal(self, value: str, expected: str) -> None:
        assert to_pascal_case(value) == expected


@pytest.mark.unit
class TestPythonClassName:
    @pytest.mark.parametrize(
        ('name', 'schema', 'expected'),
        [
            # 🔴 These five are the BYTE-STABILITY PINS and must not be edited. They are every
            # shape that already produced a valid identifier before `CI-128` sanitized this
            # transform, so they are what proves the sanitizer is the IDENTITY on valid input --
            # the property the four committed enum-class goldens depend on.
            ('order_status', 'public', 'PublicOrderStatusEnum'),
            ('thirdType', 'public', 'PublicThirdTypeEnum'),
            ('FourthType', 'public', 'PublicFourthTypeEnum'),
            ('_first_type', 'public', 'PublicFirstTypeEnum'),
            ('status', 'auth', 'AuthStatusEnum'),
        ],
    )
    def test_class_names(self, name: str, schema: str, expected: str) -> None:
        assert python_class_name(EnumInfo(name=name, values=[], schema=schema)) == expected

    @pytest.mark.parametrize(
        ('name', 'schema', 'expected'),
        [
            # More byte-stability pins: the remaining currently-valid shapes, including the
            # non-ASCII one `CI94-D2` protects.
            ('2fa_mode', 'public', 'Public2faModeEnum'),
            ('Ünïcödé', 'public', 'PublicÜnïcödéEnum'),
            ('task_state', 'public', 'PublicTaskStateEnum'),
            ('status', 'audit', 'AuditStatusEnum'),
        ],
    )
    def test_a_valid_name_is_untouched_by_the_sanitizer(self, name: str, schema: str, expected: str) -> None:
        assert python_class_name(EnumInfo(name=name, values=[], schema=schema)) == expected

    @pytest.mark.parametrize(
        ('name', 'schema', 'expected'),
        [
            # `CI-128`: every one of these emitted a class header that did not parse, at exit 0.
            ('order status', 'public', 'PublicOrderStatusEnum'),
            ('order-status', 'public', 'PublicOrderStatusEnum'),
            ('2fa mode', 'public', 'Public2faModeEnum'),
            ('mood\n', 'public', 'PublicMoodEnum'),
            ('a"b', 'public', 'PublicABEnum'),
            ('emoji\U0001f642', 'public', 'PublicEmojiEnum'),
            (' ', 'public', 'PublicEnum'),
            ('!!!', 'public', 'PublicEnum'),
            # ⚠ The SCHEMA is the second unsanitized input, and it is just as reachable:
            # `CREATE SCHEMA "my schema"` and `CREATE SCHEMA "2fa"` are both legal Postgres.
            ('order_status', 'my schema', 'My_schemaOrderStatusEnum'),
            ('mood', '2fa', '_2faMoodEnum'),
            ('2fa', '', '_2faEnum'),
        ],
    )
    def test_a_hostile_name_is_repaired(self, name: str, schema: str, expected: str) -> None:
        resolved = python_class_name(EnumInfo(name=name, values=[], schema=schema))
        assert resolved == expected
        assert unicodedata.normalize('NFKC', resolved).isidentifier()

    def test_empty_name_edge(self) -> None:
        assert python_class_name(EnumInfo(name='', values=[], schema='public')) == 'PublicEnum'

    def test_empty_schema_edge(self) -> None:
        # Both parts empty -> the bare `Enum`, which SHADOWS `from enum import Enum`. It is not a
        # special case here on purpose: `python_class_names` seeds itself with the module's
        # import-bound names, so the collision rule is what resolves it. See
        # `TestPythonClassNames.test_the_bare_Enum_name_is_reserved_by_the_import`.
        assert python_class_name(EnumInfo(name='', values=[], schema='')) == 'Enum'

    def test_it_is_not_injective_which_is_why_python_class_names_exists(self) -> None:
        # Measured on `main` BEFORE any sanitization: six legal, distinct Postgres type names
        # already collapsed onto one class name. Sanitization widens this set; it did not create
        # it. The per-name form cannot see that -- only the per-container form can.
        variants = ['order_status', 'orderStatus', 'OrderStatus', 'Order_Status', '_order_status', 'ORDER_STATUS']
        resolved = {python_class_name(EnumInfo(name=v, values=[], schema='public')) for v in variants}
        assert resolved == {'PublicOrderStatusEnum'}


@pytest.mark.unit
class TestPythonMemberNames:
    """CI-080: the LEFT-hand side of an enum member line must be a valid, unique identifier.

    On ``main`` this was ``value.lower()``, so ``CREATE TYPE t AS ENUM ('in progress')`` emitted
    ``IN PROGRESS = "in progress"`` and the entire module failed to parse -- at exit 0, which is
    what made it a release blocker rather than a cosmetic bug.
    """

    def test_the_ordinary_case_is_unchanged(self) -> None:
        # The shape castiron has always emitted, pinned so the fix cannot quietly restyle every
        # well-behaved enum in every user's repository. This is why three of four goldens do not
        # move.
        assert _names('pending', 'Active', 'OK', 'pending_new') == ['PENDING', 'ACTIVE', 'OK', 'PENDING_NEW']

    @pytest.mark.parametrize(
        ('label', 'expected'),
        [
            ('in progress', 'IN_PROGRESS'),  # step 1: space
            ('done!', 'DONE_'),  # step 1: punctuation
            ('n/a', 'N_A'),  # step 1: slash
            ('kebab-case', 'KEBAB_CASE'),  # step 1: hyphen
            ('a\tb', 'A_B'),  # step 1: control character
            ('a\x00b', 'A_B'),  # step 1: NUL -- legal in a label, fatal in an identifier
            ('', '_'),  # step 3: the empty label (CREATE TYPE t AS ENUM (''))
            ('   ', '___'),  # step 3 is not reached: three characters in, three out
            ('2fast', '_2FAST'),  # step 4: leading digit
            ('2nd pass', '_2ND_PASS'),  # steps 1 + 4
            ('Ünïcödé', 'ÜNÏCÖDÉ'),  # CI94-D2: Unicode is KEPT, not folded to ASCII
            ('class', 'CLASS_'),  # step 6: a keyword
            ('import', 'IMPORT_'),  # step 6: a keyword
            ('None', 'NONE'),  # step 6: `none` is not reserved; `None` is
            ('sum', 'SUM'),  # step 6: a builtin, but on the exemption list -- CI-100
        ],
    )
    def test_one_label_at_a_time(self, label: str, expected: str) -> None:
        assert _names(label) == [expected]

    @pytest.mark.parametrize(
        'labels',
        [
            ('in progress', 'in-progress', 'in_progress'),  # whitespace vs punctuation vs literal
            ('done', 'DONE', 'Done'),  # case only
            ('ﬁ', 'fi'),  # NFKC: 'ﬁ'.upper() == 'FI'
            ('ß', 'ss'),  # str.upper() folding: 'ß'.upper() == 'SS'
            ('a', 'a_2', 'a'),  # the suffix itself collides
            ('a', 'a', 'a', 'a_2'),  # ... and again, one further out
            ('', ' ', '  '),  # the empty guard colliding with whitespace
            ('import', 'import_', 'IMPORT_'),  # the reserved suffix colliding
        ],
    )
    def test_every_collision_family_resolves(self, labels: tuple[str, ...]) -> None:
        # CI-072: all FOUR mechanisms that can drive two labels to one identifier are separate
        # code paths to the same symptom, so all four are enumerated rather than sampled.
        members = _members(*labels)
        assert [m.label for m in members] == list(labels), 'no label may be dropped or merged'
        names = [m.name for m in members]
        assert all(n.isidentifier() for n in names), names
        keys = [unicodedata.normalize('NFKC', n) for n in names]
        assert len(set(keys)) == len(keys), f'{names} -> {keys}: two members would be one binding'

    def test_the_uniqueness_key_is_nfkc_and_not_the_raw_name(self) -> None:
        # ⚠ THE assertion that separates this fix from a fix that looks identical and is wrong.
        # Python NFKC-normalizes identifiers at compile time, so `ﬁ` and `fi` are ONE name. A raw
        # uniqueness check leaves both members bare, and the emitted module then raises
        # `TypeError: 'FI' already defined` at IMPORT -- CI-080's failure mode in a new costume.
        assert _names('ﬁ', 'fi') == ['FI', 'FI_2']
        assert unicodedata.normalize('NFKC', 'ﬁ'.upper()) == 'FI'

    def test_the_first_collider_keeps_the_bare_name(self) -> None:
        # CI94-Q1, as ruled: ordinal suffix, first label wins, never a dropped variant.
        members = _members('in progress', 'in-progress', 'in_progress')
        assert [(m.name, m.note) for m in members] == [
            ('IN_PROGRESS', None),
            ('IN_PROGRESS_2', 'name collision'),
            ('IN_PROGRESS_3', 'name collision'),
        ]

    def test_a_reserved_label_is_annotated(self) -> None:
        assert _members('import') == [EnumMember(label='import', name='IMPORT_', note='reserved keyword')]

    def test_the_ordinary_transform_carries_no_note(self) -> None:
        # CI94-D3: the value literal on the same line already IS the label, so a comment on every
        # member would be bytes in every user's file forever.
        assert all(m.note is None for m in _members('pending', 'in progress', '2fast'))

    def test_a_collision_note_wins_over_a_reserved_note(self) -> None:
        # Documented precedence: the collision explains the trailing `_2` a reader is looking at.
        assert [(m.name, m.note) for m in _members('import_', 'import')] == [
            ('IMPORT_', None),
            ('IMPORT__2', 'name collision'),
        ]

    def test_the_result_is_positionally_aligned_with_enum_values(self) -> None:
        labels = ['b', 'a', '2', '', 'a']
        members = python_member_names(EnumInfo(name='t', values=labels, schema='public'))
        assert [m.label for m in members] == labels

    def test_it_is_a_pure_function_of_the_ordered_labels(self) -> None:
        # Hard Rule #9. CI-065 is a real prior bug where a non-total sort key flipped output under
        # PYTHONHASHSEED; there is no set or dict iteration here, and this pins that.
        labels = ('in progress', 'in-progress', 'done', 'DONE', 'ﬁ', 'fi', '', '2fast', 'import')
        assert len({tuple(_names(*labels)) for _ in range(100)}) == 1

    def test_order_is_load_bearing_and_the_transform_does_not_pretend_otherwise(self) -> None:
        # The accepted cost of CI94-Q1, asserted so nobody rediscovers it as a surprise: inserting
        # a label that sorts before an existing collider RENUMBERS the later ones. Bounded to
        # colliders, visible on the line, and the reason CI-094 recommends an
        # `enum-member-overrides` escape hatch as a follow-up row.
        assert _names('a b', 'a-b') == ['A_B', 'A_B_2']
        assert _names('a-b', 'a b') == ['A_B', 'A_B_2']
        assert _members('a b', 'a-b')[1].label == 'a-b'
        assert _members('a-b', 'a b')[1].label == 'a b'

    @pytest.mark.parametrize('label', ['id', 'credits', 'copyright', 'license', 'help', 'property', 'sum'])
    def test_an_exempted_label_is_not_renamed(self, label: str) -> None:
        # ✅ CI-100, closed. `column_name_reserved_exceptions` is an EXEMPTION list -- names that
        # need NOT be renamed -- and the enum path used to apply it with `or`, i.e. as an ADDITION
        # list, so `id` was suffixed and annotated "reserved keyword" when the list says the
        # opposite. The enum path and the column path (`ir/build.py:341`) now read it the same
        # way: `string_is_reserved(...) and not column_name_reserved_exceptions(...)`.
        #
        # ⚠ Note what the `or` did NOT do. Every name on the exemption list is already a builtin
        # (`credits`/`copyright`/`license`/`help` come from `site`), so the `or` never actually
        # ADDED anything -- its only observable effect was the missing exemption.
        #
        # Dropping the note is correct under CI94-D3: `ID` IS the straight transform of `id`, so
        # there is nothing to gloss.
        member = _members(label)[0]
        assert member.name == label.upper()
        assert member.note is None

    def test_a_reserved_label_not_on_the_exemption_list_is_still_renamed(self) -> None:
        # 🔴 The counter-witness, and it is load-bearing: without it, DELETING the reserved guard
        # outright would satisfy every assertion above. The captain ruled on 2026-08-08 that the
        # enum path KEEPS its reserved guard, so CI-100 is a boolean correction and not a removal.
        assert _members('class') == [EnumMember(label='class', name='CLASS_', note='reserved keyword')]
        assert _members('import') == [EnumMember(label='import', name='IMPORT_', note='reserved keyword')]


#: The character classes that decide every reserved shape, for the generated name/label sweeps.
#:
#: 🔴 **``NFKC_UNDERSCORE`` is the point.** Three review rounds on this row all landed on "the
#: right assertion pointed at the wrong corpus", and round 2's instance was that the
#: predicate cross-check enumerated ``itertools.product('_A', ...)`` -- an alphabet that
#: **cannot contain an NFKC-active character**, while claiming in its own docstring to be the
#: authority. Six identifier-continue codepoints normalize to ``'_'``; U+FF3F is one, and it is
#: in every sweep below so that blindness cannot recur.
NFKC_UNDERSCORE = '\uff3f'  # FULLWIDTH LOW LINE -- NFKC-normalizes to '_'

#: Alphabet for generated **member names**: ASCII underscore, an NFKC-active underscore, a
#: letter, a digit. Every reserved shape is expressible in it.
NAME_ALPHABET = ('_', NFKC_UNDERSCORE, 'A', '2')

#: The class name the two GENERATED-name sweeps execute under -- ``'A'``, and the choice is the
#: single most load-bearing line in this file.
#:
#: 🔴 **It used to be ``'E'``, and ``NAME_ALPHABET`` cannot spell ``_E__``.** The sweep was
#: therefore *structurally* incapable of producing a class-private name, so the one test that
#: claims to be the authority on "what the interpreter actually does" was blind to the class-name
#: axis -- which is exactly how ``CI-113`` survived it. ``'A'`` is a letter the alphabet **does**
#: contain, so ``_A__A``, ``_A__2``, ``_A_＿A`` ... are all generated and the axis becomes
#: observable. Measured: 8 predicate-vs-interpreter disagreements before the fix, 0 after, on
#: every gate leg.
#:
#: ⚠ **Do not conclude the exposure is ASCII-reachable in the product.** ``'A'`` is a test-only
#: class name; every name :func:`python_class_name` produces ends in the literal ``Enum``, whose
#: lowercase ``num`` no ``.upper()``ed ASCII label can carry.
#: :meth:`TestCi113TheClassNameAxisIsClosed.test_ascii_only_labels_cannot_reach_it` pins that bound.
SWEEP_CLASS = 'A'

#: Alphabet for generated **labels** (which are arbitrary text, so whitespace belongs here and
#: not in ``NAME_ALPHABET``). A space is the character that produced the very first sunder
#: report -- ``' x '`` -> ``_X_``.
LABEL_ALPHABET = ('_', NFKC_UNDERSCORE, 'A', '2', ' ')


def _generated_names(max_length: int = 5) -> list[str]:
    """Every identifier over :data:`NAME_ALPHABET` up to ``max_length``."""
    return [
        candidate
        for size in range(1, max_length + 1)
        for p in itertools.product(NAME_ALPHABET, repeat=size)
        if (candidate := ''.join(p)).isidentifier()
    ]


def _generated_labels(max_length: int = 4) -> list[str]:
    """Every string over :data:`LABEL_ALPHABET` up to ``max_length``."""
    return [''.join(p) for size in range(1, max_length + 1) for p in itertools.product(LABEL_ALPHABET, repeat=size)]


#: Alphabet for the generated :func:`python_identifier` sweep. Every class of character that can
#: defeat ``str.isidentifier()`` is represented, plus the two that defeat a naive check of it:
#: ``NFKC_UNDERSCORE`` (a *valid* identifier character that normalizes to ``'_'``) and ``ﬁ`` (a
#: valid identifier character that NFKC-expands to **two** characters). A leading digit is the
#: only failure the character map itself cannot fix, so ``'2'`` is what exercises the second rule.
IDENTIFIER_ALPHABET = ('_', NFKC_UNDERSCORE, 'ﬁ', 'A', '2', ' ', '-', '"', '\n', '\t', '\x00', '.', '\U0001f642')


def _generated_identifier_inputs(max_length: int = 3) -> list[str]:
    """Every string over :data:`IDENTIFIER_ALPHABET` up to ``max_length``, plus the empty string."""
    return [''] + [
        ''.join(p) for size in range(1, max_length + 1) for p in itertools.product(IDENTIFIER_ALPHABET, repeat=size)
    ]


@pytest.mark.unit
class TestPythonIdentifier:
    """``CI-128``: the shared identifier-repair primitive, which ``CI-130`` will also call.

    ``python_identifier`` has to be **total** over arbitrary Postgres text, because that is what
    reaches it: a pg type name, a schema name, and (next row) a table name are all raw user text
    that PostgREST hands over verbatim. The sweep below is the assertion that carries the weight --
    the parametrized cases only name the shapes someone thought of.
    """

    @pytest.mark.parametrize('text', ['order_status', 'OrderStatus', '_x', 'Ünïcödé', 'ﬁ', 'a2', '__init__'])
    def test_it_is_the_identity_on_a_valid_identifier(self, text: str) -> None:
        # Including non-ASCII (`CI94-D2`, ruled by the captain: Unicode is KEPT, not folded to
        # ASCII) -- destroying an international name to satisfy a lint rule is the worse trade.
        assert python_identifier(text) == text

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('order status', 'order_status'),
            ('order-status', 'order_status'),
            ('a"b', 'a_b'),
            ('a\nb', 'a_b'),
            ('a\tb', 'a_b'),
            ('a\x00b', 'a_b'),
            ('a.b', 'a_b'),
            ('emoji\U0001f642', 'emoji_'),
        ],
    )
    def test_it_repairs_every_hostile_character(self, text: str, expected: str) -> None:
        # One character out per character in: no run-collapsing and no stripping, so `'a  b'` and
        # `'a b'` stay DISTINGUISHABLE attempts and the collision rule -- not this function --
        # resolves them when they are not.
        assert python_identifier(text) == expected

    def test_a_leading_digit_gains_exactly_one_underscore(self) -> None:
        assert python_identifier('2fa') == '_2fa'
        assert python_identifier('2') == '_2'

    def test_the_empty_string_becomes_an_underscore(self) -> None:
        # Unreachable from `python_class_name` (the `Enum` suffix means the string is never
        # empty); reachable from `CI-130`'s table-name path, where it is a different question.
        assert python_identifier('') == '_'

    def test_the_alphabet_covers_the_classes_that_matter(self) -> None:
        # A sweep that silently narrows passes vacuously -- the standing lesson from `CI-080`
        # round 2, where the corpus could not spell the shape the test claimed to guard.
        assert NFKC_UNDERSCORE in IDENTIFIER_ALPHABET
        assert unicodedata.normalize('NFKC', NFKC_UNDERSCORE) == '_'
        assert unicodedata.normalize('NFKC', 'ﬁ') == 'fi', 'the NFKC-expanding character must stay'
        assert any(c.isdigit() for c in IDENTIFIER_ALPHABET), 'the leading-digit rule needs a digit'
        assert any(not ('_' + c).isidentifier() for c in IDENTIFIER_ALPHABET), 'the map needs something to repair'
        assert len(_generated_identifier_inputs()) == 2380

    def test_no_generated_input_survives_as_a_non_identifier(self) -> None:
        # 🔴 The oracle. The test is on the NFKC form because CPython normalizes identifiers at
        # COMPILE time -- the name the compiler judges is not always the string castiron wrote.
        for text in _generated_identifier_inputs():
            repaired = python_identifier(text)
            assert unicodedata.normalize('NFKC', repaired).isidentifier(), f'{text!r} -> {repaired!r}'

    def test_one_prefix_is_always_enough(self) -> None:
        # ⚠ The spec's proof said one `'_'` prefix suffices; an IDENTICAL "one pass is enough"
        # claim was made on the enum MEMBER path and was wrong (`_repair_enum_shape` needs up to
        # three). Idempotence is that proof made falsifiable: a second pass changing anything
        # would mean the first was insufficient.
        for text in _generated_identifier_inputs():
            once = python_identifier(text)
            assert python_identifier(once) == once, f'{text!r} needed a second pass'

    def test_it_never_collapses_a_run_or_strips(self) -> None:
        # The length invariant the character map promises, which is what keeps two distinct
        # Postgres names distinguishable for the collision rule to arbitrate.
        for text in _generated_identifier_inputs():
            if text:
                assert len(python_identifier(text)) in (len(text), len(text) + 1)


def fold_map() -> dict[str, str]:
    """ASCII lowercase -> a codepoint the character map keeps that ``.upper()`` ignores, NFKC folds.

    389 such codepoints exist and they cover all 26 letters, which is what makes any generated
    class name spellable by a Postgres enum label -- the fact an earlier "unreachable (verified)"
    claim in ``naming.py`` missed. See :class:`TestCi113TheClassNameAxisIsClosed`.
    """
    folders: dict[str, str] = {}
    for cp in range(0x110000):
        char = chr(cp)
        if ('_' + char).isidentifier() and char.upper() == char:
            folded = unicodedata.normalize('NFKC', char)
            if len(folded) == 1 and 'a' <= folded <= 'z':
                folders.setdefault(folded, char)
    return folders


def crafted_class_private_label(class_name: str) -> str:
    """A Postgres enum label whose NFKC form spells ``_<class_name>__X`` -- ``CI-113``'s reproducer.

    🔴 **Module-level and imported by the emitter's executing test, on purpose** -- the same
    arrangement (and the same reasoning) as :data:`ENUM_LABEL_CORPUS`. A ``naming.py`` unit test
    cannot prove the **emitter** passes the same class name it renders the header from; only a test
    driving :class:`~castiron.emitters.PydanticEmitter` can. Sharing the construction means the two
    cannot be pointed at different labels.

    Args:
        class_name: The enclosing ``Enum`` subclass's name, e.g. ``'PublicOrderStatusEnum'``.

    Returns:
        The crafted label, spelled in ``upper()``-invariant modifier letters.
    """
    folders = fold_map()
    return ''.join(folders.get(ch, ch) for ch in f'_{class_name}__X')


#: Shapes a Postgres enum label can legally carry. Postgres allows any text up to
#: ``NAMEDATALEN``; PostgREST carries it verbatim into ``properties.<c>.enum``.
#:
#: 🔴 **Module-level, and imported by the emitter's executing test, on purpose.** Fix round 1
#: found the sunder/dunder defect by noticing that the *trigger was already committed here* --
#: ``'"quoted"'`` maps to ``_QUOTED_`` -- while the only test that actually **executes** an
#: emitted enum body ran over ``ADVERSARIAL_TEXT``, which is CI-009's docstring/comment corpus
#: and contains no symmetric-punctuation label. The right corpus and the right assertion both
#: existed and were pointed at each other's targets. They now share one list, and
#: ``test_the_emitter_executes_the_naming_corpus_too`` fails if they are separated again.
ENUM_LABEL_CORPUS = [
    '',
    ' ',
    '   ',
    '\t',
    '\n',
    '\n\ndoc\n\n',
    '\x00',
    'a\x00b',
    '2',
    '2fast',
    '2nd pass',
    'in progress',
    'in-progress',
    'in_progress',
    'n/a',
    'done!',
    'DONE',
    'done',
    'Done',
    'class',
    'import',
    'None',
    'True',
    'lambda',
    'id',
    'sum',
    'ﬁ',
    'fi',
    'ß',
    'ss',
    'Ünïcödé — 表 — 🚀',
    'ı',
    '٣',
    'x' * 400,
    '%$#@!',
    'a.b.c',
    '"quoted"',
    "it's",
    'a b',
    # ⚠ Added in fix round 1. Every one of these produced a member name that is a valid
    # identifier AND unusable as an enum member: `_sunder_` raised ValueError at import (the
    # whole module dead), `__dunder__` was silently dropped (a lost label, which CI94-Q1
    # forbids outright). A trailing space in a CREATE TYPE was all it took.
    '(none)',
    ' x ',
    ' in progress ',
    '(pending)',
    '[x]',
    '-tbd-',
    '<null>',
    '{draft}',
    '.dot.',
    '__init__',
    '__doc__',
    '_x_',
    '-',
    '--',
    '---',
    # ⚠ Added in fix round 2. Every label above reaches the sunder or dunder shape; NONE of them
    # reaches the private/mangled one from a single label -- that arrived only via the collision
    # path, so the per-label tests never saw this round's predecessor's headline shape. `'  x'`
    # closes that: two spaces map to `__`, giving `__X`, which Python name-mangles.
    '  x',
    '  2',
    # ⚠ And the NFKC-active underscores. Six identifier-continue codepoints normalize to `_`
    # (U+FF3F, U+FE33, U+FE34, U+FE4D, U+FE4E, U+FE4F); `CI94-D2` keeps them and `.upper()`
    # leaves them, so a predicate reading the RAW name cannot see the shape the compiler sees.
    # `'_x\uff3f'` raised ValueError at import; `'\uff3fx'` was silently dropped.
    '_x\uff3f',
    '\uff3fx',
    '\ufe4dx\ufe4d',
    '\uff3f\uff3fx',
]


@pytest.mark.unit
class TestEveryMemberNameIsAnIdentifier:
    """The property, enumerated rather than sampled (`CI-072`)."""

    LABELS = ENUM_LABEL_CORPUS

    def test_every_single_label_yields_an_identifier(self) -> None:
        for label in self.LABELS:
            (member,) = _members(label)
            assert member.name.isidentifier(), f'{label!r} -> {member.name!r}'

    def test_the_whole_corpus_at_once_yields_unique_identifiers(self) -> None:
        members = _members(*self.LABELS)
        assert [m.label for m in members] == self.LABELS, 'no label may be dropped'
        keys = [unicodedata.normalize('NFKC', m.name) for m in members]
        assert all(m.name.isidentifier() for m in members)
        assert len(set(keys)) == len(keys), 'two members would collapse to one binding at import'

    def test_no_member_name_is_a_python_keyword(self) -> None:
        # A keyword is not merely a bad name -- `class CLASS: pass` is a SyntaxError. The uppercase
        # transform makes this near-impossible in ASCII, so the interesting cases are the folding
        # ones, which is why the label corpus above carries `ı` (whose upper() is `I`).
        for label in self.LABELS:
            (member,) = _members(label)
            assert not keyword.iskeyword(member.name)
            assert not keyword.iskeyword(unicodedata.normalize('NFKC', member.name))

    def test_uppercasing_can_never_undo_the_character_map(self) -> None:
        # ⚠ Step 2 runs AFTER step 1, so it could in principle turn an identifier-legal character
        # into an illegal one and silently reopen CI-080. Enumerated over the ENTIRE Unicode
        # codepoint space rather than argued -- 1 114 112 codepoints, measured at 56 ms, which is
        # the whole reason it is affordable to assert instead of assume (`CI-072`).
        offenders = [
            hex(cp)
            for cp in range(0x110000)
            if ('_' + chr(cp)).isidentifier() and not ('_' + chr(cp).upper()).isidentifier()
        ]
        assert offenders == [], f'upper() breaks identifier-legality for {offenders[:20]}'


@pytest.mark.unit
class TestTheCharacterMapIsSharedWithTheColumnPath:
    """``CI85-D1``: one algorithm, one set of bugs — asserted by **identity**, not by behaviour.

    CI-080 (enum labels) and CI-085 (column names) are the same defect at two call sites, and the
    character map was written twice before it was written once. ``CI-085`` moved
    ``naming._identifier_characters`` into :mod:`castiron.ir.build` as the public
    :func:`~castiron.ir.build.identifier_characters` and repointed this module at it.

    Comparing the *objects* rather than a handful of outputs is deliberate: a re-divergence
    (someone re-adding a private copy here "just for the enum path") would keep every behavioural
    assertion green right up until the two implementations drifted, which is exactly how the
    duplication arose the first time.

    The direction of the move matters too, and it is not a taste call: ``castiron.utils.naming``
    already imports ``castiron.ir.build`` (line 21), so putting the shared helper under ``utils``
    would reverse that edge and leave a partially-initialized-module ``ImportError`` waiting for
    the first person to add a re-export to ``castiron/utils/__init__.py``.
    """

    def test_the_enum_path_and_the_column_path_call_the_same_function(self) -> None:
        import castiron.ir.build
        import castiron.utils.naming

        assert castiron.utils.naming.identifier_characters is castiron.ir.build.identifier_characters

    def test_the_module_no_longer_defines_a_private_copy(self) -> None:
        import castiron.utils.naming

        assert not hasattr(castiron.utils.naming, '_identifier_characters')

    def test_the_dependency_edge_still_points_utils_to_ir(self) -> None:
        # ``castiron.ir.build`` must not IMPORT ``castiron.utils`` -- see the class docstring.
        # Read off the AST, not the text: the module's own docstrings discuss the edge by name,
        # and a substring check would assert about prose rather than about imports.
        source = (Path(__file__).resolve().parents[3] / 'src' / 'castiron' / 'ir' / 'build.py').read_text(
            encoding='utf-8'
        )
        imported: list[str] = []
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        offenders = [name for name in imported if name.startswith('castiron.utils')]
        assert offenders == [], f'ir.build now imports {offenders} -- the utils -> ir edge has reversed'


@pytest.mark.unit
class TestInflectionWrappers:
    def test_pluralize(self) -> None:
        assert pluralize('post') == 'posts'
        assert pluralize('category') == 'categories'
        assert pluralize('child') == 'children'

    def test_singularize(self) -> None:
        assert singularize('posts') == 'post'
        assert singularize('categories') == 'category'

    def test_round_trip(self) -> None:
        assert singularize(pluralize('book')) == 'book'


@pytest.mark.unit
class TestEveryMemberNameIsUsableAsAnEnumMember:
    """🔴 The assertion whose absence let the sunder/dunder defect through fix round 0.

    ``.isidentifier()`` is **necessary and not sufficient**. ``EnumMeta`` reserves two shapes on
    top of Python's identifier rules, and both were reachable from ordinary Postgres labels:

    * ``_sunder_`` (``'(none)'`` -> ``_NONE_``) raised ``ValueError`` when the class body ran, so
      the **whole emitted module** was unusable -- at ``castiron gen`` exit 0, and after
      ``py_compile`` had passed. CI-080's failure mode relocated, not closed.
    * ``__dunder__`` (``'__init__'`` -> ``__INIT__``) was **silently dropped**, violating
      ``CI94-Q1``'s single non-negotiable: never drop a variant.

    A **trailing space** in a ``CREATE TYPE`` was enough to trigger the first. So these tests
    build a real ``Enum`` class and **execute** it; they never merely compile.
    """

    def test_every_label_round_trips_through_a_real_enum_body(self) -> None:
        # Each label alone, so a failure names the label rather than the corpus.
        for label in ENUM_LABEL_CORPUS:
            (member,) = _members(label)
            namespace = _exec_enum_clean([(member.name, label)])
            enum_class = namespace[FIXTURE_CLASS]
            assert len(list(enum_class)) == 1, f'{label!r} -> {member.name!r} was dropped by EnumMeta'  # type: ignore[call-overload]
            assert enum_class(label).value == label, f'{label!r} does not round-trip'  # type: ignore[operator]

    def test_the_whole_corpus_forms_one_working_enum(self) -> None:
        # And all at once, which is the shape a user actually gets: every label present, every
        # value exact, nothing collapsed. This is the end-to-end statement of CI94-Q1.
        members = _members(*ENUM_LABEL_CORPUS)
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members])[FIXTURE_CLASS]
        assert len(list(enum_class)) == len(ENUM_LABEL_CORPUS), 'a label was dropped or two collapsed'  # type: ignore[call-overload]
        for label in ENUM_LABEL_CORPUS:
            assert enum_class(label).value == label  # type: ignore[operator]

    @pytest.mark.parametrize(
        ('label', 'expected'),
        [
            ('(none)', '_NONE__'),
            (' x ', '_X__'),
            (' in progress ', '_IN_PROGRESS__'),
            ('(pending)', '_PENDING__'),
            ('[x]', '_X__'),
            ('-tbd-', '_TBD__'),
            ('<null>', '_NULL__'),
            ('{draft}', '_DRAFT__'),
            ('.dot.', '_DOT__'),
            ('"quoted"', '_QUOTED__'),
            ('_x_', '_X__'),
            ('__init__', '__INIT___'),
            ('__doc__', '__DOC___'),
        ],
    )
    def test_the_repaired_names_are_pinned(self, label: str, expected: str) -> None:
        (member,) = _members(label)
        assert member.name == expected
        assert member.note == 'reserved by Enum'

    @pytest.mark.parametrize('name', ['_', '__', '___', '____', '_2FAST', '_A', 'A_'])
    def test_short_and_asymmetric_names_are_left_alone(self, name: str) -> None:
        # The counter-witness. Without it, "append an underscore to everything" would pass every
        # test above -- and `_2ND_PASS`, `_` and `___` are all legal members that must not move.
        # ⚠ `'_A'` is here for a second reason after CI-113: under the sweep class `A` the prefix
        # `_A__` is meaningful, and `_A` is the proof the fourth clause needs MORE than the
        # prefix's leading characters -- `len(name) > len(mangled)` is what excludes it.
        from castiron.utils.naming import _is_enum_reserved_shape

        assert not _is_enum_reserved_shape(name, FIXTURE_CLASS)
        assert not _is_enum_reserved_shape(name, SWEEP_CLASS)

    def test_the_predicate_matches_what_the_INTERPRETER_actually_does(self) -> None:
        """⚠ ``CI94-D8`` applied to ``enum``: state the rule in ``src/``, check it against reality.

        ``enum._is_sunder`` / ``_is_dunder`` / ``_is_private`` are **private, unguaranteed and
        demonstrably version-skewed** -- 3.13 dropped a clause from ``_is_private``, and 3.10
        words the sunder error differently. So this does not compare predicate to predicate. It
        **builds a real enum class and looks at what came out**, which is the only authority that
        matters and is immune to all of that.

        A name is "reserved" exactly when the interpreter refuses it, drops it, or gives the
        member a name other than the one written. Runs on all four gate legs.

        🔴 **The sweep executes under :data:`SWEEP_CLASS` (``'A'``), not ``'E'``, and that single
        character is what makes this test able to fail at all.** ``NAME_ALPHABET`` cannot spell
        ``_E__``, so under ``'E'`` no class-private name is generated and ``CI-113`` passed here
        untouched -- the file's authority on "what the interpreter actually does" was structurally
        blind to the one axis besides the name itself. Under ``'A'`` the sweep produces ``_A__A``,
        ``_A__2``, ``_A_＿A`` ...: **8** disagreements before the fix, **0** after, identically on
        3.10 / 3.11 / 3.12 / 3.13.

        "Usable" is judged MODULO NFKC rather than byte-for-byte, and a class body that **warned**
        counts as not usable -- see :func:`_exec_enum_survives`, where both clauses are justified.
        """
        from castiron.utils.naming import _is_enum_reserved_shape

        names = _generated_names()
        # ⚠ The SIZE is pinned, not just the presence of an NFKC character. `_generated_names`
        # takes `max_length` with a default nothing else asserts -- at `max_length=2` the sweep
        # drops to 10 names containing ZERO reserved shapes, and every assertion below would pass
        # vacuously. Its sibling `_generated_labels` was already pinned; this one was not. That is
        # this file's own stated hazard ("a guard that silently narrows is worse than none")
        # applied to one generator and not the other.
        assert len(names) == 682, len(names)
        assert any(NFKC_UNDERSCORE in name for name in names), 'the sweep must exercise NFKC folding'
        assert sum(_is_enum_reserved_shape(n, SWEEP_CLASS) for n in names) > 0, 'the sweep has no reserved shape'
        # ⚠ And the class-name axis is genuinely exercised. Without this, an alphabet change that
        # stopped spelling `_A__` would silently restore exactly the blindness CI-113 exposed --
        # the same "a guard that silently narrows is worse than none" hazard as the size pin above.
        assert any(name.startswith(f'_{SWEEP_CLASS}__') for name in names), 'the sweep cannot spell a class-private'

        for name in names:
            survived = _exec_enum_survives(name, SWEEP_CLASS)
            assert _is_enum_reserved_shape(name, SWEEP_CLASS) is not survived, (
                f'{name!r}: predicate says reserved={_is_enum_reserved_shape(name, SWEEP_CLASS)}, '
                f'but the interpreter says usable={survived}'
            )

    def test_the_repair_terminates_in_at_most_three_appends(self) -> None:
        """The bound is a proof, and this is the proof executed.

        A name ending in three or more underscores can be none of the four shapes, and each
        iteration adds exactly one -- so three is the ceiling. ``__2`` is the worst case and it
        needs all three: ``__2_`` is still private, ``__2__`` is then dunder, ``__2___`` is clean.

        ⚠ **The fourth (class-private) clause cannot become the binding constraint**, which is why
        the bound survives ``CI-113`` unchanged: it is false as soon as the NFKC form ends in
        **two** underscores, a weaker requirement than the three the other clauses need. Swept
        under :data:`SWEEP_CLASS`, so the clause is actually exercised rather than argued.
        """
        from castiron.utils.naming import _is_enum_reserved_shape, _repair_enum_shape

        names = _generated_names()
        assert len(names) == 682, len(names)  # see the note in the sibling test: pin the size
        reserved = [n for n in names if _is_enum_reserved_shape(n, SWEEP_CLASS)]
        # ⚠ 192, not the 184 this pinned before CI-113 -- the 8 extra are the class-private names
        # (`_A__A`, `_A__2`, `_A_＿A`, ...) the predicate could not see. If this drops back to 184
        # the fourth clause has stopped firing.
        assert len(reserved) == 192, len(reserved)

        repaired = [_repair_enum_shape(n, SWEEP_CLASS) for n in reserved]
        assert all(not _is_enum_reserved_shape(r, SWEEP_CLASS) for r in repaired)
        assert max(len(r) - len(n) for n, r in zip(reserved, repaired)) == 3
        assert _repair_enum_shape('__2', SWEEP_CLASS) == '__2___'
        assert _repair_enum_shape('_X_', SWEEP_CLASS) == '_X__'
        assert _repair_enum_shape('__INIT__', SWEEP_CLASS) == '__INIT___'
        # The fourth clause's own worst case: two appends, and never three.
        assert _repair_enum_shape('_A__A', SWEEP_CLASS) == '_A__A__'

        # ... and every repaired name really is accepted by a real Enum, under the name the
        # compiler gives it. Deduplicated by NFKC KEY, not by raw string: two distinct repaired
        # names can normalize to one identifier (`\uff3f222__` and `_222__` both -> `_222__`),
        # and putting both in one class body is a `TypeError`, not a finding about the repair.
        by_key = {unicodedata.normalize('NFKC', name): name for name in sorted(repaired)}
        pairs = [(name, f'v{i}') for i, name in enumerate(by_key.values())]
        enum_class = _exec_enum_clean(pairs, SWEEP_CLASS)[SWEEP_CLASS]
        assert [m.name for m in enum_class] == sorted(by_key)  # type: ignore[union-attr]

    def test_the_collision_suffix_cannot_smuggle_a_reserved_shape_back_in(self) -> None:
        # 🔴 THE regression this round exists for. `''`, `' '`, `'\t'` and `'\n'` all sanitize to
        # `_`, so the collision rule produced `__2`, `__3`, `__4` -- which Python NAME-MANGLES.
        # On 3.11+ those labels vanished; on 3.10 they became `_E__2`, i.e. the member name
        # depended on the enum's class name. The suffix is therefore repaired too.
        #
        # ⚠ Executed under FIXTURE_CLASS, the class name `python_class_name` really gives the enum
        # `_members` derives from -- NOT a `class E` stand-in. Members built from one enum and
        # executed under another class are checked against a class the emitter would never write,
        # which is precisely how CI-113 stayed invisible.
        from castiron.utils.naming import _is_enum_reserved_shape

        members = _members('', ' ', '\t', '\n', '\x00', '-', '---', '   ')
        assert all(not _is_enum_reserved_shape(m.name, FIXTURE_CLASS) for m in members), [m.name for m in members]
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members])[FIXTURE_CLASS]
        assert len(list(enum_class)) == 8, 'a label was mangled away'  # type: ignore[call-overload]
        for member in members:
            assert enum_class(member.label).value == member.label  # type: ignore[operator]


@pytest.mark.unit
class TestTheOracle:
    """🔴 The generated round-trip oracle. An oracle catches shapes nobody has named yet.

    **Why this class exists, stated plainly.** CI-080 took three review rounds, and all three
    landed on the same structural mistake: *the right assertion pointed at the wrong corpus.*

    * round 0 -- the executing test ran over ``ADVERSARIAL_TEXT`` (no symmetric punctuation), so
      ``_sunder_`` and ``__dunder__`` shipped;
    * round 1 -- writing the executing test over the naming corpus exposed the **private /
      name-mangled** shape, which the collision rule itself produced;
    * round 2 -- the predicate cross-check enumerated ``product('_A', ...)``, an alphabet that
      **cannot contain an NFKC-active character**, so the NFKC shape survived in the one test
      whose docstring claimed to be the backstop.

    A bigger hand-written corpus is the wrong answer to that, because it only ever contains the
    shapes someone thought of. This does not enumerate *shapes* at all: it enumerates the
    **character classes that can produce one** -- ASCII underscore, an NFKC-active underscore, a
    letter, a digit, a space -- takes every string over them, runs the **real** transform, builds
    a **real** enum **under the real emitted class name**, and asserts every label survives. A
    further shape on the *spelling* axis fails here without anyone naming it first.

    ⚠ **Scope of that claim, because an earlier revision overstated it.** It said "a fourth shape
    on this axis fails here without anyone naming it first" while
    :func:`_exec_enum_capturing` hard-coded ``class E`` -- so the oracle was **structurally
    blind** to the one axis ``EnumMeta`` consults besides the name itself: the enclosing class
    name. ``CI-113``'s reproducer passed every assertion below. The class name is now threaded
    through, which makes the axis *observable*; the label alphabet still cannot spell a class
    name (that needs modifier-letter Unicode), so ``CI-113`` is pinned explicitly by
    :class:`TestCi113` rather than discovered here.
    """

    def test_the_alphabet_covers_the_classes_that_matter(self) -> None:
        # A guard that silently narrows is worse than none: if the alphabet loses the NFKC
        # character, this class quietly stops testing the thing round 2 was about.
        assert NFKC_UNDERSCORE in LABEL_ALPHABET
        assert unicodedata.normalize('NFKC', NFKC_UNDERSCORE) == '_'
        assert ' ' in LABEL_ALPHABET, 'whitespace produced the very first sunder report'
        assert any(c.isdigit() for c in LABEL_ALPHABET), 'a leading digit needs its own guard'
        assert any(c.isalpha() for c in LABEL_ALPHABET)
        assert len(_generated_labels()) == 780

    #: The enum the oracle emits, and the class name it is executed under -- the REAL one the
    #: Pydantic emitter would write, not a stand-in.
    ENUM = EnumInfo(name='mood', values=[], schema='public')

    def test_the_oracle_executes_under_the_real_emitted_class_name(self) -> None:
        # Guards the fix for the oracle's own blindness: if this ever reverts to a stand-in like
        # `E`, the class-name axis stops being observable and CI-113 becomes undetectable again.
        assert python_class_name(self.ENUM) == 'PublicMoodEnum'

    def test_every_generated_label_survives_as_a_real_enum_member(self) -> None:
        labels = _generated_labels()
        members = python_member_names(EnumInfo(name='mood', values=labels, schema='public'))

        # Nothing dropped or merged on the way in ...
        assert [m.label for m in members] == labels
        assert all(m.name.isidentifier() for m in members)

        # ... and nothing dropped, renamed or collapsed on the way out. One class, all 780.
        class_name = python_class_name(self.ENUM)
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members], class_name)[class_name]
        assert len(list(enum_class)) == len(labels), (
            f'{len(labels) - len(list(enum_class))} label(s) did not become members. A reserved '
            f'shape reached the emitted enum -- find it with `_is_enum_reserved_shape`.'
        )
        for label in labels:
            assert enum_class(label).value == label, f'{label!r} does not round-trip'

    def test_no_generated_name_is_left_in_a_reserved_shape(self) -> None:
        # The same property stated against the predicate rather than the interpreter, so a
        # failure says WHICH shape rather than only "a member vanished".
        from castiron.utils.naming import _is_enum_reserved_shape

        # ⚠ The predicate is asked about the SAME class name the members were derived under
        # (`python_class_name(self.ENUM)`), which is the rule CI-113 exists to enforce: a stand-in
        # here would ask a question about a class the emitter would never write.
        class_name = python_class_name(self.ENUM)
        members = python_member_names(EnumInfo(name='mood', values=_generated_labels(), schema='public'))
        offenders = [(m.label, m.name) for m in members if _is_enum_reserved_shape(m.name, class_name)]
        assert offenders == [], offenders

    def test_the_generated_names_are_unique_under_nfkc(self) -> None:
        members = python_member_names(EnumInfo(name='mood', values=_generated_labels(), schema='public'))
        keys = [unicodedata.normalize('NFKC', m.name) for m in members]
        assert len(set(keys)) == len(keys), 'two members would collapse to one binding at import'

    def test_the_oracle_is_deterministic_across_hash_seeds(self) -> None:
        """Hard Rule #9 over the generated corpus, in **subprocesses** under different seeds.

        ⚠ The first version called :func:`python_member_names` five times **in one process**,
        which cannot observe hash-seed-dependent ordering at all: ``PYTHONHASHSEED`` is fixed for
        the life of an interpreter. Mutation-verified as worthless -- replacing
        ``for label in enum.values`` with ``for label in set(enum.values)``, a literal Hard Rule #9
        violation, left it **green** while 25 other tests went red. A guard that cannot fail on
        the property it names is the exact thing this row's last three rounds were about, so it
        was shipped inside the round whose thesis was eliminating them.

        ``CI-065`` is the standing precedent: a non-total sort key really did flip output under
        ``PYTHONHASHSEED``.
        """
        script = (
            'import sys; sys.path.insert(0, ".");'
            'from castiron.ir import EnumInfo;'
            'from castiron.utils.naming import python_member_names;'
            'from tests.unit.utils.test_naming import _generated_labels;'
            'labels = _generated_labels();'
            'print("|".join(m.name for m in '
            'python_member_names(EnumInfo(name="mood", values=labels, schema="public"))))'
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
class TestCi113TheClassNameAxisIsClosed:
    """✅ ``CI-113``, closed — the fourth reserved shape, and the bound on its reachability.

    ``EnumMeta`` consults the **enclosing class name**: ``_is_private(cls_name, name)`` swallows a
    member already spelled ``_<ClassName>__x``. :func:`python_member_names` used never to receive
    a class name, so it could not see that shape; it now derives one from the ``EnumInfo`` it
    already has, via :func:`python_class_name` — the same call the emitter renders the class header
    from, so the pair cannot disagree.

    An earlier revision of ``naming.py`` called this *"unreachable through the Pydantic emitter
    (verified)"*. **That was false**, and the reason it was false is kept here rather than deleted
    with the bug: the reasoning was that ``str.upper()`` cannot produce the lowercase letters a
    class name needs — true — and it missed that **NFKC runs after ``.upper()``, in the compiler**.
    389 codepoints the character map keeps are ``upper()``-invariant *and* NFKC-fold to an ASCII
    lowercase letter, covering all 26. If Unicode ever stopped providing them the guard would
    become unreachable, which is worth a future reader knowing.
    """

    def test_the_falsifying_codepoints_exist_and_cover_every_letter(self) -> None:
        # The fact that made the old "unreachable (verified)" claim wrong. If Unicode ever stops
        # providing these, the guard becomes unreachable -- which a future reader should be told.
        folders = fold_map()
        assert set(folders) == set('abcdefghijklmnopqrstuvwxyz'), sorted(
            set('abcdefghijklmnopqrstuvwxyz') - set(folders)
        )
        assert unicodedata.normalize('NFKC', 'ª') == 'a'
        assert 'ª'.upper() == 'ª', 'upper() must NOT undo it -- that is the whole mechanism'

    def test_a_crafted_label_survives_the_class_name_clause(self) -> None:
        """The reproducer, now an assertion that the label survives -- on **every** interpreter.

        🔴 **The disappearance of the ``sys.version_info`` branch is itself the Hard Rule #9
        result.** This test used to need one: py3.10 *kept* the crafted member (under the mangled
        name, with a ``DeprecationWarning``) while py3.11+ *dropped* it, so the emitted module's
        meaning depended on which interpreter imported it -- at ``castiron gen`` exit 0. One
        unbranched assertion is the statement that it no longer does.
        """
        enum_info = EnumInfo(name='order_status', values=[], schema='public')
        class_name = python_class_name(enum_info)
        assert class_name == 'PublicOrderStatusEnum'

        label = crafted_class_private_label(class_name)
        # The premise: the crafted label really does NFKC-fold onto the class-private shape.
        assert unicodedata.normalize('NFKC', label) == f'_{class_name}__X'
        assert label != f'_{class_name}__X', 'the label must be the modifier-letter spelling'

        # 🔴 The predicate FIRES on the untouched label -- this is the clause that did not exist
        # before CI-113, asserted against the name as it arrives rather than as it leaves. The
        # third argument matters: under a different class name this same label is perfectly legal.
        assert _is_enum_reserved_shape(label, class_name), 'the crafted label must reach the fourth clause'
        assert not _is_enum_reserved_shape(label, 'SomethingElse'), 'and only under THIS class name'

        members = python_member_names(EnumInfo(name='order_status', values=[label, 'ok'], schema='public'))

        # ... and the emitted name is repaired out of the shape, into one the compiler leaves alone.
        assert not _is_enum_reserved_shape(members[0].name, class_name)
        assert members[0].name.endswith('__')
        assert unicodedata.normalize('NFKC', members[0].name) == f'_{class_name}__X__'
        assert members[0].note == 'reserved by Enum'

        # ⚠ `_exec_enum_clean`, not `_exec_enum`: silence is part of the claim. A DeprecationWarning
        # here would mean py3.10 had merely mangled the name rather than accepting it.
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members], class_name)[class_name]
        assert [m.value for m in enum_class] == [label, 'ok']  # type: ignore[union-attr]
        for value in (label, 'ok'):
            assert enum_class(value).value == value  # type: ignore[operator]

    def test_ascii_only_labels_cannot_reach_it(self) -> None:
        # The bound on the exposure, asserted rather than asserted-in-prose: the generated class
        # name always ends `Enum`, whose lowercase `num` an ASCII label cannot survive `.upper()`.
        for label in ('_publicorderstatusenum__x', '_PublicOrderStatusEnum__X', '_PUBLICORDERSTATUSENUM__X'):
            (member,) = python_member_names(EnumInfo(name='order_status', values=[label], schema='public'))
            assert unicodedata.normalize('NFKC', member.name) != '_PublicOrderStatusEnum__X'

    def test_the_oracles_alphabet_cannot_spell_a_class_name(self) -> None:
        # Why TestTheOracle does not find this on its own, stated so the two are not confused: its
        # LABEL alphabet holds no modifier letter, so no generated label can fold onto a real class
        # name. That is also why the predicate sweep had to move to SWEEP_CLASS ('A') to see the
        # axis at all -- there the NAMES are generated directly and need no folding.
        assert not (set(LABEL_ALPHABET) & set(fold_map().values()))
        assert not any(name.startswith(f'_{FIXTURE_CLASS}__') for name in _generated_names())
