"""One test per (known defect, golden) pair, asserting the **wrong** output is present.

This module is the reason the corpus is not an endorsement of current behaviour.

A golden is a several-thousand-line assertion that today's output is correct. Open WORKPLAN rows
produce bytes that reach a committed golden here. Without this module they would be
cemented as the contract — and worse, on the day one is fixed the golden would go red and the
cheapest response would be "regenerate it", silently re-endorsing whatever replaced it without
anyone checking either version.

So each defect's evidence is asserted to be **present**, and each failure message tells a future
reader the two things they need: what the correct output looks like, and exactly what to do if
the row was fixed. A witness going red is usually **good news**; the message has to say so, or a
reader under time pressure will "fix" the test.

Where a **counter-witness** exists it is asserted alongside. A witness without one is weak: it
cannot distinguish "the defect is present" from "the emitter does that to everything."
"""

import json
from typing import Any

import pytest

from castiron.ir import Schema
from tests.unit.corpus.cases import (
    KNOWN_DEFECTS,
    OPENAPI_FIXTURE,
    SYNTHETIC_TORTURE,
    TESTBED_PUBLIC,
    case_by_id,
)
from tests.unit.corpus.pipeline import module_compiles


def witness_failed(row_id: str, golden: str, witness: str, *, fixed_action: str) -> str:
    """Build the standard "the witness is gone" failure message.

    Args:
        row_id: The WORKPLAN row id.
        golden: The golden the witness lives in.
        witness: What was expected to be present, and why it is wrong.
        fixed_action: What to do if the row really was fixed.

    Returns:
        The rendered message.
    """
    defect = KNOWN_DEFECTS[row_id]
    return (
        f"{row_id}'s witness is GONE from golden {golden!r}.\n\n"
        f'  Defect:  {defect.summary}\n'
        f'  Witness: {witness}\n'
        f'  Correct: {defect.why_it_is_wrong}\n\n'
        f'If {row_id} was FIXED:  {fixed_action}\n'
        f'If {row_id} was NOT fixed: this golden has drifted for some other reason, which is a\n'
        f'                        Hard Rule #9 (byte-stable output) violation -- investigate\n'
        f'                        before regenerating anything.'
    )


#: The standard remediation sentence, so every witness says the same thing the same way.
FIX_ACTION = (
    'regenerate the goldens, delete this row from KNOWN_DEFECTS in\n'
    "                        tests/unit/corpus/cases.py, drop it from every case's `defects`,\n"
    '                        flip the case status to "asserted" if no defect remains, delete\n'
    '                        this witness test, and say so in the PR body.'
)


def _ir(case_id: str, corpus_irs: dict[tuple[str, Any], Schema]) -> dict[str, Any]:
    """Return the IR this branch's code PRODUCES for a case.

    ⚠ Deliberately **not** the committed ``ir.json``. A witness that reads the golden file is a
    witness about bytes somebody already accepted: on the day a defect is fixed it would stay
    green until the golden was regenerated, so the first thing a developer saw would be a golden
    mismatch, and the cheapest response to that is "regenerate" -- which is precisely the move
    this whole mechanism exists to prevent. Reading the live IR makes the witness fire on the
    behaviour change itself, before anything is regenerated. (Measured: with the golden-file
    version, a patched ``classify_table_type`` left all six CI-075 witnesses GREEN. Those six are
    now retired -- CI-075 is fixed -- but the design point they proved is why the successor guard
    :class:`TestEveryViewIsClassifiedAsAView` reads the live IR too.)

    Args:
        case_id: The corpus case.
        corpus_irs: The session-scoped IR fixture.

    Returns:
        ``Schema.as_dict()`` for that case.
    """
    case = case_by_id(case_id)
    return corpus_irs[(case.family.family_id, case.source_options)].as_dict()


def _module(case_id: str, case_modules: dict[str, str]) -> str:
    """Return the module this branch's code PRODUCES for a case (not the committed golden).

    Same reasoning as :func:`_ir`.

    Args:
        case_id: The corpus case.
        case_modules: The session-scoped emitted-module fixture.

    Returns:
        The emitted module text.
    """
    return case_modules[case_id]


