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


def python_identifier(text: str) -> str:
    """Repair ``text`` into a valid Python identifier, keeping every character it legally can.

    **The shared identifier-repair primitive.** It is the *only* place the
    character-map-plus-leading-position repair is written; :func:`python_class_name` calls it, and
    ``CI-130`` (table / model class names, the same defect family) is expected to call it rather
    than fork it. Two rules, in this order:

    1. **Character map** -- :func:`~castiron.ir.build.identifier_characters`, one character out per
       character in, every non-identifier character replaced by ``'_'``, **Unicode kept**
       (``CI94-D2``), no run-collapsing and no stripping. Deliberately the *same* map the column
       path (:func:`~castiron.ir.build.standardize_column_name`) and the enum-label path
       (:func:`python_member_names`) use -- ``CI85-D1`` put it in ``ir/build.py`` precisely so
       there would be one algorithm and one set of bugs. This is its **third** consumer.
    2. **Leading-position guard.** After the map, every character is XID_Continue, so the only
       remaining failure is a first character that is not XID_Start -- a digit. One ``'_'`` prefix
       repairs it.

    ⚠ **The test is on the NFKC form**, because CPython normalizes identifiers to NFKC *at compile
    time*: the name the compiler judges is not always the string castiron wrote. Same reason
    :func:`_is_enum_reserved_shape` and ``ir/build.py``'s ``_repair_column_shape`` normalize.

    **One prefix is provably enough, and it is pinned rather than believed.** Every character
    surviving ``identifier_characters`` satisfies ``('_' + c).isidentifier()``, i.e. is
    XID_Continue; prefixing the XID_Start character ``'_'`` therefore yields an identifier, and
    ``'_'`` is itself NFKC-invariant so the prefix cannot change how the tail normalizes.
    ``TestPythonIdentifier`` falsifies that over a generated hostile alphabet rather than trusting
    the argument -- an identical "one pass is enough" claim was made on the enum **member** path
    and was wrong.

    **Total over its input domain**: it never raises and never returns a non-identifier. The empty
    string becomes ``'_'``. That case is unreachable from :func:`python_class_name` (the ``Enum``
    suffix is always appended, so the assembled string is never empty), but it is reachable from
    ``CI-130``'s table-name path, where an empty name is a different question.

    Args:
        text: The raw source name -- a Postgres type name, schema name, or assembled class name.

    Returns:
        A string ``s`` for which ``unicodedata.normalize('NFKC', s).isidentifier()`` holds.
    """
    repaired = identifier_characters(text)
    if not repaired:
        return '_'
    if not unicodedata.normalize('NFKC', repaired).isidentifier():
        repaired = f'_{repaired}'
    return repaired


def _assemble_class_name(schema: str, name: str) -> str:
    """PascalCase-assemble a schema-prefixed, ``Enum``-suffixed class name from clean parts.

    The transform itself, extracted verbatim from :func:`python_class_name` so that the sanitizing
    wrapper and the "was this name repaired?" predicate (:func:`_class_name_is_repaired`) share
    **one** assembly rather than each carrying a copy. Two derivations of one name is the defect
    ``CI-114`` was; this keeps there being exactly one.

    Args:
        schema: The enum's schema, already through :func:`~castiron.ir.build.identifier_characters`.
        name: The enum's type name, already through ``identifier_characters``.

    Returns:
        The assembled name -- **not** yet guaranteed to be a valid identifier (a leading-digit
        schema such as ``2fa`` survives the character map untouched).
    """
    if not name:
        return f'{schema.capitalize()}Enum'

    clean_name = name
    if clean_name.startswith('_'):
        clean_name = clean_name[1:]

    if '_' not in clean_name and any(c.isupper() for c in clean_name):
        class_name = clean_name[0].upper() + clean_name[1:] + 'Enum'
    else:
        class_name = ''.join(word.capitalize() for word in clean_name.split('_')) + 'Enum'

    return f'{schema.capitalize()}{class_name}'


