# The generated code

What castiron writes into your repository, and what it promises about it. This page is about
the *output* — the fidelity of the input is [a separate
page](../sources/openapi.md).

## It is clean as emitted, not cleaned afterwards

castiron runs no formatter over its output. There is no post-hoc `ruff` or `black` pass whose
version could change the bytes, no timestamp, no generated-on banner, and no source URL in the
file — the same schema and the same options produce the same bytes, every time. That is what
makes generated models safe to commit and to diff, and it is the foundation the planned
`castiron check` drift-guard will stand on.

Being unformatted afterwards means the emitter has to get it right the first time, so it does:

!!! success "The promise"
    Emitted output is clean under ruff's **`F`** (Pyflakes), **`UP`** (pyupgrade) and **`I`**
    (isort) rules, **at ruff's own default settings** — not castiron's. A generated module does
    not trip the linter of the project it was just added to.

The import block in particular is written pre-sorted: sections in `__future__` → standard
library → third party order, `import X` before `from X import ...`, same-module `from` imports
merged onto one line with names ordered constant → class → rest, all case-insensitive. It is
byte-identical to what `ruff check --select I --fix` would have written.

**What is *not* promised, stated as plainly as the promise:**

- **Nothing about `E501`.** castiron's own house limit is 120 columns and the longest line it has
  been measured to emit is 101 characters, but that is an observation, not a guarantee — the
  length is driven by `Field(description=...)` carrying your own SQL comment, and your comments
  are as long as you wrote them. At ruff's 88-column default, a long comment will be flagged.
- **Nothing about non-default rule sets.** `D`, `ANN`, `PL`, and friends are not considered.

The promise is enforced, not asserted: every reachable emission (4 corpus inputs × 128
configuration points = **512 modules**) is written to disk as `.py` and linted by a real ruff
subprocess with `--isolated`, on every leg of the test matrix. Adding an allowance list back is a
deliberate decision, not a way to turn a red test green.

