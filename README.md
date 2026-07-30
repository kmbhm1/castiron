# castiron

**A schema→typed-code compiler for Python.**

Point castiron at a schema source — a Supabase URL, your SQL migrations, or a
live database — and get typed models (and, soon, a typed client for tables,
views, and RPCs). A `check` mode fails CI when your application code drifts from
the schema. **No database connection required** for the OpenAPI and migrations
sources.

> _"It's cast-iron. It doesn't care."_ — and neither should your types drift.

---

> 🚧 **Status: pre-alpha.** castiron is under active development and is **not yet
> released**. The pipeline, sources, and emitters are being built out — see the
> roadmap. It is the successor to
> [`supabase-pydantic`](https://github.com/kmbhm1/supabase-pydantic), carrying
> forward its schema-fidelity engine on a source-agnostic architecture.

## The idea

```
   sources                       Schema IR                    emitters
 ┌───────────┐               ┌───────────────┐            ┌──────────────┐
 │ OpenAPI   │──┐            │               │         ┌──│ Pydantic     │
 │ migrations│──┼──parse──▶  │  one typed,    │──emit──▶┼──│ SQLAlchemy   │
 │ live DB   │──┘            │  formal model  │         ├──│ typed client │
 └───────────┘               └───────────────┘         └──│ TS / Zod ... │
                                    │                      └──────────────┘
                                    └──────────▶  check  (drift guard in CI)
```

Pluggable **sources** parse a schema into one formalized **Schema IR**; pluggable
**emitters** turn the IR into typed code. **`check`** re-emits in memory and fails
if the committed output has drifted.

## Why "castiron"

Cast iron is durable, low-maintenance, and does one job for decades. It also puns
on type **cast**ing — casting an untyped schema into hard, checked types.

## Development

castiron uses [`uv`](https://docs.astral.sh/uv/) and `hatchling`.

```bash
uv sync                 # create the environment and install dev deps
uv run castiron --version
make validate           # ruff + mypy + pytest (the pre-push gate)
```

## License

[MIT](LICENSE) © Kevin Boehm