def python_class_name(enum: EnumInfo) -> str:
    """Build a PascalCase, schema-prefixed, ``Enum``-suffixed class name for an enum.

    Handles snake_case (``order_status`` -> ``OrderStatusEnum``), camelCase
    (``thirdType`` -> ``ThirdTypeEnum``), PascalCase (``FourthType`` -> ``FourthTypeEnum``),
    a leading underscore (``_first_type`` -> ``FirstTypeEnum``), and the empty-name edge.
    The final name is prefixed by the (capitalized) schema, e.g.
    ``public.order_status`` -> ``PublicOrderStatusEnum``.

    🔴 **Both inputs are sanitized and the assembled name is repaired** (``CI-128``). Neither was,
    and both are raw Postgres text: PostgREST reports the pg type name in ``format`` verbatim, so
    ``CREATE TYPE "order status"`` emitted ``class PublicOrder statusEnum(str, Enum):`` --
    ``SyntaxError``, with ``castiron gen`` exiting **0**. The **schema** is the second input and is
    just as reachable (``CREATE SCHEMA "2fa"`` is legal and produced the leading-digit name
    ``2faMoodEnum``). Both now go through :func:`python_identifier`.

    **The transform is the identity on every name that already produced a valid identifier** --
    ``identifier_characters`` is the identity on such text and the leading-position guard does not
    fire, so no previously-valid output moves. That is the byte-stability property (Hard Rule #9)
    the committed goldens pin, and the five pre-existing ``TestPythonClassName`` cases are its
    unit-level pins.

    ⚠ **This is the per-name form, and it cannot see a collision** -- it does not know what other
    types exist. ``python_class_name`` is not injective (``order_status``, ``orderStatus``,
    ``OrderStatus``, ``Order_Status``, ``_order_status`` and ``ORDER_STATUS`` all map to
    ``PublicOrderStatusEnum``), and sanitization widens that set further. **A caller that owns a
    whole schema must use :func:`python_class_names`**, which allocates a unique name per type.
    Same division of labour, and same warning, as ``standardize_column_name`` vs
    ``column_identifiers`` in ``ir/build.py``.

    **No reserved-word guard is needed here**, unlike the member path: every name this returns ends
    in the literal ``Enum`` (both branches append it), so it can never be a Python keyword or
    builtin. The one degenerate case -- schema ``''`` and name ``''`` producing ``Enum``, which
    would shadow ``from enum import Enum`` -- is handled by :func:`python_class_names`' collision
    rule, which seeds itself with the module's import-bound names, not by a special case here.

    Args:
        enum: The enum whose Python class name to build.

    Returns:
        The Python enum class name, guaranteed to be a valid identifier after NFKC normalization.
    """
    return python_identifier(_assemble_class_name(identifier_characters(enum.schema), identifier_characters(enum.name)))


