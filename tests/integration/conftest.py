"""Env-gated fixtures for the live-source suite.

These tests need the external ``castiron-testbed`` apparatus (see ``tests/integration/README.md``).
It is deliberately **not** part of this repository, so every fixture skips loudly when the
environment is not configured. Nothing here is part of ``make validate``.

Three properties this module is responsible for, in order of how badly they fail if broken:

1. **A skip is a skip, never a silent pass.** ``CASTIRON_TEST_POSTGREST_URL`` is the single
   master switch (SEED-D6 — one switch, not two: a second flag creates a state where the URL
   is set and the tests silently skip anyway, which is exactly the "was this path ever
   exercised?" failure of standing lesson CI6-Q7). The autouse ``_require_testbed`` fixture
   makes every test in this directory skip when it is unset, whether or not that test
   remembers to ask for a live fixture.
2. **No socket call can reach ``make test``.** Belt and braces: each test module carries
   ``pytestmark = pytest.mark.integration``, :func:`pytest_collection_modifyitems` re-applies
   that marker to anything in this directory that forgot it, and ``make test`` runs
   ``-m "not integration"``. Any one of the three would do; all three means neither a
   forgotten marker nor a stray env var puts a network call inside the static gate.
3. **The API key never reaches a test's namespace.** It is read in exactly one fixture and
   captured in one closure (:func:`live_document`), so no test function holds it as a local and
   no fixture repr renders it. Anything printed on a failure goes through
   :func:`castiron.cli.errors.redact` first — the same masking the CLI uses (CI-063/CI-068),
   reused rather than re-implemented.

   ⚠ **This claim shipped one size too large, and CI-089 measured it.** It used to end
   "...and ``--showlocals`` has nothing to print". A closure hides the key from *test* functions;
   it does not hide it from a **traceback**, because ``--showlocals`` renders the locals of every
   frame — and the loader's own frame binds ``key``. Any exception escaping :func:`_fetch` was
   therefore enough: a malformed ``CASTIRON_TEST_POSTGREST_URL`` printed the key in full, three
   times, because ``fetch_openapi_document`` violated its documented ``Raises:`` contract. What
   makes the property true is that **nothing escapes** :func:`_fetch` — see its ``except
   Exception`` and ``pytrace=False`` — with the fetcher's contract fixed in ``src/`` behind it.

``CASTIRON_TEST_SEED_REVISION`` is attached to **every** failing report by
:func:`pytest_runtest_makereport`. That is the mechanism that makes a moved assertion
attributable: a failure that quotes seed ``3150132`` when the last green run quoted ``3150131``
is a schema change, not a castiron change.
"""

import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from castiron.cli.errors import redact
from castiron.ir import Schema
from castiron.sources.openapi import build_schema_from_document, fetch_openapi_document

#: The master switch. Unset ⇒ every test in this directory skips.
URL_ENV = 'CASTIRON_TEST_POSTGREST_URL'

#: The testbed's local anon JWT. A localhost-scoped demo credential — still treated as a secret.
KEY_ENV = 'CASTIRON_TEST_POSTGREST_KEY'

#: **Reserved for CI-010/011** (the live-database source). Unused by this suite; declared here so
#: both halves of ``tests/integration/`` agree on one spelling before the second half is written.
DSN_ENV = 'CASTIRON_TEST_DB_DSN'

#: The testbed repo's short SHA, recorded on every failing report (see the module docstring).
REVISION_ENV = 'CASTIRON_TEST_SEED_REVISION'

#: What a document loader accepts: the schema name to request via ``Accept-Profile``.
DocumentLoader = Callable[[str], Mapping[str, Any]]

#: This suite's directory — the scope :func:`pytest_collection_modifyitems` must restrict itself to.
_HERE = Path(__file__).parent


