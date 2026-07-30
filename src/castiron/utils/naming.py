"""Python-identifier naming helpers for emitters.

Ports supabase-pydantic's ``to_pascal_case`` and ``EnumInfo.python_class_name`` /
``python_member_name`` (moved off the IR onto this row per CI-003 D8), plus thin
``pluralize`` / ``singularize`` wrappers over ``inflection``. Keeping ``inflection``
behind a single call site here isolates the runtime dependency (eases a future move to
an optional extra).
"""

import inflection

from castiron.ir import EnumInfo


def to_pascal_case(value: str) -> str:
    """Convert a snake_case (or single-word) string to PascalCase.

    Args:
        value: The string to convert, e.g. ``'order_status'``.

    Returns:
        The PascalCase form, e.g. ``'OrderStatus'``.
    """
    return ''.join(word.capitalize() for word in value.split('_'))


def python_class_name(enum: EnumInfo) -> str:
    """Build a PascalCase, schema-prefixed, ``Enum``-suffixed class name for an enum.

    Handles snake_case (``order_status`` -> ``OrderStatusEnum``), camelCase
    (``thirdType`` -> ``ThirdTypeEnum``), PascalCase (``FourthType`` -> ``FourthTypeEnum``),
    a leading underscore (``_first_type`` -> ``FirstTypeEnum``), and the empty-name edge.
    The final name is prefixed by the (capitalized) schema, e.g.
    ``public.order_status`` -> ``PublicOrderStatusEnum``.

    Args:
        enum: The enum whose Python class name to build.

    Returns:
        The Python enum class name.
    """
    if not enum.name:
        return f'{enum.schema.capitalize()}Enum'

    clean_name = enum.name
    if clean_name.startswith('_'):
        clean_name = clean_name[1:]

    if '_' not in clean_name and any(c.isupper() for c in clean_name):
        class_name = clean_name[0].upper() + clean_name[1:] + 'Enum'
    else:
        class_name = ''.join(word.capitalize() for word in clean_name.split('_')) + 'Enum'

    return f'{enum.schema.capitalize()}{class_name}'


def python_member_name(value: str) -> str:
    """Return the lowercased base member name for an enum value.

    The emitter uppercases and reserved-name-guards this before emitting.

    Args:
        value: The raw enum value, e.g. ``'Pending_New'``.

    Returns:
        The lowercased base name, e.g. ``'pending_new'``.
    """
    return value.lower()


def pluralize(word: str) -> str:
    """Return the plural form of ``word`` (via ``inflection``)."""
    result: str = inflection.pluralize(word)
    return result


def singularize(word: str) -> str:
    """Return the singular form of ``word`` (via ``inflection``)."""
    result: str = inflection.singularize(word)
    return result
