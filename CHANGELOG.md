# CHANGELOG


## v0.4.0 (2026-08-09)

### Features

- **ir**: Report volatility as unknown below PostgREST 13.0.5
  ([`041496a`](https://github.com/kmbhm1/castiron/commit/041496a4ab76b1f6a0dd4bcf4a7b31761cbec942))

castiron's only volatility signal was "the /rpc/ path has no GET operation", and that inference is
  only sound on PostgREST >= 13.0.5. Below that floor the document carries a GET for EVERY function,
  so castiron silently reported every mutation as `is_read_only=True` -- exactly what CI-012's typed
  client would turn into a GET request for an INSERT.

The boundary is exact and it is NOT 12-vs-14. `makeProcPathItem` in
  src/PostgREST/Response/OpenAPI.hs reads

pe = (mempty :: PathItem) & get ?~ getOp & post ?~ postOp

unconditionally at v12.2.3, v12.2.12 (the last 12.x) and v13.0.4, and

pe = case pdVolatility pd of Volatile -> (mempty :: PathItem) & post ?~ postOp _ -> (mempty ::
  PathItem) & get ?~ getOp & post ?~ postOp

from v13.0.5 onward (PR #4174, CHANGELOG [13.0.5] - 2025-08-24). So 13.0.0-13.0.4 are affected too
  and no 12.x release carries the fix.

Below the floor -- or when info.version is absent or unparseable, treated the same way,
  conservatively -- `FunctionInfo.volatility` and `.is_read_only` are both None. The IR already
  modelled this: both fields are tri-state and their docstring already promised "None means unknown,
  never a guess", so the honest degradation costs zero shape change. A consumer's rule becomes "POST
  unless is_read_only is True", which fails safe: a None misread as falsy gives a POST, correct and
  merely unoptimised, rather than a GET for an INSERT.

The new `Schema.postgrest_version` records the document's verbatim info.version so a consumer can
  tell "unknown because the format cannot say" from "unknown because the server is too old". It is
  provenance, not schema: no emitter may write it into generated output, or `castiron check` would
  report drift on a server upgrade against an unchanged schema (Hard Rule #9). A regression test
  proves two documents differing only in info.version emit byte-identical modules.

Argument order is deliberately NOT gated. `makeProcGetParams` is byte-identical across the boundary,
  so a sub-floor GET array is still declaration order -- and because a sub-floor server emits it for
  VOLATILE functions too, order and VARIADIC are recovered in MORE cases there, not fewer. Gating
  them would have destroyed real fidelity.

Also repairs the CI-005 fixture, which declared 12.2.3 while carrying mixed /rpc/ verb sets -- a
  document that version cannot produce, the third instance of the CI-076 class after CI-133. Its
  verb shape is the >= 13.0.5 shape, so the version was the wrong byte; it is now 14.14. The
  sub-floor test fixture is synthetic and labelled as such in the file and in every test that reads
  it: it is evidence about castiron, never about PostgREST.

`castiron gen` prints one warning naming the observed version and the affected function count,
  before the --infer-generated-primary-keys early return.

The five ir.json goldens gain one additive key each; no module golden and no fingerprint manifest
  moves, and no golden's volatility census changes.

Refs: CI-141, CI-145; PostgREST PR #4174.


## v0.3.1 (2026-08-09)

### Performance Improvements

- **cli**: Scan a message once per pass, not once per secret
  ([`43a8af6`](https://github.com/kmbhm1/castiron/commit/43a8af6756fa789a8367fe8ccda2fc1dabe529e9))

`redact` was quadratic in the length of the text on three counts, two of them fixed here. It is only
  reachable through `RedactingFilter`, which runs on every log record and on a rendered traceback --
  neither length-capped the way `_snippet` caps an error body -- so a source adapter logging a
  document is enough to hit it.

Measured at d4455ae, 20 000 characters: DSNs 0.511 s, a base64 blob carrying no secret at all 0.185
  s, both in one message 0.979 s. Here: 0.00178 s, 0.00019 s and 0.00063 s -- 287x, 963x and 1554x,
  and 12.8x/3.9x/7.7x growth for 8x the input against 69.9x/64.9x/63.7x before.

- the bare `<secret>@host` mop-up ran one `re.sub` over the whole message per host, and the host
  count grows with the message; hosts are now deduplicated, totally ordered, and matched in one
  alternation pass. - `_URL_USERINFO`'s greedy scheme run restarted from every position of a run,
  which made `redact` quadratic on text it never masks; a lookbehind anchors it at the run start and
  a lookahead re-imposes the old pattern's real precondition, so the match set is unchanged. - the
  key spellings ran one `str.replace` each; they now share one pass.

The `***` veto moves from the mop-up's replacement function into its pattern, which it has to for
  the alternation to be safe: as a test on the whole match, one already-masked URL at the end of a
  greedy run vetoed masking every bare `<secret>@host` in front of it.
  `hunter2pass@h,https://u:hunter2pass@h/x` leaks on main and does not here.

Equivalence was proved rather than assumed, against a frozen copy of the implementation this
  replaces: 82 enumerated shapes and 131 878 exhaustive inputs byte-identical, and of 193 575
  planted-secret cases where the old code hid the secret, the new code hides all 193 575 -- zero
  regressions, one improvement (the veto leak above). Tests go 219 -> 332, `errors.py` stays at 100%
  coverage, and 8/8 mutants that revert part of the fix are killed.

Refs CI-073.


## v0.3.0 (2026-08-09)

### Features

- **ir**: Recover and declare RPC declaration order
  ([`d41a5db`](https://github.com/kmbhm1/castiron/commit/d41a5db86ba2e62131af1e2c7251d84db720a4dd))

castiron read a function's parameter order out of the POST body's `properties` and handed it
  downstream as pg declaration order. It is not: `properties` is a JSON **object**, and PostgREST
  serializes it sorted by name. CI-012 will generate positional RPC calls from this list, and a
  positional call built from an alphabetical order type-checks, runs, and returns the wrong answer.

The same document carries the real order in TWO places, both of which survive because they are JSON
  **arrays**:

- the GET operation's `parameters` -- emitted only for a STABLE or IMMUTABLE function, and true pg
  declaration order. Measured against `pg_proc.proargnames` with three purpose-built
  anti-alphabetical probes, which falsified the only rival reading ("required first, then
  alphabetical within group"): for probe_mixed the rival predicts [p_alpha, p_zulu, p_beta] and the
  document says [p_zulu, p_alpha, p_beta]. - the POST body's `required` -- emitted for EVERY
  function regardless of volatility, listing only the non-defaulted arguments. Postgres forbids a
  defaulted parameter before a non-defaulted one, so that set is necessarily the declaration-order
  PREFIX, never a scattered subset. This is the only order signal a VOLATILE function exposes, and a
  VOLATILE mutation with no defaults recovers its order in full.

`_declaration_order` reads the GET when it has one, else `required` followed by a name-sorted tail,
  else keeps the body order. Every ordering comes from a list in document order; `sorted()` appears
  once, inside an equality comparison (Hard Rule #9).

Because the recovery is genuinely partial, the new IR field is a three-member enum rather than a
  bool. `ParameterOrder.DECLARED_PREFIX` earns its complexity by having a concrete consumer action:
  CI-012 emits `def f(p_zulu, p_alpha, *, p_beta=None)` -- positional prefix, keyword-only tail --
  because Python's `*` divides on required-vs-defaulted, exactly the axis castiron can prove. A bool
  would collapse that into False and throw away a correct positional prefix on the commonest RPC
  shape there is. No `prefix_length` field: the known positions ARE the `has_default=False`
  parameters, the same array by construction, and a second encoding of one fact is Hard Rule #6.

The function row grows to a 9-tuple carrying the member itself. It rides the ROW, not the source,
  for CI-090's reason: on this source "order known" happens to correlate with volatility, but a
  live-DB source reads `pg_proc.proargnames` (ordered for VOLATILE too) and the DDL source will
  carry mixed provenance in one document.

Measured effect: 4 of 13 corpus artifacts move, all `ir.json`, and every diff line is inside
  `functions[]` -- `tables` and `enums` decode identical and no parameter's content changed, only
  its position plus one new key. ZERO `*.py.txt` goldens and ZERO fingerprint rows move, because no
  emitter consumes `Schema.functions`. So no generated output changes and nobody's `castiron check`
  goes red because of this.

KNOWN_DEFECTS gains `CI-078`, cited by the two `testbed-public` cases: `create_order` is VOLATILE
  with one required argument of three, so `required` fixes the first position and the defaulted tail
  stays name-sorted -- the testbed declares (p_customer_id, p_status, p_lines) and the golden still
  says [p_customer_id, p_lines, p_status]. That byte is still wrong. What changed is that castiron
  now DECLARES the limit. The fixture case is deliberately NOT cited: it is hand-authored, so no
  byte in it can be proved wrong, and citing it would manufacture evidence.

Honest residual: `ParameterOrder.UNKNOWN` has no live witness. It needs a VOLATILE function with two
  or more arguments and every one defaulted, and no capture -- present or pending -- has one, so it
  rests on a synthetic unit test that says so in its own comment. `required`-is-declaration-order is
  likewise measured on STABLE probes only. A falsifier cross-checks the two ordered encodings
  against each other on every push wherever both are informative (two functions, asserted by name so
  it cannot pass vacuously). CI-140 tracks seeding the VOLATILE probes.

Refs: CI-078 (half 2), CI-139, CI-140


## v0.2.0 (2026-08-08)

### Bug Fixes

- **cli**: Report renamed model class names
  ([`906b50a`](https://github.com/kmbhm1/castiron/commit/906b50a5bf0538a089c1abbb9175f4daaa122bcd))

The fourth and last member of the rename family castiron already tells users about, in the same
  voice and on the same channel: one aggregated `castiron: ...` warning per run, at most three
  names, then `and N more`. It is the loudest of the four in consequence -- a repaired column keeps
  its wire name through `Field(alias=...)`, while a repaired table name survives only as a comment.

Reported from the emitter's own `class_stems`, never re-derived.

Refs: CI-130

- **emitters**: Emit the resolved table class names
  ([`883520b`](https://github.com/kmbhm1/castiron/commit/883520b042fd81d5947e3fed039a025a95fc66e1))

Resolve every table's class stem ONCE in `_write` and thread it to the five class headers, the
  docstrings, the `Inherits from` trailer and -- the part a second derivation would break -- the
  type annotation of every relationship field pointing at that table. Recomputing `to_pascal_case`
  at the annotation site would name `OrderLines` where the header says `OrderLines_2`; that is
  CI-114.

Relationship FIELD names are a different namespace with a different rule, so they go through
  `standardize_column_name` (the shipped column rule), which also covers a reserved word (`class:
  list[Class]` was a SyntaxError) and a leading underscore (a NameError at import that compiles
  cleanly).

`_reserved_class_names` is unchanged in shape and now flows the resolved stems into the enum
  allocator automatically, exactly as CI-128 designed it to. A renamed stem carries `# original name
  was ...` above each of its classes, escaped through `_py_string` so a hostile name cannot split
  the comment.

Refs: CI-130

- **emitters**: Sanitize and allocate table class stems
  ([`e65fac6`](https://github.com/kmbhm1/castiron/commit/e65fac65052387b3518782ea333b9fef8ef043f1))

A table name is a quoted Postgres identifier and PostgREST carries it verbatim as a `definitions`
  key, but `to_pascal_case` only splits on `_` and capitalizes -- it never removes a character.
  `CREATE TABLE "order lines"` therefore produced the class stem `Order lines`, and `"2fast"`
  produced `2fast`. Neither is a Python identifier.

Add `python_class_stem` (the per-name transform, calling the shared `python_identifier` CI-128
  landed rather than forking it) and `python_class_stems` (the per-container allocator). A stem is a
  stem, not a name: it binds five top-level classes, so `suffixes` makes the allocator test all of
  them -- measured, tables `order` and `order_insert` collide in `OrderInsert` while their stems
  look distinct.

The collision rule itself is EXTRACTED from `python_class_names` into `_allocate_class_names` and
  shared, per CI-128's interface contract: one Option-B mechanism, not two. `python_class_names` is
  byte-identical in behaviour.

Refs: CI-130

### Features

- **ir**: Record whether a constraint name was synthesized
  ([`8a30091`](https://github.com/kmbhm1/castiron/commit/8a30091af0674fad0ab97e5e89e679f92455d8b2))

The Schema IR asserted two things the OpenAPI source cannot know. Both rows (CI-084, CI-090) are one
  subject -- the OpenAPI foreign-key model is lossy -- and they move the same five goldens and the
  same KNOWN_DEFECTS entries, so they ship together.

CI-084: `ColumnInfo.is_foreign_key` is now True **iff** a resolved forward `ForeignKeyInfo` names
  that column. PostgREST filters relations by privilege but not the `<fk/>` markers that reference
  them, so a marker can name a table absent from `definitions`. The builder correctly dropped that
  edge and then set the flag from the constraint anyway, leaving the IR contradicting itself: a
  consumer reading the flag found no relationship, a consumer reading the edge list never learned
  the column was special. Fixed in `ir/build.py`, not in the parser, because the builder already
  owns "is the target in this schema?" and the DDL and live-DB sources hit the identical case. The
  FOREIGN KEY `ConstraintInfo` is RETAINED -- it is the only evidence the database has one, and it
  is what the new CLI notice parses to name the missing target. No second IR field records the
  dangling edge; `parse_constraint_definition_for_fk` already reads it (Hard Rule #6).

CI-090: `ConstraintInfo.name_is_synthesized` and `ForeignKeyInfo.name_is_synthesized` (bool, default
  False, appended last) record that a constraint name was manufactured from a naming template rather
  than read from the database. The fact is otherwise destroyed at the point of synthesis: a
  fabricated `<t>_<c>_fkey` is byte-identical to what Postgres names a genuinely default-named
  constraint, so nothing downstream can tell them apart. The testbed names one
  `order_lines_order_fk` in SQL; castiron reports `order_lines_order_id_fkey`. The synthesis is not
  FK-specific -- PRIMARY KEY (`<t>_pkey`) and a view's downgraded UNIQUE (`<t>_<cols>_key`) are
  manufactured by the same rule, all 35 constraints in the public capture.

The flag rides the ROW, not the source: the constraint row contract widens from a 5-tuple to a
  6-tuple and the fk row from a 7-tuple to an 8-tuple. A source-level switch would cover the two
  sources that exist or are next, but not the DDL source, where a named `CONSTRAINT x FOREIGN KEY
  ...` clause and a bare inline `REFERENCES` land in one document with different provenance. Per row
  is strictly more expressive and costs one migration through the construction sites instead of two.
  Both contracts are documented in `ir/build.py`'s module docstring, and every `Args:` block naming
  their arity was updated with them.

Two specified consumers, written into the field docstrings so the next author does not have to
  rediscover them: `castiron check` (CI-021) compares constraint names only when both sides report
  False, and the SQLAlchemy/DDL emitters (CI-030/CI-031) omit `name=` when it is True, letting
  Postgres apply its own default. Both remove a false drift positive by construction.

A fifth CLI notice warns once per run when a foreign key points at a table the schema does not
  contain, naming up to three. It is emitted BEFORE `report()`'s identity-inference early return and
  is not gated on the OpenAPI source: the predicate reads only the IR, and a DDL or single-schema
  live-DB run reaches the same state. Unlike its four siblings, which are renames, this one reports
  a structural loss -- the emitted module looks perfectly ordinary while a relationship the user
  knows their database has is simply absent.

Goldens: exactly 5 of 13 artifacts move, all `ir.json`. Zero emitted-module goldens and zero
  fingerprint manifests -- neither change reaches the emitter (Hard Rule #9). The only non-additive
  diff line in the whole set is `ledger_refs.ledger_id` flipping `is_foreign_key` true -> false.

KNOWN_DEFECTS: CI-084 is retired (no wrong byte remains). CI-090 STAYS, rewritten -- the golden
  still says `order_lines_order_id_fkey` while the database says `order_lines_order_fk`, so that
  byte is still wrong; what changed is that castiron now declares the fabrication instead of hiding
  it. `pg_constraint.conname` in CI-010/CI-011 retires the entry.

Docs: `docs/sources/openapi.md` credited name synthesis to foreign keys only and had no row at all
  for a dangling marker. Both corrected, with two new sections covering the warning and the
  manufactured names.


## v0.1.1 (2026-08-08)

### Bug Fixes

- **emitters**: Disambiguate colliding enum classes
  ([`495fe3f`](https://github.com/kmbhm1/castiron/commit/495fe3f911cb11854ecb8f356e17e7657c8be8f5))

`python_class_name` is not injective and never was: `order_status`, `orderStatus`, `OrderStatus`,
  `Order_Status`, `_order_status` and `ORDER_STATUS` are six legal, distinct Postgres types that all
  map to `PublicOrderStatusEnum`. Driven through the real emitter with two of them present, the
  module carried two identical `class PublicOrderStatusEnum(str, Enum):` definitions -- it compiled,
  it imported, and the second silently won the binding. CI94-Q1 forbids that: never drop or merge a
  variant.

The domain is the module's whole top-level namespace, not the enum registry. Measured:
  `EnumInfo('status', schema='order')` and `TableInfo('order_status_enum')` both want
  `OrderStatusEnum`, and the model won -- `OrderStatusEnum('open')` returned a pydantic model.
  Neither name is exotic.

Add `EnumClass` + `python_class_names(enums, reserved)`, a naming-layer DTO and per-container
  allocator modelled on `column_identifiers`: one entry per input, positionally aligned, `_2`/`_3`
  suffixes spelled as the two shipped collision rules already spell them, keyed on the NFKC form
  because two distinct strings can be one binding at compile time (`PublicXﬁEnum`/`PublicXfiEnum` is
  a real, constructible witness).

Unrepaired names are allocated FIRST (captain ruling on CI-128-Q1, Option B). `schema.enums` is
  sorted and ' ' and '-' sort before '_', so plain first-come would hand a hostile `order status`
  the clean class name and rename the well-behaved `order_status` a user already imports -- which
  `check` mode would then report as drift. Determinism is preserved: two passes over the input by
  index, a holder map read by key and membership only, never iterated.

The emitter seeds the allocator with every other top-level name the module binds -- the import block
  (derived from PYDANTIC_TYPE_MAP, not hand-listed, so it cannot rot), the CustomModel bases, and
  all four class variants per table regardless of config, so an unrelated flag cannot rename a
  user's enum. The seed is built by calling `_class_name`/`_write_name`, so CI-130's table-stem
  sanitization will flow into it automatically.

The resolved names are computed ONCE in `_write` and threaded to both consumers -- the class header
  and the column annotation -- because two independent derivations of one name is exactly CI-114.

`python_member_names` gains an optional `class_name`. CI-113 argued that deriving it internally
  makes a wrong class name impossible; a collision falsifies that, and measured, the derived default
  silently drops a crafted label the resolved name keeps.

Repairs and collisions are surfaced to the user (captain ruling on CI-128-Q4) through the two
  mechanisms castiron already has: an aggregated `castiron: ...` stderr notice alongside the CI-085
  column one, and an `# original name was ...` comment above the class. The suffixed case names what
  took the bare name.

No golden moved; coverage 100%.

Refs: CI-128

- **emitters**: Exempt the curated reserved names
  ([`0f49821`](https://github.com/kmbhm1/castiron/commit/0f49821e2de20630a552dc2b9ecf7f8f9b3ebefe))

Step 6 of `python_member_names` read the curated exemption list with `or`:

string_is_reserved(name.lower()) or column_name_reserved_exceptions(...)

`column_name_reserved_exceptions` is an EXEMPTION list -- names that need NOT be renamed -- and the
  column path reads it that way (`ir/build.py:341`, as `and not`). Applying it with `or` inverted
  its meaning for the enum path, so the label `id` emitted

ID_ = "id" # original name was "id" (reserved keyword)

a rename the list explicitly exempts, carrying a comment that states the opposite of the intent. One
  boolean, and the two paths now agree.

⚠ Note what the `or` did NOT do, because the row's phrasing ("behaved as an addition list") is true
  of the intent and not of the measured effect: every name on the exemption list is already a
  builtin -- `credits`, `copyright`, `license` and `help` come from `site` -- so the `or` never
  actually ADDED anything. Its only observable effect was the missing exemption.

The guard itself is KEPT, per the captain's ruling of 2026-08-08: this is a boolean correction, not
  a removal. `class` and `import` are still renamed, and a counter-witness test pins that, so
  deleting the guard cannot pass silently.

Dropping the note on an exempted label is correct under CI94-D3: `ID` IS the straight transform of
  `id`, so there is nothing to gloss.

Behaviour change is bounded to seven labels (`id`, `credits`, `copyright`, `license`, `help`,
  `property`, `sum`). No golden moves -- no committed golden carries an exempted label.

Refs: CI-100, CI94-Q4

- **emitters**: Import Enum from the enum registry
  ([`23adadc`](https://github.com/kmbhm1/castiron/commit/23adadc3d17787ff33b5fca297950d7d5dfc1f06))

`_imports` gated `from enum import Enum` on a COLUMN carrying `enum_info`, while `_enum_section`
  renders from `schema.enums`. Two sources of truth for one import: an enum reachable only through
  the registry emitted its class with no import, so the generated module raised `NameError: name
  'Enum' is not defined` at import -- while `castiron gen` exited 0. Same defect class as CI-080 and
  CI-110: output that does not run, reported as success, which is the highest-severity class now
  that 0.1.0 is on PyPI.

Replace both conditions with one `_emits_enums(schema)` predicate read by the import block and the
  section alike. Fixing the duplicated condition in place would only have reset the clock.

Not user-reachable through the shipped OpenAPI source today -- `_collect_enum_registry` builds
  `schema.enums` from columns -- but reachable by constructing the IR directly (castiron's own suite
  already does) and ordinary the moment `sources/ddl/` lands, where a `CREATE TYPE` with no column
  using it is normal.

The new tests EXECUTE the module rather than `compile()` it, which is the assertion that would have
  caught this: `TestCi080TheCommentIsTotalOverItsInput` builds exactly this shape and stayed green
  because a missing import is invisible to a parse. Counter-witness included over both suppressing
  axes (`generate_enums=False`, empty registry); mutation-checked in both directions.

Refs: CI-114

- **emitters**: Sanitize the enum class name
  ([`1d8a42f`](https://github.com/kmbhm1/castiron/commit/1d8a42fff49fbc14d7fefb959ceb2682e582e8b8))

PostgREST reports a Postgres type name in `format` verbatim, and the OpenAPI source splits it on the
  last '.' keeping both halves byte-for-byte. Neither the schema nor the type name was sanitized
  before being PascalCase-assembled into the enum class header, so `CREATE TYPE "order status"`
  emitted

class PublicOrder statusEnum(str, Enum):

-> SyntaxError, with `castiron gen` exiting 0. The same is true of a hyphen, a newline (which splits
  the header across two lines), a quote, an emoji, and of the *schema* half: `CREATE SCHEMA "2fa"`
  produced the leading-digit name `2faMoodEnum`. Same emit-invalid-Python-at-exit-0 family as
  CI-080, CI-085 and CI-110, and equally source-reachable.

Add `python_identifier`, the shared repair primitive: the character map from
  `ir/build.py::identifier_characters` (CI85-D1 -- one algorithm, now three consumers, Unicode kept
  per CI94-D2) plus a single leading-position `_` prefix, tested on the NFKC form because CPython
  normalizes identifiers at compile time. It is total (`''` -> `'_'`) and is deliberately the only
  place the repair is written, so CI-130 (table class names, the same defect family) can call it
  rather than fork it.

`python_class_name` now sanitizes both inputs and repairs the assembly. The PascalCase transform
  itself is extracted verbatim into `_assemble_class_name` so the sanitizing wrapper and the
  forthcoming "was this repaired?" predicate share one assembly rather than each carrying a copy.

The transform is the IDENTITY on every name that already produced a valid identifier, so no
  committed golden moves; the five pre-existing `TestPythonClassName` cases are kept unedited as the
  byte-stability pins.

Refs: CI-128

- **emitters**: See Enum's class-name clause
  ([`e82708a`](https://github.com/kmbhm1/castiron/commit/e82708a861be392fbf809155bdd5dfa444f728d9))

`_is_enum_reserved_shape` received only the member name, so it could not test CPython's FOURTH
  reserved shape: `EnumMeta` calls `_is_private(cls_name, name)`, which swallows a member already
  spelled `_<ClassName>__x`. A crafted label was therefore emitted unrepaired, and the generated
  module SILENTLY DROPPED it on py3.11+ while py3.10 kept it under the mangled name -- `castiron
  gen` exiting 0 either way.

That breached two rules at once: CI94-Q1's one non-negotiable ("never drop a variant"), and Hard
  Rule #9, because the emitted module's MEANING depended on which interpreter imported it. All four
  gate legs now agree.

Thread a REQUIRED `class_name` into `_is_enum_reserved_shape` and `_repair_enum_shape`; a default
  would let a future call site silently reacquire the blindness this row exists to remove. The
  clause is CPython's `_is_private` restated, never imported (CI94-D8) -- measured equivalent on
  3.10-3.13, the 3.13 delta being behaviourally inert because it compares a `str` to a `list`.

`python_member_names(enum)` keeps its signature and derives the class name via
  `python_class_name(enum)`. The WORKPLAN row proposed a parameter; deriving it internally is
  strictly better, because the class name is a pure function of the `EnumInfo` already in hand and
  `_enum_section` renders the header from that same call -- so a caller CANNOT supply a class name
  the emitter will not use.

The <=3-append repair bound is unchanged and still a proof: the new clause is false as soon as the
  NFKC form ends in two underscores, weaker than the three the other clauses need, so it can never
  bind. Measured max_append == 3 on every leg, before and after.

Tests. The interpreter cross-check sweep was the file's stated authority and was structurally blind
  here: it executed under `class E`, and NAME_ALPHABET cannot spell `_E__`. It now sweeps under
  `'A'`, which the alphabet does contain -- 8 predicate-vs-interpreter disagreements before the fix,
  0 after, and the reserved count moves 184 -> 192. "Survived" now also requires the class body to
  have warned about NOTHING, which is load-bearing rather than tidy: py3.10 KEEPS a class-private
  member and announces it only by DeprecationWarning, so without that clause the sweep would pass on
  main and fail after the fix on 3.10 alone.

Every helper that executes members built by `python_member_names` now runs them under
  `python_class_name` of the same `EnumInfo` (FIXTURE_CLASS) instead of a `class E` stand-in --
  members checked against a class the emitter would never write is exactly how this survived. An
  emitter-level test drives the crafted label through PydanticEmitter, because a naming.py test
  cannot prove the emitter passes the class name it renders. Verified by mutation: removing the
  fourth clause reddens 4 tests on all four interpreters.

TestCi113TheClassNameAxisIsOpen was pinned-as-present by design and is replaced by
  TestCi113TheClassNameAxisIsClosed. The disappearance of its `sys.version_info` branch is itself
  the Hard Rule #9 result.

Refs: CI-113, CI94-D8, CI94-Q1


## v0.1.0 (2026-08-07)

### Bug Fixes

- **cli**: Anchor the userinfo mop-up on the host
  ([`31cb589`](https://github.com/kmbhm1/castiron/commit/31cb5898569bce2191c024cf8e787688e440f6b0))

Review of PR #8 found the mop-up masks nothing when the password itself contains a colon.
  `http.client._get_hostport` slices the netloc at `host.rfind(':')`, so the fragment it quotes back
  is the password's SUFFIX AFTER ITS LAST COLON -- not the password. Searching for
  `f'{secret}@{host}'` with the whole password therefore matched nothing:

URL : https://user:1:eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9@x.supabase.co/rest/v1/ REDACTED : Could
  not reach https://user:***@x.supabase.co/rest/v1/: nonnumeric port:
  'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9@x.supabase.co'

⚠ The urlsplit oracle test cannot catch this: urlsplit().password is `1:eyJ...`, which is absent
  from the message, so `password not in redact(...)` passes while the JWT prints in full. Not
  CLI-reachable today (the boundary refuses first) -- but this is the defence-in-depth layer, and
  CI-010's DSNs reopen the path.

Anchor on the HOST instead, which subsumes every split point and any other renderer's choice of one.
  The rewrite is positional, so it is not gated on _MIN_REDACTABLE_KEY -- which is the point, since
  a short password is exactly what the spelling pass cannot reach. The URL occurrence is skipped
  when the run already ends in `***@host`, so the scheme and the diagnostic username survive, and
  quotes are excluded from the run so `nonnumeric port: '...'` keeps its quoting.

Two further findings from the same review, both pinned rather than assumed:

- The colonless branch was DOUBLY redundant. Deleting `found.append` alone, or not masking in the
  return alone, each left 880/880 green. The two mechanisms are now genuinely distinct -- the return
  decides that the *scheme* survives (`https://***@host`), registering the host is what masks a bare
  re-occurrence -- and each has its own test. The recurrence test uses a SHORT token deliberately,
  so the spelling pass cannot mask it and only the mop-up can.

- The comment claimed the mop-up "is exact, cannot mis-fire". False: it was an unanchored,
  un-length-gated str.replace, and `redact('https://a:b@ ; contact bob@example.com')` returned
  `bo***@example.com`. The empty-host guard removes that case entirely, and the docstring now states
  the real guarantee -- every non-whitespace, non-quote run ending in `@host`, for each host that
  carried userinfo in the same text -- including that an unrelated address at the *same* host is
  over-masked. _MIN_REDACTABLE_KEY's rationale (a short substring mangling unrelated text) does not
  apply to a positional rewrite; that is now argued in the docstring rather than silently waived.

Also pins the variadic wiring: mutating `_key_spellings(key, *url_secrets)` to `_key_spellings(key)`
  passed 880/880, because `test_spellings_of_several_secrets_are_merged_and_ordered` tests the
  function and nothing tested that `redact` feeds it. The new test puts the password's recurrence
  away from any `@host`, so only the spelling pass can reach it.

- **cli**: Close the debug-log key leak and four review findings
  ([`d29801b`](https://github.com/kmbhm1/castiron/commit/d29801b7a10796c009f7f53e23d6f4062db12bb0))

Remediates the CI-006 review round. The files overlap (gen.py and test_gen.py carry four of the five
  concerns), so this is one commit describing all of it rather than a split that would misattribute
  hunks.

1. HIGH -- the API key leaked on stderr under -vv/--debug. configure_logging's handler never passed
  through redact(), and the fetcher logs the normalized target at DEBUG while
  normalize_postgrest_url preserves the query string, so `--debug --from '...?apikey=SECRET'`
  printed the key in full directly above a correctly redacted Error: line. It compounds: the
  internal-error message tells users to rerun with --debug and report the output, so castiron was
  instructing people to publish their own secret. The redactor is now passed into configure_logging
  and installed on the HANDLER, not the `castiron` logger -- Logger.handle applies only the
  originating logger's filters, then callHandlers applies each handler's, and every castiron line
  arrives on a child logger by propagation, so a logger-level filter would have fired for none of
  them. The test gap that allowed this: TestSecrets only ran at the default verbosity. Secrets are
  now asserted across -v/-vv/--debug on both the query-string and the literal-key path.

2. The traversal guard was blind on Windows. PureWindowsPath('/evil.py') and 'C:evil.py' are both
  is_absolute() == False, yet `/` discards the left operand for both -- so `filename = "/schema.py"`
  in a committed pyproject.toml wrote to the drive root for anyone running castiron in that repo.
  Now also refuses drive- and root-anchored paths, proven with PureWindowsPath so the guard holds on
  a POSIX runner.

3. The Hint: lines of spec §3.1 were missing -- an undeclared deviation. The 401 hint naming the
  key's provenance (--key / CASTIRON_KEY / SUPABASE_KEY / none) is the mitigation CI6-Q2 accepted
  the ambient SUPABASE_KEY fallback on; without it a user who silently picks up another project's
  key has no way to find out. Also adds the --from hint for an unreachable source and the --schema
  hint for an empty schema, which names the document castiron actually read since the engine's
  message does not.

4. CI6-D5a (captain, 2026-07-31): config-file relative paths now resolve against the CONFIG FILE's
  directory, as ruff/mypy/coverage do, not the cwd. Running from a subdirectory previously failed to
  find `from`, or -- with an absolute `from` -- silently wrote into the wrong tree at exit 0. The
  config file exists so CI and local runs share one source of truth, and CI-021's check must not
  give a cwd-dependent answer.

5. Restores the version-gated tomllib import. The try/except form made mypy bind tomllib from the
  first import, which typeshed marks 3.11+ only; ignore_missing_imports silenced that and the module
  collapsed to Any, so tomllib.load and TOMLDecodeError stopped being typechecked entirely. It also
  failed --python-version 3.12/3.13 with no-redef, hidden only by the pinned python_version =
  "3.10". Both clause headers carry the pragma because coverage excludes a clause, not a statement
  -- 100% now holds on both matrix legs.

Folded in: `--filename .` no longer overwrites the output directory with a regular file at exit 0; a
  path-layer ValueError (an embedded NUL from a config filename) is exit 1, not exit 70 "this is a
  bug"; the resolved config file logs at INFO so -v shows it; a config `emit` naming no registered
  emitter is exit 1 naming the file rather than a bare exit 2; and _require_table/_castiron_table
  use the same article helper as _coerce.

Goldens unchanged, byte-identity with CI-005's golden re-verified.

- **cli**: Give key spellings a total order
  ([`421b854`](https://github.com/kmbhm1/castiron/commit/421b8541ca3b0565cd5958647f105bbe12f5db1b))

CI-065, from the CI-061 final review -- which falsified that row's own "zero mutation survivors"
  claim. `_key_spellings` emits four spellings (raw, trimmed, unicode_escape'd,
  escaped-and-trimmed), but each new one independently satisfied every existing assertion, so none
  was individually pinned and the longest-first ordering the docstring justified was unverified.
  Three items:

(a) Two tests that pin the individual spellings. The escaped-only case uses a key with an interior
  literal backslash, so trimming collapses onto the raw spelling and only unicode_escape can match
  what repr produced; the trimmed-only case uses a leading control character in text some layer
  already stripped. Both assert non-vacuity first. Measured: dropping any one of the four spellings
  now fails at least one test.

(b) The sort is now total. `sorted` is stable, so equal-length spellings kept their *set-iteration*
  order, which varies with PYTHONHASHSEED -- measured on main, `_key_spellings('\rabcdefgh ')`
  returns one order under seeds 0/3/6/7 and a different one under 1/2/4/5/8/9. Nondeterminism in a
  security-relevant path is against the spirit of Hard Rule #9 even though `redact` never touches
  emitted output. `key=lambda s: (-len(s), s)` fixes it; a test runs ten subprocesses under seeds
  0-9 and asserts one answer.

(c) The docstring claimed the escaped spelling covers "%r, !r, json.dumps and every other renderer".
  Measured counter-examples: unicode_escape escapes a non-ASCII character where json.dumps does not
  (so a key containing `é` survives), and leaves a quote alone where both json.dumps and repr escape
  it. It now claims exactly the %r/!r case, names the json.dumps limit, and points at `sanitize_key`
  as why the covered case is the one that matters. The mask is deliberately NOT widened -- that is
  recorded as deferred, not fixed here.

The variadic signature landed with CI-066, which needs it; this commit owns the ordering and the
  docstring so each subject is true of its own diff.

- **cli**: Mask URL userinfo and secret parameters
  ([`d6334fb`](https://github.com/kmbhm1/castiron/commit/d6334fb5191737e242db55bb9bfbe9d9d2924d81))

CI-066. Two credential paths printed in full on main, both reproducible offline with no socket:

$ castiron gen --from 'https://user:SECRETPASSWORD123@x.supabase.co' --dry-run Error: Could not
  reach https://user:SECRETPASSWORD123@x.supabase.co/rest/v1/:

nonnumeric port: 'SECRETPASSWORD123@x.supabase.co'

$ castiron gen --from 'https://x.supabase.co/rest/v1/?service_role_key=SEEKRIT' --dry-run Error:
  Could not reach https://x.supabase.co/rest/v1/?service_role_key=SEEKRIT: ...

The second one is the more dangerous of the pair: `_SECRET_QUERY` required the credential word to
  start immediately after the `?`/`&`, so every prefixed spelling -- service_role_key,
  sb-publishable-key, x-api-key, anon_key -- was invisible to it, and a service-role key outranks an
  anon key. `_SECRET_PARAM_NAME` now reads a credential word as a delimited *segment* of the
  parameter name (`-`, `_`, `.`, the ends, or a camelCase hump), matched against the percent-decoded
  name and rewritten as found; `#` joins the leading class because a Supabase auth redirect carries
  access_token in the fragment. Measured: no false positive on monkey, keyword, keys, tokens,
  turnkey, select, order or limit.

The first one appears TWICE, and only once in URL shape: http.client._get_hostport raises
  `nonnumeric port: '<pw>@<host>'` off the whole netloc, with no scheme in front. So a URL-only
  regex closes the visible half and leaves the other. `_mask_url_userinfo` therefore reports what it
  masked and mops up the bare `<pw>@<host>` exactly -- not gated on _MIN_REDACTABLE_KEY, which is
  what closes a four-character password.

Regex, not urlsplit, and argued rather than assumed: the leak arrives as an exception *message*, and
  urlsplit raises ValueError on precisely the malformed URLs this defends (`https://user:S@[::1`,
  `h:notaport`). A mask that raises on malformed input is a mask that leaks. The userinfo pattern is
  greedy so it backtracks to the LAST `@`, matching urllib's own netloc.rpartition semantics; tests
  pin it against urlsplit().password rather than against itself. The username survives
  (`user:***@host`) because "you connected as postgres, not app_user" is the diagnostic value of the
  line -- and because CI-010's postgresql://user:password@host/db DSNs will depend on exactly this
  shape.

Second layer, per CI-063's "both layers" and the captain's CI066-Q1 ruling: `--from` refuses a
  userinfo URL outright (exit 2) with a message that names the shape and never the value. It costs
  nothing -- urllib never applies userinfo as HTTP Basic auth, so such a URL cannot succeed under
  any circumstance -- and it removes the trigger instead of masking the symptom. Wired as a click
  callback so CASTIRON_FROM, SUPABASE_URL and the [tool.castiron] default map are covered too.

Tests follow CI-063's lesson (test the encodings the input can actually arrive in: canonical, repr,
  %r-of-bytes, JSON, quoted, percent-encoded, truncated) and CI6-Q7's (enumerate every printed
  surface, not a sample). Every new assertion was run against unpatched code and observed to fail;
  seven mutants, zero survivors.

- **cli**: Redact a schemeless userinfo --from
  ([`a559222`](https://github.com/kmbhm1/castiron/commit/a5592222104e93d12df0f89328f39a04970cda46))

CI-068, folded in by captain ruling. I reported this and stopped; the review then found the part
  that raises the stakes -- the secret does not have to be on the command line:

$ SUPABASE_URL='postgres:SECRET@db.x.supabase.co:5432/postgres' castiron gen --dry-run Error: --from
  'postgres:SECRET@db.x.supabase.co:5432/postgres' is neither a URL nor an existing file. ...

CASTIRON_FROM, SUPABASE_URL and a `from = "..."` in pyproject.toml all reach the same echo, so "it
  was in the shell history anyway" does not hold. Note the asymmetry PR #8 itself created:
  `postgresql://user:pw@host` is now safely refused, while the schemeless `postgres:user:pw@host` --
  the shape psql connection strings actually circulate in -- printed in full. urlsplit reads
  `postgres:` as the scheme, so there is no `//` for `_URL_USERINFO` to anchor on.

Loosening that anchor is not an option, and that analysis stands: `redact` scans arbitrary prose,
  where a bare `a:b@c` is far more often ordinary text than a credential. The fix avoids the
  question entirely. `redact_source` is a SINGLE-OPTION-VALUE transform used only by the one surface
  that echoes the raw --from value back, so there is no surrounding text for a false positive to
  land in, and it can be more aggressive than `redact`: everything up to the value's last `@` is
  masked (rpartition, matching how urlsplit derives userinfo from a netloc). It composes with
  `redact` rather than replacing it, so the query-parameter and `scheme://` rules keep working.

Accepted cost, asserted rather than left to be discovered: a NONEXISTENT path containing an `@` is
  over-masked in this one message (`./my@dir/x.json` -> `***@dir/x.json`). Only the "neither a URL
  nor an existing file" path prints it, so a path that resolves is never affected, and over-masking
  a filename the user just typed is far cheaper than printing a DSN password.

Also carries the CLI-level test that the previous commit's scheme scoping earns: a postgresql:// DSN
  reaches the source layer instead of being refused, with its password still masked out of the
  message.

- **cli**: Redact exception text and stack info too
  ([`29953c9`](https://github.com/kmbhm1/castiron/commit/29953c9f106ace6bf27d59b3701448225cb1888d))

`RedactingFilter` masked `record.msg` only, but `logging.Formatter.format` concatenates the message
  with `record.exc_text` (the `exc_info` traceback) and `record.stack_info`. A
  `logger.exception(...)` would therefore print the raw exception message, the traceback, and the
  source lines `stack_info` renders -- at ERROR, which the default WARNING threshold shows with no
  `--debug` asked for. That reopens the `?apikey=...` leak CI-006 closed, in the very output
  castiron's internal-error message tells users to paste into a public issue.

Nothing in `src/` writes `logger.exception` today (`grep -rn 'exc_info\| \.exception('` finds none),
  so this is forward risk rather than a live bug -- but the first live-DB/MySQL/DDL source adapter
  that logs a failed request is the trap, so it lands before them. `exc_info` is rendered through a
  default `logging.Formatter` (matching the handler's own layout byte for byte) and then cleared, so
  a formatter that ignores `exc_text` cannot re-derive the unredacted traceback from the live
  exception.

Every new assertion was verified fallible against the unpatched filter.

Refs CI-061, CI6-Q7, CI6-D7.

- **cli**: Redact the --debug traceback
  ([`d8dc8a6`](https://github.com/kmbhm1/castiron/commit/d8dc8a6045021321cac3f13b1c9f9b6d3383cab8))

CI-062. `cli_error_handling` re-raised an unexpected exception under --debug so the interpreter
  would print the traceback -- which means that output never passed through `redact`. The `Error:`
  line above it was clean; the chained block below it was not:

castiron.sources.errors.SourceFetchError: https://x.supabase.co/rest/v1/?apikey=SUPERSECRET failed

During handling of the above exception, another exception occurred: ... RuntimeError: inner blew up
  while handling the fetch failure

Aggravated by `internal_error_message`, which asks the user to rerun with --debug and paste the
  result into a public issue. Same class as CI-061's `RedactingFilter` gap, one layer up: castiron
  now renders the traceback itself with `traceback.format_exception(exc)` (the single-argument form
  is 3.10+, castiron's floor) and echoes it through `redact`, so the __cause__/__context__ chain is
  masked like every other printed string.

Do not inherit the row's original premise -- "every SourceError becomes a redacted ClickException
  first, so the key is safe" was false, and CI-063 records why. This path is not a live leak on
  main; it opens the moment a non-SourceError carries a URL in its str(), or one is raised while a
  SourceFetchError is being handled. Both shapes are now tested.

Consequence, and the captain's CI066-Q2 ruling: an internal error under --debug now exits 70
  (EX_SOFTWARE) instead of Python's 1 for an uncaught exception. That is what CI6-D9's table always
  said, and it removes a caveat from the scripting contract rather than adding one. castiron has
  never been released, so no user contract breaks -- and this stops being free the instant 0.1.0
  ships. `docs/reference/exit-codes.md` carried an admonition stating the old behaviour as fact; a
  reference table made false by a change is a defect of that change, so it is rewritten here rather
  than deferred. The rejected alternative (print redacted, then re-raise anyway) would have had the
  interpreter print the *unredacted* traceback a second time, defeating the row.

Two tests asserted the behaviour this deliberately changes
  (test_debug_re_raises_the_original_exception,
  test_debug_lets_the_exception_escape_so_python_prints_the_traceback); both are replaced, not
  dropped. Every new test asserts non-vacuity (a traceback really was printed) before asserting the
  secret is absent, and was observed to fail against the unpatched handler.

- **cli**: Redact the local-path origin surfaces too
  ([`65a59c7`](https://github.com/kmbhm1/castiron/commit/65a59c7bebd5e493ef5eb458f81306d9fe7bcca7))

The seven-site enumeration missed an eighth: `source_origin`'s non-URL branch returned
  `str(Path(source))` and `load_schema` returned `str(path)`, both unredacted, both printed -- the
  first into `schema_hint`'s "castiron read <origin>" line, the second into the run summary.
  `source_origin`'s docstring already promised "redacted and safe to print".

A filesystem path is an unlikely place for a key, so this is low risk -- but the claim being made is
  an enumeration with no exceptions (CI6-D7), and `?apikey=` survives a URL pasted into a `curl -o`
  filename.

Both new tests verified fallible against the unredacted returns.

Refs CI-061 fix round, CI6-D7.

- **cli**: Restore the key redaction to redact_source
  ([`17424ff`](https://github.com/kmbhm1/castiron/commit/17424ff0c0eb8f4d55cc4f47bbac875b7fe082ba))

Regression I introduced in `fix(cli): redact a schemeless userinfo --from`. `gen.py` called
  `redact(source, key)`; I replaced it with `redact_source(source)`, and `redact_source` ended at
  `return redact(source)` -- no key. So the commit that closed a credential leak on this surface
  opened a different one on the same surface. Measured:

$ castiron gen --from 'nosuchfile-<jwt>.json' --key '<jwt>' main : Error: --from
  'nosuchfile-***.json' is neither a URL nor ... branch : Error: --from 'nosuchfile-<jwt>.json' is
  neither a URL nor ...

Same for any query parameter outside the credential-word list: `?bearerthing=<key>` leaked on my
  branch and is masked on main. That is CI6-D7 ("redact the key from *every* printed string") broken
  on spec §11.2's surface 7 -- the one surface the spec singles out as needing key coverage.

`redact_source(source, key)`, threaded from `gen`.

⚠ Why no test caught it, which is the part worth keeping: every CI-068 test ran without --key, so
  the suite did not merely miss the leak, it actively PINNED the key-dropping behaviour -- a mutant
  that removed the key argument was killed by nothing and would have looked equivalent. That is
  CI6-Q7 exactly ("was this path ever exercised?"), and the enumeration discipline from round 2 did
  not catch it because I enumerated the constructs I wrote rather than diffing behaviour against
  main.

The generalizable rule, since this is the second time a fix round introduced what it was fixing:
  when a round adds a new call site or a new wrapper, run the same command on both branches and diff
  the OUTPUT. New tests passing is not the check; the behavioural differential is.

Both regressions in this round were found that way by the reviewer, and both are now pinned by tests
  observed to fail against the unpatched code.

- **cli**: Scope the userinfo refusal to http sources
  ([`5f93f27`](https://github.com/kmbhm1/castiron/commit/5f93f27cf48a3e2e41d87252bd42636d89fc009b))

Review of PR #8: `reject_url_userinfo` refused a userinfo URL of ANY scheme, and a test pinned
  `postgresql://postgres:...@db...` as refused at exit 2. The measurement that justified the refusal
  -- that castiron cannot successfully fetch from such a URL under any circumstance -- is about
  *HTTP* fetching: urllib never applies userinfo as HTTP Basic auth, so http.client either fails to
  parse the netloc or fails to resolve a host named `u@host`. It does not extend to psycopg.

`postgresql://user:password@host/db` is the canonical libpq connection string. When CI-010's
  live-database source lands, the standard way to name a Postgres source would have been refused
  with a message telling the user to "pass the key with --key or CASTIRON_KEY" -- meaningless for a
  DSN. And the contradiction was already in the tree: `redact`'s docstring justifies masking
  userinfo *precisely because* CI-010 will carry those DSNs, while this function refused them
  outright.

Scoped to `URL_SCHEMES`, imported from `cli.config` rather than redeclared -- that constant's own
  comment says it exists so exactly one rule decides what counts as a network source, and `config`
  does not import `errors`, so no cycle. The spec's "no import from cli/config.py" note was about
  avoiding `looks_like_url`; a shared scheme tuple is the opposite of a duplicated rule.

The two layers now have deliberately different scopes, which is the correct shape rather than an
  exception: the boundary refuses what cannot work, the mask covers everything castiron might print.
  The DSN case loses no protection -- the new test asserts the password is still masked out of a
  message naming the DSN it accepts.

- **cli**: Stop a rendered API key from printing in full
  ([`4c1ac75`](https://github.com/kmbhm1/castiron/commit/4c1ac75547e3d0f1d1d1ec26a45b7b66819f849b))

`redact` masks the key only where it appears VERBATIM, so any surface that renders it escapes past
  the mask. `http.client.putheader` does exactly that: a key carrying a control character trips
  `ValueError('Invalid header value %r' % value)`, whose `%r` escapes the character that broke it.
  The ValueError is wrapped as a SourceFetchError, the error boundary dutifully calls
  `redact(str(exc), key)`, and it matches nothing -- the whole JWT prints on the ordinary `Error:`
  line at exit 1, offline, with no flags. `putheader` validates before the socket opens, so no
  network is needed to reproduce it. The realistic trigger is mundane: a key file with Windows line
  endings (`--key "$(cat key.txt)"` strips the \n and leaves the \r) or a paste with a stray
  newline.

Defended in both layers, per the captain's call:

1. `sanitize_key`, wired as the `--key` callback so it covers the CASTIRON_KEY and SUPABASE_KEY
  fallbacks too, trims surrounding control characters (a CRLF key file has exactly one sensible
  reading) and REFUSES an interior one, naming the likely cause and never the value. The trigger
  never reaches an HTTP client. 2. `_key_spellings` widens `redact` to the trimmed and escaped
  spellings, so `%r`, `!r` and `json.dumps` are covered for whatever renderer nobody has found yet.
  A clean key is masked exactly as before.

The lesson is one level past CI6-Q7: a mutation harness proves an assertion is real and a path was
  exercised, but it cannot find a `redact` call that is present, tested, and simply does not work on
  the value it is given.

Every new assertion was verified fallible against main's behaviour, both defences off.

Refs CI-061 fix round, CI6-D7, CI6-Q7.

- **cli**: Stop the --from callback raising on a bad URL
  ([`f9e041d`](https://github.com/kmbhm1/castiron/commit/f9e041ddb2642ae8b77d34f94c79cff60d3c38a1))

Regression I introduced in `fix(cli): scope the userinfo refusal to http sources`. Reading the
  scheme with `urlsplit` reintroduced exactly the failure mode CI-066-D1 rejected it for -- it
  raises on the malformed URLs this row exists to defend -- at the one place it does the most
  damage.

The option callback runs inside click's `make_context`, OUTSIDE `cli_error_handling`, so the
  ValueError escaped the error boundary entirely. Measured against a real process:

main my branch exit code 70 1 traceback, no --debug no yes, unredacted

$ castiron gen --from 'https://user:SECRETPASSWORD123@[::1' --dry-run ValueError: Invalid IPv6 URL

That regressed three things at once: CI6-D9's exit-code contract, which docs/reference/exit-codes.md
  states as "every path out of gen ends at one of the codes above"; **CI-062, which is commit 2 of
  this same PR**, whose entire purpose is that no unredacted traceback prints without --debug; and
  the spec's own CI-066-D1 rationale. It is a credential surface too: urlsplit's `_checknetloc`
  ValueError quotes the whole netloc, so a URL with U+FF20 (NFKC-normalizes to `@`) prints the
  password inside the raw traceback -- that leak is pre-existing on main, but the
  raw-traceback-at-exit-1 presentation was new.

Split the scheme off by hand instead: `source.split('://', 1)[0].lower()`. No parse, nothing to
  raise, and `.lower()` keeps urlsplit's case-folding (RFC 3986 schemes are case-insensitive) --
  pinned by its own test, since dropping it was a silent survivor.

Reachable through --from, CASTIRON_FROM, SUPABASE_URL and [tool.castiron] from.
  `TestRejectUrlUserinfo`'s clean-source list had no malformed URL and
  `test_a_malformed_url_never_raises` guards `redact`, not the boundary; both gaps are now closed.

⚠ Note for whoever writes the next CLI test: the obvious spelling of the end-to-end assertion
  (`'Traceback' not in result.output`) is VACUOUS. Measured -- CliRunner swallows an escaping
  exception into `result.exception` and leaves `output` empty, so it passes against the bug. The
  test asserts `isinstance(result.exception, SystemExit)`, because every path out of the CLI is a
  SystemExit and anything else escaped.

- **emitters**: Emit an isort-clean import block
  ([`cf131b4`](https://github.com/kmbhm1/castiron/commit/cf131b43de3075232f419f3679976985844544d1))

Every module castiron has ever emitted reports `I001` under default ruff settings, and a codegen
  tool whose first published output trips the linter of the project it was just added to has spent
  its credibility on contact. Measured on origin/main: 512 of 512 reachable emissions (4 corpus
  inputs x 128 config points).

This is three changes, not one -- `render_import_block` alone does not clear it:

1. `emitters/base.py::render_import_block` now groups the way ruff's isort does at its defaults:
  `__future__` / stdlib / third-party sections one blank line apart, `import X` before `from X
  import ...` within a section, same-module imports merged, and names ordered by `order-by-type`
  (which is why `UUID4` precedes `BaseModel`; plain alphabetical stays dirty). The target was
  derived by running `ruff check --select I --fix` over castiron's whole import vocabulary, not from
  the isort documentation.

2. `emitter.py::_write` joins the import block to the body with ONE blank line, not two. Measured
  both directions: ruff accepts exactly one blank before a comment and two before code, and the
  section after the imports always opens with `# CUSTOM CLASSES`. `ruff format` agrees with the
  one-blank form, so there is no check-vs-format conflict.

3. `emitter.py::_imports` imports `pydantic.Field` only when the body calls it. It was
  unconditional, and that was a live `F401` in 32 of the 512 emissions -- all reachable through
  `castiron gen --no-crud-models --no-null-parent-classes`. The body is now rendered first and the
  imports computed from it, so the condition cannot drift from the renderer that creates it.

Hard Rule #9 is the live risk: `_imports` builds a `set`, and grouping replaces one total sort with
  three nested orderings. All three keys are total by construction; 1000 shuffles and 11
  `PYTHONHASHSEED` values each produce exactly one output, and the corpus A5 sweep already targets
  the richest import block castiron can emit.

Nothing below the import block changes. Asserted, not assumed: over all 512 emissions against
  origin/main the import region moved in 512 and the body in 0, and each of the six committed
  goldens is byte-identical from its first section comment onward. That invariant is what lets the
  CI-080 / CI-092 / CI-075 fixes land next with a purely semantic golden diff.

Adds `tests/unit/corpus/test_lint.py`, a pinned ruff subprocess over every reachable emission. An
  `ast` invariant cannot detect `I001` at all -- import ordering is pure style -- so ruff is the
  only honest oracle. It lints a `tmp_path` `.py` copy so the goldens stay `*.py.txt` and out of
  `ruff format .`'s reach, and it accounts for every finding by named open row rather than asserting
  a blanket clean: `CI-092`'s two entries are still open, and the guard goes red when they land so
  it tightens itself.

Refs: CI-094, CI94-Q3 (captain override), CI94-D8/D9/D10/D12

- **emitters**: Emit valid enum member identifiers
  ([`34f568c`](https://github.com/kmbhm1/castiron/commit/34f568cb80fbf619f16c55ec233b0d0be92437c1))

castiron emitted Python that does not parse, and exited 0 doing it.

`python_member_name` was `value.lower()`, so a Postgres enum label carrying a space, punctuation or
  a leading digit became an invalid identifier on the LEFT of an enum member line:

class PublicJobStateEnum(str, Enum): IN PROGRESS = "in progress" DONE! = "done!" 2FAST = "2fast" #
  SyntaxError: invalid decimal literal

The whole module fails to parse, so every model in it is unreachable -- and `castiron gen` printed
  `wrote .../schema.py` and exited 0, which means a future `check`-mode user would have seen green.
  That is what made this a release blocker rather than a cosmetic bug.

Replaced by a per-enum `python_member_names(enum) -> list[EnumMember]`: a collision rule is not
  expressible one label at a time (CI94-D1). It takes the IR's `EnumInfo` rather than a new shape
  (Hard Rule #6). Character map, empty and leading-digit guards, the reserved guard unchanged, then
  uniquify.

Two details that are load-bearing rather than incidental:

* Uniqueness is keyed on the NFKC-NORMALIZED candidate (CI94-Q1, captain). Python NFKC-normalizes
  identifiers at compile time, so `fi` and the ligature `<U+FB01>` are ONE binding;
  `str.isidentifier()` cannot see it and `.upper()` performs the same folding. A raw check emits a
  module that raises TypeError at IMPORT -- this defect in a new costume. So the tests execute the
  emitted module, they do not merely compile it. * Colliding labels get `_2`, `_3`, ... and a `#
  original name was "..."` comment. No label is ever dropped, and the value literal is always exact.
  The accepted cost is recorded in CI94-Q1: inserting a label upstream that sorts before an existing
  collider renumbers the later ones.

The label inside that comment now renders through `_py_string` (CI94-D3). The spec called this
  unreachable; it is not, and only because of this fix -- the reserved guard now reads the
  *sanitized* name, and `dir(builtins)` contains `__doc__`, so the label "\n\ndoc\n\n" maps to
  `__DOC__` and fires it. A raw label would split the comment across lines and break the module. The
  collision comment reaches it more directly still.

CI-085 stays open and `synthetic-torture` still declares `compiles=False`: the module now has valid
  enum member names and still does not parse, because the COLUMN names are a different call site.
  Two defects, one symptom -- and fixing one and not the other is now the demonstration rather than
  the argument.

Refs: CI-080, CI-094, CI94-Q1, CI94-D1, CI94-D2, CI94-D3, CI94-D7

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **emitters**: Escape newlines in string literals
  ([`ea3d69a`](https://github.com/kmbhm1/castiron/commit/ea3d69aa4cda3ba386d2620f35a8fdb7d02ef851))

`_py_string` escaped only backslashes and double quotes, emitting every other character raw. A
  newline therefore produced an unterminated single-line string literal, i.e. generated Python that
  does not parse:

note: str | None = Field(default=None, description="line one line two") SyntaxError: unterminated
  string literal

A multi-line `COMMENT ON COLUMN` is ordinary SQL and PostgREST carries it verbatim in
  `properties.<c>.description`, so this fires on real schemas with no flags and no unusual input.
  Reproduced on 026af0f, unpatched.

`json.dumps(..., ensure_ascii=False)` fixes it: JSON's escape alphabet is a strict subset of
  Python's, so the literal is accepted verbatim, while `ensure_ascii=False` keeps non-ASCII readable
  (the CLI writes UTF-8).

Not a strict no-op: a TAB now renders as `\t` where it was previously emitted raw. Both parse; the
  escaped form is the correct one, and a test pins the difference so it stays deliberate. No
  committed golden contains a tab, a newline or a control character, so neither golden moves.

This is exactly the CI-063 class: the call was present, enumerated and pinned by passing tests, and
  the transformation was still wrong for an encoding the input actually arrives in.

Refs: CI9-Q1

- **emitters**: Give an empty enum a class body
  ([`01fda6a`](https://github.com/kmbhm1/castiron/commit/01fda6a46c192f16fa4834d5f7d9cf668b1a9b50))

`CREATE TYPE t AS ENUM ()` is legal Postgres. PostgREST reports it as `"enum": []`, it reaches
  `schema.enums` through the real source path, and the emitter wrote a class header with NO BODY:

class PublicJobStateEnum(str, Enum):

# CUSTOM CLASSES

`IndentationError`, at exit 0 -- the same "unparseable output, green exit" class this row exists to
  close, and the same whole-module blast radius.

PRE-EXISTING, not a CI-094 regression: verified byte-identical on origin/main @ 0a70513. Folded in
  because this is the last code row before an immutable publish, and kept as its own commit so it
  stays severable if the scope is questioned.

Emits `pass` rather than skipping the class. The column carrying the type still annotates itself
  `PublicJobStateEnum`, so omitting the class would trade an IndentationError for a NameError. An
  empty `Enum` subclass is valid and is the honest rendering of an empty Postgres enum. A
  counter-witness asserts `pass` appears only when the body would otherwise be empty.

No committed golden moves: no corpus input carries an empty enum.

Refs: CI-094

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **emitters**: Reject enum member names Enum reserves
  ([`b38efdd`](https://github.com/kmbhm1/castiron/commit/b38efdd3015d75159df1c5f82b9b4eb7bb44e346))

`.isidentifier()` is necessary and NOT sufficient. The CI-080 fix produced member names that are
  valid identifiers and unusable as Enum members, so the defect moved rather than closing: same exit
  0, same whole-module blast radius, `py_compile` still passing.

Three reserved shapes, all reachable from ordinary Postgres labels:

'(none)' -> _NONE_ _sunder_ ValueError at import, module unusable '__init__' -> __INIT__ __dunder__
  member SILENTLY DROPPED '' + ' ' -> _, __2 mangled 3.11+ drop it; 3.10 emits `_E__2`

A TRAILING SPACE in a CREATE TYPE is enough to trigger the first: ' x ' becomes _X_. Symmetric
  punctuation does it too -- (pending), [x], -tbd-, <null>, {draft}, .dot., "quoted".

The third one is the worst and it is castiron's own doing: the COLLISION SUFFIX creates it. Four
  labels that sanitize alike get `_`, `__2`, `__3`, `__4`, and Python name-mangles anything with two
  leading and fewer than two trailing underscores at COMPILE time. On 3.11+ those labels vanish; on
  3.10 the member is named after the enum CLASS (`_E__2`), so emitted meaning depended on the
  interpreter -- a Hard Rule #9 problem on top of a correctness one. Two of the three violate
  CI94-Q1's one non-negotiable outright: never drop a variant.

Fixed by `_repair_enum_shape`, applied after the leading-digit guard AND to every collision
  candidate. It appends `_` until the name is clean, which terminates in at most three appends by
  proof rather than observation: a name ending in three or more underscores can be none of the three
  shapes, and each iteration adds one. `__2` is the worst case and needs all three.

`_is_enum_reserved_shape` is deliberately NOT `enum._is_sunder` et al: those are private,
  unguaranteed and demonstrably version-skewed (3.13 dropped a clause from `_is_private`; 3.10 words
  the sunder error differently). This is CI94-D8's pattern -- state the rule in src/ so emitted
  bytes never depend on a CPython internal, and check it against the interpreter in a test. That
  test compares against EXECUTED BEHAVIOUR over 254 generated names, not against the private
  predicates, so it is immune to their skew and runs on all four legs.

WHY IT GOT THROUGH, since that is the more useful half: the right assertion and the right corpus
  both already existed and were pointed at each other's targets.
  `test_any_enum_label_yields_an_addressable_member` executes the module -- but ran over
  ADVERSARIAL_TEXT, which is CI-009's docstring/comment corpus with no symmetric-punctuation label.
  `TestEveryMemberNameIsAnIdentifier` had the trigger committed in its own corpus ('"quoted"' ->
  _QUOTED_) and asserted only `.isidentifier()`. The naming corpus is now module-level and shared,
  the executing test runs over the union, and a guard fails if they are separated.

The 15 new labels were added to that shared corpus, so every future enum test inherits them. No
  committed golden moves: no corpus label reaches any of the three shapes.

Refs: CI-080, CI-094, CI94-Q1, CI94-D8

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **emitters**: Scope the isort claim to what castiron emits
  ([`7ecbcee`](https://github.com/kmbhm1/castiron/commit/7ecbcee87550f56bba5017e073f8b78223a6c5d9))

`render_import_block` claimed to match "what `ruff check --select I` produces under default
  settings". Measured against real ruff, it diverges on five shapes. None is reachable from
  castiron's own vocabulary -- the complete 32767-subset power set is still I001/F401-clean -- but
  the function and `STDLIB_MODULES` live in `emitters/base.py`, shared with CI-012 and CI-030, where
  the next author reads that sentence as a guarantee. A claim broader than what holds is the CI-097
  / CI-104 shape.

Two of the five were sort-key bugs and are now fixed, because they cost six lines and move no
  emitted byte:

- CONSTANT requires len > 1, so `T` ranks as a variable and sorts last. `name.isupper()` alone put
  it first. - Names and modules order naturally, not lexicographically: `Item2` before `Item10`,
  `pkg2` before `pkg10`.

The other three are design gaps, not sort keys, so they are documented precisely and their
  unreachability is now asserted rather than trusted: an unlisted stdlib module lands in third
  party, relative imports get no LOCALFOLDER section, and `as` aliases are merged where ruff splits
  them. `TestDivergencesFromRealIsort` pins both what the renderer does and that nothing castiron
  can emit reaches it.

`STDLIB_MODULES` is deliberately not pre-widened -- an entry no importer reaches is unfalsifiable.
  Instead the classification guard now enumerates the import literals of EVERY module under
  `castiron/emitters/` rather than the Pydantic emitter's alone, so a future emitter's `import uuid`
  fails that test instead of silently re-opening I001 for its users.

Also folded in, from the same review:

- `lint()` passes a timeout; a hung ruff failed the gate by hanging it. - The synthetic-torture
  exclusion matches both ruff spellings of a syntax error. The pre-commit pin (0.6.9) prints
  `SyntaxError:` and the resolved version (0.16.0) prints `invalid-syntax:`; that skew is CI-105 and
  still open, so matching one spelling passes vacuously under the other. - The import-grouping
  contract test checks module order PER KIND. Across a whole section it was wrong -- a section
  deliberately puts every `import X` before every `from X import ...`, so `import typing` + `from
  datetime import date` is correct isort output that the old assertion rejected. It passed only
  because 'datetime' < 'enum'. - `_FIELD_CALL` and the three sites that render a Field call now
  share `_field_call()`, so the sentinel cannot drift from the emitted text. - The golden `.py`
  naming guard scans all three golden directories, not just the corpus's. The other two are exactly
  the ones no tool regenerates. - `test_the_module_never_uses_a_name_it_did_not_import` promised
  more than it checked; renamed to what it does. - `sections: list[str]` annotated; the Hard Rule #9
  paragraph now names the 4-tuple the code actually sorts on; two type-map docstrings no longer say
  emitters build "one flat, sorted import set".

`is_import_statement` catches ValueError alongside SyntaxError: the emitter carries NUL-bearing
  literals for CI-009, and `ast.parse('\x00')` raises ValueError on py3.10 where later versions
  raise SyntaxError. Caught by the 3.10 leg of `make test-matrix` and nowhere else -- CI-081/CI-082.

Refs: CI-094, CI-105

- **emitters**: Strip NUL from generated docstrings
  ([`b13220d`](https://github.com/kmbhm1/castiron/commit/b13220d3a1ffdaff5f3a3e946fa9db910b79eecc))

U+0000 is the one code point no escaping saves. A raw NUL anywhere in a module makes CPython raise
  `SyntaxError: source code string cannot contain null bytes` at import, so castiron wrote schema.py
  successfully and the user's import failed -- the exact failure shape commit 1 exists to prevent.

Reproduced end to end, unpatched, from a document whose description is the JSON string "ab":

TableInfo.description == 'a\x00b' -> castiron writes schema.py (1372 bytes) generate SUCCEEDS ->
  import schema SyntaxError: source code string cannot contain null bytes

The PR previously half-closed the hole: commit 1 made `_py_string` NUL-safe via json.dumps, so the
  COLUMN-comment path went from broken-on-main to fixed, while the new table-docstring path became
  the one place a NUL still broke.

"Postgres text cannot contain NUL" is true of Postgres and was recorded at the site -- but it is a
  property of one SOURCE, not of the input. The OpenAPI source accepts any JSON document via --from,
  and is an ordinary JSON escape. The renderer must be total over its actual input domain, not over
  the domain its best-behaved caller happens to supply.

STRIPPED rather than escaped, and the choice is measured, not taste. All three candidates compile;
  only stripping preserves decision D6 (an empty or whitespace-only comment is indistinguishable
  from an absent one):

strip -> NUL-only comment collapses to "no comment" D6 HOLDS visible \x00 -> emits a body of four
  chars the user never wrote real NUL escape -> emits a body, and relocates the NUL into __doc__

Nothing is lost from the system of record: the builder does not strip, so TableInfo.description and
  Schema.as_dict() still carry the NUL.

Removal precedes .strip() -- .strip() does not treat NUL as whitespace, so strip-then-remove leaves
  a NUL followed by two spaces and 'a' as ' a', rendering six spaces of indent instead of four.
  Pinned by test (mutant N3; the symmetric NUL cases could not kill it).

Neither golden moves: no fixture contains a NUL.

Also documents that duplicate table_details rows for one (schema, table) resolve last-row-wins --
  deliberate, stateless and order-deterministic, unreachable from the OpenAPI source (one row per
  sorted definition key) but reachable by CI-010, whose rows come from a query that could LEFT JOIN
  a table twice.

Verified on 3.10/3.11/3.12/3.13: 1149 passed, 100.00% coverage.

Refs: CI-009

- **emitters**: Test the enum shape on the NFKC-normalized name
  ([`5e1e111`](https://github.com/kmbhm1/castiron/commit/5e1e111ef53877e4bba7f74d8c1bf46701d055a6))

The three reserved shapes were still all reachable, because the predicate read the RAW name while
  CPython normalizes identifiers to NFKC at COMPILE time.

Seven identifier-continue codepoints normalize to `_` and six are non-ASCII (U+FF3F FULLWIDTH LOW
  LINE, U+FE33, U+FE34, U+FE4D, U+FE4E, U+FE4F). `_identifier_characters` keeps them by CI94-D2 and
  `.upper()` leaves them, so:

'_x＿' -> _X＿ NFKC-> _X_ ValueError: _sunder_ names ... reserved '＿x' -> _＿X NFKC-> __X silently
  dropped; E('＿x') raises

Over {_, x, U+FF3F} lengths 1-4 (120 labels), measured per label: 2 raised at import and 49 were
  silently dropped. Both are CI-080's failure mode again, and the dropping half violates CI94-Q1's
  one non-negotiable.

The mechanism was already documented 60 lines below and applied to the uniqueness key -- and only to
  the uniqueness key. The interpreter said so plainly: it printed `_sunder_ names, such as '_X_'`,
  the NORMALIZED name, not the `_X＿` castiron wrote.

Fixed CONVERGENTLY rather than with a fourth special case: the predicate normalizes first, so it
  sees exactly what the compiler sees BY CONSTRUCTION. Python's identifier normalization *is* NFKC,
  so there is no further shape waiting on this axis. The <=3-append bound survives, because the
  appended character is ASCII `_`, which is NFKC-invariant.

Two docstrings corrected, both of which were actively dangerous:

* `python_member_names` claimed "one append is provably enough" and said "both predicates" when
  there are three. One append is NOT enough for the private shape -- `' x'` -> __X -> __X_ -> __X__
  -> __X___ needs all three. That wrong proof sat on the PUBLIC function while
  `_repair_enum_shape`'s own docstring was right, so a maintainer had a written licence to replace
  the loop with a single `+ '_'` and silently reopen the mangling defect. * `EnumMember.name` stated
  the "usable as an Enum member" guarantee unconditionally. It has one limit: CPython's
  `_is_private` also takes the ENCLOSING CLASS NAME, which this function does not know. Unreachable
  through the Pydantic emitter (verified), but `python_member_names` is documented as
  emitter-agnostic and anticipates reuse. A guarantee without its limits is the sentence CI-077
  exists to punish.

The cross-check test that called itself "the only authority that matters" was structurally blind to
  this: it enumerated `product('_A', ...)`, an alphabet that CANNOT contain an NFKC-active
  character. Its alphabet now carries one, and "usable" is judged modulo NFKC -- demanding byte
  equality would flag benign normalization as a defect and let a genuinely dropped member hide
  behind it.

Also adds a generated ORACLE (`TestTheOracle`): every string over {_, U+FF3F, A, 2, space} up to
  length 4 -- 780 labels -- through the real transform, into a real enum, every label asserted to
  round-trip. It enumerates the CHARACTER CLASSES that produce shapes rather than the shapes
  themselves, so a fourth shape on this axis fails without anyone naming it first. Three review
  rounds all found "the right assertion pointed at the wrong corpus"; a bigger hand-written corpus
  is the wrong answer to that.

Mutation-tested with caches cleared (CI-101 clause 3), 5 mutants, all caught: neutered repair,
  one-append-instead-of-loop, dropped NFKC, dropped private clause, unrepaired collision candidate.

Corpus additions: ' x' and ' 2' (no existing label reached the mangled shape from a SINGLE label --
  it arrived only via the collision path) plus four NFKC-active labels. Stale `# step 5` comments
  renumbered to 6, and a comment pointing at a test name that no longer existed now greps.

Refs: CI-080, CI-094, CI94-Q1, CI94-D2, CI94-D8, CI-072, CI-077

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **ir**: Agree on empty namespaces, length-check UNIQUE
  ([`8b0ab5e`](https://github.com/kmbhm1/castiron/commit/8b0ab5e4b99f07b824aa825ce109b037fb4f9568))

Three CI-005 review leftovers in the builder, all unreachable from real PostgREST output, all traps
  for CI-011's author:

1. `_rank_enum_candidates` read an empty token namespace as "bare" via `token_namespace or
  default_schema`, so a degenerate `.status` token resolved three ways in one document --
  `public.status` for a parameter, nothing for a scalar or array column. Qualification is now
  decided by `is not None`, so an empty qualifier is a qualifier everywhere. 2.
  `determine_relationship_type` length-checked PRIMARY KEY but not UNIQUE. A composite `<pk/>` on a
  view is recorded as `UNIQUE (a, b)` and `update_columns_with_constraints` sets `is_unique` on
  *both* members, so an FK naming one member alone claimed MANY_TO_ONE and would emit a singular
  attribute where a list belongs. `_is_singly_unique` mirrors the sole-PK check, still trusting the
  column flag when no UNIQUE constraint names it. 3. `_find_enum_type` and `_rank_enum_candidates`
  documented a resolution order the code does not have: the middle "then `default_schema`" tier
  cannot fire at a site that passes `allow_any_schema=True` with a namespace always supplied. Both
  docstrings now name that one deliberate exception.

Neither behavior change moves a committed golden (Hard Rule #9): both goldens are byte-identical,
  and the four new behavioral assertions were verified fallible against the unpatched builder.

Refs CI-005 post-merge follow-ups 1-3, CI-061.

- **ir**: Match enums on their namespace, not the bare name
  ([`cc17c96`](https://github.com/kmbhm1/castiron/commit/cc17c96f793b250524b2662f050295bce89e35a1))

Two schemas may define a same-named enum, and every lookup compared only the bare type name. A
  document with `a: public.status` and `b: audit.status` gave BOTH columns the same member list --
  whichever namespace happened to sort first -- so the generated model rejected a valid value and
  accepted an invalid one, deterministically, with no warning. It is silently wrong code, which is
  the worst failure mode a schema compiler has.

The namespace is not decoration here: for the OpenAPI source it comes from the `format` token's own
  prefix, which PostgREST emits whenever the type sits outside search_path. A single document
  therefore reaches this collision from ordinary user data.

`normalize_type_name` is replaced by `split_type_name`, which returns `(namespace, bare_name)`
  instead of discarding the qualifier, and all three matching sites use it: the direct column
  mapping, the array-element branch via `UserEnumType.matches_type_name`, and `_match_enum` for
  function parameters. A qualified token must match both parts; an unqualified one still matches on
  name alone, because that is all it carries.

The direct-mapping lookup keeps a bare-name fallback for when no enum reports the mapping's
  namespace, so a source that does not report owning schemas consistently is no worse off than
  before; it never fires when the namespaces do line up.

Also thread `disable_model_prefix_protection` into `add_constraints_to_table_details` and
  `add_foreign_key_info_to_table_details`. They standardized column names without it while the
  column builder honored it, so with the flag ON a `model_id` primary key left
  `ConstraintInfo.columns` naming `field_model_id` -- a column that does not exist. The PK/FK flags
  were never set and `primary_key()` returned a phantom name. CI-005 is the first change to expose
  that flag on a public entrypoint, so this would have shipped a knob that silently corrupts the IR.

Both are pre-existing CI-003 code; neither was reachable from real user data until this PR. Neither
  golden moves.

- **ir**: Repair column names illegal in Python
  ([`f13de36`](https://github.com/kmbhm1/castiron/commit/f13de3697be58812475e52335bfbfdd9c6ebe99d))

A column name Postgres accepts as a quoted identifier but Python does not accept as a field name
  reached the emitter verbatim. `castiron gen` printed `wrote out/schema.py`, exited 0, and the
  module raised `SyntaxError` on import -- falsifying the headline promise on a legal schema.

`standardize_column_name` is widened from "reserved word / model_ prefix" to "any name unusable as a
  Pydantic v2 field name", and a per-table collision rule (`column_identifiers`) guarantees no
  column is ever dropped or merged. The wire name is preserved on `ColumnInfo.alias` exactly as the
  shipped `class` -> `field_class` path already does, so sanitizing loses no fidelity.

Four shapes, all measured: 2fast -> field_2fast alias="2fast" SyntaxError before space name ->
  space_name alias="space name" SyntaxError before kebab-case -> kebab_case alias="kebab-case"
  SyntaxError before _private -> field__private alias="_private" compiled, NameError at import
  (CI85-Q2)

Every guard tests the NFKC form, because that is what the compiler reads: `clａss` (fullwidth a) is
  not a keyword by inspection and binds a field literally named `class`, and `ﬁ`/`fi` are one
  binding, so the uniqueness key and the alias rule normalize too.

`standardize_column_name` has four call sites and a mismatch between them is silent, so the FK and
  constraint marshalers now resolve through `resolved_column_name` rather than recomputing a
  per-name answer the per-table collision rule may have superseded.

No-change property, measured rather than argued: the `openapi-fixture`, `testbed-public` and
  `testbed-inventory` fingerprint manifests are byte-identical to origin/main (384 emissions
  unchanged), and the reserved axis is enumerated over every keyword and builtin x both values of
  disable_model_prefix_protection with zero movement. Only `synthetic-torture` moves: +9/-9 in the
  module golden, exactly six changed values in `ir.json`, and `compiles` flips no -> yes on all 128
  rows.

The corpus lint exclusion is deleted rather than left inert; the sweep widens from 384 to 512
  emissions and is clean under `--select F,UP,I --isolated`.

Tests: a new executing oracle over 258 enumerated names -- compiled, exec'd, instantiated by wire
  name, `model_dump(by_alias=True)` asserted equal to the payload -- plus an enumerated
  NFKC-reserved sweep. Mutation-verified: 9 mutants, 9 caught, with __pycache__ cleared around each.

- **ir**: Resolve a bare enum token against the schema being built
  ([`5cf7e04`](https://github.com/kmbhm1/castiron/commit/5cf7e045345962a30b2fb7bc11656b75a1a358fd))

Round 1 made a schema-qualified token namespace-exact but left the unqualified branch as "first name
  match wins". Enum rows arrive sorted by (namespace, type_name), so a bare token bound to whichever
  schema sorts first: with `status` in search_path and `audit.status` outside it, a `status[]`
  column emitted `list[AuditStatusEnum]` -- rejecting the valid values and accepting the invalid
  ones. That is the original failure mode, still live, reachable from one ordinary document.

The gap was conceptual, not mechanical. By the model round 1 established -- PostgREST omits the
  prefix exactly when the type is in search_path -- a bare token MEANS "the schema under
  construction". That meaning was never encoded: `add_user_defined_types_to_tables` had `schema` in
  hand and never consulted it in the array branch, and `_match_enum` never received it at all.

`_rank_enum_candidates` now states the resolution order once, and every matching site calls it: the
  token's own namespace when qualified, else the schema being built, else (bare tokens only) any
  remaining namespace. A qualified miss deliberately does not fall through -- naming a schema
  castiron has no enum for is a statement, not a gap. `schema` is threaded into
  `construct_parameters` from `construct_functions`, using the function's own schema so an
  unqualified type in an `audit` function means `audit.<type>`.

Also record the MATCHED enum's namespace rather than the mapping's when the bare-name fallback
  fires. They differ only in that case, and the mapping's would name a schema that does not own the
  type -- e.g. a `SalesStatusEnum` for a type owned by `audit`. Unreachable from the OpenAPI source
  today, latent for the live-DB source.

- **logging**: Guard exc_info by truthiness, not None
  ([`e94cbe0`](https://github.com/kmbhm1/castiron/commit/e94cbe0d62829bd2098e243966928e05bb9b2b8f))

`RedactingFilter` guarded with `if record.exc_info is not None`, but `Logger._log` normalizes only a
  TRUTHY `exc_info`: `logger.error('x', exc_info=False)` (or `0`, `()`, `[]`, `''`) reaches the
  record verbatim. `formatException` then raised TypeError/IndexError -- from inside
  `Handler.handle`, which is OUTSIDE `StreamHandler.emit`'s try, so it escaped into the caller and
  turned a log line into control flow. It fires only when a redactor is installed: never in a source
  adapter's unit tests, always in the real CLI. `logger.error('fetch failed', exc_info=self._debug)`
  is exactly the shape the next adapter will write, and it would have exited 70 without --debug.

CPython's own `Formatter.format` guards by truthiness; match it.

Verified fallible: the falsy-exc_info test fails against the previous HEAD for all five falsy
  values. The verbosity test is parametrized rather than looped so a failure names the level (and
  its inert `# type: ignore` is gone -- mypy's `files` is `src` only).

Refs CI-061 fix round.

- **sources**: Make the TLS and view-key paths behave as documented
  ([`609e37d`](https://github.com/kmbhm1/castiron/commit/609e37de5a89adb8880f7829868a22de24f5975b))

Four corrections to the OpenAPI source, all found by executed repro rather than inspection.

The trust-store message was unreachable. `AbstractHTTPHandler.do_open` does `except OSError as err:
  raise URLError(err)` and `ssl.SSLCertVerificationError` IS an OSError, so a certificate failure
  arrives wrapped in `URLError.reason` and never matched `except ssl.SSLError`. The guidance that
  decision CI5-D3 records as binding -- that castiron verifies against the OS trust store, not a
  bundled certifi -- never reached the one user who needs it. Unwrap `.reason` so both the wrapped
  and bare shapes produce it. The old test injected a bare SSLError, a shape urllib never produces
  here, so it passed while the behavior was broken; it now injects the realistic wrapping and fails
  against the old code.

`http.client` exceptions escaped the error contract entirely. IncompleteRead, BadStatusLine and
  LineTooLong are neither OSError nor ValueError, so a truncated response propagated raw instead of
  as SourceFetchError -- the same class of escape already fixed for ValueError, one layer down.

A VIEW no longer gets a synthesized primary-key constraint. PostgREST does propagate `<pk/>` markers
  through views, but `TableInfo.primary_key()` is defined to return `[]` for a VIEW, so carrying the
  marker left `ColumnInfo.primary` and `primary_key()` contradicting each other. The Pydantic
  emitter reads `primary_key()` and was right by luck; the SQLAlchemy emitter and the typed client
  will read `col.primary`. Dropping the row at the source keeps the IR self-consistent without
  touching CI-003 semantics. Foreign keys on a view are still carried, and the loss is recorded in
  the fidelity floor.

An empty foreign-key marker no longer produces rows. `<fk table='' column=''/>` names nothing; the
  builder dropped the edge but the synthesized constraint row survived and set `is_foreign_key` on a
  column with no relationship.

Finally, `_NOTE_BLOCK` is now composed from the same marker shapes as `_PK_MARKER`/`_FK_MARKER`.
  Spelling them out twice let them drift: the block hard-coded single spaces while extraction
  allowed `\s+`, so a marker written with two spaces was detected but its block was not stripped --
  and the raw marker text landed verbatim in the emitted `Field(description=...)`.

Neither golden moves.

- **sources**: Raise SourceFetchError on a bad URL
  ([`8cef2fd`](https://github.com/kmbhm1/castiron/commit/8cef2fdad385683204c7da24eede7fd755087edf))

`fetch_openapi_document` documents `Raises: SourceFetchError`, and the contract did not hold.
  `normalize_postgrest_url` is called OUTSIDE that function's own try block, and `urlsplit` raises a
  bare `ValueError` on a malformed URL, so it escaped.

Two consequences, both measured with a fake JWT before the fix:

1. A CREDENTIAL LEAK in the live-source suite. An escaping ValueError produces a real traceback, and
  `--showlocals` renders every frame's locals -- including the `key` free variable that
  `live_document`'s closure exists to hide:

CASTIRON_TEST_POSTGREST_URL='http://[::1' pytest --showlocals -m integration before: the full key
  printed 3x after: 0x

2. A user-facing UX bug. `castiron gen --from 'http://[::1'` exited **70** with "internal error
  (ValueError: Invalid IPv6 URL) ... This is a bug in castiron, please report it at .../issues". A
  user who mistyped a bracket was told to open an issue. Now exit **1**, naming the URL, with the
  --from hint.

Fixed at the root, in two places -- `normalize_postgrest_url` was not the only unguarded `urlsplit`.
  `cli.config.looks_like_url` is a PREDICATE that ran *first*, and it raised too; a predicate must
  degrade to a yes/no, never to a raise (same argument as `reject_url_userinfo` and `redact`,
  CI-066-D1). Fixing only the fetcher left exit 70 exactly where it was. Fixing it inside
  `normalize_postgrest_url` rather than at its call site also makes `cli.gen.source_origin`'s "Never
  raises:" true -- it catches SourceError only, and it runs inside the error boundary's own `except
  SourceError`, where a raise escapes the boundary entirely and prints an unredacted traceback.

The new message names the URL but never `str(exc)`, deliberately: `_checknetloc` raises "netloc
  'user:SECRET@exa..' contains invalid characters under NFKC normalization" -- the netloc WITH
  userinfo and WITHOUT a `scheme://`, which is exactly the anchor `redact` needs. Echoing it would
  have opened a leak while closing one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **sources**: Stop reading write verbs as table evidence
  ([`6e772a2`](https://github.com/kmbhm1/castiron/commit/6e772a29d75fd53a09b9466cfdbc15b1805eed98))

`classify_table_type()` inferred a relation's kind from which HTTP verbs PostgREST exposes, on the
  assumption that post/patch/delete track write privileges. Measured against real PostgREST --
  v14.14 and pinned v12.2.3, by the CI-008 testbed dispatch -- they track Postgres AUTO-UPDATABILITY
  instead. A GRANT SELECT-only simple view is auto-updatable, so PostgREST emits write verbs for it,
  and castiron read that as "base table".

The signal was noise: 24 of the 26 relations in the committed capture carry write verbs, including 3
  of its 5 views. Those three then kept a primary key a view cannot have, because CI5-D14a's `<pk/>`
  -> UNIQUE downgrade never fired.

Now one signal (CI94-Q2, captain): a non-empty `required` array means BASE TABLE, else VIEW. The
  half that is certain is a catalog fact rather than a PostgREST behaviour -- `required` is exactly
  the NOT NULL set, and a view column's pg_attribute.attnotnull is false, so a view never carries
  it. Measured over the capture's 26 definitions: 20 carry `required` and every one is a base table,
  0 exceptions.

CI5-D6's bias toward BASE TABLE is deliberately REVERSED. It rested on "misreading a table as a view
  empties its primary key", and that is void in the only cell where it applies: a base table lands
  there only if PostgREST reports no NOT NULL column, and a Postgres PRIMARY KEY column IS NOT NULL,
  so such a table has no primary key to empty. The capture's one instance, `all_nullable_readonly`,
  carries no `<pk/>` marker at all -- it is now reported as a VIEW, which changes one IR field and
  zero emitted bytes. That residual is pinned by a test as an accepted decision, not left to be
  rediscovered.

Net on the real capture: 23/26 -> 25/26 correct.

`is_writable` is still computed and logged. "This looked like evidence and provably is not" is worth
  seeing in a debug trace.

A third signal was rejected rather than overlooked: "a `<pk/>` outside a non-empty `required`
  implies a VIEW" is measured true 6/6 with 0 exceptions, but is provably equivalent to the rule
  above on any document PostgREST can emit, so it would be dead logic that reads as live. It is
  asserted as a test instead, over every committed document, so the model's justification stays
  falsifiable.

The signature is unchanged (CI94-D6) -- this is a patch, not API churn. The verb-independence test
  enumerates all 8 verb subsets x 3 `required` states; its predecessor asserted the false premise in
  its own name.

The three strict xfails in tests/integration/ are deleted, not relaxed, and the
  `all_nullable_readonly` pin is re-pointed. Both edits are UNEXECUTED here: the live suite needs
  the castiron-testbed apparatus and is excluded from the static gate on every leg.

Refs: CI-075, CI-094, CI94-Q2, CI94-D5, CI94-D6, CI-008, CI5-D6, CI5-D14a

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **tests**: Satisfy the pinned pre-push ruff, not just the resolved one
  ([`e025353`](https://github.com/kmbhm1/castiron/commit/e025353e30563b7bf6ada37e889083c94b650302))

The push hook runs ruff v0.6.9 (.pre-commit-config.yaml) while `uv.lock` resolves 0.16.0, and only
  0.6.9 still carries UP038 -- so `isinstance(node, (ast.Import, ast.ImportFrom))` passed `make
  validate` and was rejected at `git push`. Use the PEP 604 form, which both accept and which py3.10
  (the floor) supports for isinstance.

⚠ This is CI-105 biting for the second time in one branch: the same version skew already forced the
  syntax-error guard in test_lint.py to match two spellings. Worth recording that a green `make
  validate` does NOT imply a green `git push` while the pin and the lock disagree.

Note the hook flagged only this file because pre-commit passes it the CHANGED paths.
  `src/castiron/cli/config.py:373` and `src/castiron/ir/models.py:412` carry the same UP038 shape
  and are latent -- they will fail the push of whichever PR next touches them. Left alone here:
  unrelated files, and dragging them in would widen a release-gate fix.

Refs: CI-094, CI-105

- **tests**: Stop a malformed URL leaking the key
  ([`b4b9f37`](https://github.com/kmbhm1/castiron/commit/b4b9f379bfcf7e232fbc789ce2403d84d3cadae2))

Defence in depth for the leak fixed at the root in `sources`. `_fetch` caught `SourceError` only,
  which is exactly as good as the fetcher's `Raises:` contract -- and that contract did not hold, so
  a bare ValueError escaped and `--showlocals` printed the API key three times. Broadened to `except
  Exception` with `redact` and `pytrace=False`, per the CI-063 precedent (sanitize at the boundary
  AND harden the mask). The point of the broad clause is that it does not depend on being right
  about what the code below can raise, which is the assumption that failed.

Verified the broad clause cannot swallow a pytest outcome: `Skipped` and `Failed` derive from
  BaseException, not Exception. And verified the layer holds ALONE -- with the src fix reverted and
  only this change present, the key printed 0 times.

Two docstring claims corrected in the same file, both measured rather than reasoned:

- Property 3 said the key "never reaches a test's namespace ... and --showlocals has nothing to
  print". A closure hides the key from TEST functions; it does not hide it from a TRACEBACK, because
  --showlocals renders every frame and the loader's own frame binds `key`. What makes the property
  true is that nothing escapes `_fetch`.

- `pytest_collection_modifyitems` said an unfiltered hook makes "the gate report success having run
  nothing". False twice: `make test` carries --cov-fail-under=90, and a totally deselected run exits
  5 anyway. The silent case is PARTIAL deselection (184 passed / 1236 deselected, exit 0). The
  filter is still the primary guard; the docstring now says which net catches what. Also 1024/1024
  -> the measured 1420.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

- **types**: Drop an unused import and typing.Tuple
  ([`2edee9e`](https://github.com/kmbhm1/castiron/commit/2edee9e76d613de75cd4bd5e74c8e3b9cb96802e))

castiron emitted code its own linter rejects. Two `PYDANTIC_TYPE_MAP` entries put a ruff finding
  into every repository castiron is pointed at:

F401 `ipaddress.IPv6Network` imported but unused x1 UP035 `typing.Tuple` is deprecated, use `tuple`
  instead x1 UP006 Use `tuple` instead of `Tuple` x3

`cidr` resolved to `IPv4Network` while importing `IPv4Network, IPv6Network`, and `point` resolved to
  `Tuple[float, float]`. Neither shape castiron reports changes here -- `point` is still a 2-tuple
  of floats, `cidr` is still an IPv4Network -- only the spelling and the import. The PEP 585 builtin
  needs no import at all: it is a valid *runtime* expression on >=3.9, which matters because
  castiron emits `from __future__ import annotations` only when the schema has foreign keys.

These are castiron's first deliberate divergences from supabase-pydantic's resolution strings, so
  the module docstring says which two and why.

Found by an enumerated sweep of all 65 map entries, not by sampling (CI-072): shape (a) an import
  naming a symbol the resolution never uses, shape (b) a `typing` generic with a builtin equivalent.
  Exactly two instances, and the counter-check matters -- `inet` imports two ipaddress names and
  uses both.

`tests/unit/types/test_pydantic_map.py` now carries that sweep as a structural invariant over the
  whole map, derived from `PYDANTIC_TYPE_MAP` itself so a 66th entry cannot escape it. Demonstrated
  to fail on all four mutants, including two that a hand-written key list would have missed.

`KNOWN_LINT_DEFECTS` is deleted. PR #15's ruff guard deliberately asserted only that every finding
  was owned by a named open row, because CI-092 was that row; it was built to self-close, it did,
  and the guard now asserts *clean* over all 384 lintable emissions.

Refs: CI-092, CI-094, CI94-Q3(c)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01KLx5Mb5ru4KezavHoW5uMN

### Features

- **cli**: Add gen, the project config file, and file output
  ([`c33103c`](https://github.com/kmbhm1/castiron/commit/c33103cd2af2e8d66061b8529a7ec193592590c7))

Turn three merged libraries into one command a stranger can run:

castiron gen --from https://<ref>.supabase.co --emit pydantic

cli.py becomes a cli/ package -- the command surface, the [tool.castiron] config, the write path,
  the error boundary and the fidelity notices are separate concerns, and CI-021 adds check.py beside
  gen.py. castiron.cli:cli still resolves, so the console entrypoint is unchanged.

The project config file is the ROADMAP's stated fix to supabase-pydantic, whose gen had no --config
  at all, so CI and local runs could not share one source of truth. Precedence is click's own --
  command line > environment > [tool.castiron] > default -- populated by an eager --config callback
  into ctx.default_map rather than hand-rolled per option. Every boolean is a --x/--no-x pair, which
  is what lets a flag override a config value in both directions. Unknown or mistyped keys are a
  hard error with a "did you mean", because a silently-ignored typo produces output that is wrong
  invisibly. [tool.castiron.check] is reserved for CI-021: parsed, validated, ignored.

Secrets never round-trip through a committed file: `key` is rejected outright from the config with a
  pointer to CASTIRON_KEY, and every string the CLI prints goes through redact() first. That closes
  a real leak -- normalize_postgrest_url preserves the query string and the source embeds the
  normalized target in its errors, so --from '...?apikey=SECRET' would otherwise print the secret on
  any failure.

Failure is loud. supabase-pydantic's gen logged a connection error and returned, exiting 0; here 1
  is an actionable failure, 2 a usage error, 70 a castiron bug (traceback behind --debug), and 3 is
  reserved now so CI-021's drift code never has to be renumbered.

The write path is the first time castiron touches the filesystem, and it is byte-preserving: no
  post-hoc ruff pass, no banner, and an explicit newline='\n' -- without it Python rewrites \n to
  \r\n on Windows and CI-021's check reports permanent drift. Traversal, collision and an
  all-or-nothing --no-overwrite pre-flight are each guarded.

The OpenAPI source cannot see nextval()/identity defaults, so a Supabase surrogate key looks
  required on every Insert model. --infer-generated-primary-keys stays off (matching the library,
  CI5-D7) and a single conditional warning fires only when a real table would change, naming the
  exact flag.

Adds one marker-gated runtime dependency, tomli on 3.10 only (tomllib is stdlib from 3.11), and
  promotes the source's _INTEGER_FAMILY to public so the CLI notice and the inference share one
  definition.

Logging is stdlib, not loguru (captain decision CI6-D11): a library configures no handlers, only the
  CLI attaches one.

Refs: CI-006 (decisions CI6-D1..D15). Closes harness Q-1 -- no short alias.

- **emitters**: Add the emitter registry
  ([`9704436`](https://github.com/kmbhm1/castiron/commit/9704436aecca788e777af47d49a47e119f219b4b))

Map a --emit name onto the emitter that serves it, in emitters/ rather than in the CLI, so CI-021's
  `check` and programmatic callers share one lookup. Registering CI-012's SQLAlchemy emitter or
  CI-030/031's client emitter is now a single EMITTERS entry instead of a CLI edit, and
  click.Choice(sorted(EMITTERS)) derives --emit's validation, help text and error message from that
  same dict.

EmitterSpec carries the emitter's name, its default output file name, and a factory taking an
  EmitterConfig -- the three things a caller needs before it has an emitter instance. It is frozen:
  the registry is read-only at runtime.

Refs: CI-006 (spec §4.5, decision CI6-D13).

- **emitters**: Add the Pydantic v2 emitter, type resolution, and emitter abstraction
  ([`595e2ab`](https://github.com/kmbhm1/castiron/commit/595e2ab11f5d2b5a80b63e8e0f44d458b2a49d80))

Turn a castiron.ir.Schema into type-safe Pydantic v2 model source, reproducing supabase-pydantic's
  fidelity on a source-neutral footing. Three reusable layers:

- castiron.types: the shared type-resolution layer (raw_type -> Python type + imports), where the
  IR's deferred type resolution finally happens. Handles jsonb (Json union), enums, arrays (incl.
  array-of-enum), constr/StringConstraints parsed from CHECK constraints, datetime, and uuid. -
  castiron.emitters.base: the Emitter ABC (emit(schema) -> list[EmittedFile], no file I/O) - the
  seam every future emitter reuses. Deterministic, byte-stable output (no timestamped filenames). -
  castiron.emitters.pydantic: the PydanticEmitter, including nested foreign-key relationship fields
  (one-to-one/many singular vs. pluralized list, reverse- relationship synthesis, self-refs), behind
  a framework-neutral EmitterConfig.

Adds pydantic>=2 and inflection as runtime dependencies (the emitter's target + name pluralization);
  castiron.ir stays stdlib-only.

Tests: 187 cases, 100% coverage of the new packages, including determinism, the full 7-way
  relation-type matrix, and array type-vocabulary parity.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01Di85kqWWgiLf6Mecv4J9K9

- **emitters**: Render table comments as docstrings
  ([`b8546fe`](https://github.com/kmbhm1/castiron/commit/b8546fe590ce161dfba8cf955c370b1b28f9737a))

A table's SQL comment now appears as the first body paragraph of the docstring on every class
  generated for that table -- Base, Insert, Update, the operational class, and the opt-in Parent
  (captain's ruling CI9-Q2 (A), one uniform rule with no per-class exception). Insert/Update are the
  classes consumers actually import, so withholding it there would strip context from the most-read
  surface.

It is APPENDED, never a replacement for the generated summary line. The summary is castiron's
  statement about the class; the comment is the user's statement about the table. Replacing it would
  give UsersBaseSchema, UsersInsert and UsersUpdate identical docstrings and destroy the only line
  saying which variant you are reading -- and it keeps the golden diff pure insertion, so a reviewer
  can verify "nothing was rewritten" from the deletion count alone.

Escaping is total, because a SQL comment is arbitrary user text injected into a Python source file:
  backslashes doubled first, then EVERY double quote escaped (a comment containing `"""` would
  otherwise terminate the docstring early -- an injection that breaks the module).

Lines are split on '\n', never with str.splitlines(), which also breaks on \x0b \x0c \x1c-\x1e \x85
  and the Unicode line/paragraph separators -- all legal in a Postgres comment, and all silently
  re-indented if splitlines() is used. Enumerated in tests so a future "simplification" fails.

Goldens: - tests/unit/sources/openapi: 42 insertions / 9 deletions, exactly as the spec
  pre-computed. 12 docstrings = 3 commented tables x 4 classes; the 9 deletions are all one-line
  docstrings; the 3 uncommented tables gained nothing. All eight reviewer checks worked and
  recorded. - tests/unit/emitters/pydantic: BYTE-IDENTICAL. That is the proof this change is
  additive.

Verified by differential against 026af0f: with descriptions stripped, the emitted module is
  byte-identical to the base revision.

Docs ship with the behaviour rather than as a follow-up: the fidelity table in
  docs/sources/openapi.md said table comments were "dropped ... the IR has no field for them yet",
  which is now false, and six generated-output snippets across docs/ and README.md mirrored the
  golden and would have gone stale. Each snippet was verified to be a verbatim substring of real
  output.

Refs: CI-009, CI9-Q2

- **ir**: Add TableInfo.description
  ([`65e2ffc`](https://github.com/kmbhm1/castiron/commit/65e2ffc3b369370bc342cea7f71411fb476ae7dd))

A table's SQL comment (COMMENT ON TABLE) had nowhere to live in the IR, so CI-005 dropped
  `definitions.<t>.description` on the floor. Add `TableInfo.description: str | None`, appended last
  so positional construction and every existing `as_dict()` key stay put (the `Schema.functions`
  precedent).

Sources deliver it through a new **table row (3-tuple)**, `(schema, table_name, description)`,
  rather than a 13th element on the column tuple: unpacking a 12-tuple into 13 names raises
  ValueError, so widening a documented contract would break every existing source and test row, and
  it would denormalize one table-level fact across N column rows.

Normalization (CRLF->LF, strip, '' -> None, non-str -> None) lives in the builder, not the source,
  so CI-010's `obj_description(c.oid, 'pg_class')` inherits it for free instead of re-deriving and
  re-breaking it. The 3-tuple is exactly what that query yields per row.

A row naming a table with no column rows is skipped, never created: a table exists in the IR only
  because a source reported columns for it, and inventing one here would add a class to emitted
  output.

Nothing renders yet; no emitter output changes.

Refs: CI-009, CI5-Q4

- **ir**: Add the function/RPC model to the Schema IR
  ([`758fcba`](https://github.com/kmbhm1/castiron/commit/758fcba4f1a4be29fb89a5d5225ca98f6679b5c7))

The IR could describe tables, views, columns, keys and enums but had no way to carry a database
  function, so a source had nowhere to put PostgREST's /rpc/* endpoints. Add FunctionInfo and
  ParameterInfo plus the FunctionVolatility and ParameterMode enums, hung off Schema.functions the
  way enums already is, and a sixth positional row contract -- a function 8-tuple carrying parameter
  5-tuples -- that build_schema turns into those nodes.

Fields a coarse source cannot know are tri-state: None means unknown, never a guess. Two volatility
  fields rather than one, because a source can honestly assert "non-volatile" without knowing
  whether that means STABLE or IMMUTABLE, and is_read_only is exactly the signal a typed client
  needs. Raw source codes (provolatile, proargmodes) are normalized in the build layer via
  VOLATILITY_MAP / PARAMETER_MODE_MAP, mirroring how CONSTRAINT_TYPE_MAP already handles contype.
  UserEnumType.matches_type_name's normalization is extracted to a module-level normalize_type_name
  so parameter enum linkage shares one implementation instead of copying it.

Backward compatible by construction: function_details is appended as the LAST build_schema parameter
  (inserting it sixth would silently break a caller that passes schema positionally) and
  Schema.functions is appended last, so positional construction still works. A zero-function schema
  emits byte-identically, now asserted by an explicit test against the CI-004 golden. The one
  visible change is Schema.as_dict(), which necessarily gains a "functions": [] key because
  _serialize walks dataclasses.fields; the two affected CI-003 assertions are updated.

Nothing consumes Schema.functions until the typed client. This is a deliberate build-ahead so that
  live-DB pg_proc introspection later *enriches* the model rather than redesigning it.

Refs CI-005 (captain decisions CI5-D1, CI5-D2).

- **ir**: Formalize the Schema IR
  ([`191af59`](https://github.com/kmbhm1/castiron/commit/191af594f0e19bcc55013f595ec4e9243f2afc7c))

Add castiron.ir - the single typed, canonical data model that every source produces and every
  emitter consumes (the spine of the source -> IR -> emitter architecture). Ports
  supabase-pydantic's schema-construction pipeline (construct_table_info plus the
  column/constraint/relationship/enum marshalers) onto a source-neutral, dependency-free footing
  (stdlib only).

- ir/models.py: mutable dataclasses - Schema (root: tables + a de-duplicated enum registry),
  TableInfo, ColumnInfo, ConstraintInfo, ForeignKeyInfo, RelationshipInfo, EnumInfo, SortedColumns,
  and normalized RelationType / ConstraintType enums. Carries raw type signal (raw_type) only;
  Python-type resolution is deferred to the emitter (CI-004). - ir/build.py: the tuple-contract ->
  IR builder, a faithful port including the relationship double-run, bridge-table detection, and
  enum attachment. - Schema.as_dict() gives stable, JSON-serializable output for the forthcoming
  drift-check; determinism is guarded by tests, not immutability.

Tests: tests/unit/ir/ - 74 cases, 100% coverage of the ir/ package, including determinism,
  double-run idempotence, and a stdlib-only import guard. No new runtime dependency (still
  click-only).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>

Claude-Session: https://claude.ai/code/session_01Di85kqWWgiLf6Mecv4J9K9

- **sources**: Add the OpenAPI/PostgREST source adapter
  ([`fa0b6d5`](https://github.com/kmbhm1/castiron/commit/fa0b6d5e7c705c8d9c2e5d5d5aac8a5c3bb7711d))

castiron could build a Schema only from hand-written tuple rows. This is the first real source and
  the differentiated wedge: a full Schema IR from a Supabase/PostgREST URL plus an API key, with no
  database credentials and no driver.

The adapter is split hard in two. fetch.py makes the one authenticated GET and is the only code in
  castiron that touches the network; parse.py is a pure document -> rows function. That split is why
  every parser test loads a JSON fixture and nothing anywhere mocks HTTP, why the CLI will get an
  offline --from ./openapi.json for free, and why check mode has a network-free path. It is built on
  stdlib urllib.request, so this adds ZERO runtime dependencies -- a schema compiler should not make
  every user install an async HTTP stack for a single request. The accepted cost, TLS verified
  against the OS trust store rather than a bundled certifi, is named in the SSL error message.

The parser emits the six positional row contracts and calls the existing build_schema, so the ~400
  lines of ported fidelity (reserved-name aliasing, flag propagation, relationship typing,
  reverse-FK synthesis, bridge-table detection, enum linkage) are reused rather than reimplemented.
  It normalizes Swagger's int32/int64 into the pg vocabulary with a two-entry alias table instead of
  introducing a second type map; the one genuine gap that exposed -- a missing 'character' key -- is
  fixed in the shared PYDANTIC_TYPE_MAP, since information_schema emits that token too and the
  live-DB source would have hit the same hole.

Determinism is load-bearing for drift checking, so definitions and /rpc/* keys are sorted (PostgREST
  builds both from a Haskell hash map, whose order is not contractual) while properties order is
  preserved, because that order is real: pg ordinal position and argument position. A test parses a
  key-reordered copy of a document and asserts identical rows.

The fidelity floor is written into module docstrings AND asserted by tests so it cannot move
  silently: unique/check/exclude constraints do not exist in the document at all; an integer
  surrogate primary key arrives with no default because PostgREST drops nextval() (an opt-in
  inference is available behind infer_generated_primary_keys, default off); views carry no marker
  and report every column as nullable, so classification is a two-signal heuristic biased toward
  BASE TABLE; and a function's return type and set-returning flag are never encoded. Failure is
  loud, never silent: a document with no exposed tables raises rather than emitting an empty models
  file.

Refs CI-005 (captain decisions CI5-D3 through CI5-D12).

- **sources**: Carry the OpenAPI table comment
  ([`e717081`](https://github.com/kmbhm1/castiron/commit/e7170810d19edda38f725408308782a60e0b8a5a))

PostgREST puts a table's COMMENT ON TABLE in `definitions.<t>.description`, and CI-005 dropped it
  because the IR had nowhere to put it (CI5-Q4). Emit it as the table 3-tuple `(schema, table_name,
  description)` and forward it from the source entrypoint.

One row per *parsed* table, including tables with no comment (as `None`), so the contract stays
  uniform. A definition skipped above -- not a JSON object, or no `properties` -- contributes no
  table and therefore no row, which is what stops a comment from conjuring a table (and a generated
  class) into being.

`_as_str` is the only narrowing done here: a non-string `description` is not mistaken for one.
  Everything else -- CRLF, trimming, empty-to-None -- is the builder's job, so CI-010's live-DB path
  inherits it rather than re-deriving it.

Rows follow `sorted(definitions)`, so ordering is deterministic (Hard Rule #9); `definitions` is a
  Haskell hash map upstream and its document order is not contractual.

The six pre-existing row tuples are byte-identical to 026af0f for the committed fixture, verified by
  differential rather than by assertion. Emitter output is unchanged; no golden moves.

Refs: CI-009, CI5-Q4