def _table(ir: dict[str, Any], name: str) -> dict[str, Any]:
    """Return one table from a decoded IR golden."""
    for table in ir['tables']:
        if table['name'] == name:
            found: dict[str, Any] = table
            return found
    raise AssertionError(f'table {name!r} is absent from the IR golden; the corpus input changed')


def _class_body(module: str, class_name: str) -> str:
    """Return the source of one top-level class, up to the next top-level statement."""
    marker = f'\nclass {class_name}('
    start = module.index(marker) + 1
    rest = module[start + len(marker) :]
    end = rest.find('\nclass ')
    return module[start : start + len(marker) + (len(rest) if end == -1 else end)]


# ---------------------------------------------------------------------------
# CI-075 -- FIXED. What was the corpus's most valuable witness is now its regression guard.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEveryViewIsClassifiedAsAView:
    """The CI-075 witness, read the right way round.

    ``TestCi075ViewMisclassification`` used to assert that three of the capture's five views
    carried a ``# Primary Keys`` block they could not have, with the other two as a counter-witness
    proving the emitter does not put that block on everything. CI-075 is fixed
    (``classify_table_type`` no longer reads write verbs as evidence of relation kind), so the
    witness is retired -- but **deleting its counter-witness would throw away the evidence**. It is
    the same measurement; it now says all **five**.

    ⚠ Class names are spelled out per case rather than derived: ``maximal`` sets ``singular_names``,
    so ``active_customers`` emits as ``ActiveCustomer`` there. Deriving them would re-implement the
    emitter's naming rule inside its own test, which is how a test stops being independent evidence.
    """

    #: All five VIEWs in the ``testbed-public`` capture, in both emitted configurations.
    VIEWS = [
        ('testbed-public-default', 'ActiveCustomersBaseSchema'),
        ('testbed-public-default', 'LedgerSummaryBaseSchema'),
        ('testbed-public-default', 'WritableCustomerViewBaseSchema'),
        ('testbed-public-default', 'OrderReportBaseSchema'),
        ('testbed-public-default', 'MvCustomerSpendBaseSchema'),
        ('testbed-public-maximal', 'ActiveCustomerBaseSchema'),
        ('testbed-public-maximal', 'LedgerSummaryBaseSchema'),
        ('testbed-public-maximal', 'WritableCustomerViewBaseSchema'),
        ('testbed-public-maximal', 'OrderReportBaseSchema'),
        ('testbed-public-maximal', 'MvCustomerSpendBaseSchema'),
    ]

    @pytest.mark.parametrize(('case_id', 'emitted_class'), VIEWS)
    def test_a_view_has_no_primary_key_block(
        self, case_id: str, emitted_class: str, case_modules: dict[str, str]
    ) -> None:
        body = _class_body(_module(case_id, case_modules), emitted_class)
        assert '# Primary Keys' not in body, (
            f'{emitted_class} is a VIEW and has grown a "# Primary Keys" block. A view has no '
            f'primary key: CI5-D14a downgrades its <pk/> marker to UNIQUE. Three of these five '
            f'looked exactly like this before CI-075 was fixed -- this is that regression.'
        )

    def test_a_base_table_still_has_one(self, case_modules: dict[str, str]) -> None:
        # THE COUNTER-WITNESS, and it matters as much here as it did before: without it, the test
        # above cannot tell "views are classified correctly" from "the emitter stopped emitting
        # that block at all", which would pass just as green.
        for emitted_class in ('CustomersBaseSchema', 'OrdersBaseSchema', 'ProductsBaseSchema'):
            body = _class_body(_module('testbed-public-default', case_modules), emitted_class)
            assert '# Primary Keys' in body, f'{emitted_class} is a BASE TABLE and must keep its primary key'

    def test_the_ir_records_every_view_as_a_view(self, corpus_irs: dict[tuple[str, Any], Schema]) -> None:
        ir = _ir('testbed-public-default', corpus_irs)
        views = ('active_customers', 'ledger_summary', 'writable_customer_view', 'order_report', 'mv_customer_spend')
        for name in views:
            assert _table(ir, name)['table_type'] == 'VIEW', name
        for name in ('customers', 'orders', 'products', 'rls_locked_notes', 'partially_visible'):
            assert _table(ir, name)['table_type'] == 'BASE TABLE', name

    def test_the_downgrade_now_fires_for_the_three_that_used_to_miss(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        # The observable payoff, stated as the thing a user gets rather than as a table_type
        # string: CI5-D14a's <pk/> -> UNIQUE downgrade could never fire on these three, so they
        # carried a PRIMARY KEY constraint a view cannot have.
        ir = _ir('testbed-public-default', corpus_irs)
        for name in ('active_customers', 'ledger_summary', 'writable_customer_view'):
            table = _table(ir, name)
            assert [c['type'] for c in table['constraints'] if c['type'] == 'PRIMARY KEY'] == [], name
            assert [c['type'] for c in table['constraints'] if c['type'] == 'UNIQUE'] == ['UNIQUE'], name
            assert [c['name'] for c in table['columns'] if c['primary']] == [], name

    def test_the_three_reclassified_views_are_shaped_exactly_like_the_two_that_were_right(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        """⚠ The check that caught a gap in this fix's own golden-delta prediction.

        The predicted IR delta was derived from ``parse.py``'s constraint downgrade alone and
        under-counted, because the DOWNSTREAM step
        ``ir.build.update_columns_with_constraints`` also reacts to a UNIQUE constraint: it sets
        ``is_unique`` **and** ``unique_partners``. So each reclassified view gained
        ``"unique_partners": ["id"]`` as well.

        That is not a new behaviour and not a defect this row introduced -- it is exactly the
        shape ``order_report`` and ``mv_customer_spend`` (the two views that classified correctly
        all along) already carried on ``main``. Asserting the five are now identical in shape is
        what turns "I can explain the extra lines" into evidence.
        """
        ir = _ir('testbed-public-default', corpus_irs)
        shapes = {}
        for name, key in (
            ('active_customers', 'id'),
            ('ledger_summary', 'id'),
            ('writable_customer_view', 'id'),
            ('order_report', 'order_id'),
            ('mv_customer_spend', 'customer_id'),
        ):
            column = next(c for c in _table(ir, name)['columns'] if c['name'] == key)
            shapes[name] = (column['primary'], column['is_unique'], column['unique_partners'] == [key])
        assert set(shapes.values()) == {(False, True, True)}, shapes

    def test_the_one_accepted_residual_is_pinned_as_expected_not_forgotten(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        """⚠ ``all_nullable_readonly`` is a BASE TABLE that castiron now reports as a VIEW.

        This is the **known, ruled, accepted** cost of `CI94-Q2` -- 25 of 26 relations correct,
        up from 23. It is pinned here so it is a recorded decision rather than an unnoticed miss.

        It is provably inert: a base table lands in this cell only if PostgREST reports **no NOT
        NULL column**, and a Postgres PRIMARY KEY column is NOT NULL -- so it has no primary key
        for the VIEW reading to empty. Asserted below rather than argued: the relation carries no
        ``<pk/>`` marker, so it has no constraints either way and its emitted module is unchanged.
        """
        table = _table(_ir('testbed-public-default', corpus_irs), 'all_nullable_readonly')
        assert table['table_type'] == 'VIEW'
        assert table['constraints'] == [], 'the residual is only inert while there is no key to lose'
        assert [c['name'] for c in table['columns'] if c['primary']] == []


# ---------------------------------------------------------------------------
# CI-084 — reaches the IR golden only; the emitted module shows nothing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi084DanglingForeignKey:
    def test_the_column_is_flagged_a_foreign_key_with_nothing_to_point_at(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        ir = _ir('testbed-public-default', corpus_irs)
        table = _table(ir, 'ledger_refs')
        ledger_id = next(column for column in table['columns'] if column['name'] == 'ledger_id')
        assert (ledger_id['is_foreign_key'], table['foreign_keys']) == (True, []), witness_failed(
            'CI-084',
            'testbed-public/ir.json',
            'ledger_refs.ledger_id has is_foreign_key=True while ledger_refs.foreign_keys is [] '
            '-- a consumer reads the flag and then finds no relationship.',
            fixed_action=(
                'this needs a CAPTAIN CALL on the semantics before it is a fix at all\n'
                '                        (drop the flag / keep it with an explicit dangling marker /\n'
                '                        warn). Once ruled: ' + FIX_ACTION
            ),
        )

    def test_the_constraint_survives_naming_a_table_that_is_not_in_the_schema(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        ir = _ir('testbed-public-default', corpus_irs)
        definitions = [constraint['constraint_definition'] for constraint in _table(ir, 'ledger_refs')['constraints']]
        assert 'FOREIGN KEY (ledger_id) REFERENCES private_ledger(id)' in definitions
        # The other half of the defect: the referenced table is not in the schema at all, because
        # the API role cannot see it. That is what makes the edge dangling rather than merely
        # unrecorded.
        assert not any(table['name'] == 'private_ledger' for table in ir['tables'])


# ---------------------------------------------------------------------------
# CI-090 — a synthesized name that the testbed's own SQL proves wrong.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi090SynthesizedConstraintName:
    """The sharpest witness in the corpus, because an oracle exists.

    The OpenAPI document carries no constraint name, so castiron fabricates Postgres's *default*
    spelling. The testbed's schema names this constraint ``order_lines_order_fk`` in SQL — so the
    synthesized value is demonstrably, not hypothetically, wrong. It is harmless for the Pydantic
    emitter, which never renders it, and load-bearing for the SQLAlchemy/DDL emitters that come
    next: a ``name=`` that does not match the database is a ``castiron check`` false positive,
    i.e. a broken build for a user who changed nothing.
    """

    def test_the_fk_name_is_the_postgres_default_and_not_the_real_one(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        ir = _ir('testbed-public-default', corpus_irs)
        names = [fk['constraint_name'] for fk in _table(ir, 'order_lines')['foreign_keys']]
        assert names == ['order_lines_order_id_fkey'], witness_failed(
            'CI-090',
            'testbed-public/ir.json',
            'order_lines.order_id\'s FK is named "order_lines_order_id_fkey" -- the pg DEFAULT '
            'spelling castiron synthesizes. The testbed schema names it "order_lines_order_fk" '
            '(ADD CONSTRAINT order_lines_order_fk FOREIGN KEY (order_id) ...), so the value in '
            'this golden is provably fabricated rather than read.',
            fixed_action=(
                'the fix lands with the live-DB source (CI-010/CI-011), which\n'
                '                        can read pg_constraint.conname. Then: ' + FIX_ACTION
            ),
        )

    def test_every_forward_edge_name_follows_the_default_template(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        # CI6-Q7: enumerate, do not sample. If ANY foreign key in the real capture carried a name
        # that is not the synthesized template, the synthesis would be partial and CI-090's
        # description would be wrong.
        #
        # Scoped to a table's own FOREIGN KEY *constraints*, which are the forward edges the
        # parser records. `TableInfo.foreign_keys` also holds REVERSE edges, synthesized by
        # `analyze_table_relationships` and deliberately carrying the owning side's constraint
        # name (so `customers.id` shows `customer_tags_customer_id_fkey`). That is intended
        # product behaviour, not a second defect -- asserting the template over it would be
        # asserting something castiron never claimed.
        ir = _ir('testbed-public-default', corpus_irs)
        checked = 0
        for table in ir['tables']:
            for constraint in table['constraints']:
                if constraint['type'] != 'FOREIGN KEY':
                    continue
                checked += 1
                expected = f'{table["name"]}_{constraint["columns"][0]}_fkey'
                assert constraint['constraint_name'] == expected, (
                    f'{table["name"]}.{constraint["columns"][0]} carries constraint name '
                    f'{constraint["constraint_name"]!r}, not the synthesized {expected!r}. If '
                    f'castiron has learned to read real constraint names, CI-090 is fixed -- see '
                    f'KNOWN_DEFECTS in tests/unit/corpus/cases.py.'
                )
        # Not vacuous: a document with no FK constraint would pass the loop above trivially, so
        # the count is what proves the enumeration ran. Ten forward edges across nine relations
        # (customer_tags carries two), one of which -- order_report -- is a VIEW, where SQL has no
        # FK constraint to name at all, so that spelling is fabricated twice over.
        assert checked == 10, f'expected 10 forward FK edges in the public capture, enumerated {checked}'


# ---------------------------------------------------------------------------
# CI-076 — the hand-authored fixture's fictional shape, asserted to be fictional.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi076FictionalForeignKeyTarget:
    def test_the_fixture_points_a_foreign_key_at_a_view(self, corpus_irs: dict[tuple[str, Any], Schema]) -> None:
        ir = _ir('openapi-fixture-default', corpus_irs)
        targets = [fk['foreign_table_name'] for fk in _table(ir, 'restricted_table')['foreign_keys']]
        assert 'active_users_view' in targets, witness_failed(
            'CI-076',
            'openapi-fixture/ir.json',
            "restricted_table.owner_id points at 'active_users_view' -- a VIEW as an FK TARGET, "
            'which neither PostgREST nor SQL can produce.',
            fixed_action=(
                'the fixture was corrected to a shape a real source emits.\n'
                "                        Confirm tests/unit/sources/openapi/conftest.py's\n"
                '                        provenance docstring was updated too, then: ' + FIX_ACTION
            ),
        )

    def test_the_marker_is_present_in_the_committed_fixture_document(self) -> None:
        # Asserted against the raw document, not just the IR: this is the fixture's own bytes
        # carrying a shape PostgREST cannot emit (SEED-F1, CONFIRMED on v14.14 and v12.2.3).
        document = OPENAPI_FIXTURE.input_path.read_text(encoding='utf-8')
        assert "<fk table='active_users_view' column='id'/>" in document

    def test_no_captured_input_contains_the_fictional_shape(self) -> None:
        # The corpus's evidence about PostgREST comes from the captures, and this is what proves
        # the captures are clean of the shape the fixture invented.
        for family in (TESTBED_PUBLIC,):
            document = json.loads(family.input_path.read_text(encoding='utf-8'))
            views = {'active_customers', 'ledger_summary', 'order_report', 'mv_customer_spend'}
            text = json.dumps(document)
            for view in views:
                assert f"<fk table='{view}'" not in text, (
                    f'{family.family_id}: a captured document names the VIEW {view!r} as an FK '
                    f'target. SEED-F1 measured that PostgREST never does this -- if it now does, '
                    f'CI-076 needs reopening with this document as the counter-example.'
                )


# ---------------------------------------------------------------------------
# CI-085 — the column-name half — is FIXED. The witness is retired and INVERTED into the
# regression guard below, following `TestEveryViewIsClassifiedAsAView`'s pattern: the
# counter-witness is KEPT, because deleting it would throw away the evidence.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEveryHostileColumnIsRepairedAndAliased:
    """The CI-085 witness, read the right way round.

    ``TestCi085ColumnIdentifiersAreNotSanitized`` used to assert that ``2fast``, ``space name``
    and ``kebab-case`` reached the emitted module **verbatim**, and that the module therefore did
    not parse — at ``castiron gen`` exit 0. CI-085 is fixed
    (:func:`~castiron.ir.build.column_identifiers` repairs the identifier and
    :func:`~castiron.ir.build.get_alias` preserves the wire name), so the witness is retired.

    ⚠ **The counter-witness is kept**, exactly as it was for CI-075: without
    :meth:`test_the_safe_column_alongside_them_is_emitted_normally`, this class cannot tell "the
    hostile names were repaired" from "the emitter renames everything".
    """

    #: Legal quoted Postgres identifiers that are illegal — or unusable — Python attribute names,
    #: paired with the identifier castiron now emits and the alias that carries the wire name.
    #: ⚠ Spelled out rather than derived: deriving them would re-implement the repair rule inside
    #: its own test, which is how a test stops being independent evidence.
    REPAIRED_COLUMNS = (
        ('2fast', 'field_2fast', 'str = Field(alias="2fast")'),
        ('space name', 'space_name', 'str | None = Field(default=None, alias="space name")'),
        ('kebab-case', 'kebab_case', 'str | None = Field(default=None, alias="kebab-case")'),
    )

    @pytest.mark.parametrize(('wire', 'emitted', 'declaration'), REPAIRED_COLUMNS)
    def test_a_hostile_column_is_emitted_under_its_repaired_name(
        self, wire: str, emitted: str, declaration: str, case_modules: dict[str, str]
    ) -> None:
        module = _module('synthetic-torture-default', case_modules)
        assert f'    {wire}: ' not in module, (
            f'the column {wire!r} is emitted verbatim again. That is CI-085 regressing: the '
            f'module will not import, and castiron will still exit 0 while writing it.'
        )
        assert f'    {emitted}: {declaration}' in module, (
            f'{wire!r} should emit as `{emitted}: {declaration}`. The repaired identifier is what '
            f'makes the module importable; the alias is what keeps the wire name recoverable.'
        )

    def test_the_module_parses_and_the_repair_is_what_makes_it_parse(self, case_modules: dict[str, str]) -> None:
        module = _module('synthetic-torture-default', case_modules)
        assert module_compiles(module), (
            'the identifier-hostile golden no longer parses. This is the CI-085 regression guard: '
            'castiron must never again write a module it cannot import while exiting 0.'
        )

    def test_the_safe_column_alongside_them_is_emitted_normally(self, case_modules: dict[str, str]) -> None:
        # THE COUNTER-WITNESS, kept from the retired witness class and load-bearing in both
        # directions: before the fix it proved the defect was about the identifier and not about
        # the table; now it proves the repair is targeted and does not rename what already works.
        module = _module('synthetic-torture-default', case_modules)
        assert '    ok_column: str | None = Field(default=None)' in module
        assert 'alias="ok_column"' not in module, 'a valid column name must not acquire an alias'

    def test_ci_080_and_ci_085_were_distinct_defects_and_were_fixed_independently(
        self, case_modules: dict[str, str]
    ) -> None:
        # CI-085's WORKPLAN row asked whether it is the same defect as CI-080. It is not (CI7-Q4,
        # re-confirmed as CI94-D7). This document was the *demonstration* rather than the
        # argument: for one PR CI-080 was fixed here and CI-085 was not, in the same module, and
        # the module still did not parse. That is impossible for one defect.
        #
        # The independence claim is preserved now that both are fixed: the two blocks are still
        # disjoint, and each is repaired by its OWN rule -- the enum path prefixes `_` where the
        # column path prefixes `field_`, which is the divergence CI85-D2 requires (a leading
        # underscore is fine on an Enum member and raises NameError on a pydantic field).
        module = _module('synthetic-torture-default', case_modules)
        enum_block = module[module.index('class PublicTaskStateEnum') : module.index('# CUSTOM CLASSES')]
        columns_block = _class_body(module, 'HostileColumnsBaseSchema')

        # They are visible independently: neither block contains the other's evidence.
        assert '2fast' not in enum_block, 'the CI-080 evidence must be independent of the CI-085 one'
        assert 'TaskState' not in columns_block, 'the CI-085 witness must be independent of the CI-080 one'

        # CI-080's repair: every enum member name is a valid identifier, and `2nd pass` took the
        # ENUM path's leading-underscore prefix.
        member_names = [line.split(' = ')[0].strip() for line in enum_block.splitlines() if ' = "' in line]
        assert member_names == ['IN_PROGRESS', 'DONE', 'N_A', '_2ND_PASS']
        assert all(name.isidentifier() for name in member_names)

        # CI-085's repair: the same leading-digit hazard on a COLUMN took `field_`, not `_`.
        assert '    field_2fast: ' in columns_block
        assert '    _2fast: ' not in columns_block, (
            'a pydantic field must not start with an underscore -- it compiles and raises '
            'NameError at import, which is exactly why the two paths diverge here.'
        )

        # ... and now the module parses, because both call sites are repaired.
        assert module_compiles(module)


# ---------------------------------------------------------------------------
# Two fidelity WINS the same capture proves. Asserted, never characterized.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFidelityWins:
    """Not every notable shape in the corpus is a defect. These two are castiron working."""

    def test_the_same_named_cross_schema_enums_stay_distinct(
        self, corpus_irs: dict[tuple[str, Any], Schema], case_modules: dict[str, str]
    ) -> None:
        # The shape that once handed one enum's members to the other class: `audit.status` and
        # `public.status` share a NAME and share nothing else.
        ir = _ir('testbed-public-default', corpus_irs)
        by_key = {(enum['schema'], enum['name']): enum['values'] for enum in ir['enums']}
        assert by_key[('audit', 'status')] == ['ok', 'warn', 'error']
        assert by_key[('public', 'status')] == ['active', 'inactive', 'archived']

        module = _module('testbed-public-default', case_modules)
        assert 'class AuditStatusEnum' in module and 'class PublicStatusEnum' in module
        audit_block = _class_body(module, 'AuditStatusEnum')
        assert 'active' not in audit_block, "AuditStatusEnum has been handed PublicStatusEnum's members"

    def test_the_correctly_classified_views_prove_the_ci5_d14a_downgrade_works(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        # The downgrade IS implemented and IS reachable -- CI-075 is a classification bug, not a
        # missing feature. Recording that distinction is what keeps the fix scoped.
        ir = _ir('testbed-public-default', corpus_irs)
        order_report = _table(ir, 'order_report')
        assert order_report['table_type'] == 'VIEW'
        unique = [c for c in order_report['constraints'] if c['type'] == 'UNIQUE']
        assert unique, 'order_report is a VIEW whose <pk/> marker should survive as a UNIQUE constraint'
        assert not [c for c in order_report['constraints'] if c['type'] == 'PRIMARY KEY']


@pytest.mark.unit
class TestSyntheticTortureCoversWhatNothingElseDoes:
    def test_no_captured_input_contains_an_identifier_hostile_shape(self) -> None:
        # CI6-Q7, stated as an assertion rather than a note: this is WHY the synthetic input
        # exists. If a future capture grows a hostile shape, this goes red and the synthetic
        # input may be droppable -- a deliberate decision, not a silent one.
        for family in (TESTBED_PUBLIC,):
            document = json.loads(family.input_path.read_text(encoding='utf-8'))
            for name, definition in document['definitions'].items():
                for column in definition.get('properties', {}):
                    assert column.isidentifier(), f'{family.family_id}: {name}.{column} is identifier-hostile'
            for enum_values in _iter_enums(document):
                for label in enum_values:
                    assert label.replace('_', 'x').isalnum(), f'{family.family_id}: enum label {label!r} is hostile'

    def test_the_synthetic_family_now_carries_no_defect_at_all(self) -> None:
        # ⚠ **Rewritten, not merely left in place.** This used to loop `for row_id in ('CI-085',)`
        # and assert the synthetic family was that row's only carrier. CI-085 is now fixed, so the
        # tuple would be **empty** and the test would pass **vacuously** -- a guard that cannot
        # fail, printing green forever, which is the `CI-083` shape this repository has been bitten
        # by twice. Its successor asserts the stronger and now-true thing, and it CAN fail: the day
        # someone adds a defect to this family, or drops the family, it goes red.
        from tests.unit.corpus.cases import CASES, KNOWN_DEFECTS

        synthetic = [case for case in CASES if case.family.family_id == SYNTHETIC_TORTURE.family_id]
        assert synthetic, 'the synthetic-torture family has vanished from the case table'
        for case in synthetic:
            assert case.defects == (), (
                f'{case.case_id} has grown defect(s) {case.defects}. Both rows this input was '
                f'built to carry (CI-080, CI-085) are FIXED; it is now a regression guard, not '
                f'evidence of anything wrong.'
            )
            assert case.status == 'asserted'
            assert case.compiles is True

        # And the retired rows are gone from the registry, in both directions.
        assert 'CI-085' not in KNOWN_DEFECTS
        assert 'CI-080' not in KNOWN_DEFECTS


def _iter_enums(document: dict[str, Any]) -> list[list[str]]:
    """Return every ``enum`` label list in a document's column definitions."""
    found: list[list[str]] = []
    for definition in document['definitions'].values():
        for prop in definition.get('properties', {}).values():
            if isinstance(prop, dict) and isinstance(prop.get('enum'), list):
                found.append(prop['enum'])
    return found
