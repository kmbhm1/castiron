"""Source-neutral type resolution: raw type token -> resolved Python type + imports.

This is a faithful port of supabase-pydantic's ``adapt_type_map`` (array-suffix
handling + map lookup + default fallback) and the array-element branch of
``process_udt_field``, re-expressed as a small, mypy-strict-friendly
:class:`TypeResolution` value instead of supabase-pydantic's ``tuple[str, str | None]``
with newline-joined import blobs.

Two deliberate design choices (per the CI-004 spec):

- Imports are carried as a tuple of **single import lines**, so an emitter can
  assemble one flat, deduplicated import set rather than splicing multi-line blobs.
  Single lines are also what lets ``emitters.base.render_import_block`` regroup and
  merge them deterministically (Hard Rule #9, CI-094); a blob could not be re-sectioned.
- Array-element resolution keys off :attr:`castiron.ir.ColumnInfo.array_element_type`
  (the IR field) rather than the dropped ``udt_name``. The Postgres type map carries
  both vocabularies (``int4`` and ``integer``, ``timestamptz`` and ``timestamp with
  time zone``), so the two are type-equivalent (see the CI4-Q3 parity test).

This module stays free of ``pydantic``/``inflection`` imports: it maps to type
*strings*, and each emitter supplies its own target map.
"""

from dataclasses import dataclass

from castiron.ir import ColumnInfo


@dataclass(frozen=True)
class TypeResolution:
    """A resolved target type plus the imports its use requires.

    Attributes:
        python_type: The rendered type expression, e.g. ``'int'``, ``'list[int]'`` or
            ``'dict | list[dict] | list[Any] | Json'``.
        imports: Each element is one import line, e.g. ``('import datetime',)`` or
            ``('from typing import Any', 'from pydantic import Json')``.
    """

    python_type: str
    imports: tuple[str, ...] = ()


TypeMap = dict[str, TypeResolution]

#: The fallback resolution for an unknown type token: ``Any`` plus its import.
DEFAULT_RESOLUTION = TypeResolution('Any', ('from typing import Any',))


def _clean_element(name: str) -> str:
    """Normalize an array-element type token for a map lookup.

    Strips Postgres array decoration -- leading underscores (``_int4``), a trailing
    ``[]`` (``test_status[]``), and surrounding double quotes (``"FourthType"``) --
    then lowercases the result.

    Args:
        name: The raw array-element token (from ``array_element_type`` or a ``[]`` type).

    Returns:
        The cleaned, lowercased token ready for a type-map lookup.
    """
    name = name.strip()
    while name.startswith('_'):
        name = name[1:]
    if name.endswith('[]'):
        name = name[:-2]
    if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
        name = name[1:-1]
    return name.lower()


def resolve(raw_type: str, type_map: TypeMap, default: TypeResolution = DEFAULT_RESOLUTION) -> TypeResolution:
    """Resolve a raw type token to a :class:`TypeResolution` via ``type_map``.

    Ports ``adapt_type_map``: a ``[]`` suffix wraps the base lookup in ``list[...]``;
    otherwise a case-insensitive map lookup is used, falling back to ``default``.

    Args:
        raw_type: The raw source type token (e.g. ``'integer'`` or ``'integer[]'``).
        type_map: The target type map to look the token up in.
        default: The resolution to use when the token is not in ``type_map``.

    Returns:
        The resolved :class:`TypeResolution`.
    """
    if raw_type.endswith('[]'):
        base = _clean_element(raw_type)
        base_resolution = type_map.get(base, default)
        return TypeResolution(f'list[{base_resolution.python_type}]', base_resolution.imports)
    return type_map.get(raw_type.lower(), default)


def resolve_column_type(
    col: ColumnInfo, type_map: TypeMap, default: TypeResolution = DEFAULT_RESOLUTION
) -> TypeResolution:
    """Resolve a column's Python type via ``type_map``, handling arrays.

    Ports the array branch of ``process_udt_field`` but keyed on
    :attr:`castiron.ir.ColumnInfo.array_element_type` (the IR field) rather than the
    dropped ``udt_name``. Enum overlay is intentionally *not* applied here -- an emitter
    overlays the enum class from ``col.enum_info`` so this resolver stays source- and
    emitter-neutral.

    Args:
        col: The column to resolve.
        type_map: The target type map (e.g. the Pydantic map).
        default: The resolution to use for an unknown token or element.

    Returns:
        The resolved :class:`TypeResolution` (a ``list[...]`` for array columns).
    """
    raw = col.raw_type
    if raw.lower() == 'array' or raw.lower().endswith('[]'):
        if col.array_element_type:
            element = _clean_element(col.array_element_type)
        elif raw.lower().endswith('[]'):
            element = _clean_element(raw)
        else:
            element = ''
        base_resolution = type_map.get(element, default) if element else default
        return TypeResolution(f'list[{base_resolution.python_type}]', base_resolution.imports)
    return resolve(raw, type_map, default)
