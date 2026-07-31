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
from pathlib import Path
from typing import Any

import click

# ``tomllib`` is stdlib from 3.11; the 3.10 leg installs the marker-gated ``tomli`` (CI6-D1).
# The try/except form rather than ``if sys.version_info``: on 3.10 the ``import tomllib`` line
# still *executes* (it raises), so both matrix legs report 100% coverage from one pragma,
# whereas a version check leaves the 3.11+ branch permanently unreached on 3.10.
try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - only on the 3.10 matrix leg, where tomllib is absent
    import tomli as tomllib

logger = logging.getLogger(__name__)

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


class ConfigError(click.ClickException):
    """A ``[tool.castiron]`` table castiron cannot use (exit code 1)."""


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
        The click ``default_map`` fragment: click parameter names → validated values.

    Raises:
        ConfigError: The file is not valid TOML or cannot be read, the table is missing
            (when ``explicit``), a key is unknown or mistyped, or the forbidden ``key``
            entry is present.
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
        defaults[param] = _coerce(path, raw_key, expected, value)

    return defaults


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
            # Bound to an annotated local deliberately: at ``python_version = "3.10"`` mypy
            # analyses the ``tomli`` branch above, and returning the call directly would trip
            # ``warn_return_any`` whenever that package resolves to ``Any``.
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
        raise ConfigError(f'{path}: [tool.castiron] must be a table, but it is a {_toml_type_name(node)}.')
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
        raise ConfigError(
            f"{path}: [tool.castiron] '{raw_key}' must be a table, but it is a {_toml_type_name(value)}. "
            'It is reserved for `castiron check`.'
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
