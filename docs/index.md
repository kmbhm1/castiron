# castiron

**A schema→typed-code compiler for Python.**

Point castiron at a schema source — a Supabase URL, your SQL migrations, or a
live database — and get typed models (and, soon, a typed client for tables,
views, and RPCs). A `check` mode fails CI when your application code drifts from
the schema.

!!! warning "Pre-alpha"
    castiron is under active development and not yet released. Docs will grow as
    the compiler pipeline lands. It is the successor to
    [`supabase-pydantic`](https://github.com/kmbhm1/supabase-pydantic).

## Architecture

Pluggable **sources** parse a schema into one formalized **Schema IR**; pluggable
**emitters** turn the IR into typed code. **`check`** re-emits in memory and fails
if the committed output has drifted from the schema.

## Install (once released)

```bash
uv add castiron        # or: pip install castiron
```

## Develop

```bash
uv sync
uv run castiron --version
make validate
```
