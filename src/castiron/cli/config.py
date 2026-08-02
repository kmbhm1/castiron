"""The ``[tool.castiron]`` project config: discovery, parsing, validation, precedence.

The ROADMAP calls the project config file a deliberate fix to supabase-pydantic, whose
``gen`` had no ``--config`` at all, so CI and local runs could not share one source of
truth. This module is that fix.

**Precedence is click's, not ours.** The eager ``--config`` callback populates
``ctx.default_map``; click's own ``Parameter.consume_value`` then resolves
*command line → environment variable → default map → built-in default*, in that order.
Hand-rolling the chain (``if value is None: value = cfg.get(...)``) would be more code,
less testable, and would silently diverge per option.

Consequences of that choice, all deliberate:

- **Config keys are the flag names** (dashes and underscores interchangeable), with exactly
  one alias: ``from`` → the click parameter ``source``, because ``from`` is a Python keyword.
- **Lists replace, never merge** — ``--emit pydantic`` on the command line fully overrides
  ``emit = ["pydantic", "sqlalchemy"]`` in the file.
- Because every boolean flag is declared as a ``--x/--no-x`` pair, a config value can be
  overridden from the command line **in both directions**, so no ``--no-config`` escape
  hatch is needed.

``key`` is **never** readable here (CI6-D7): ``pyproject.toml`` is a committed file.
"""

import difflib
import logging
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import click

from castiron.emitters import EMITTERS

# ``tomllib`` is stdlib from 3.11; the 3.10 leg installs the marker-gated ``tomli`` (CI6-D1).
# The version gate -- not ``try/except ModuleNotFoundError`` -- is deliberate and typing-driven:
# with the try/except form mypy binds ``tomllib`` from the *first* import, typeshed marks it
# 3.11+-only, ``ignore_missing_imports`` silences that, and the whole module collapses to
# ``Any`` -- so ``tomllib.load`` and ``tomllib.TOMLDecodeError`` stop being typechecked at all
# (a typo in the ``except`` clause would only surface on a user's malformed TOML). The gate
# below keeps real typeshed/``tomli`` types on every interpreter. Both clause headers carry
# the pragma because coverage excludes a *clause*, not a whole statement: exactly one branch
# runs per interpreter, so without both, every matrix leg reports the other one as a miss.
if sys.version_info >= (3, 11):  # pragma: no cover - version-gated import, 3.11+ leg
    import tomllib
else:  # pragma: no cover - version-gated import, 3.10 leg (tomllib is not stdlib there)
    import tomli as tomllib

logger = logging.getLogger(__name__)

#: URL schemes that mark a ``from`` value as a network source rather than a filesystem path.
#: Declared here because the config layer is the first place a ``from`` value has to be
#: classified (a URL must not be anchored to the config file's directory), and
#: :mod:`castiron.cli.gen` imports it so exactly one rule decides for both.
URL_SCHEMES: tuple[str, ...] = ('http', 'https')

#: The nested table castiron reads, in **every** file — including a standalone one passed
#: to ``--config``. One rule, zero ambiguity, and the block copy-pastes between files.
CONFIG_TABLE: tuple[str, str] = ('tool', 'castiron')

#: The file the discovery walk looks for.
PYPROJECT_FILENAME = 'pyproject.toml'

#: Sub-tables that are parsed, validated as tables, and otherwise ignored by ``gen``.
#: ``check`` is reserved for CI-021, which gives it meaning.
RESERVED_TABLES: frozenset[str] = frozenset({'check'})

#: Keys that must never appear in a config file, whatever the file is called.
FORBIDDEN_KEYS: frozenset[str] = frozenset({'key'})

_STRING = 'string'
_STRING_ARRAY = 'array of strings'
_NUMBER = 'number'
_BOOLEAN = 'boolean'

