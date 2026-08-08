"""Python-identifier naming helpers for emitters.

Ports supabase-pydantic's ``to_pascal_case`` and ``EnumInfo.python_class_name`` (moved off
the IR onto this row per CI-003 D8), plus thin ``pluralize`` / ``singularize`` wrappers over
``inflection``. Keeping ``inflection`` behind a single call site here isolates the runtime
dependency (eases a future move to an optional extra).

⚠ **Enum member naming is deliberately NOT a port.** upstream's ``python_member_name`` is
``value.lower()`` (``supabase_pydantic/core/models.py:42-46``), which is not identifier-safe:
``CREATE TYPE t AS ENUM ('in progress')`` emitted ``IN PROGRESS = "in progress"`` and the whole
module failed to parse, at exit 0. There is no upstream design to port, so :func:`python_member_names`
below is castiron's own (CI-080).
"""

import unicodedata
from dataclasses import dataclass

import inflection

from castiron.ir import EnumInfo
from castiron.ir.build import column_name_reserved_exceptions, identifier_characters, string_is_reserved


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


@dataclass(frozen=True)
class EnumMember:
    """One emitted enum member: the schema's label and the identifier castiron derives from it.

    Attributes:
        label: The Postgres enum label, verbatim. This is the ground truth, and it is what the
            emitted value literal carries -- so the transform below is never lossy, however much
            it mangles the name.
        name: The Python identifier. Guaranteed ``name.isidentifier()``, guaranteed **usable as a
            member of a ``str``-mixin ``Enum``** (a strictly stronger claim -- see
            :func:`_is_enum_reserved_shape`), and guaranteed unique within one
            :func:`python_member_names` call **after NFKC normalization**.

            🔴 The ``Enum`` half of that guarantee has **one known exception, filed as CI-113**,
            and it is stated here because a guarantee without its limits is the sentence
            ``CI-077`` exists to punish. CPython's ``_is_private`` takes the **enclosing class
            name** and also swallows a member already spelled ``_<ClassName>__x``. This function
            does not receive the class name, so it cannot see that shape.

            ⚠ **It is reachable, and an earlier revision of this docstring claimed the opposite
            "(verified)".** That claim was wrong. It reasoned that ``str.upper()`` cannot produce
            the lowercase letters a class name needs -- true -- and missed that **NFKC runs
            after** it, in the compiler. **389** codepoints the character map keeps are
            ``upper()``-invariant *and* NFKC-fold to an ASCII lowercase letter (``ª``->``a``,
            ``ᵘ``->``u``, ``ⁿ``->``n``, ...), covering all 26 letters, so any class name is
            spellable. Driven through the real emitter: the label
            ``'_PᵘᵇˡᵢᶜOʳᵈᵉʳSᵗªᵗᵘˢEⁿᵘᵐ__X'`` normalizes to ``_PublicOrderStatusEnum__X``, and
            ``castiron gen`` exits **0** while py3.10 keeps the member and py3.11+ **silently drop
            the label** -- both ``CI94-Q1``'s "never drop a variant" and Hard Rule #9's
            interpreter-independence, at once.

            **The true condition:** reachable only when a label's NFKC form spells
            ``_<ClassName>__…`` for the class it is emitted into. **Unreachable from ASCII-only
            labels**, because the generated suffix ``Enum`` always contributes lowercase ``num``
            that ``.upper()`` would destroy -- so it takes deliberately-crafted modifier-letter
            Unicode. Left open rather than closed in code: threading the class name through would
            be a signature change on the last row before an immutable publish, for an adversarial
            input. Pinned as present by ``TestCi113`` in ``tests/unit/utils/test_naming.py``, the
            way ``CI-085`` and ``CI-100`` are pinned, so the gap stays visible.
        note: Why ``name`` is not the straight transform of ``label`` -- ``'reserved by Enum'``,
            ``'reserved keyword'`` or ``'name collision'`` -- or ``None`` when it is. When more
            than one applies (labels ``['import_', 'import']``, where the second is renamed
            twice), the **last** one wins: it is what explains the final trailing character a
            reader is looking at, and the label itself is on the same line either way.
    """

    label: str
    name: str
    note: str | None = None


