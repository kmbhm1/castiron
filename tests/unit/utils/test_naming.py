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
    python_member_names,
    singularize,
    to_pascal_case,
)

#: The repository root, so the subprocess determinism probe can import `tests.` and `src/`.
REPO_ROOT = Path(__file__).parents[3]


def _names(*labels: str) -> list[str]:
    """The member names ``python_member_names`` derives for ``labels``, in order."""
    return [m.name for m in python_member_names(EnumInfo(name='t', values=list(labels), schema='public'))]


def _members(*labels: str) -> list[EnumMember]:
    return python_member_names(EnumInfo(name='t', values=list(labels), schema='public'))


def _exec_enum(pairs: list[tuple[str, str]], class_name: str = 'E') -> dict[str, object]:
    """Build and **execute** a real ``Enum`` class body from ``(member_name, value)`` pairs.

    ⚠ Execution, not ``compile()``. All three shapes CI-080's fix round 1 closed are invisible to
    a parse: ``_sunder_`` raises when the class body runs, ``__dunder__`` is silently swallowed by
    ``EnumMeta``, and a mangled name is rewritten by the compiler. A test that only compiles
    reports green on a module that cannot be imported.

    ⚠ **Warnings are captured and asserted on, not suppressed** -- see :func:`_exec_enum_clean`.
    On py3.10 a ``DeprecationWarning`` is the *only* signal that a name was name-mangled, because
    3.10 still creates the member (under ``_E__X``) rather than dropping it. Muting it would
    blind the one interpreter that reports the defect at all.

    🔴 **``class_name`` is a parameter and not a hard-coded ``E``, and that is the point.**
    ``EnumMeta`` consults the **enclosing class name** (``_is_private``), so an oracle that always
    executes under ``class E`` is structurally blind to that axis -- it cannot observe ``CI-113``
    no matter how many labels it is given. An oracle blind to an axis is the same defect this
    whole row has been about, so :class:`TestTheOracle` passes the **real** emitted class name
    from :func:`python_class_name`.
    """
    return _exec_enum_capturing(pairs, class_name)[0]


