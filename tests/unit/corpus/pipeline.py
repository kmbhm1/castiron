"""The one pipeline that turns a corpus input into the exact bytes of a committed artifact.

**Why this module exists at all.** The regeneration tool writes the goldens and the test suite
compares against them. If those two rendered bytes by separate code paths, a golden could be
"regenerated" into a shape the tests never check, or vice versa — the corpus would agree with
itself while guarding nothing. So there is exactly one definition of each artifact's bytes here,
and both ``regenerate.py`` and ``conftest.py`` import it.

Every function is pure and offline: it reads committed files and calls castiron. Nothing here
opens a socket, and nothing here writes.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from castiron.emitters import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
from castiron.ir import Schema
from castiron.sources.openapi import build_schema_from_document
from tests.unit.conftest import GOLDEN_TOOL_VERSION
from tests.unit.corpus.cases import (
    GOLDEN_DIR,
    CorpusCase,
    InputFamily,
    SourceOptions,
    all_config_points,
    config_key,
)

#: Every committed text artifact is written with these settings, and every test reads bytes and
#: compares bytes. ``newline=''`` on the write side means "write ``\n`` exactly as given" — the
#: default would translate to ``os.linesep`` and produce CRLF goldens on Windows.
ENCODING = 'utf-8'


@dataclass(frozen=True)
class Counters:
    """Structural counters for one emitted artifact.

    Computed **textually** (``startswith``), never via :mod:`ast`, because a ``characterized``
    golden may not parse — and the counters are most useful exactly when it does not. They are
    what makes a manifest or golden diff reviewable rather than a wall of hashes: a changed row
    shows *how* it changed.

    Attributes:
        lines: Physical line count.
        chars: Character count.
        classes: Top-level ``class X...`` statements.
        fields: Indented ``name: annotation`` lines (model fields).
        imports: ``import``/``from`` lines.
    """

    lines: int
    chars: int
    classes: int
    fields: int
    imports: int

    def as_row(self) -> str:
        """Render the counters as the manifest's fixed-width tail."""
        return f'{self.lines:6d} {self.chars:8d} {self.classes:4d} {self.fields:5d} {self.imports:4d}'

    def delta(self, other: 'Counters') -> str:
        """Render ``self`` → ``other`` as a signed, human-readable delta.

        Args:
            other: The counters to compare against (the "actual" side).

        Returns:
            One line naming every counter that moved, or a note that none did.
        """
        parts = []
        for name in ('lines', 'chars', 'classes', 'fields', 'imports'):
            before, after = getattr(self, name), getattr(other, name)
            if before != after:
                parts.append(f'{name} {before}->{after} ({after - before:+d})')
        return ', '.join(parts) if parts else 'no structural counter moved (a whitespace or text-only change)'


def count_structure(text: str) -> Counters:
    """Compute :class:`Counters` for an emitted module.

    Args:
        text: The emitted module text.

    Returns:
        The structural counters.
    """
    lines = text.splitlines()
    classes = sum(1 for line in lines if line.startswith('class '))
    imports = sum(1 for line in lines if line.startswith(('import ', 'from ')))
    fields = sum(1 for line in lines if _is_field_line(line))
    return Counters(lines=len(lines), chars=len(text), classes=classes, fields=fields, imports=imports)


def _is_field_line(line: str) -> bool:
    """Whether ``line`` is an indented model-field declaration (``    name: annotation``)."""
    if not line.startswith('    ') or line.startswith('     '):
        return False
    body = line[4:]
    if not body or body.startswith(('#', '"', "'")):
        return False
    return ': ' in body


def sha256_text(text: str) -> str:
    """Return the sha256 of ``text`` encoded as UTF-8.

    Args:
        text: The text to digest.

    Returns:
        The lowercase hex digest.
    """
    return hashlib.sha256(text.encode(ENCODING)).hexdigest()


def load_document(family: InputFamily) -> dict[str, Any]:
    """Read and decode one corpus input document.

    Args:
        family: The input family to read.

    Returns:
        The decoded document.
    """
    decoded: dict[str, Any] = json.loads(family.input_path.read_text(encoding=ENCODING))
    return decoded


def build_ir(document: dict[str, Any], family: InputFamily, options: SourceOptions) -> Schema:
    """Build the Schema IR from a decoded corpus document.

    Args:
        document: The decoded input document.
        family: The family (supplies the schema name).
        options: The source half of the config point.

    Returns:
        The populated :class:`~castiron.ir.Schema`.
    """
    return build_schema_from_document(
        document,
        schema=family.schema,
        disable_model_prefix_protection=options.disable_model_prefix_protection,
        infer_generated_primary_keys=options.infer_generated_primary_keys,
    )


def emit_module(schema: Schema, config: EmitterConfig) -> str:
    """Emit the single Pydantic module for ``schema``, with the header version **pinned**.

    🔴 The pin is the whole reason this is not a bare ``PydanticEmitter(config)``. See
    :data:`~tests.unit.conftest.GOLDEN_TOOL_VERSION`: semantic-release rewrites
    ``castiron.__version__`` in the release commit, so a live version here would make every
    committed golden and all 512 manifest rows a function of the release cycle.

    Args:
        schema: The IR to render.
        config: The emitter half of the config point.

    Returns:
        The emitted module text.
    """
    return PydanticEmitter(config, tool_version=GOLDEN_TOOL_VERSION).emit(schema)[0].content


def render_ir_golden(schema: Schema) -> str:
    """Render a :class:`~castiron.ir.Schema` as the committed ``ir.json`` bytes.

    ``sort_keys=False`` is deliberate: ``Schema.as_dict()`` already guarantees a stable
    projection (declaration order for fields, list order preserved), and *sorting* here would
    destroy real information — ``properties`` order is pg ``attnum`` and function argument order,
    which castiron depends on and which a golden must therefore pin.

    Args:
        schema: The IR to serialize.

    Returns:
        The golden text, ending in exactly one newline.
    """
    return json.dumps(schema.as_dict(), indent=2, sort_keys=False, ensure_ascii=False) + '\n'


