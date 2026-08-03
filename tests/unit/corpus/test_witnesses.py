"""One test per (known defect, golden) pair, asserting the **wrong** output is present.

This module is the reason the corpus is not an endorsement of current behaviour.

A golden is a several-thousand-line assertion that today's output is correct. Six open WORKPLAN
rows produce bytes that reach a committed golden here. Without this module they would be
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


def _ir(case_id: str) -> dict[str, Any]:
    """Read a case's committed IR golden."""
    decoded: dict[str, Any] = json.loads(case_by_id(case_id).golden_ir.read_text(encoding='utf-8'))
    return decoded


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
# CI-075 — the corpus's most valuable witness, because it comes with a clean pair.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi075ViewMisclassification:
    """One document, one emitter, one run: the only difference between the two groups is the bug.

    ``classify_table_type()`` infers a relation's kind from which HTTP verbs PostgREST exposes.
    Measured against real PostgREST (CI-008): the verbs follow Postgres **auto-updatability**,
    not write privileges, so an auto-updatable view is writable through the API and classifies as
    a BASE TABLE. CI5-D14a's "a view's ``<pk/>`` becomes UNIQUE" downgrade then never fires and
    the view keeps a primary key it cannot have.
    """

    #: ``(case_id, emitted class)`` for each VIEW that misclassifies as BASE TABLE and therefore
    #: wrongly keeps a primary key. Spelled out per case rather than derived: ``maximal`` sets
    #: ``singular_names``, so ``active_customers`` emits as ``ActiveCustomer`` there and
    #: ``ActiveCustomers`` under the defaults. Deriving the name would re-implement the emitter's
    #: naming rule inside its own test, which is how a test stops being independent evidence.
    MISCLASSIFIED = [
        ('testbed-public-default', 'ActiveCustomersBaseSchema'),
        ('testbed-public-default', 'LedgerSummaryBaseSchema'),
        ('testbed-public-default', 'WritableCustomerViewBaseSchema'),
        ('testbed-public-maximal', 'ActiveCustomerBaseSchema'),
        ('testbed-public-maximal', 'LedgerSummaryBaseSchema'),
        ('testbed-public-maximal', 'WritableCustomerViewBaseSchema'),
    ]

    #: The counter-witness: VIEWs that classify correctly. Same document, same emitter, same run.
    CORRECT = [
        ('testbed-public-default', 'OrderReportBaseSchema'),
        ('testbed-public-default', 'MvCustomerSpendBaseSchema'),
        ('testbed-public-maximal', 'OrderReportBaseSchema'),
        ('testbed-public-maximal', 'MvCustomerSpendBaseSchema'),
    ]

    @pytest.mark.parametrize(('case_id', 'emitted_class'), MISCLASSIFIED)
    def test_a_misclassified_view_still_carries_a_primary_key_block(self, case_id: str, emitted_class: str) -> None:
        case = case_by_id(case_id)
        assert case.golden_module is not None
        body = _class_body(case.golden_module.read_text(encoding='utf-8'), emitted_class)
        assert '# Primary Keys' in body, witness_failed(
            'CI-075',
            case_id,
            f'{emitted_class} carries a "# Primary Keys" block, which it must not: the relation '
            f"is a VIEW, and CI5-D14a downgrades a view's <pk/> marker to UNIQUE.",
            fixed_action=FIX_ACTION,
        )

    @pytest.mark.parametrize(('case_id', 'emitted_class'), CORRECT)
    def test_a_correctly_classified_view_has_no_primary_key_block(self, case_id: str, emitted_class: str) -> None:
        # THE COUNTER-WITNESS. Without it, the test above cannot tell "this view is misclassified"
        # from "the emitter puts that block on everything".
        case = case_by_id(case_id)
        assert case.golden_module is not None
        body = _class_body(case.golden_module.read_text(encoding='utf-8'), emitted_class)
        assert '# Primary Keys' not in body, (
            f'{emitted_class} is a VIEW that castiron classifies CORRECTLY, and it has just grown '
            f'a "# Primary Keys" block. That is a REGRESSION, not a fix: CI-075 has widened from '
            f'3 of 5 views to 4 of 5.'
        )

    def test_the_ir_records_the_misclassification_itself(self) -> None:
        ir = _ir('testbed-public-default')
        wrong = {name: _table(ir, name)['table_type'] for name in ('active_customers', 'ledger_summary')}
        assert wrong == {'active_customers': 'BASE TABLE', 'ledger_summary': 'BASE TABLE'}, witness_failed(
            'CI-075',
            'testbed-public/ir.json',
            'active_customers and ledger_summary are recorded as BASE TABLE in the IR; both are '
            'VIEWs in the testbed schema (CREATE VIEW, confirmed against the pg_dump).',
            fixed_action=FIX_ACTION,
        )
        # The counter-witness at IR level: two views the heuristic gets right.
        assert _table(ir, 'order_report')['table_type'] == 'VIEW'
        assert _table(ir, 'mv_customer_spend')['table_type'] == 'VIEW'