def _is_enum_reserved_shape(name: str) -> bool:
    """Whether ``name`` is unusable as an ``Enum`` member, even though it is a valid identifier.

    ``str.isidentifier()`` is necessary and **not sufficient**. Three *shapes* are reserved on
    top of Python's identifier rules, and :func:`~castiron.ir.build.identifier_characters` produces all three
    freely -- from labels as ordinary as a **trailing space** nobody noticed in a ``CREATE TYPE``:

    * ``_sunder_`` (``'(none)'`` -> ``_NONE_``) -- ``ValueError`` when the class body runs, so
      the **whole emitted module** is unusable at import.
    * ``__dunder__`` (``'__init__'`` -> ``__INIT__``) -- the member is **silently dropped**.
    * **private / name-mangled** (``'' , ' '`` colliding -> ``_``, ``__2``) -- Python mangles any
      name with two leading underscores and fewer than two trailing ones, at *compile* time, so
      the member never has the name castiron wrote. ⚠ This one is produced by the **collision
      rule itself**, and its behaviour is **interpreter-dependent**: 3.11+ drop the label
      entirely, while 3.10 emits a member called ``_<ClassName>__2`` -- and if the mangled form
      then happens to be sunder (``__2_`` -> ``_E__2_``) 3.10 raises instead. Output whose
      meaning depends on the running interpreter is a Hard Rule #9 problem on top of a
      correctness one.

    All three are CI-080's failure mode relocated, not closed: ``compile()`` passes and
    ``castiron gen`` still exits 0. Two of them violate ``CI94-Q1``'s one non-negotiable
    ("never drop a variant") outright.

    ⚠ **Deliberately re-implemented rather than importing ``enum._is_sunder`` /
    ``enum._is_dunder`` / ``enum._is_private``**, which are private, unguaranteed, and demonstrably
    version-skewed (3.13 dropped a clause from ``_is_private``; 3.10 words its error differently).
    This is the ``CI94-D8`` pattern PR #15 used for ``STDLIB_MODULES``: state the rule explicitly
    here so emitted bytes never depend on a CPython internal, and *cross-check it against the
    interpreter in a test* that runs on all four gate legs.

    🔴 **The test is applied to the NFKC-normalized name, and that is what makes it total rather
    than a list of shapes someone thought of.** CPython normalizes identifiers to NFKC **at
    compile time**, so the name ``Enum`` actually receives is ``NFKC(name)``, not what castiron
    wrote. **Seven** identifier-continue codepoints normalize to ``'_'`` and six of them are
    non-ASCII (``U+FF3F`` FULLWIDTH LOW LINE, ``U+FE33``, ``U+FE34``, ``U+FE4D``, ``U+FE4E``,
    ``U+FE4F``); ``identifier_characters`` **keeps** them by ``CI94-D2`` and ``str.upper()``
    leaves them alone. So ``'_x＿'`` -> ``_X＿``, which is not sunder by inspection and
    *is* sunder to the compiler -- it raised ``ValueError: _sunder_ names, such as '_X_'``,
    printing the **normalized** name, which is the interpreter telling us plainly which string it
    judged.

    Normalizing first means this predicate sees the same *string* the compiler sees, **by
    construction**, so no further shape waits on the **spelling** axis. That is the whole reason
    it is done here rather than by adding a fourth special case: the same mechanism is already
    load-bearing for the uniqueness key in :func:`python_member_names`, and it belongs on both
    consumers or neither.

    ⚠ **"Sees what the compiler sees" is true modulo the ENCLOSING CLASS NAME, which ``Enum``
    also consults, and this predicate does not receive.** An earlier revision said it
    unqualified, in this docstring, which is where a maintainer reads it. ``_is_private`` swallows
    a member already spelled ``_<ClassName>__x``; that shape is **filed as CI-113**, reachable
    only from deliberately-crafted Unicode, and pinned as present by a test rather than fixed
    here. So: total over spelling, **not** total over the (name, class name) pair.

    Args:
        name: A candidate member name, already known to be a valid identifier.

    Returns:
        ``True`` when ``Enum`` would reject, rename or swallow ``name``.
    """
    name = unicodedata.normalize('NFKC', name)
    sunder = len(name) > 2 and name[0] == name[-1] == '_' and name[1] != '_' and name[-2] != '_'
    dunder = len(name) > 4 and name[:2] == name[-2:] == '__' and name[2] != '_' and name[-3] != '_'
    private = name.startswith('__') and not name.endswith('__')
    return sunder or dunder or private