#: Per-process memo for :func:`emissions_for_family`, keyed by ``family_id``.
#:
#: The 128-point sweep is the corpus's dominant cost, and two independent callers need it in one
#: pytest process: the ``corpus_emissions`` fixture and ``regenerate.intended_artifacts()``.
#: Measured under coverage, computing it twice cost ~1.0 s **per interpreter leg**, i.e. ~4 s on
#: the four-leg gate, for two identical results.
#:
#: Safe because the function is pure over committed bytes: a family's document is a fixed file on
#: disk, so the emissions are a function of ``family_id`` alone within one process. It caches
#: nothing across processes, so ``regenerate.py`` run standalone is unaffected.
_EMISSION_CACHE: dict[str, dict[str, str]] = {}


def emissions_for_family(document: dict[str, Any], family: InputFamily) -> dict[str, str]:
    """Emit every one of the 128 config points for one input, reusing the 4 distinct IRs.

    The IR is a function of ``(document, schema, source_options)`` and there are only 4 distinct
    source-option pairs, so building it once per pair rather than once per config point is what
    keeps the whole sweep inside its cost budget.

    Args:
        document: The decoded input document.
        family: The input family.

    Returns:
        ``config_key`` → emitted module text, for all 128 points.
    """
    cached = _EMISSION_CACHE.get(family.family_id)
    if cached is not None:
        return cached

    irs: dict[SourceOptions, Schema] = {}
    emissions: dict[str, str] = {}
    for emitter_config, source_options in all_config_points():
        if source_options not in irs:
            irs[source_options] = build_ir(document, family, source_options)
        emissions[config_key(emitter_config, source_options)] = emit_module(irs[source_options], emitter_config)
    _EMISSION_CACHE[family.family_id] = emissions
    return emissions


def module_compiles(text: str) -> bool:
    """Whether ``text`` parses as Python.

    Args:
        text: The emitted module text.

    Returns:
        ``True`` when :func:`compile` accepts it.
    """
    try:
        compile(text, '<castiron-corpus>', 'exec')
    except SyntaxError:
        return False
    return True


#: Per-process memo for :func:`render_manifest`, keyed by ``family_id``. Same purity argument as
#: :data:`_EMISSION_CACHE`, and the same two callers. Rendering a manifest runs ``compile()`` over
#: all 128 emitted modules, so doing it twice per family meant 512 redundant parses of modules up
#: to 46 KB on every interpreter leg.
_MANIFEST_CACHE: dict[str, str] = {}


def render_manifest(family: InputFamily, emissions: dict[str, str]) -> str:
    """Render the committed fingerprint manifest for one input family.

    Deterministic by construction: rows are emitted in ``config_key`` order, which is a total
    order over a fully enumerated product.

    Args:
        family: The input family.
        emissions: ``config_key`` → emitted text, as returned by :func:`emissions_for_family`.

    Returns:
        The manifest text, ending in exactly one newline.
    """
    cached = _MANIFEST_CACHE.get(family.family_id)
    if cached is not None:
        return cached

    provenance = _provenance_line(family)
    header = [
        '# castiron corpus fingerprint manifest - GENERATED. DO NOT EDIT BY HAND.',
        '# regenerate: uv run python -m tests.unit.corpus.regenerate --write',
        f'# input:      {family.input_path.name}',
        f'# schema:     {family.schema}',
        f'# provenance: {provenance}',
        f'# tool:       castiron {GOLDEN_TOOL_VERSION} (pinned sentinel -- tests/unit/conftest.py)',
        '# columns:    <config-key>  <sha256>  lines  chars  classes  fields  imports  compiles',
    ]
    rows = [
        f'{key}  {sha256_text(text)}  {count_structure(text).as_row()}  {"yes" if module_compiles(text) else "no"}'
        for key, text in sorted(emissions.items())
    ]
    manifest = '\n'.join([*header, *rows]) + '\n'
    _MANIFEST_CACHE[family.family_id] = manifest
    return manifest


def _provenance_line(family: InputFamily) -> str:
    """Render the manifest header's one-line provenance summary."""
    if family.provenance_path is None:
        return f'{family.origin} - castiron CI-005 fixture (see CI-076)'
    record = json.loads(family.provenance_path.read_text(encoding=ENCODING))
    if family.origin == 'captured':
        return (
            f'{record["origin"]} - castiron-testbed {record["seed_revision"]} - PostgREST {record["postgrest_version"]}'
        )
    return f'{record["origin"]} - hand-authored, {record["authored_by"]}'


def artifacts_for_case(case: CorpusCase, document: dict[str, Any]) -> dict[Path, str]:
    """Render every committed artifact this case owns.

    Args:
        case: The corpus case.
        document: Its family's decoded input document.

    Returns:
        Path → intended text, for the case's IR golden and (when it owns one) its module golden.
    """
    schema = build_ir(document, case.family, case.source_options)
    artifacts: dict[Path, str] = {case.golden_ir: render_ir_golden(schema)}
    # `openapi-fixture-default` points at CI-005's committed golden, which lives OUTSIDE
    # `golden/`. This row does not own those bytes and must never rewrite them (acceptance
    # criterion 14: both pre-existing goldens stay byte-unchanged from origin/main). The test
    # suite still compares against it -- it is only the WRITE that is withheld.
    if case.golden_module is not None and case.golden_module.is_relative_to(GOLDEN_DIR):
        artifacts[case.golden_module] = emit_module(schema, case.emitter_config)
    return artifacts