# ---------------------------------------------------------------------------
# CI-084 — reaches the IR golden only; the emitted module shows nothing.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi084DanglingForeignKey:
    def test_the_column_is_flagged_a_foreign_key_with_nothing_to_point_at(self) -> None:
        ir = _ir('testbed-public-default')
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

    def test_the_constraint_survives_naming_a_table_that_is_not_in_the_schema(self) -> None:
        ir = _ir('testbed-public-default')
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

    def test_the_fk_name_is_the_postgres_default_and_not_the_real_one(self) -> None:
        ir = _ir('testbed-public-default')
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

    def test_every_forward_edge_name_follows_the_default_template(self) -> None:
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
        ir = _ir('testbed-public-default')
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
    def test_the_fixture_points_a_foreign_key_at_a_view(self) -> None:
        ir = _ir('openapi-fixture-default')
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
# CI-080 / CI-085 — two defects, two call sites, one symptom.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCi080EnumLabelsAreNotIdentifierSafe:
    def test_the_enum_member_names_are_not_valid_python(self) -> None:
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        offenders = [line.strip() for line in module.splitlines() if ' = "' in line and not line.startswith(' ' * 8)]
        illegal = [line for line in offenders if not line.split(' = ')[0].isidentifier()]
        assert illegal, witness_failed(
            'CI-080',
            'synthetic-torture/default.py.txt',
            'the emitted enum carries members whose NAMES are not identifiers -- '
            'IN PROGRESS = "in progress", N/A = "n/a", 2ND PASS = "2nd pass".',
            fixed_action=FIX_ACTION,
        )
        assert 'IN PROGRESS = "in progress"' in module
        assert 'N/A = "n/a"' in module

    def test_the_value_literals_are_correct_even_though_the_names_are_not(self) -> None:
        # The counter-witness, and it matters: CI9-Q1 fixed the VALUE literal (the right-hand
        # side) and CI-080 is the MEMBER NAME (the left-hand side). Asserting the values are
        # right is what proves this witness is about CI-080 and not a relapse of CI9-Q1.
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        for label in ('in progress', 'done', 'n/a', '2nd pass'):
            assert f'= "{label}"' in module, f'the enum VALUE {label!r} is wrong, which would be a CI9-Q1 relapse'