def seed_revision() -> str:
    """Return the testbed revision this run is measured against, or a loud placeholder.

    Returns:
        The value of ``CASTIRON_TEST_SEED_REVISION``, or ``'unknown (CASTIRON_TEST_SEED_REVISION
        unset)'`` — never an empty string, because a blank line in a failure report reads as
        "there is no seed revision" rather than "nobody exported it".
    """
    return os.environ.get(REVISION_ENV) or f'unknown ({REVISION_ENV} unset)'


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test collected from **this directory** as ``integration``.

    Property 2 of the module docstring: a new file in ``tests/integration/`` that forgets
    ``pytestmark`` still cannot be selected by ``make test``'s ``-m "not integration"``.

    ⚠ The path filter is load-bearing, not defensive. ``pytest_collection_modifyitems`` is a
    **session** hook: a conftest that implements it is handed the *whole* collected item list, not
    just the items under its own directory. Without the filter it marks the entire suite
    ``integration`` and ``-m "not integration"`` then deselects every test there is.

    **What that costs, re-measured on 2026-08-02 (CI-089) rather than asserted.** This docstring
    used to end "the gate reports success having run nothing". That is **false twice over**, and
    both corrections matter more than the original claim did. With this filter removed
    (pytest 9.1.1)::

        pytest -m "not integration"                                   -> 1420 deselected, exit 5
        pytest -m "not integration" --cov=... --cov-fail-under=90     -> exit 1, coverage 31.57%

    First, ``make test`` carries ``--cov-fail-under=90`` and a run of nothing cannot clear a 90%
    floor. Second — and this is the part that was simply wrong — a **totally** deselected run
    exits **5**, not 0: ``session.testscollected`` is the *post*-deselection count, so pytest's
    own "no tests collected" already fires. Nothing about this scenario is silent.

    What IS silent is **partial** deselection, where enough items survive to keep exit 5 away::

        (a filter bug sparing one directory) -> 184 passed, 1236 deselected, exit 0
                                             -> with the floor: exit 1, coverage 50.59%

    and that is the shape worth fearing, because ``184 passed`` reads like success where
    ``1420 deselected`` does not. The filter below is still the primary guard; the coverage floor
    (now on **every** ``make test-matrix`` leg, ``CI-088``) is the backstop that catches the
    partial case late and obscurely, reporting "coverage too low" for what is really "a conftest
    hook in a subdirectory is global until you scope it" (standing lesson ``CI-083``).

    Args:
        items: Every item collected in the session.
    """
    for item in items:
        if item.path.is_relative_to(_HERE):
            item.add_marker(pytest.mark.integration)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Iterator[None]:
    """Attach the testbed seed revision to every failing report.

    A golden or a fidelity assertion can move for two entirely different reasons — castiron
    changed, or the schema under it changed — and only one of them is a bug. Stamping the
    revision onto the failure is what keeps those distinguishable without asking the reader to
    remember which testbed commit they were running.

    Args:
        item: The test item (unused; part of the hook contract).
        call: The phase result (unused; part of the hook contract).

    Yields:
        ``None`` — control returns to pytest, which fills in the report.
    """
    del item, call  # the hook contract passes them; the revision does not depend on either
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]  # hookwrapper result, untyped upstream
    if report.failed:
        report.sections.append(('castiron-testbed seed revision', seed_revision()))


@pytest.fixture(scope='session')
def postgrest_url() -> str:
    """The PostgREST API root, or skip the whole suite."""
    url = os.environ.get(URL_ENV)
    if not url:
        pytest.skip(f'{URL_ENV} is not set; start the castiron-testbed apparatus to run live-source tests')
    return url


@pytest.fixture(scope='session', autouse=True)
def _require_testbed(postgrest_url: str) -> None:
    """Skip every test in this directory when the apparatus is not configured.

    Autouse so the guarantee is structural rather than per-test: a future test that asks for no
    live fixture at all still cannot make a network call on a contributor's machine.

    Args:
        postgrest_url: The master-switch fixture, which skips when unset.
    """
    del postgrest_url  # requested for its skip side effect only


def _fetch(url: str, key: str | None, schema: str) -> Mapping[str, Any]:
    """Fetch one schema's document, failing with a redacted, revision-stamped message.

    ⚠ ``except Exception``, **not** ``except SourceError`` -- deliberately, and measured (CI-089).
    ``SourceError`` alone is only as good as the fetcher's ``Raises:`` contract, and that contract
    did not hold: ``normalize_postgrest_url`` let ``urlsplit``'s bare ``ValueError`` escape, so a
    malformed ``CASTIRON_TEST_POSTGREST_URL`` produced a real traceback -- and ``pytest
    --showlocals`` renders every frame's locals, which includes the ``key`` free variable that
    :func:`live_document`'s closure exists to hide. Reproduced with a fake JWT before the fix::

        CASTIRON_TEST_POSTGREST_URL='http://[::1' pytest --showlocals -m integration
        -> the full key printed 3x (loader, _fetch, fetch_openapi_document)

    The contract is fixed at the root, in ``src/`` -- this is the second layer, and having both is
    the settled house position (CI-063: sanitize at the boundary *and* harden the mask). The point
    of the broad clause is that it does not depend on being right about what the code below can
    raise, which is exactly the assumption that failed.

    Args:
        url: The API root.
        key: The API key, which never appears in the failure message.
        schema: The schema to request via ``Accept-Profile``.

    Returns:
        The decoded document.
    """
    failure: str | None = None
    try:
        return fetch_openapi_document(url, key=key, schema=schema)
    except Exception as exc:
        # Built here but raised BELOW, outside the ``except`` block: ``pytest.fail(pytrace=False)``
        # prints no traceback, so nothing renders the chained exception's unmasked message either.
        # The type is named because the message no longer implies SourceError.
        failure = redact(f'{type(exc).__name__}: {exc}', key)
    pytest.fail(
        f'Could not read the {schema!r} document from the castiron-testbed apparatus '
        f'(seed revision {seed_revision()}): {failure}',
        pytrace=False,
    )


@pytest.fixture(scope='session')
def live_document(postgrest_url: str) -> DocumentLoader:
    """Return a loader that fetches one schema's OpenAPI document from the live apparatus.

    A **callable** rather than a value so the API key stays inside this closure: no test
    function ever binds it, so it cannot reach a traceback, a fixture repr, or ``--showlocals``.
    Results are memoized per schema, so the whole session makes one request per schema.

    Args:
        postgrest_url: The API root.

    Returns:
        ``loader(schema)`` → the decoded document for that schema.
    """
    key = os.environ.get(KEY_ENV)
    cache: dict[str, Mapping[str, Any]] = {}

    def loader(schema: str) -> Mapping[str, Any]:
        if schema not in cache:
            cache[schema] = _fetch(postgrest_url, key, schema)
        return cache[schema]

    return loader


@pytest.fixture(scope='session')
def live_document_refetch(postgrest_url: str) -> DocumentLoader:
    """Like :func:`live_document`, but **never** memoized — one real request per call.

    Hard Rule #9 is a claim about castiron's *output*, and proving it end to end needs a second,
    independent trip through the network and the parser rather than a second pass over one cached
    dict. Separate from :func:`live_document` so the ordinary fixtures cannot accidentally start
    making a request per test.

    Args:
        postgrest_url: The API root.

    Returns:
        ``loader(schema)`` → a freshly fetched document.
    """
    key = os.environ.get(KEY_ENV)

    def loader(schema: str) -> Mapping[str, Any]:
        return _fetch(postgrest_url, key, schema)

    return loader


@pytest.fixture(scope='session')
def live_public_document(live_document: DocumentLoader) -> Mapping[str, Any]:
    """The raw ``public`` document — for facts about *PostgREST*, not about the IR."""
    return live_document('public')


@pytest.fixture(scope='session')
def live_public_schema(live_document: DocumentLoader) -> Schema:
    """The IR built from the testbed's ``public`` document."""
    return build_schema_from_document(live_document('public'), schema='public')


@pytest.fixture(scope='session')
def live_public_schema_inferred(live_document: DocumentLoader) -> Schema:
    """The ``public`` IR with ``--infer-generated-primary-keys`` (CI5-D7) turned on."""
    return build_schema_from_document(live_document('public'), schema='public', infer_generated_primary_keys=True)


@pytest.fixture(scope='session')
def live_inventory_schema(live_document: DocumentLoader) -> Schema:
    """The IR built from the testbed's second exposed schema."""
    return build_schema_from_document(live_document('inventory'), schema='inventory')


@pytest.fixture(scope='session')
def live_edge_schema(live_document: DocumentLoader) -> Schema:
    """The IR built from the quarantine schema — never a golden input (spec §5.1)."""
    return build_schema_from_document(live_document('edge'), schema='edge')