def _exec_enum_capturing(
    pairs: list[tuple[str, str]], class_name: str = 'E'
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


def _exec_enum_clean(pairs: list[tuple[str, str]], class_name: str = 'E') -> dict[str, object]:
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
            ('order_status', 'public', 'PublicOrderStatusEnum'),
            ('thirdType', 'public', 'PublicThirdTypeEnum'),
            ('FourthType', 'public', 'PublicFourthTypeEnum'),
            ('_first_type', 'public', 'PublicFirstTypeEnum'),
            ('status', 'auth', 'AuthStatusEnum'),
        ],
    )
    def test_class_names(self, name: str, schema: str, expected: str) -> None:
        assert python_class_name(EnumInfo(name=name, values=[], schema=schema)) == expected

    def test_empty_name_edge(self) -> None:
        assert python_class_name(EnumInfo(name='', values=[], schema='public')) == 'PublicEnum'


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
            enum_class = namespace['E']
            assert len(list(enum_class)) == 1, f'{label!r} -> {member.name!r} was dropped by EnumMeta'
            assert enum_class(label).value == label, f'{label!r} does not round-trip'

    def test_the_whole_corpus_forms_one_working_enum(self) -> None:
        # And all at once, which is the shape a user actually gets: every label present, every
        # value exact, nothing collapsed. This is the end-to-end statement of CI94-Q1.
        members = _members(*ENUM_LABEL_CORPUS)
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members])['E']
        assert len(list(enum_class)) == len(ENUM_LABEL_CORPUS), 'a label was dropped or two collapsed'
        for label in ENUM_LABEL_CORPUS:
            assert enum_class(label).value == label

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
        from castiron.utils.naming import _is_enum_reserved_shape

        assert not _is_enum_reserved_shape(name)

    def test_the_predicate_matches_what_the_INTERPRETER_actually_does(self) -> None:
        """⚠ ``CI94-D8`` applied to ``enum``: state the rule in ``src/``, check it against reality.

        ``enum._is_sunder`` / ``_is_dunder`` / ``_is_private`` are **private, unguaranteed and
        demonstrably version-skewed** -- 3.13 dropped a clause from ``_is_private``, and 3.10
        words the sunder error differently. So this does not compare predicate to predicate. It
        **builds a real enum class and looks at what came out**, which is the only authority that
        matters and is immune to all of that.

        A name is "reserved" exactly when the interpreter refuses it, drops it, or gives the
        member a name other than the one written. Runs on all four gate legs.
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
        assert sum(_is_enum_reserved_shape(name) for name in names) > 0, 'the sweep contains no reserved shape'
        for name in names:
            try:
                enum_class = _exec_enum([(name, 'v')])['E']
                members = list(enum_class)
                # ⚠ "Usable" is judged MODULO NFKC, not byte-for-byte. The compiler normalizes
                # every identifier, so `_\uff3f` legitimately becomes the member `__` -- the label
                # is not lost, the value round-trips, and a source-level reference to the name
                # castiron wrote normalizes to the same binding. Demanding byte equality would
                # flag benign normalization as a defect and, worse, would let a genuinely
                # dropped member hide behind the same assertion.
                survived = (
                    len(members) == 1
                    and members[0].name == unicodedata.normalize('NFKC', name)
                    and enum_class('v').value == 'v'
                )
            except ValueError:
                survived = False
            assert _is_enum_reserved_shape(name) is not survived, (
                f'{name!r}: predicate says reserved={_is_enum_reserved_shape(name)}, but the '
                f'interpreter says usable={survived}'
            )

    def test_the_repair_terminates_in_at_most_three_appends(self) -> None:
        """The bound is a proof, and this is the proof executed.

        A name ending in three or more underscores can be none of the three shapes, and each
        iteration adds exactly one -- so three is the ceiling. ``__2`` is the worst case and it
        needs all three: ``__2_`` is still private, ``__2__`` is then dunder, ``__2___`` is clean.
        """
        from castiron.utils.naming import _is_enum_reserved_shape, _repair_enum_shape

        names = _generated_names()
        assert len(names) == 682, len(names)  # see the note in the sibling test: pin the size
        reserved = [n for n in names if _is_enum_reserved_shape(n)]
        assert len(reserved) == 184, len(reserved)

        repaired = [_repair_enum_shape(n) for n in reserved]
        assert all(not _is_enum_reserved_shape(r) for r in repaired)
        assert max(len(r) - len(n) for n, r in zip(reserved, repaired)) == 3
        assert _repair_enum_shape('__2') == '__2___'
        assert _repair_enum_shape('_X_') == '_X__'
        assert _repair_enum_shape('__INIT__') == '__INIT___'

        # ... and every repaired name really is accepted by a real Enum, under the name the
        # compiler gives it. Deduplicated by NFKC KEY, not by raw string: two distinct repaired
        # names can normalize to one identifier (`\uff3f222__` and `_222__` both -> `_222__`),
        # and putting both in one class body is a `TypeError`, not a finding about the repair.
        by_key = {unicodedata.normalize('NFKC', name): name for name in sorted(repaired)}
        enum_class = _exec_enum_clean([(name, f'v{i}') for i, name in enumerate(by_key.values())])['E']
        assert [m.name for m in enum_class] == sorted(by_key)

    def test_the_collision_suffix_cannot_smuggle_a_reserved_shape_back_in(self) -> None:
        # 🔴 THE regression this round exists for. `''`, `' '`, `'\t'` and `'\n'` all sanitize to
        # `_`, so the collision rule produced `__2`, `__3`, `__4` -- which Python NAME-MANGLES.
        # On 3.11+ those labels vanished; on 3.10 they became `_E__2`, i.e. the member name
        # depended on the enum's class name. The suffix is therefore repaired too.
        from castiron.utils.naming import _is_enum_reserved_shape

        members = _members('', ' ', '\t', '\n', '\x00', '-', '---', '   ')
        assert all(not _is_enum_reserved_shape(m.name) for m in members), [m.name for m in members]
        enum_class = _exec_enum_clean([(m.name, m.label) for m in members])['E']
        assert len(list(enum_class)) == 8, 'a label was mangled away'
        for member in members:
            assert enum_class(member.label).value == member.label


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

        members = python_member_names(EnumInfo(name='mood', values=_generated_labels(), schema='public'))
        offenders = [(m.label, m.name) for m in members if _is_enum_reserved_shape(m.name)]
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
class TestCi113TheClassNameAxisIsOpen:
    """🔴 ``CI-113``, pinned as **present** — not asserted as correct, and not fixed here.

    ``EnumMeta`` consults the **enclosing class name**: ``_is_private(cls_name, name)`` also
    swallows a member already spelled ``_<ClassName>__x``. :func:`python_member_names` never
    receives the class name, so it cannot see that shape.

    An earlier revision of ``naming.py`` claimed this was *"unreachable through the Pydantic
    emitter (verified)"*. **That was false.** The reasoning was that ``str.upper()`` cannot
    produce the lowercase letters a class name needs — true — and it missed that **NFKC runs
    after ``.upper()``, in the compiler**. Measured below: 389 codepoints the character map keeps
    are ``upper()``-invariant *and* NFKC-fold to an ASCII lowercase letter, covering all 26.

    **Left open deliberately.** Closing it means threading the class name into
    :func:`python_member_names` — a signature change on the last row before an immutable publish,
    for an input that requires deliberately-crafted modifier-letter Unicode. This follows the
    repo's established pattern for known-wrong-but-visible: ``CI-085`` ships with
    ``compiles=False``, ``CI-100`` ships pinned-as-present. **These assertions are written so that
    FIXING CI-113 turns them red**, which is the signal to delete them.
    """

    @staticmethod
    def _fold_map() -> dict[str, str]:
        """ASCII lowercase -> a codepoint the map keeps that ``.upper()`` ignores and NFKC folds."""
        folders: dict[str, str] = {}
        for cp in range(0x110000):
            char = chr(cp)
            if ('_' + char).isidentifier() and char.upper() == char:
                folded = unicodedata.normalize('NFKC', char)
                if len(folded) == 1 and 'a' <= folded <= 'z':
                    folders.setdefault(folded, char)
        return folders

    def test_the_falsifying_codepoints_exist_and_cover_every_letter(self) -> None:
        # The fact that made the old "unreachable (verified)" claim wrong. If Unicode ever stops
        # providing these, CI-113 closes itself and this class should go.
        folders = self._fold_map()
        assert set(folders) == set('abcdefghijklmnopqrstuvwxyz'), sorted(
            set('abcdefghijklmnopqrstuvwxyz') - set(folders)
        )
        assert unicodedata.normalize('NFKC', 'ª') == 'a'
        assert 'ª'.upper() == 'ª', 'upper() must NOT undo it -- that is the whole mechanism'

    def test_a_crafted_label_is_still_swallowed_by_the_class_name_clause(self) -> None:
        enum_info = EnumInfo(name='order_status', values=[], schema='public')
        class_name = python_class_name(enum_info)
        assert class_name == 'PublicOrderStatusEnum'

        folders = self._fold_map()
        label = ''.join(folders.get(ch, ch) for ch in f'_{class_name}__X')
        members = python_member_names(EnumInfo(name='order_status', values=[label, 'ok'], schema='public'))

        # castiron considers the name clean -- that is the defect, stated as the defect.
        assert not _is_enum_reserved_shape(members[0].name)
        assert unicodedata.normalize('NFKC', members[0].name) == f'_{class_name}__X'

        enum_class = _exec_enum([(m.name, m.label) for m in members], class_name)[class_name]
        surviving = [m.value for m in enum_class]
        if sys.version_info >= (3, 11):
            assert surviving == ['ok'], (
                'CI-113 appears FIXED: the crafted label survived. Delete this class, delete the '
                'CI-113 caveats in naming.py, and close the WORKPLAN row.'
            )
        else:
            # py3.10 keeps it under the mangled name instead of dropping it -- the same defect
            # wearing the interpreter-dependent costume that makes it a Hard Rule #9 issue too.
            assert surviving == [label, 'ok']
            assert [m.name for m in enum_class] == [f'_{class_name}__X', 'OK']

    def test_ascii_only_labels_cannot_reach_it(self) -> None:
        # The bound on the exposure, asserted rather than asserted-in-prose: the generated class
        # name always ends `Enum`, whose lowercase `num` an ASCII label cannot survive `.upper()`.
        for label in ('_publicorderstatusenum__x', '_PublicOrderStatusEnum__X', '_PUBLICORDERSTATUSENUM__X'):
            (member,) = python_member_names(EnumInfo(name='order_status', values=[label], schema='public'))
            assert unicodedata.normalize('NFKC', member.name) != '_PublicOrderStatusEnum__X'

    def test_the_oracles_alphabet_cannot_spell_a_class_name(self) -> None:
        # Why TestTheOracle does not find this on its own, stated so the two are not confused.
        assert not (set(LABEL_ALPHABET) & set(self._fold_map().values()))
