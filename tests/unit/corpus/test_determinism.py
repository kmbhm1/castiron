"""The determinism harness: Hard Rule #9, measured across eleven enumerated axes.

"Emitter output is byte-stable" is the premise ``castiron check`` (CI-021) rests on, and
**every false positive in check is a broken build for someone who changed nothing.** "Run it
twice" tests one axis of eleven. This module enumerates all of them and states, per axis,
exactly what is and is not covered — because an axis silently presented as covered is worse than
one honestly labelled absent (CI6-Q7).

===  ==========================================  =========================================
Axis  What it varies                              Status here
===  ==========================================  =========================================
A1    Emit twice from one IR object               asserted (also in ``test_goldens.py``)
A2    Build the IR twice from one document        asserted
A3    Input key order (``definitions``/``paths``) asserted
A4    Fresh process, same interpreter             asserted (the A5 sweep is fresh by construction)
A5    ``PYTHONHASHSEED``                          asserted, 10 seeds, subprocesses
A6    Interpreters 3.10-3.13                      ⚠ **asserted by the GATE, not by a test**
A7    Line endings / encoding / hook safety       asserted in ``test_goldens.py``
A8    Multi-file output ordering                  ⚠ **pinned, NOT exercised** (one file today)
A9    Idempotence / no input mutation             asserted
A10   Locale                                      asserted (folded into the A5 sweep)
A11   Timezone / clock                            asserted (folded into the A5 sweep)
A12   Working directory                           asserted (folded into the A5 sweep)
A13   CLI end-to-end bytes                        asserted
===  ==========================================  =========================================

⚠ **A6 is covered by the gate, not by anything in this file.** Spawning a second interpreter
from a test is not portable — uv-managed pythons need not be on ``PATH`` in CI. The whole corpus
runs on all four legs of ``make test-matrix`` (py3.10/3.11/3.13/3.12) and of CI, and *that* is
the evidence. ``make validate-fast`` is single-interpreter and does **not** cover this axis; it
says so in its own help text and it is not the gate (CI-081/CI-082).

⚠ **A8 is pinned, not exercised.** castiron has one emitter that emits one file, so there is no
multi-file ordering to get wrong *yet*. The assertion is a **tripwire**: the day a second file
appears, its ordering is already pinned by a test that exists. Presenting it as covered would be
the CI6-Q7 failure; presenting it as absent would lose the tripwire.

Why A10/A11/A12 are folded into A5's subprocesses rather than given their own sweeps: none of
these axes is enumerable (256 hash seeds, unbounded locales, unbounded timezones), so CI-072's
"enumerate, do not sample" does not apply the way it does to a finite alternation. What applies
instead is *demonstrated fallibility* — the sweep was run against a deliberately
set-order-dependent emitter and observed to catch it. Ten subprocesses cost ~0.45 s; forty would
cost four times that and buy nothing.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from castiron.emitters import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
from castiron.ir import Schema
from tests.unit.corpus.cases import CASES, REPO_ROOT, TESTBED_INVENTORY, TESTBED_PUBLIC, CorpusCase, InputFamily
from tests.unit.corpus.conftest import case_ids, family_ids, iter_cases, iter_families
from tests.unit.corpus.pipeline import build_ir, emit_module, load_document, render_ir_golden

#: Hash seeds swept in the A5 subprocess run. Ten is enough that a set-order-dependent emitter is
#: caught with overwhelming probability (measured fallible: see the DEV audit), and cheap enough
#: to run on all four interpreter legs.
HASH_SEEDS = tuple(range(10))

#: Locales, timezones and working directories varied JOINTLY with the hash seed (axes A10/A11/A12),
#: so each value is exercised at least five times across the ten runs.
LOCALES = ('C', 'C.UTF-8')
TIMEZONES = ('UTC', 'Pacific/Kiritimati')

#: The child program: build + emit + print a digest. Deliberately tiny -- it must not import the
#: test package, or the subprocess would need the repo's pytest config to be importable.
_CHILD = """
import hashlib, json, sys
from castiron.sources.openapi import build_schema_from_document
from castiron.emitters import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
document = json.loads(open(sys.argv[1], encoding='utf-8').read())
schema = build_schema_from_document(document, schema=sys.argv[2])
text = PydanticEmitter(EmitterConfig()).emit(schema)[0].content
print(hashlib.sha256(text.encode('utf-8')).hexdigest())
"""


# --------------------------------------------------------------------------- A1 / A2


@pytest.mark.unit
class TestBuilderAndEmitterAreStateless:
    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_a2_building_the_ir_twice_gives_identical_bytes(
        self, case: CorpusCase, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        # A2. `build_schema` runs `analyze_table_relationships`, which MUTATES tables in place and
        # synthesizes reverse foreign keys -- so "build it twice" is a real statefulness question
        # here, not a formality.
        document = corpus_documents[case.family.family_id]
        first = build_ir(document, case.family, case.source_options)
        second = build_ir(document, case.family, case.source_options)
        assert render_ir_golden(first) == render_ir_golden(second)

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_a1_emitting_twice_from_one_ir_gives_identical_bytes(
        self, case: CorpusCase, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        # A1, on the same IR OBJECT -- the shape that catches emitter statefulness. Enumerated
        # over all 128 configs by test_manifest.py, which compares each against a committed
        # constant; that is strictly stronger than self-consistency, so this is not a sampling gap.
        schema = corpus_irs[(case.family.family_id, case.source_options)]
        assert emit_module(schema, case.emitter_config) == emit_module(schema, case.emitter_config)


# --------------------------------------------------------------------------- A3


@pytest.mark.unit
class TestInputKeyOrder:
    """CI5-D8 exists because "PostgREST emits ``definitions`` in hash order"."""

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_a3_reversing_the_document_key_order_changes_nothing(
        self, family: InputFamily, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        document = corpus_documents[family.family_id]
        reversed_document = dict(document)
        for section in ('definitions', 'paths'):
            reversed_document[section] = dict(reversed(list(document[section].items())))
        # Round-trip through JSON so the child sees exactly the encoding a real fetch produces.
        shuffled = json.loads(json.dumps(reversed_document))

        original = emit_module(build_ir(document, family, _defaults()), EmitterConfig())
        shuffled_output = emit_module(build_ir(shuffled, family, _defaults()), EmitterConfig())
        assert shuffled_output == original, (
            f'{family.family_id}: reversing definitions/paths order changed the emitted bytes. '
            f'PostgREST emits both in hash order, so castiron sorts them (CI5-D8) -- if that sort '
            f'was lost, every user would see spurious `check` drift on an unchanged schema.'
        )

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_a3_column_order_within_a_definition_is_preserved_not_sorted(
        self, family: InputFamily, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        # The other half of CI5-D8, and it is NOT symmetric: `properties` order is real
        # information (pg attnum / function argument order) and must survive, while `definitions`
        # order is a hash artifact and must not. A blanket sort would destroy the first.
        document = corpus_documents[family.family_id]
        name, definition = next(iter(document['definitions'].items()))
        wire_order = list(definition['properties'])
        schema = build_ir(document, family, _defaults())
        table = next(t for t in schema.tables if t.name == name)
        # `alias` carries the wire name whenever the builder renamed a column (a Python reserved
        # word such as `class` becomes `field_class`, keeping `class` in `alias`). Comparing
        # `alias or name` therefore asserts two things at once: the ORDER survived, and the
        # original wire name is still recoverable rather than merely overwritten.
        assert [column.alias or column.name for column in table.columns] == wire_order, (
            f'{family.family_id}: {name} column order no longer matches the document. That order '
            f'is pg attnum, not an artifact -- sorting it loses real information.'
        )


# --------------------------------------------------------------------------- A4 / A5 / A10-A12


@pytest.mark.unit
class TestProcessEnvironment:
    """A5 (+A4, A10, A11, A12): ten fresh subprocesses, varying four environment axes jointly."""

    def test_a5_the_output_does_not_depend_on_the_hash_seed_locale_timezone_or_cwd(self, tmp_path: Path) -> None:
        # PYTHONHASHSEED is a LIVE bug class on this codebase, not a theoretical one: CI-065
        # records `sorted(<set>, key=len, reverse=True)` producing a different order per seed.
        # Set iteration and dict construction are real risks in both ir/build.py and the
        # emitter's import collection.
        document = TESTBED_PUBLIC.input_path
        runs: list[tuple[dict[str, str], str]] = []
        for index, seed in enumerate(HASH_SEEDS):
            env = {
                **os.environ,
                'PYTHONHASHSEED': str(seed),
                'LC_ALL': LOCALES[index % len(LOCALES)],
                'TZ': TIMEZONES[index % len(TIMEZONES)],
                # CI-061-Q1: a stale .pyc would let a subprocess run code the parent never wrote.
                'PYTHONDONTWRITEBYTECODE': '1',
            }
            cwd = REPO_ROOT if index % 2 == 0 else tmp_path
            result = subprocess.run(
                [sys.executable, '-c', _CHILD, str(document), TESTBED_PUBLIC.schema],
                capture_output=True,
                text=True,
                check=True,
                env=env,
                cwd=cwd,
            )
            runs.append(
                ({'seed': str(seed), 'LC_ALL': env['LC_ALL'], 'TZ': env['TZ'], 'cwd': str(cwd)}, result.stdout.strip())
            )

        digests = {digest for _, digest in runs}
        assert len(digests) == 1, _env_failure(runs)

    def test_the_sweep_covers_each_locale_timezone_and_cwd_at_least_five_times(self) -> None:
        # CI6-Q7: the joint sweep is only honest if each folded axis is genuinely exercised.
        # Without this, a future edit to HASH_SEEDS could silently reduce a folded axis to zero
        # runs while the sweep still looked like it covered four axes.
        locales = [LOCALES[index % len(LOCALES)] for index in range(len(HASH_SEEDS))]
        timezones = [TIMEZONES[index % len(TIMEZONES)] for index in range(len(HASH_SEEDS))]
        cwds = ['repo' if index % 2 == 0 else 'tmp' for index in range(len(HASH_SEEDS))]
        for label, values, expected in (('locale', locales, LOCALES), ('tz', timezones, TIMEZONES)):
            for value in expected:
                assert values.count(value) >= 5, f'{label} {value!r} runs only {values.count(value)} time(s)'
        assert cwds.count('repo') >= 5 and cwds.count('tmp') >= 5


def _env_failure(runs: list[tuple[dict[str, str], str]]) -> str:
    """Render every run's full environment, so the reviewer can bisect the axes by hand."""
    lines = [
        'Emitted bytes DIFFER across the environment sweep. Hard Rule #9 is violated: castiron '
        'output is not a pure function of the schema.',
        '',
        'Every run, with its full environment (bisect the axes from here):',
    ]
    lines.extend(
        f'  {digest[:16]}  seed={env["seed"]:>2}  LC_ALL={env["LC_ALL"]:<8}  TZ={env["TZ"]:<20}  cwd={env["cwd"]}'
        for env, digest in runs
    )
    return '\n'.join(lines)