@dataclass(frozen=True)
class EnumMember:
    """One emitted enum member: the schema's label and the identifier castiron derives from it.

    Attributes:
        label: The Postgres enum label, verbatim. This is the ground truth, and it is what the
            emitted value literal carries -- so the transform below is never lossy, however much
            it mangles the name.
        name: The Python identifier. Guaranteed ``name.isidentifier()``, guaranteed **usable as a
            member of a ``str``-mixin ``Enum`` of the class name this enum is emitted under** (a
            strictly stronger claim -- see :func:`_is_enum_reserved_shape`), and guaranteed unique
            within one :func:`python_member_names` call **after NFKC normalization**.

            🔴 **The ``Enum`` half of that guarantee is over the ``(name, class name)`` pair, and
            the class name half of it is what ``CI-113`` closed.** CPython's ``_is_private`` takes
            the **enclosing class name** and swallows a member already spelled
            ``_<ClassName>__x``. :func:`python_member_names` now derives that class name itself
            via :func:`python_class_name` -- the same call :meth:`_enum_section` renders the
            header from -- so the predicate tests the pair, not the spelling alone.

            ⚠ **Why the guard is necessary, kept because it is the fact an earlier revision got
            wrong.** That revision called the shape "unreachable through the Pydantic emitter
            (verified)", reasoning that ``str.upper()`` cannot produce the lowercase letters a
            class name needs. True, and beside the point: **NFKC runs after** ``.upper()``, in the
            compiler. **389** codepoints the character map keeps are ``upper()``-invariant *and*
            NFKC-fold to an ASCII lowercase letter (``ª``->``a``, ``ᵘ``->``u``, ``ⁿ``->``n``,
            ...), covering **all 26** letters, so any class name is spellable. Driven through the
            real emitter, the label ``'_PᵘᵇˡᵢᶜOʳᵈᵉʳSᵗªᵗᵘˢEⁿᵘᵐ__X'`` normalizes to
            ``_PublicOrderStatusEnum__X``; before the fix ``castiron gen`` exited **0** while
            py3.10 kept the member and py3.11+ **silently dropped the label** -- both
            ``CI94-Q1``'s "never drop a variant" and Hard Rule #9's interpreter-independence, at
            once. It now repairs to ``_PublicOrderStatusEnum__X__`` and survives identically on
            3.10 / 3.11 / 3.12 / 3.13.

            **Exposure bound, still true and still worth stating:** the shape is **unreachable
            from ASCII-only labels**, because the generated suffix ``Enum`` always contributes
            lowercase ``num`` that ``.upper()`` would destroy -- it takes deliberately-crafted
            modifier-letter Unicode. Pinned by ``TestCi113TheClassNameAxisIsClosed`` in
            ``tests/unit/utils/test_naming.py``.
        note: Why ``name`` is not the straight transform of ``label`` -- ``'reserved by Enum'``,
            ``'reserved keyword'`` or ``'name collision'`` -- or ``None`` when it is. When more
            than one applies (labels ``['import_', 'import']``, where the second is renamed
            twice), the **last** one wins: it is what explains the final trailing character a
            reader is looking at, and the label itself is on the same line either way.
    """

    label: str
    name: str
    note: str | None = None


def _is_enum_reserved_shape(name: str, class_name: str) -> bool:
    """Whether ``name`` is unusable as an ``Enum`` member, even though it is a valid identifier.

    ``str.isidentifier()`` is necessary and **not sufficient**. Four *shapes* are reserved on
    top of Python's identifier rules, and :func:`~castiron.ir.build.identifier_characters` produces all four
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
    * **class-private** (``_<ClassName>__x``) -- the fourth shape, and the one ``CI-113`` closed.
      ``EnumMeta`` calls ``_is_private(cls_name, name)``, which treats any member already spelled
      ``_<ClassName>__…`` (and not ending ``__``) as a normal attribute rather than a member. It
      is the **only** shape that depends on something other than the name's own spelling, which is
      why ``class_name`` is a **required** parameter here rather than a defaulted one: a default
      would let a future call site silently reacquire the blindness this clause exists to remove.

    All four are CI-080's failure mode relocated, not closed: ``compile()`` passes and
    ``castiron gen`` still exits 0. Three of them violate ``CI94-Q1``'s one non-negotiable
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

    ✅ **The predicate is now total over the ``(name, class name)`` pair, not only over spelling**
    (``CI-113``). The class-private rule is CPython's ``_is_private`` restated, per ``CI94-D8`` --
    restate, never import -- and ``class_name`` is NFKC-normalized for the same reason ``name`` is:
    :func:`python_class_name` does not sanitize its input, so a non-ASCII type name can reach here.

    ⚠ **The interpreter-dependence this clause removes, recorded because it is the Hard Rule #9
    half of the defect.** A class-private member is **kept** by py3.10 (under the mangled name,
    with a ``DeprecationWarning``) and **dropped** by py3.11+. So before this fix the *meaning* of
    an emitted module depended on which interpreter imported it, at ``castiron gen`` exit 0. All
    four gate legs now agree.

    ⚠ **Measured equivalence of ``enum._is_private`` across the gate legs**, since ``CI94-D8``
    obliges this restatement to track reality rather than one interpreter: the source is
    **identical on 3.10 / 3.11 / 3.12**, and **3.13 drops one clause**
    (``name[pat_len:pat_len+1] != ['_']``). That delta is **behaviourally inert** -- it compares a
    ``str`` to a ``list``, so it was always ``True``. ``_is_private`` therefore behaves identically
    on all four legs, and one restatement is correct for all of them.

    Args:
        name: A candidate member name, already known to be a valid identifier.
        class_name: The enclosing ``Enum`` subclass's name -- the one the member will actually be
            emitted under. **Required**, deliberately: see the class-private bullet above.

    Returns:
        ``True`` when ``Enum`` would reject, rename or swallow ``name``.
    """
    name = unicodedata.normalize('NFKC', name)
    class_name = unicodedata.normalize('NFKC', class_name)
    sunder = len(name) > 2 and name[0] == name[-1] == '_' and name[1] != '_' and name[-2] != '_'
    dunder = len(name) > 4 and name[:2] == name[-2:] == '__' and name[2] != '_' and name[-3] != '_'
    private = name.startswith('__') and not name.endswith('__')
    mangled = f'_{class_name}__'
    # `len(name) > len(mangled)` is what makes `name[-2]` safe as well as excluding the bare prefix.
    class_private = len(name) > len(mangled) and name.startswith(mangled) and (name[-1] != '_' or name[-2] != '_')
    return sunder or dunder or private or class_private