That sweep covers **all four** corpus inputs, including the deliberately identifier-hostile one.
Until `0.1.0` it covered three: the hostile input emitted a module that did not parse, and a
linter has nothing to say about a file it cannot read. [Column names are now
repaired](#column-names), so the carve-out is gone rather than merely unused.

## Model class names

Every table produces a **class stem** — `orders` → `Orders` — and five classes hang off it:
`OrdersBaseSchema`, `OrdersParent`, `OrdersInsert`, `OrdersUpdate` and the operational `Orders`.
A table name is a quoted identifier in Postgres, so `CREATE TABLE "order lines"` is legal and
PostgREST reports it verbatim.

The rule, in order:

1. **Sanitize, then PascalCase.** Every character Python will not accept inside an identifier
   becomes `_` — the same map the [column](#column-names) and [enum label](#enum-member-names)
   paths use — and `to_pascal_case` then splits on `_`. So a space or a hyphen becomes a word
   boundary: `"order lines"` → `OrderLines`. A leading digit gains one `_`. Unicode is **kept**:
   `Ünïcödé` comes through untouched.
2. **Singularize first, when `--singular-names` is set.** `orders` → `Order`.
3. **Collision resolution** — see below.

| Postgres table | Class stem |
| --- | --- |
| `order_lines` | `OrderLines` |
| `"order lines"` | `OrderLines` |
| `"order-lines"` | `OrderLines` |
| `ORDER_LINES` | `OrderLines` |
| `"2fast"` | `_2fast` |
| `"a""b"` | `AB` |
| `"Ünïcödé"` | `Ünïcödé` |

!!! warning "Until `0.1.2` these emitted a module that did not parse"
    `"order lines"` emitted `class Order linesBaseSchema(CustomModel):` — a `SyntaxError`, with
    `castiron gen` exiting `0` — and the bad stem reached all five class headers, every relationship
    field pointing at that table, and each class's docstring. **No name that was already valid
    changed**, so regenerating moves nothing unless your schema contains one of these.

!!! note "`orderLines` becomes `Orderlines`, not `OrderLines`"
    The table path capitalizes each `_`-separated word and lowercases the rest, so a camelCase
    table name loses its inner capitals. That is what castiron has always emitted and it is not a
    parse problem, so it was left alone. (Enum *type* names do have a camelCase branch — the two
    transforms are not the same function.)

### Colliding tables get an ordinal suffix

Three different things collapse two tables onto one set of class names, and **all three predate
the repair above**:

* **The assembly is not injective**: `order_lines`, `ORDER_LINES`, `"order lines"` and
  `"order-lines"` all become `OrderLines`.
* **`--singular-names` merges names**: `orders` and `order` both become `Order`.
* **A stem binds five class names, not one**: the tables `order` and `order_insert` have obviously
  distinct stems and both want the name `OrderInsert`.

Nothing is ever dropped or merged. Every table gets its own five classes; the ones that cannot keep
the natural stem get `_2`, `_3`, … and a comment saying what took it. A stem is only allocated when
**every** name derived from it is free — including against castiron's own `CustomModel` bases and
everything the import block binds, so a table called `custom_model` or `base_model` cannot rebind
them.

!!! note "Well-behaved names are allocated first"
    Tables whose name needed **no** repair claim their stem **before** repaired ones do, the same
    way [enum classes](#enum-class-names) do. Adding `CREATE TABLE "order lines"` to a database that
    already had `order_lines` should not rename a class you already import. Two *equally*
    well-behaved colliders still need an arbitration, and there the first in schema order keeps it.

```sql
CREATE TABLE public."order lines" (id int primary key);
CREATE TABLE public."order-lines" (id int primary key);
CREATE TABLE public.order_lines   (id int primary key);
```

```python
# original name was "order lines" (name collision, OrderLines is taken by "order_lines")
class OrderLines_2BaseSchema(CustomModel):
    """OrderLines_2 Base Schema."""

    # Columns
    id: int


# original name was "order-lines" (name collision, OrderLines is taken by "order_lines")
class OrderLines_3BaseSchema(CustomModel):
    """OrderLines_3 Base Schema."""

    # Columns
    id: int


class OrderLinesBaseSchema(CustomModel):
    """OrderLines Base Schema."""

    # Columns
    id: int
```

The comment is repeated above **every** class the stem produces, because a class header carries
nothing else of the source: once the stem is repaired the Postgres table name is otherwise
unrecoverable from the module. It is emitted **only** when the name changed.

**Relationship fields follow the resolved class**, never the one the table name suggests — a
foreign key into `"order lines"` is annotated `list[OrderLines_2]` above, not `list[OrderLines]`.
Their *field* names are model fields, so they follow the [column rule](#the-rule) instead: a table
called `"order lines"` gives `order_lines: list[...]`, and one called `class` gives `field_class`.

`castiron gen` prints one aggregated line to stderr when a table is renamed:

```
castiron: 2 tables are not emitted under the class name their name suggests
(order lines -> OrderLines_2 (name collision, OrderLines is taken by order_lines),
order-lines -> OrderLines_3 (name collision, OrderLines is taken by order_lines)) -- the
original table name is preserved in a comment above each generated class, so the module
still records which table it came from; only the Python class name differs.
```

!!! warning "The same positional caveat as everywhere else"
    Ordinal suffixes are positional, so adding a table upstream that sorts before an existing
    collider renumbers the later ones. Allocating unrepaired names first removes the common case —
    a hostile name displacing a well-behaved one — but not the general one.

## Enum class names

The class header is built from two pieces of raw Postgres text — the **schema** and the **type
name** — and PostgREST reports both verbatim. A Postgres type name is a quoted identifier, so
`CREATE TYPE public."order status"` and `CREATE SCHEMA "2fa"` are both perfectly legal and neither
produces a usable Python class name on its own.

The rule, in order:

1. **PascalCase assembly** — `public.order_status` → `PublicOrderStatusEnum`. snake_case,
   camelCase (`thirdType` → `PublicThirdTypeEnum`), PascalCase and a leading underscore
   (`_first_type` → `PublicFirstTypeEnum`) are each handled; the schema is capitalized and
   prefixed, and `Enum` is always appended.
2. **Identifier repair** — every character that cannot appear in a Python identifier becomes `_`,
   and a leading digit gains one `_`. Unicode is **kept**, not folded to ASCII: `public.Ünïcödé`
   stays `PublicÜnïcödéEnum`.
3. **Collision resolution** — see below.

This is the same character map the [column](#column-names) and [enum label](#enum-member-names)
paths use, so a name that is repaired one way in one place is repaired the same way everywhere.

| Postgres type | Class name |
| --- | --- |
| `public.order_status` | `PublicOrderStatusEnum` |
| `public."order status"` | `PublicOrderStatusEnum` |
| `public."order-status"` | `PublicOrderStatusEnum` |
| `public."a""b"` | `PublicABEnum` |
| `"my schema".order_status` | `My_schemaOrderStatusEnum` |
| `"2fa".mood` | `_2faMoodEnum` |

!!! warning "Until `0.1.1` these emitted a module that did not parse"
    `public."order status"` emitted `class PublicOrder statusEnum(str, Enum):` — a `SyntaxError`,
    with `castiron gen` exiting `0`. A type name containing a newline split the header across two
    lines. The bad name also propagated into every column annotation referencing the type, so a
    fix that repaired only the header would not have been one. **No name that was already valid
    changed**, so regenerating moves nothing unless your schema contains one of these.

### Collisions get an ordinal suffix

Two distinct Postgres types can want the same class name, and this is not exotic:
`order_status`, `orderStatus`, `OrderStatus`, `Order_Status`, `_order_status` and `ORDER_STATUS`
all assemble to `PublicOrderStatusEnum`. The collision domain is the **whole module**, not just
the enums — a table named `order_status_enum` produces a model class called `OrderStatusEnum`,
which an enum `order.status` also wants.

Nothing is ever dropped or merged. Every type gets its own class; the ones that cannot keep the
natural name get `_2`, `_3`, … and a comment saying what took it.

!!! note "Well-behaved names are allocated first"
    Enum types whose name needed **no repair** claim their class name **before** repaired ones do.
    Without that, adding `CREATE TYPE "order status"` to a database that already had `order_status`
    would hand the new hostile type the clean `PublicOrderStatusEnum` and silently rename the
    working one — because `' '` and `'-'` sort before `'_'`. A schema addition should not rename a
    class you already import.

    Two *equally* well-behaved colliders still need an arbitration, and there the first in schema
    order keeps the name.

**[Table model class names](#model-class-names) always win.** Model class stems are allocated
first and an enum yields to them — including to a stem that itself carries an ordinal — because a
table's model is the stable thing your imports point at.

```sql
CREATE TYPE public."order status" AS ENUM ('open');
CREATE TYPE public."order-status" AS ENUM ('shut');
CREATE TYPE public.order_status   AS ENUM ('done');
CREATE TYPE "2fa".mood            AS ENUM ('ok');
```

```python
# original name was "public.order status" (name collision, PublicOrderStatusEnum is taken by "public.order_status")
class PublicOrderStatusEnum_2(str, Enum):
    OPEN_ = "open"  # original name was "open" (reserved keyword)


# original name was "public.order-status" (name collision, PublicOrderStatusEnum is taken by "public.order_status")
class PublicOrderStatusEnum_3(str, Enum):
    SHUT = "shut"


class PublicOrderStatusEnum(str, Enum):
    DONE = "done"


# original name was "2fa.mood" (identifier repair)
class _2faMoodEnum(str, Enum):
    OK = "ok"
```

A class header carries nothing else of the source, so the `# original name was …` comment is the
only record of which Postgres type a class came from — unlike a member line, where the value
literal on the same line already is the label. It is emitted **only** when the name changed.

`castiron gen` also prints one aggregated line to stderr when this happens, so a rename is not
something you have to find by reading the file:

```
castiron: 1 enum type is not emitted under the class name their Postgres type name suggests
(public.order status -> PublicOrderStatusEnum_2 (name collision, PublicOrderStatusEnum is taken by
public.order_status)) -- the original type name is preserved in a comment above each class, so the
generated module still records which type it came from; only the Python class name differs.
```

!!! warning "The same positional caveat as label collisions"
    Ordinal suffixes are positional. Adding a type upstream that sorts before an existing collider
    renumbers the later ones, so `PublicOrderStatusEnum_2` can come to mean a different Postgres
    type after a `CREATE TYPE`. Allocating unrepaired names first removes the common case — a
    hostile name displacing a well-behaved one — but not the general one.

## Enum member names

A Postgres enum becomes a `str, Enum` class. The **member value is always the label, verbatim** —
whatever castiron has to do to the *name*, nothing is ever lost, dropped or merged, and every
label gets exactly one member.

```sql
CREATE TYPE public.ticket_state AS ENUM
  ('in progress', 'in-progress', 'done', '2nd pass', '(none)', 'import', '');
```

```python
class PublicTicketStateEnum(str, Enum):
    IN_PROGRESS = "in progress"
    IN_PROGRESS_2 = "in-progress"  # original name was "in-progress" (name collision)
    DONE = "done"
    _2ND_PASS = "2nd pass"
    _NONE__ = "(none)"  # original name was "(none)" (reserved by Enum)
    IMPORT_ = "import"  # original name was "import" (reserved keyword)
    _ = ""
```

The trailing comment appears **only when the name is not the straight transform of the label**.
The value literal is right there on the same line and already *is* the label, so glossing every
member would be noise in every user's file forever.

### How a label becomes a name

In this order:

1. **Sanitize.** Every character Python will not accept inside an identifier becomes `_`, one
   character out per character in. Nothing is collapsed and nothing is stripped: `'a b'` and
   `'a  b'` stay distinguishable attempts. Non-ASCII identifier characters are **kept**, not
   folded to ASCII — `'Ünïcödé'` becomes `ÜNÏCÖDÉ`, not `________`.
2. **Uppercase.** `pending` → `PENDING`, as it always has been.
3. **Empty label guard.** `CREATE TYPE t AS ENUM ('')` is legal Postgres; the empty label becomes
   `_`.
4. **Leading-digit guard.** `'2nd pass'` → `_2ND_PASS`, addressable as `E._2ND_PASS`.
5. **Enum-shape repair** — see below.
6. **Reserved-name guard.** A name that spells a Python keyword or builtin gets a trailing
   underscore: `import` → `IMPORT_`. A short curated list of builtins — `id`, `credits`,
   `copyright`, `license`, `help`, `property`, `sum` — is **exempt** and keeps its plain name.
7. **Collision resolution** — see below.

### `Enum` reserves shapes that Python allows

`str.isidentifier()` is necessary and **not sufficient**. Four name shapes are legal Python
identifiers that `EnumMeta` will not give you back, and step 1 produces all of them from
ordinary labels — a trailing space in a `CREATE TYPE` is enough:

| Shape | What `Enum` does | Reached from |
| --- | --- | --- |
| `_sunder_` | **raises `ValueError` when the class body runs** — the whole generated module is unusable at import | `'(none)'` → `_NONE_` |
| `__dunder__` | the member is **silently dropped** | `'__init__'` → `__INIT__` |
| `__private` (two leading underscores, fewer than two trailing) | name-mangled at compile time, so the member never has the name that was written; 3.11+ drop it, 3.10 keeps it under a different name | the collision suffix itself: `_` + `_2` → `__2` |
| `_<ClassName>__x` (**class-private**) | treated as a normal attribute rather than a member — 3.11+ drop it, 3.10 keeps it mangled | a label whose NFKC form spells the enclosing class name; see below |

castiron repairs those shapes by appending `_` until the name is usable — at most three times,
which is a bound rather than an observation. That is why `'(none)'` emits `_NONE__` and not
`_NONE_`.

Three of those four would violate the one thing enum naming never does — drop a label — and they
would do it at exit code `0`, which is why the repair exists rather than a warning.

!!! note "The fourth shape depends on the class name, not just the label"
    `Enum`'s private-name check also consults the **enclosing class name**, so whether a name is
    reserved is a property of the *pair*. castiron checks members against the class name it
    **actually writes in the header** — including the `_2` a [collision](#collisions-get-an-ordinal-suffix)
    allocated — so the two cannot disagree.

    Reaching this shape takes deliberately-crafted Unicode and is **impossible from ASCII-only
    labels**: the generated class name always ends in `Enum`, whose lowercase `num` cannot survive
    `.upper()`. But 389 codepoints are `upper()`-invariant *and* NFKC-fold to an ASCII lowercase
    letter, so `'_PᵘᵇˡᵢᶜOʳᵈᵉʳSᵗªᵗᵘˢEⁿᵘᵐ__X'` normalizes to `_PublicOrderStatusEnum__X`. It is
    repaired to `_PᵘᵇˡᵢᶜOʳᵈᵉʳSᵗªᵗᵘˢEⁿᵘᵐ__X__` and the label survives **identically on Python 3.10
    through 3.13** — before this was repaired, the generated module's meaning depended on which
    interpreter imported it.

### Names are compared after NFKC normalization

CPython normalizes identifiers to NFKC **at compile time**, so two strings that look different
can be one binding: `ﬁ = 1; fi = 2` leaves a single name worth `2`. `str.upper()` performs the
same folding (`'ﬁ'.upper() == 'FI'`, `'ß'.upper() == 'SS'`). castiron therefore tests both the
reserved shapes and uniqueness against the **normalized** candidate — the string the compiler
will actually see — rather than the one it wrote.

Without that, a perfectly legal Postgres enum would emit a module raising `TypeError` at import.
With it:

```python
class PublicKindEnum(str, Enum):
    FI = "ﬁ"
    FI_2 = "fi"  # original name was "fi" (name collision)
    ID = "id"
    SS = "ß"
    SS_2 = "SS"  # original name was "SS" (name collision)
```

### Collisions get an ordinal suffix

When two labels want the same name, the first keeps it and each later one gets `_2`, `_3`, … until
the normalized candidate is free. The suffix can itself create a reserved shape, so the result is
re-repaired before it is accepted.

!!! warning "The cost, stated up front"
    Ordinal suffixes are positional, so **inserting a label upstream that sorts before an existing
    collider renumbers the later ones** — `E.IN_PROGRESS_2` can come to mean a different label
    after an `ALTER TYPE`. The effect is bounded to colliding labels and it is visible on the line,
    because the value literal sits right next to the name. The alternative, a label-derived digest
    suffix, is stable but permanently unreadable; refusing to generate is not an option while there
    is no override mechanism to point you at.

!!! note "`id`, `license`, `help` and four others are *exempt*"
    A short curated list — `id`, `credits`, `copyright`, `license`, `help`, `property`, `sum` — is
    **exempt** from step 6, so those labels keep their plain names: `id` emits `ID = "id"`, with no
    trailing underscore and no comment. The same list exempts the same names on the [column
    path](#column-names), so the two read it identically.

    Until `0.1.0` the enum path applied that list as an *addition* rather than an exemption, so
    `id` emitted `ID_ = "id"  # original name was "id" (reserved keyword)` — a rename the list
    exists to prevent, annotated with the opposite of the reason. **If you generated with `0.1.0`,
    regenerating will drop the trailing underscore from those seven names.** The label in the value
    literal was never affected.

## Column names

A column name is a *quoted identifier* in Postgres, which means it can be almost any string —
`"2fast"`, `"space name"`, `"kebab-case"`, `"_private"` — and PostgREST carries it through
verbatim. Almost none of those are usable as a Pydantic field name, so castiron repairs the
**attribute** and keeps the **wire name** on a `Field(alias=...)`.

**The value on the wire is always the real column name.** The generated models read and write the
column your database actually has; only the attribute you type in Python differs.

```python
class HostileColumnsBaseSchema(CustomModel):
    """Hostile Columns Base Schema."""

    # Primary Keys
    id: int

    # Columns
    field_2fast: str = Field(alias="2fast")
    kebab_case: str | None = Field(default=None, alias="kebab-case")
    ok_column: str | None = Field(default=None)
    space_name: str | None = Field(default=None, alias="space name")
```

### The rule

1. **Sanitize.** Every character Python will not accept inside an identifier becomes `_`, one
   character out per character in — the same map the enum path uses. There is no run-collapsing
   and no stripping, so `'a b'` and `'a  b'` stay distinguishable attempts. Non-ASCII identifier
   characters are **kept**: a column named `Ünïcödé` is already legal Python and comes through
   untouched, with no alias.
2. **Prefix `field_` if the result still is not usable.** That covers an empty name, a leading
   digit (`2fast` → `field_2fast`), and a **leading underscore** (`_private` →
   `field__private`) — Pydantic reserves leading-underscore names for private attributes and
   raises `NameError` when the class body runs, so a leading underscore is unusable even though
   it compiles.
3. **Prefix `field_` if it is a Python keyword, a builtin, or starts with `model_`.** This is the
   long-standing rule: `class` → `field_class`. Seven names are exempt and keep their spelling —
   `id`, `credits`, `copyright`, `license`, `help`, `property`, `sum`.
4. **Uniquify, per table.** The first column to want a name keeps it; each later collider gets
   `_2`, `_3`, … `'space name'`, `'space-name'` and `'space_name'` in one table become
   `space_name`, `space_name_2` and `space_name_3`. **Nothing is ever dropped or merged.**

Ordering is the column order the source reported — Postgres `attnum` — so the result is
deterministic for a given schema.

!!! note "`ﬁ` and `fi` are the same identifier to Python"
    CPython normalizes identifiers to NFKC at compile time, so two column names that look
    different can be one attribute. castiron tests every guard, the uniqueness key **and** the
    alias rule against the normalized form — the string the compiler will actually see. Without
    that, a column named `ﬁ` would bind the attribute `fi`, carry no alias, and its wire name
    would be silently lost.

### castiron tells you when it renames something

A repair changes the attribute you have to type, so `gen` says so — **once per run**, at
`WARNING`, and the run still exits `0` because the generated code is correct:

```
castiron: 3 column names are not usable as a Python field name and were renamed
(t.2fast -> field_2fast, t.space name -> space_name, t.kebab-case -> kebab_case) --
the original name is preserved on the wire via Field(alias=...), so the generated
models still read and write the real column; only the attribute you type in Python differs.
```

The long-standing keyword rename (`class` → `field_class`) does **not** warn: that behaviour has
not changed, and a schema that worked yesterday should not grow a new warning today.

!!! warning "`field_` and `_2` are part of the public shape"
    The attribute name is what your code types, so changing this scheme later would be a breaking
    change. Two consequences worth knowing up front: the `field_` prefix is permanent, and ordinal
    suffixes are **positional** — adding a column upstream that sorts before an existing collider
    can renumber the later ones. `ALTER TABLE … ADD COLUMN` appends at the end, so in practice a
    new column can only take a *higher* ordinal. The alias on each line disambiguates them for a
    reader either way.
