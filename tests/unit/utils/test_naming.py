import keyword
import unicodedata

import pytest

from castiron.ir import EnumInfo
from castiron.utils.naming import (
    EnumMember,
    pluralize,
    python_class_name,
    python_member_names,
    singularize,
    to_pascal_case,
)


def _names(*labels: str) -> list[str]:
    """The member names ``python_member_names`` derives for ``labels``, in order."""
    return [m.name for m in python_member_names(EnumInfo(name='t', values=list(labels), schema='public'))]


def _members(*labels: str) -> list[EnumMember]:
    return python_member_names(EnumInfo(name='t', values=list(labels), schema='public'))


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
            ('class', 'CLASS_'),  # step 5: a keyword
            ('import', 'IMPORT_'),  # step 5: a keyword
            ('None', 'NONE'),  # step 5: `none` is not reserved; `None` is
            ('sum', 'SUM_'),  # step 5: a builtin (and see the CI-100 note below)
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
    def test_the_exemption_list_is_carried_verbatim_bug_and_all(self, label: str) -> None:
        # ⚠ NOT an endorsement. `column_name_reserved_exceptions` is an EXEMPTION list -- names
        # that need NOT be renamed -- and the enum path applies it with `or`, i.e. as an ADDITION
        # list, so `id` gets suffixed and annotated "reserved keyword" when it is the opposite.
        # That is filed as CI-100 (CI94-Q4) and is deliberately out of scope: CI-080 rewrote the
        # surrounding lines and tidying it in passing is the CI-074 trap.
        #
        # This test pins the bug as PRESENT so CI-100 has a red test to turn green -- it does not
        # pin it as correct, and the assertion below is written so that fixing CI-100 fails it.
        member = _members(label)[0]
        assert member.name == f'{label.upper()}_'
        assert member.note == 'reserved keyword', 'CI-100 (still open) makes this annotation false'


@pytest.mark.unit
class TestEveryMemberNameIsAnIdentifier:
    """The property, enumerated rather than sampled (`CI-072`)."""

    #: Shapes a Postgres enum label can legally carry. Postgres allows any text up to
    #: ``NAMEDATALEN``; PostgREST carries it verbatim into ``properties.<c>.enum``.
    LABELS = [
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
    ]

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