#: Canonical (underscored) config key → (click parameter name, expected TOML type).
CONFIG_KEYS: dict[str, tuple[str, str]] = {
    'from': ('source', _STRING),
    'emit': ('emit', _STRING_ARRAY),
    'output': ('output', _STRING),
    'filename': ('filename', _STRING),
    'schema': ('schema', _STRING),
    'timeout': ('timeout', _NUMBER),
    'overwrite': ('overwrite', _BOOLEAN),
    'infer_generated_primary_keys': ('infer_generated_primary_keys', _BOOLEAN),
    'crud_models': ('crud_models', _BOOLEAN),
    'enums': ('enums', _BOOLEAN),
    'foreign_keys': ('foreign_keys', _BOOLEAN),
    'null_parent_classes': ('null_parent_classes', _BOOLEAN),
    'singular_names': ('singular_names', _BOOLEAN),
    'model_prefix_protection': ('model_prefix_protection', _BOOLEAN),
}

#: Keys whose value is a filesystem path. Resolved against the **config file's own
#: directory**, not the process cwd (decision CI6-D5a) -- the way ruff, mypy and coverage all
#: read their config. The config file exists so CI and local runs share one source of truth,
#: which it cannot do if ``output = "src/myapp/models"`` means a different directory
#: depending on where you happened to stand; and CI-021's ``check`` must not give a
#: cwd-dependent answer. A ``from`` that is a URL is never anchored.
PATH_KEYS: frozenset[str] = frozenset({'from', 'output'})


class ConfigError(click.ClickException):
    """A ``[tool.castiron]`` table castiron cannot use (exit code 1)."""


def looks_like_url(value: str) -> bool:
    """Whether ``value`` is a network source rather than a filesystem path.

    ⚠ **Never raises, and that is the point (CI-089).** ``urlsplit`` raises a bare ``ValueError``
    on a malformed URL -- ``urlsplit('http://[::1')`` is ``ValueError: Invalid IPv6 URL`` -- and
    this predicate runs *before* the source is chosen, so it was the **first** thing a typo'd URL
    hit. The result was exit **70** with "this is a bug in castiron, please report it at
    ...issues": a user who mistyped a bracket was told to open an issue. Same argument as
    :func:`~castiron.cli.errors.reject_url_userinfo` and :func:`~castiron.cli.errors.redact` --
    malformed input must degrade to a yes/no, never to a raise (CI-066-D1).

    A raising ``urlsplit`` falls back to splitting the scheme off by hand, which is what
    ``reject_url_userinfo`` does for the same reason. That is total: every input ``urlsplit``
    rejects has a ``//`` (both raising branches sit behind ``url[:2] == '//'`` after the scheme is
    stripped), so the ``'://'`` test cannot miss one. Answering **True** for ``http://[::1`` is
    deliberate -- it is a network source with a broken URL, not a filesystem path, so it reaches
    ``normalize_postgrest_url`` and fails as a :class:`~castiron.sources.SourceFetchError` naming
    the URL at exit 1.

    Args:
        value: The ``--from`` value, a URL or a path.

    Returns:
        Whether ``value`` names a scheme castiron fetches over the network.
    """
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        scheme = value.split('://', 1)[0].lower() if '://' in value else ''
    return scheme in URL_SCHEMES


def canonical_key(key: str) -> str:
    """Return the underscored spelling of a config key (dashes and underscores agree)."""
    return key.replace('-', '_')


def display_key(key: str) -> str:
    """Return the documented, dashed spelling of a canonical config key."""
    return key.replace('_', '-')


def valid_config_keys() -> list[str]:
    """Return every accepted ``[tool.castiron]`` key in its documented spelling."""
    return sorted(display_key(key) for key in [*CONFIG_KEYS, *RESERVED_TABLES])