@pytest.mark.unit
class TestCi085ColumnIdentifiersAreNotSanitized:
    #: Legal quoted Postgres identifiers that are illegal Python attribute names.
    HOSTILE_COLUMNS = ('2fast', 'space name', 'kebab-case')

    @pytest.mark.parametrize('column', HOSTILE_COLUMNS)
    def test_a_hostile_column_name_reaches_the_emitted_module_verbatim(self, column: str) -> None:
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        assert f'    {column}: ' in module, witness_failed(
            'CI-085',
            'synthetic-torture/default.py.txt',
            f'the column {column!r} is emitted verbatim as a field name, producing a module that does not parse.',
            fixed_action=FIX_ACTION,
        )

    def test_the_module_does_not_parse(self) -> None:
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        assert not module_compiles(module), witness_failed(
            'CI-085',
            'synthetic-torture/default.py.txt',
            'the emitted module raises SyntaxError. castiron currently emits invalid Python for '
            'identifier-hostile schemas, and this golden is the static proof of it.',
            fixed_action=FIX_ACTION,
        )

    def test_the_safe_column_alongside_them_is_emitted_normally(self) -> None:
        # Counter-witness: an ordinary column in the SAME table emits fine, so the defect is
        # about the identifier and not about the table, the emitter or the document.
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        assert '    ok_column: str | None = Field(default=None)' in case.golden_module.read_text(encoding='utf-8')

    def test_ci_080_and_ci_085_are_distinct_defects(self) -> None:
        # CI-085's WORKPLAN row asks whether it is the same defect as CI-080. Measured here: it is
        # not. The two live at different call sites and are visible independently -- the enum
        # module below has NO hostile column, and the column table has NO enum.
        case = case_by_id('synthetic-torture-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        enum_block = module[module.index('class PublicTaskStateEnum') : module.index('# CUSTOM CLASSES')]
        assert '2fast' not in enum_block, 'the CI-080 witness must be independent of the CI-085 one'
        columns_block = _class_body(module, 'HostileColumnsBaseSchema')
        assert 'TaskState' not in columns_block, 'the CI-085 witness must be independent of the CI-080 one'


# ---------------------------------------------------------------------------
# Two fidelity WINS the same capture proves. Asserted, never characterized.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFidelityWins:
    """Not every notable shape in the corpus is a defect. These two are castiron working."""

    def test_the_same_named_cross_schema_enums_stay_distinct(self) -> None:
        # The shape that once handed one enum's members to the other class: `audit.status` and
        # `public.status` share a NAME and share nothing else.
        ir = _ir('testbed-public-default')
        by_key = {(enum['schema'], enum['name']): enum['values'] for enum in ir['enums']}
        assert by_key[('audit', 'status')] == ['ok', 'warn', 'error']
        assert by_key[('public', 'status')] == ['active', 'inactive', 'archived']

        case = case_by_id('testbed-public-default')
        assert case.golden_module is not None
        module = case.golden_module.read_text(encoding='utf-8')
        assert 'class AuditStatusEnum' in module and 'class PublicStatusEnum' in module
        audit_block = _class_body(module, 'AuditStatusEnum')
        assert 'active' not in audit_block, "AuditStatusEnum has been handed PublicStatusEnum's members"

    def test_the_correctly_classified_views_prove_the_ci5_d14a_downgrade_works(self) -> None:
        # The downgrade IS implemented and IS reachable -- CI-075 is a classification bug, not a
        # missing feature. Recording that distinction is what keeps the fix scoped.
        ir = _ir('testbed-public-default')
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

    def test_the_synthetic_input_is_the_only_source_of_ci_080_and_ci_085(self) -> None:
        from tests.unit.corpus.cases import CASES

        for row_id in ('CI-080', 'CI-085'):
            carriers = {case.family.family_id for case in CASES if row_id in case.defects}
            assert carriers == {SYNTHETIC_TORTURE.family_id}, (
                f'{row_id} is now carried by {carriers}. If a capture grew the shape, update this '
                f'test and reconsider whether the synthetic input is still needed.'
            )


def _iter_enums(document: dict[str, Any]) -> list[list[str]]:
    """Return every ``enum`` label list in a document's column definitions."""
    found: list[list[str]] = []
    for definition in document['definitions'].values():
        for prop in definition.get('properties', {}).values():
            if isinstance(prop, dict) and isinstance(prop.get('enum'), list):
                found.append(prop['enum'])
    return found
