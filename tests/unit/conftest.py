"""Shared fixtures for the unit suite.

CI-005's hand-authored PostgREST document and its committed golden module are **reused, not
copied**: the golden is exactly what ``PydanticEmitter(EmitterConfig()).emit(...)`` produces
with all defaults, so a CLI run that writes different bytes has altered emitter output --
which Hard Rule #9 and CI-021's ``check`` both forbid.
"""

import json
from pathlib import Path
from typing import Any

import pytest

#: CI-005's PostgREST OpenAPI document (see ``tests/unit/sources/openapi/conftest.py``).
OPENAPI_FIXTURE_PATH = Path(__file__).parent / 'sources' / 'openapi' / 'fixtures' / 'postgrest_openapi.json'

#: The committed golden module CI-005 emits from that document with default settings.
OPENAPI_GOLDEN_PATH = Path(__file__).parent / 'sources' / 'openapi' / 'golden' / 'schema.py.txt'


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