def discover_config_file(start: Path) -> Path | None:
    """Return the nearest ``pyproject.toml`` at or above ``start``, or ``None``.

    The **first** hit wins, whether or not it carries a ``[tool.castiron]`` table: one
    ``pyproject.toml`` defines the project, and continuing the walk could silently inherit a
    parent monorepo's settings.

    Args:
        start: The directory to begin the upward walk from (normally the cwd).

    Returns:
        The nearest ``pyproject.toml``, or ``None`` if there is none.
    """
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / PYPROJECT_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config_table(path: Path, *, explicit: bool) -> dict[str, Any]:
    """Read and validate the ``[tool.castiron]`` table from ``path``.

    Args:
        path: The TOML file to read.
        explicit: Whether the user named this file with ``--config``/``CASTIRON_CONFIG``.
            An explicit file with no ``[tool.castiron]`` table is an error — silently using
            nothing from a file you asked for by name is worse than failing.

    Returns:
        The click ``default_map`` fragment: click parameter names → validated values, with
        every :data:`PATH_KEYS` value anchored to ``path``'s own directory (CI6-D5a).

    Raises:
        ConfigError: The file is not valid TOML or cannot be read, the table is missing
            (when ``explicit``), a key is unknown or mistyped, an ``emit`` entry names no
            registered emitter, or the forbidden ``key`` entry is present.
    """
    document = _read_toml(path)
    table = _castiron_table(path, document)
    if table is None:
        if explicit:
            raise ConfigError(
                f'{path} has no [tool.castiron] table. Add one, or drop --config to use the '
                f'nearest {PYPROJECT_FILENAME}.'
            )
        logger.debug(f'{path} has no [tool.castiron] table; using built-in defaults')
        return {}

    defaults: dict[str, Any] = {}
    for raw_key, value in table.items():
        key = canonical_key(raw_key)
        if key in FORBIDDEN_KEYS:
            raise ConfigError(
                f"{path}: [tool.castiron] must not contain '{raw_key}': {PYPROJECT_FILENAME} is "
                'committed. Pass --key or set CASTIRON_KEY.'
            )
        if key in RESERVED_TABLES:
            _require_table(path, raw_key, value)
            logger.debug(f'{path}: [tool.castiron.{raw_key}] is reserved for `castiron check`; ignoring it here')
            continue
        if key not in CONFIG_KEYS:
            raise ConfigError(_unknown_key_message(path, raw_key, key))
        param, expected = CONFIG_KEYS[key]
        coerced = _coerce(path, raw_key, expected, value)
        if key == 'emit':
            _require_registered_emitters(path, raw_key, coerced)
        if key in PATH_KEYS:
            coerced = anchor_path(path.parent, coerced)
        defaults[param] = coerced

    return defaults


def anchor_path(base: Path, value: str) -> str:
    """Resolve a config-file path value against ``base`` (the config file's directory).

    Args:
        base: The directory holding the config file the value came from.
        value: The raw ``from``/``output`` string.

    Returns:
        ``value`` unchanged when it is a URL or already absolute; otherwise joined onto
        ``base``. Symlinks are deliberately **not** resolved: an auto-discovered config file
        is already an absolute path, and an explicit ``--config ../other/pyproject.toml``
        should stay relative so the summary keeps printing short paths.
    """
    if looks_like_url(value):
        return value
    candidate = Path(value)
    if candidate.is_absolute():
        return value
    return str(base / candidate)


def resolve_config(explicit: Path | None, *, start: Path) -> tuple[Path | None, dict[str, Any]]:
    """Resolve the config file to use and the click ``default_map`` it produces.

    Args:
        explicit: The path given via ``--config``/``CASTIRON_CONFIG``, if any.
        start: The directory the discovery walk starts from when there is no explicit path.

    Returns:
        The config file actually used (``None`` when there is none) and its ``default_map``.

    Raises:
        ConfigError: The resolved file is unusable (see :func:`load_config_table`).
    """
    if explicit is not None:
        return explicit, load_config_table(explicit, explicit=True)
    discovered = discover_config_file(start)
    if discovered is None:
        return None, {}
    return discovered, load_config_table(discovered, explicit=False)


def config_option_callback(ctx: click.Context, param: click.Parameter, value: Path | None) -> Path | None:
    """Eager ``--config`` callback: load the config file into ``ctx.default_map``.

    click's ``iter_params_for_processing`` sorts eager parameters ahead of every non-eager
    one, so this has populated ``ctx.default_map`` before any other option resolves its
    value — which is the whole precedence mechanism.

    Args:
        ctx: The click context whose ``default_map`` is populated.
        param: The ``--config`` parameter (unused; part of click's callback contract).
        value: The explicit config path, or ``None``.

    Returns:
        The config file actually used — explicit or discovered — or ``None``, so the
        command body can report it under ``-v``.
    """
    del param  # click's callback contract; the parameter itself carries no information here
    if ctx.resilient_parsing:
        return value
    used, defaults = resolve_config(value, start=Path.cwd())
    if defaults:
        ctx.default_map = {**(ctx.default_map or {}), **defaults}
    return used