def _repair_enum_shape(name: str) -> str:
    """Append ``'_'`` until ``name`` is usable as an ``Enum`` member.

    **Terminates in at most three appends, and that bound is a proof rather than an observation.**
    A name whose NFKC form ends in three or more underscores can be none of the three shapes:
    sunder needs ``name[-2] != '_'``, dunder needs ``name[-3] != '_'``, and private needs the name
    *not* to end ``'__'``. Every iteration adds one trailing underscore, so the loop cannot run
    more than three times. (``__2`` is the worst case and needs all three: ``__2_`` is still
    private, ``__2__`` is then dunder, ``__2___`` is finally clean.)

    ⚠ **One append is NOT enough** -- a claim that used to appear on
    :func:`python_member_names` and was wrong. It holds for sunder and dunder and fails for the
    private shape, which is exactly the one that arrives via the collision suffix.

    The proof survives NFKC because the appended character is ASCII ``'_'``, which is
    NFKC-invariant: appending cannot change how the rest of the name normalizes, so each pass
    adds exactly one underscore to the normalized form too.

    Appending rather than prefixing is deliberate: a prefix would create *more* leading
    underscores and walk further into the mangled shape, and it would also break the alignment
    between a member name and the label a reader is looking at.

    Args:
        name: A candidate member name.

    Returns:
        ``name``, with as many trailing underscores as it takes.
    """
    while _is_enum_reserved_shape(name):
        name = f'{name}_'
    return name


