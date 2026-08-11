"""Shared fixtures and constants for the unit suite.

CI-005's hand-authored PostgREST document and its committed golden module are **reused, not
copied**: the golden is exactly what ``PydanticEmitter(EmitterConfig(), tool_version=
GOLDEN_TOOL_VERSION).emit(...)`` produces with all defaults, so a CLI run that writes different
bytes has altered emitter output -- which Hard Rule #9 and CI-021's ``check`` both forbid. The
one token that differs from a real ``castiron gen`` is the recorded version; see
:data:`GOLDEN_TOOL_VERSION` for why, and ``tests/unit/cli/test_gen.py`` for how the CLI's end-to-
end byte proof stays exact anyway.
"""

import json
from pathlib import Path
from typing import Any

import pytest

#: The castiron version every **committed golden** records in its provenance header.
#:
#: 🔴 Pinned, and deliberately **not** :data:`castiron.__version__`. ``pyproject.toml`` declares
#: ``version_variables = ["src/castiron/__init__.py:__version__"]``, so ``python-semantic-release``
#: rewrites ``__version__`` *inside the release commit itself*. A golden that embedded the live
#: version would therefore turn ``main`` red on every release -- 6 module goldens and 512 manifest
#: rows at once -- in a commit no developer authored and none can regenerate.
#:
#: ⚠ **Pinning, not suppressing.** A ``tool_version=None -> no header`` escape would have kept the
#: goldens byte-identical (a very tempting zero delta) at the cost of the corpus never once
#: linting or golden-ing a header. That is the CI-092 shape exactly -- *"nothing in this
#: repository had ever run a linter over emitted bytes"*. The goldens keep full fidelity modulo
#: one token, and the token itself is proved separately: ``test_emitter.py`` asserts the default
#: **is** ``castiron.__version__`` and that changing only the version changes exactly one line.
#:
#: The string is not a valid PEP 440 version, on purpose: it can never be mistaken for a release.
GOLDEN_TOOL_VERSION = '0.0.0-corpus'

#: CI-005's PostgREST OpenAPI document (see ``tests/unit/sources/openapi/conftest.py``).
OPENAPI_FIXTURE_PATH = Path(__file__).parent / 'sources' / 'openapi' / 'fixtures' / 'postgrest_openapi.json'

#: The committed golden module CI-005 emits from that document with default settings.
OPENAPI_GOLDEN_PATH = Path(__file__).parent / 'sources' / 'openapi' / 'golden' / 'schema.py.txt'

#: CI-141's SYNTHETIC sub-floor document -- ``info.version = '12.2.3 (519615d)'`` and a ``get`` on
#: every ``/rpc/`` path. ⚠ Evidence about **castiron**, never about PostgREST; the upstream claim
#: rests on ``makeProcPathItem``, not on this file (see the file's own ``info.description``).
OPENAPI_SUB_FLOOR_PATH = (
    Path(__file__).parent / 'sources' / 'openapi' / 'fixtures' / 'postgrest_openapi_v12_shaped.json'
)


@pytest.fixture
def openapi_fixture_path() -> Path:
    """The CI-005 PostgREST OpenAPI JSON document, on disk."""
    return OPENAPI_FIXTURE_PATH


@pytest.fixture
def openapi_fixture_document() -> dict[str, Any]:
    """The CI-005 PostgREST OpenAPI document, decoded."""
    decoded: dict[str, Any] = json.loads(OPENAPI_FIXTURE_PATH.read_text(encoding='utf-8'))
    return decoded


@pytest.fixture
def openapi_golden_text() -> str:
    """The committed golden module text emitted from that document with all defaults."""
    return OPENAPI_GOLDEN_PATH.read_text(encoding='utf-8')


@pytest.fixture
def openapi_sub_floor_document() -> dict[str, Any]:
    """CI-141's SYNTHETIC document as a PostgREST below 13.0.5 would serve it, decoded."""
    decoded: dict[str, Any] = json.loads(OPENAPI_SUB_FLOOR_PATH.read_text(encoding='utf-8'))
    return decoded