def _read_toml(path: Path) -> dict[str, Any]:
    """Parse ``path`` as TOML, or raise :class:`ConfigError`."""
    try:
        with path.open('rb') as handle:
            # Bound to an annotated local deliberately: whichever branch of the version gate
            # mypy analyses, ``tomllib`` degrades to ``Any`` in any environment where that
            # package is absent, and returning the call directly would then trip
            # ``warn_return_any`` (part of ``--strict``).
            document: dict[str, Any] = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f'{path} is not valid TOML: {exc}') from exc
    except OSError as exc:
        raise ConfigError(f'Could not read {path}: {exc}') from exc
    return document


def _castiron_table(path: Path, document: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``[tool.castiron]`` table, ``None`` when absent."""
    node: Any = document
    for part in CONFIG_TABLE:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    if not isinstance(node, dict):
        actual = _toml_type_name(node)
        raise ConfigError(f'{path}: [tool.castiron] must be a table, but it is {_article(actual)} {actual}.')
    table: dict[str, Any] = node
    return table


def _unknown_key_message(path: Path, raw_key: str, key: str) -> str:
    """Build the unknown-key error, with a "did you mean" and the full valid list."""
    close = difflib.get_close_matches(key, CONFIG_KEYS, n=1)
    suggestion = f" Did you mean '{display_key(close[0])}'?" if close else ''
    return (
        f"{path}: unknown key '{raw_key}' in [tool.castiron].{suggestion} Valid keys: {', '.join(valid_config_keys())}."
    )


def _require_table(path: Path, raw_key: str, value: Any) -> None:
    """Validate that a reserved key holds a table."""
    if not isinstance(value, dict):
        actual = _toml_type_name(value)
        raise ConfigError(
            f"{path}: [tool.castiron] '{raw_key}' must be a table, but it is {_article(actual)} {actual}. "
            'It is reserved for `castiron check`.'
        )


def _require_registered_emitters(path: Path, raw_key: str, names: list[str]) -> None:
    """Validate every ``emit`` entry against the registry, naming the file (CI6-D6).

    click's ``Choice`` would also reject an unknown name, but as a **usage** error (exit 2)
    that never mentions the config file -- and a typo in a committed ``pyproject.toml`` is
    exactly the case the config layer exists to diagnose well.
    """
    unknown = [name for name in names if name not in EMITTERS]
    if unknown:
        raise ConfigError(
            f"{path}: [tool.castiron] '{raw_key}' names no registered emitter: "
            f'{", ".join(repr(name) for name in unknown)}. '
            f'Registered emitters: {", ".join(sorted(EMITTERS))}.'
        )


def _coerce(path: Path, raw_key: str, expected: str, value: Any) -> Any:
    """Validate a config value against its expected TOML type and normalize it.

    The value itself is never echoed back: a ``from`` URL can carry a credential in its
    query string, and the type name is what the user needs anyway.
    """
    if expected == _BOOLEAN and isinstance(value, bool):
        return value
    if expected == _STRING and isinstance(value, str):
        return value
    if expected == _NUMBER and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if expected == _STRING_ARRAY and isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(
        f"{path}: [tool.castiron] '{raw_key}' must be {_article(expected)} {expected}, "
        f'but it is {_article(_toml_type_name(value))} {_toml_type_name(value)}.'
    )


def _article(noun: str) -> str:
    """Return ``'an'`` or ``'a'`` to suit ``noun``."""
    return 'an' if noun[0] in 'aeiou' else 'a'


def _toml_type_name(value: Any) -> str:
    """Return the TOML type name of a parsed value (``bool`` before ``int``, deliberately)."""
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'table'
    return type(value).__name__
