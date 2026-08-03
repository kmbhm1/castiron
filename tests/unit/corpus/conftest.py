"""Session fixtures for the golden corpus, and the autouse blocker that keeps it offline.

**Offline by construction, not by convention.** Every corpus case reads a committed file;
nothing calls ``fetch_openapi_document``, nothing resolves a hostname. That is a property worth
having *enforced*, because CI-086 records that CI still runs a bare ``pytest`` — so the suite's
offline guarantee currently holds by absence of configuration rather than by construction. The
autouse :func:`_no_sockets` fixture closes that: inside this directory, opening a socket raises.

**Cost discipline.** The 4 distinct IRs per input and the 128 emissions per input are built
**once per session** and shared by every test module. Nothing re-reads a golden per
parametrization. Without that, the 128-config sweep would be recomputed by each module that
touches it and the corpus would blow its budget on the four-interpreter gate.

⚠ **This module must never implement ``pytest_collection_modifyitems`` or any other session-level
collection hook.** That hook is handed the *whole* collected item list, not just this directory's
— an unfiltered one in a subdirectory conftest is what marked all 950 unit tests ``integration``
and made ``make test`` deselect 1 024 of 1 024 while exiting 0 (CI-083). If a future need forces
one, it must be path-scoped exactly as ``tests/integration/conftest.py`` does, and the deselect
counts must be verified in **both** directions before and after.
"""

import socket
from pathlib import Path
from typing import Any

import pytest

from castiron.ir import Schema
from tests.unit.corpus.cases import CASES, FAMILIES, CorpusCase, InputFamily, SourceOptions
from tests.unit.corpus.pipeline import build_ir, emissions_for_family, load_document
from tests.unit.corpus.regenerate import intended_artifacts, writable


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any socket call raise, for the duration of every corpus test.

    Belt and braces with the ``unit`` marker: the corpus is *supposed* to be a pure function of
    committed bytes, and this turns that from a claim into something the suite cannot violate by
    accident. A test that starts reaching for the network fails loudly here rather than passing
    on a machine that happens to have the testbed running.

    Args:
        monkeypatch: pytest's patcher, which restores the real socket module after each test.
    """

    def blocked(*args: object, **kwargs: object) -> None:
        raise RuntimeError(
            'A corpus test tried to open a socket. The golden corpus is offline by construction: '
            'every case reads a committed file. If you need a live source, the test belongs in '
            'tests/integration/ behind the `integration` marker.'
        )

    monkeypatch.setattr(socket, 'socket', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


@pytest.fixture(scope='session')
def corpus_documents() -> dict[str, dict[str, Any]]:
    """Every corpus input document, decoded once per session.

    Returns:
        ``family_id`` → the decoded document.
    """
    return {family.family_id: load_document(family) for family in FAMILIES}


@pytest.fixture(scope='session')
def corpus_irs(corpus_documents: dict[str, dict[str, Any]]) -> dict[tuple[str, SourceOptions], Schema]:
    """The IR for every ``(family, source_options)`` pair the case table reaches.

    Args:
        corpus_documents: The decoded input documents.

    Returns:
        ``(family_id, source_options)`` → the built :class:`~castiron.ir.Schema`.
    """
    irs: dict[tuple[str, SourceOptions], Schema] = {}
    for case in CASES:
        key = (case.family.family_id, case.source_options)
        if key not in irs:
            irs[key] = build_ir(corpus_documents[case.family.family_id], case.family, case.source_options)
    return irs


@pytest.fixture(scope='session')
def corpus_emissions(corpus_documents: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Every input emitted at all 128 config points, computed once per session.

    Args:
        corpus_documents: The decoded input documents.

    Returns:
        ``family_id`` → (``config_key`` → emitted module text).
    """
    return {family.family_id: emissions_for_family(corpus_documents[family.family_id], family) for family in FAMILIES}


@pytest.fixture(scope='session')
def case_modules(corpus_emissions: dict[str, dict[str, str]]) -> dict[str, str]:
    """The emitted module for each Tier-A case, keyed by case id.

    Reuses the 128-point sweep rather than emitting a sixth time, so the readable goldens and the
    manifest rows are provably the *same bytes* rather than two computations that agree by
    coincidence.

    Args:
        corpus_emissions: The full per-family sweep.

    Returns:
        ``case_id`` → emitted module text.
    """
    return {case.case_id: corpus_emissions[case.family.family_id][case.config_key] for case in CASES}


@pytest.fixture(scope='session')
def regeneration_write_set() -> dict[Path, str]:
    """What ``regenerate.py`` would write, computed once per session.

    Session-scoped because ``intended_artifacts()`` re-runs the entire corpus (4 inputs × 128
    config points). Recomputing it per test cost ~0.64 s each and dominated the corpus's whole
    time budget on all four interpreter legs.

    Returns:
        Path → intended text, restricted to the paths the tool may write.
    """
    return writable(intended_artifacts())


def iter_cases() -> list[CorpusCase]:
    """Return the corpus cases (a helper for ``pytest.mark.parametrize``)."""
    return list(CASES)


def case_ids() -> list[str]:
    """Return the corpus case ids, in table order."""
    return [case.case_id for case in CASES]


def iter_families() -> list[InputFamily]:
    """Return the input families (a helper for ``pytest.mark.parametrize``)."""
    return list(FAMILIES)


def family_ids() -> list[str]:
    """Return the input family ids, in table order."""
    return [family.family_id for family in FAMILIES]