def python_member_names(enum: EnumInfo) -> list[EnumMember]:
    """Derive one unique, valid Python identifier per label, in ``enum.values`` order.

    Takes the IR's :class:`~castiron.ir.EnumInfo` (Hard Rule #6 -- it already carries the ordered
    labels; there is no second enum shape) and returns one entry per label, **positionally
    aligned with** ``enum.values``. Nothing is ever dropped or merged: a label that cannot keep
    its natural name gets a suffixed one, and its value literal still carries the label exactly.

    The algorithm, in this order. **The order is load-bearing.**

    1. Map every character to an identifier-legal one
       (:func:`~castiron.ir.build.identifier_characters`, **shared** with the column path --
       ``CI85-D1`` moved it there so one algorithm serves both).
    2. Uppercase, which is what keeps ``PENDING``/``ACTIVE``/``OK`` byte-identical to what
       castiron has always emitted.
    3. Empty guard -- ``CREATE TYPE t AS ENUM ('')`` is legal Postgres; it becomes ``_``.
    4. Leading-digit guard -- ``'2nd pass'`` becomes ``_2ND_PASS``. A leading underscore is not
       name-mangled and is addressable as ``E._2ND_PASS``; whether it is *also* a ``_sunder_``
       name is step 5's problem, not this step's.
    5. Enum-shape guard (:func:`_repair_enum_shape`) -- append ``'_'`` while the name is
       ``_sunder_``, ``__dunder__`` or **private/name-mangled**. ``str.isidentifier()`` is
       **necessary but not sufficient**: ``Enum`` raises on the first and silently drops the other
       two.
    6. Reserved guard, **carried over verbatim from the emitter** (see the note below).
    7. Uniquify **last**, over the final names, keyed on the NFKC-normalized candidate -- and
       every candidate is re-repaired, because the ordinal suffix can itself create shape 3.

    ⚠ **Steps 5 and 7 both normalize before testing, and step 5 may need up to THREE appends.**
    An earlier revision of this docstring claimed "one append is provably enough"; that was true
    of sunder and dunder and **false** of the private shape, which is the one the collision suffix
    produces. :func:`_repair_enum_shape` loops, and its bound is proved there -- do not replace it
    with a single ``+ '_'``.

    ⚠ **Step 7 must key on the NFKC form, and this is not pedantry** (``CI94-Q1``, ruled). Python
    NFKC-normalizes identifiers at compile time, so two distinct strings can be one binding:
    ``ﬁ = 1; fi = 2`` leaves a single name worth ``2``. ``str.isidentifier()`` does not catch it
    (``'ﬁ'.isidentifier()`` is ``True``) and ``str.upper()`` performs the same folding
    (``'ﬁ'.upper() == 'FI'``, ``'ß'.upper() == 'SS'``). A raw uniqueness check would emit a module
    that raises ``TypeError`` at **import** -- CI-080's own failure mode in a new costume.

    ⚠ **Step 6 reads the exemption list the same way the column path does** --
    ``string_is_reserved(...) and not column_name_reserved_exceptions(...)``, matching
    ``ir/build.py``'s ``standardize_column_name``. It was ``or`` until ``CI-100``, which treated an
    *exemption* list as an *addition* list: the label ``id`` emitted
    ``ID_ = "id"  # original name was "id" (reserved keyword)``, a comment stating the opposite of
    the truth. Note what the ``or`` did **not** do -- every name on the exemption list is already a
    builtin, so it never actually added anything; its only observable effect was the missing
    exemption.

    Determinism (Hard Rule #9): the result is a pure function of the ordered ``enum.values``
    list. That order is contractual (pg's ``enumsortorder``, preserved end to end by the OpenAPI
    source). No set iteration and no dict ordering reaches the output; the collision counter walks
    ``2, 3, 4, …`` deterministically.

    Args:
        enum: The enum whose members to name.

    Returns:
        One :class:`EnumMember` per label, in ``enum.values`` order.
    """
    used: set[str] = set()
    members: list[EnumMember] = []

    for label in enum.values:
        name = identifier_characters(label).upper()
        if not name:
            name = '_'
        if not name.isidentifier():
            name = f'_{name}'

        note: str | None = None
        if _is_enum_reserved_shape(name):
            name = _repair_enum_shape(name)
            note = 'reserved by Enum'
        if string_is_reserved(name.lower()) and not column_name_reserved_exceptions(name.lower()):
            name = f'{name}_'
            note = 'reserved keyword'

        # ⚠ The ordinal suffix can itself create a reserved shape -- `_` plus `_2` is `__2`,
        # which Python name-mangles -- so every candidate is repaired again, and the loop
        # re-checks uniqueness afterwards rather than assuming the repair kept it unique.
        candidate = _repair_enum_shape(name)
        ordinal = 1
        while unicodedata.normalize('NFKC', candidate) in used:
            ordinal += 1
            candidate = _repair_enum_shape(f'{name}_{ordinal}')
            note = 'name collision'

        used.add(unicodedata.normalize('NFKC', candidate))
        members.append(EnumMember(label=label, name=candidate, note=note))

    return members


def pluralize(word: str) -> str:
    """Return the plural form of ``word`` (via ``inflection``)."""
    result: str = inflection.pluralize(word)
    return result


def singularize(word: str) -> str:
    """Return the singular form of ``word`` (via ``inflection``)."""
    result: str = inflection.singularize(word)
    return result