# --------------------------------------------------------------------------- A8


@pytest.mark.unit
class TestOutputFileOrdering:
    """⚠ **A8 is PINNED, NOT EXERCISED.** One emitter, one file, today.

    These assertions cannot fail for the right reason yet — there is no second file whose order
    could be wrong. They exist as a tripwire so that the day an emitter emits two files, the
    ordering contract is already under test instead of being invented then. Saying so plainly is
    the point: an unexercised axis presented as covered is the CI6-Q7 failure.
    """

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_a8_the_emitted_file_list_is_exactly_one_named_schema_py(
        self, case: CorpusCase, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        schema = corpus_irs[(case.family.family_id, case.source_options)]
        files = PydanticEmitter(case.emitter_config).emit(schema)
        assert [f.path for f in files] == ['schema.py']

    def test_a8_output_filename_changes_the_path_and_not_one_byte_of_content(
        self, corpus_irs: dict[tuple[str, Any], Schema]
    ) -> None:
        # This is the LICENCE for excluding `output_filename` from the 128-config sweep. If it
        # ever touched `.content`, the sweep would be missing an output-affecting axis and the
        # manifest would be guarding 128 of 256 reachable outputs.
        schema = next(iter(corpus_irs.values()))
        default = PydanticEmitter(EmitterConfig()).emit(schema)
        renamed = PydanticEmitter(EmitterConfig(output_filename='models.py')).emit(schema)
        assert [f.path for f in renamed] == ['models.py']
        assert [f.content for f in renamed] == [f.content for f in default]


# --------------------------------------------------------------------------- A9


@pytest.mark.unit
class TestNoMutation:
    """The IR nodes are **mutable** dataclasses (decision D1), so this is a real risk."""

    @pytest.mark.parametrize('family', iter_families(), ids=family_ids())
    def test_a9_building_the_ir_does_not_mutate_the_input_document(self, family: InputFamily) -> None:
        document = load_document(family)
        pristine = json.loads(family.input_path.read_text(encoding='utf-8'))
        build_ir(document, family, _defaults())
        assert document == pristine, (
            f'{family.family_id}: build_schema_from_document mutated the document it was given. '
            f'A caller that builds two schemas from one document would get different answers.'
        )

    @pytest.mark.parametrize('case', iter_cases(), ids=case_ids())
    def test_a9_emitting_does_not_mutate_the_schema(
        self, case: CorpusCase, corpus_documents: dict[str, dict[str, Any]]
    ) -> None:
        schema = build_ir(corpus_documents[case.family.family_id], case.family, case.source_options)
        before = render_ir_golden(schema)
        emit_module(schema, case.emitter_config)
        assert render_ir_golden(schema) == before, (
            f'{case.case_id}: emit() mutated the schema. Emitting twice, or running two emitters '
            f'over one IR, would then produce different output the second time.'
        )


# --------------------------------------------------------------------------- A13


@pytest.mark.unit
class TestCliEndToEnd:
    """A13: the bytes a user's repository actually receives, through the CLI's own write path."""

    def test_a13_the_cli_writes_the_committed_golden_byte_for_byte(self, tmp_path: Path) -> None:
        # Extends CI-006's fixture-only exit criterion to a REAL captured document, and covers the
        # CLI's write path (newline='\n') rather than just the emitter's return value. Offline:
        # `--from` takes a local path, so no network and no testbed.
        from click.testing import CliRunner

        from castiron.cli import cli

        case = next(c for c in CASES if c.case_id == 'testbed-inventory-default')
        assert case.golden_module is not None
        result = CliRunner().invoke(
            cli,
            [
                'gen',
                '--from',
                str(TESTBED_INVENTORY.input_path),
                '--emit',
                'pydantic',
                '--schema',
                TESTBED_INVENTORY.schema,
                '--output',
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        written = (tmp_path / 'schema.py').read_bytes()
        assert written == case.golden_module.read_bytes(), (
            'The CLI wrote different bytes than the committed golden. Either the CLI altered '
            'emitter output on its write path, or the golden is stale.'
        )

    def test_a13_running_the_cli_twice_writes_identical_bytes(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from castiron.cli import cli

        args = [
            'gen',
            '--from',
            str(TESTBED_INVENTORY.input_path),
            '--schema',
            TESTBED_INVENTORY.schema,
            '--output',
            str(tmp_path),
        ]
        runner = CliRunner()
        assert runner.invoke(cli, args).exit_code == 0
        first = (tmp_path / 'schema.py').read_bytes()
        assert runner.invoke(cli, args).exit_code == 0
        assert (tmp_path / 'schema.py').read_bytes() == first

    def test_a13_the_written_module_imports_and_instantiates(self, tmp_path: Path) -> None:
        # CI-091: a harness can be weaker than the thing it tests. `exec` into a bare dict
        # reports OK for a module that CANNOT be instantiated -- `from __future__ import
        # annotations` makes pydantic resolve types lazily via `cls.__module__` in `sys.modules`,
        # so a module absent from sys.modules fails at first instantiation, not at exec. The real
        # environment is reproduced here: a ModuleType registered in sys.modules, then an actual
        # model instantiated.
        from types import ModuleType

        from click.testing import CliRunner

        from castiron.cli import cli

        result = CliRunner().invoke(
            cli,
            [
                'gen',
                '--from',
                str(TESTBED_INVENTORY.input_path),
                '--schema',
                TESTBED_INVENTORY.schema,
                '--output',
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        source = (tmp_path / 'schema.py').read_text(encoding='utf-8')

        module = ModuleType('castiron_corpus_generated')
        sys.modules[module.__name__] = module
        try:
            exec(compile(source, '<castiron-corpus>', 'exec'), module.__dict__)  # noqa: S102
            region = module.__dict__['RegionsBaseSchema'](id=1, code='NA', name='North America')
            assert region.name == 'North America'
        finally:
            del sys.modules[module.__name__]


def _defaults() -> Any:
    """Return the default :class:`SourceOptions` (imported lazily to keep the header short)."""
    from tests.unit.corpus.cases import SourceOptions

    return SourceOptions()
