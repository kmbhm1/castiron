"""Regenerate the golden corpus — and, by default, **write nothing**.

    uv run python -m tests.unit.corpus.regenerate            # inspect: writes nothing, exit 1 on drift
    uv run python -m tests.unit.corpus.regenerate --write    # accept:  rewrites committed goldens

**Why the default does not write.** A tool whose easy path is "rewrite the file" will be used
that way under pressure, and a golden that is regenerated whenever it goes red has stopped
guarding anything. So the default mode regenerates into ``dist/scratch/<date>-ci-007-corpus/``,
prints a classified report of everything that moved, and **exits 1** if anything did. That makes
this command the reviewer's mechanical check as well as the author's: exit 0 proves every
committed golden is exactly what this branch's code produces.

⚠ **It never writes ``inputs/``.** Re-capturing an input and regenerating a golden are different
operations with different provenance: a changed input must arrive with a changed
``provenance.json``, from the testbed's ``capture.sh``, and no code path here can do that by
accident. ``test_corpus_selfcheck.py`` asserts the write set is a subset of ``golden/`` ∪
``fingerprints/``.
"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from castiron.utils.textdiff import changed_line_counts, unified_hunks
from tests.unit.corpus.cases import (
    CASES,
    FAMILIES,
    FINGERPRINT_DIR,
    GOLDEN_DIR,
    REPO_ROOT,
    fingerprint_path,
)
from tests.unit.corpus.pipeline import (
    ENCODING,
    artifacts_for_case,
    count_structure,
    emissions_for_family,
    load_document,
    render_manifest,
)

#: Where a non-writing run puts its regenerated copies (Hard Rule #11 — never a loose file at the
#: repo root, never a tracked path).
SCRATCH_ROOT = REPO_ROOT / 'dist' / 'scratch'


def intended_artifacts() -> dict[Path, str]:
    """Render every committed corpus artifact this branch's code would produce.

    Returns:
        Path → intended text, covering every case's IR golden, every owned module golden, and
        every input family's fingerprint manifest.
    """
    artifacts: dict[Path, str] = {}
    documents = {family.family_id: load_document(family) for family in FAMILIES}
    for case in CASES:
        artifacts.update(artifacts_for_case(case, documents[case.family.family_id]))
    for family in FAMILIES:
        emissions = emissions_for_family(documents[family.family_id], family)
        artifacts[fingerprint_path(family)] = render_manifest(family, emissions)
    return artifacts


def writable(artifacts: dict[Path, str]) -> dict[Path, str]:
    """Filter ``artifacts`` down to the paths this tool is permitted to write.

    The permitted set is ``golden/`` ∪ ``fingerprints/``. Everything else — the corpus inputs, and
    CI-005's golden module, which this row references but does not own — is read-only here.

    Args:
        artifacts: Every rendered artifact.

    Returns:
        Only the artifacts under a writable root.
    """
    return {
        path: text
        for path, text in artifacts.items()
        if path.is_relative_to(GOLDEN_DIR) or path.is_relative_to(FINGERPRINT_DIR)
    }


def classify(artifacts: dict[Path, str]) -> tuple[list[Path], list[Path], list[Path]]:
    """Split artifacts into unchanged / changed / missing, against what is committed.

    Args:
        artifacts: Path → intended text.

    Returns:
        ``(unchanged, changed, missing)``, each sorted by path.
    """
    unchanged: list[Path] = []
    changed: list[Path] = []
    missing: list[Path] = []
    for path, text in sorted(artifacts.items()):
        if not path.is_file():
            missing.append(path)
        elif path.read_text(encoding=ENCODING) == text:
            unchanged.append(path)
        else:
            changed.append(path)
    return unchanged, changed, missing


def _rel(path: Path) -> str:
    """Render ``path`` relative to the repository root when possible."""
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:  # pragma: no cover - artifacts always live under the repo root
        return str(path)


def _report_change(path: Path, text: str) -> list[str]:
    """Describe one changed artifact: counters, line counts, and the first hunks.

    Nothing that changed is ever summarized as "differs" — that word is what makes a reviewer
    reach for ``--write`` instead of reading.
    """
    committed = path.read_text(encoding=ENCODING)
    before, after = committed.splitlines(keepends=True), text.splitlines(keepends=True)
    added, removed = changed_line_counts(before, after)

    lines = [
        f'  CHANGED  {_rel(path)}',
        f'           lines +{added} / -{removed}; {count_structure(committed).delta(count_structure(text))}',
    ]
    lines.extend(_outline_delta(committed, text))
    lines.extend(
        f'           {line}'
        for line in unified_hunks(before, after, fromfile='committed', tofile='produced', context=2)
    )
    return lines


def _outline_delta(committed: str, produced: str) -> list[str]:
    """Report added/removed top-level ``class X(Y):`` lines.

    The outline is what localizes a change: a diff that adds 200 lines but no class is a field or
    docstring change, while one that adds 26 classes is a config or naming change.
    """
    before = {line for line in committed.splitlines() if line.startswith('class ')}
    after = {line for line in produced.splitlines() if line.startswith('class ')}
    report = []
    for label, names in (('+ class', sorted(after - before)), ('- class', sorted(before - after))):
        for name in names[:5]:
            report.append(f'           {label} {name}')
        if len(names) > 5:
            report.append(f'           {label} ... and {len(names) - 5} more')
    return report


def main(argv: list[str] | None = None) -> int:
    """Run the regeneration tool.

    Args:
        argv: Command-line arguments (defaults to ``sys.argv[1:]``).

    Returns:
        ``0`` when every committed golden already matches this branch's output (or when
        ``--write`` applied the changes); ``1`` when anything moved and ``--write`` was not given.
    """
    parser = argparse.ArgumentParser(
        prog='python -m tests.unit.corpus.regenerate',
        description='Regenerate the castiron golden corpus. Writes NOTHING unless --write is given.',
    )
    parser.add_argument(
        '--write',
        action='store_true',
        help='Rewrite the committed goldens and manifests. The only mode that touches a tracked file.',
    )
    args = parser.parse_args(argv)

    artifacts = writable(intended_artifacts())
    unchanged, changed, missing = classify(artifacts)

    print(f'castiron golden corpus: {len(artifacts)} artifact(s) regenerated from {len(CASES)} case(s).')
    print(f'  unchanged: {len(unchanged)}   changed: {len(changed)}   missing: {len(missing)}')

    for path in missing:
        print(f'  MISSING  {_rel(path)}')
    for path in changed:
        print('\n'.join(_report_change(path, artifacts[path])))

    if not changed and not missing:
        print('\nEvery committed golden is exactly what this branch produces. Nothing to do.')
        return 0

    if args.write:
        for path in changed + missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(artifacts[path], encoding=ENCODING, newline='')
        print(f'\nWROTE {len(changed) + len(missing)} artifact(s).')
        print('⚠ The PR body needs a `## Golden delta` section stating the CAUSE, DIRECTION and')
        print('  PREDICTED magnitude of this diff -- written BEFORE regenerating. A description')
        print('  written afterwards is not a prediction and is worth nothing to a reviewer.')
        return 0

    scratch = SCRATCH_ROOT / f'{dt.date.today().isoformat()}-ci-007-corpus'
    for path in changed + missing:
        target = scratch / path.relative_to(REPO_ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(artifacts[path], encoding=ENCODING, newline='')
    print(f'\nWrote regenerated copies to {_rel(scratch)}/ and left every committed golden alone.')
    print('Re-run with --write once you have written the `## Golden delta` prediction.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