def _repair_enum_shape(name: str, class_name: str) -> str:
    """Append ``'_'`` until ``name`` is usable as an ``Enum`` member of ``class_name``.

    **Terminates in at most three appends, and that bound is a proof rather than an observation.**
    A name whose NFKC form ends in three or more underscores can be none of the four shapes:
    sunder needs ``name[-2] != '_'``, dunder needs ``name[-3] != '_'``, and private needs the name
    *not* to end ``'__'``. Every iteration adds one trailing underscore, so the loop cannot run
    more than three times. (``__2`` is the worst case and needs all three: ``__2_`` is still
    private, ``__2__`` is then dunder, ``__2___`` is finally clean.)

    **The fourth (class-private) clause does not weaken that bound and cannot become the binding
    constraint**: it is false as soon as the NFKC form ends in **two** underscores, which is a
    weaker requirement than the three the other clauses need. Measured: ``max_append == 3`` over
    the full generated sweep on 3.10 / 3.11 / 3.12 / 3.13, both before and after ``CI-113``.

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
        class_name: The enclosing ``Enum`` subclass's name, threaded straight into the predicate.

    Returns:
        ``name``, with as many trailing underscores as it takes.
    """
    while _is_enum_reserved_shape(name, class_name):
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
       ``_sunder_``, ``__dunder__``, **private/name-mangled**, or **class-private**
       (``_<ClassName>__x``, the fourth shape, ``CI-113``). ``str.isidentifier()`` is **necessary
       but not sufficient**: ``Enum`` raises on the first and silently drops the other three.
       The class name that guard needs is derived here, from the ``enum`` already in hand.
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
    # ⚠ Derived here rather than accepted as a parameter, and that is deliberate (`CI-113`). The
    # class name is a pure function of the `EnumInfo` this function already receives,
    # `python_class_name` is the single authority for it, and `_enum_section` renders the class
    # header from that same call -- so computing it internally makes it IMPOSSIBLE for a caller to
    # supply a class name the emitter will not use. A parameter would create exactly that
    # possibility, for no gain; add one if a non-Python emitter ever needs a different name.
    class_name = python_class_name(enum)
    used: set[str] = set()
    members: list[EnumMember] = []

    for label in enum.values:
        name = identifier_characters(label).upper()
        if not name:
            name = '_'
        if not name.isidentifier():
            name = f'_{name}'

        note: str | None = None
        if _is_enum_reserved_shape(name, class_name):
            name = _repair_enum_shape(name, class_name)
            note = 'reserved by Enum'
        if string_is_reserved(name.lower()) and not column_name_reserved_exceptions(name.lower()):
            name = f'{name}_'
            note = 'reserved keyword'

        # ⚠ The ordinal suffix can itself create a reserved shape -- `_` plus `_2` is `__2`,
        # which Python name-mangles -- so every candidate is repaired again, and the loop
        # re-checks uniqueness afterwards rather than assuming the repair kept it unique.
        candidate = _repair_enum_shape(name, class_name)
        ordinal = 1
        while unicodedata.normalize('NFKC', candidate) in used:
            ordinal += 1
            candidate = _repair_enum_shape(f'{name}_{ordinal}', class_name)
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
