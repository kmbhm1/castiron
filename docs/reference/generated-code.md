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
- **Nothing about a module castiron could not emit validly in the first place** — see
  [identifier-hostile column names](#column-names-are-not-sanitized-yet) below.

The promise is enforced, not asserted: every reachable emission (4 corpus inputs × 128
configuration points) is written to disk as `.py` and linted by a real ruff subprocess with
`--isolated`, on every leg of the test matrix. Adding an allowance list back is a deliberate
decision, not a way to turn a red test green.

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
   underscore: `import` → `IMPORT_`.
7. **Collision resolution** — see below.

### `Enum` reserves shapes that Python allows

`str.isidentifier()` is necessary and **not sufficient**. Three name shapes are legal Python
identifiers that `EnumMeta` will not give you back, and step 1 produces all of them from
ordinary labels — a trailing space in a `CREATE TYPE` is enough:

| Shape | What `Enum` does | Reached from |
| --- | --- | --- |
| `_sunder_` | **raises `ValueError` when the class body runs** — the whole generated module is unusable at import | `'(none)'` → `_NONE_` |
| `__dunder__` | the member is **silently dropped** | `'__init__'` → `__INIT__` |
| `__private` (two leading underscores, fewer than two trailing) | name-mangled at compile time, so the member never has the name that was written; 3.11+ drop it, 3.10 keeps it under a different name | the collision suffix itself: `_` + `_2` → `__2` |

castiron repairs those shapes by appending `_` until the name is usable — at most three times,
which is a bound rather than an observation. That is why `'(none)'` emits `_NONE__` and not
`_NONE_`.

Two of those three would violate the one thing enum naming never does — drop a label — and they
would do it at exit code `0`, which is why the repair exists rather than a warning.

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
    ID_ = "id"  # original name was "id" (reserved keyword)
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

!!! note "`id`, `license`, `help` and four others"
    A short curated list — `id`, `credits`, `copyright`, `license`, `help`, `property`, `sum` —
    also picks up the trailing underscore, and the comment reads `(reserved keyword)` even though
    those are not keywords. That list exists to *exempt* column names from renaming, and the enum
    path reads it as an addition instead. It is a known defect, tracked, and deliberately left
    visible rather than quietly patched inside an unrelated change. The label in the value literal
    is unaffected.

## Known limitations

### The class-name axis is not covered

`Enum`'s private-name check also consults the **enclosing class name**, and castiron's member
naming does not receive it. A label whose NFKC form spells `_<ClassName>__something` for the class
it is being emitted into will therefore slip past the repair — the member is dropped on Python
3.11+ and kept under a mangled name on 3.10, at exit code `0`.

**This is unreachable from ASCII-only labels.** The generated class name always ends in `Enum`,
whose lowercase `num` cannot survive `.upper()`. Reaching it takes deliberately-crafted Unicode:
389 codepoints are `upper()`-invariant *and* NFKC-fold to an ASCII lowercase letter, so
`'_PᵘᵇˡᵢᶜOʳᵈᵉʳSᵗªᵗᵘˢEⁿᵘᵐ__X'` normalizes to `_PublicOrderStatusEnum__X`.

It is **filed and pinned by a test, not fixed**, in `0.1.0`.

### Column names are not sanitized yet

Enum *member* names are made identifier-safe; **column names are not**. A column whose name is a
legal quoted Postgres identifier but not a legal Python one — `2fast`, `space name`,
`kebab-case` — is emitted verbatim as a field name, producing a module that does not parse. The
run still exits `0`.

This is a known, open defect, pinned by a golden that is asserted to *fail* to compile, so it
cannot be fixed silently or regress unnoticed. Ordinary columns in the same table emit normally;
a column whose name merely collides with a Python keyword or builtin is handled properly and
becomes `field_class: str | None = Field(default=None, alias="class")`.

Because such a module does not parse, it is also the one thing excluded from the lint promise
above — a linter has nothing to say about a file it cannot read.
