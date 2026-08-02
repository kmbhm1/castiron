# Integration tests — the live-source suite

These tests run castiron against a **real** PostgREST serving a **real** Postgres schema. They
are the only tests in this repository that open a socket, and they are **not** part of
`make validate`.

Everything here is optional. `make test` excludes them and they skip themselves when the
apparatus is not configured, so you can contribute to castiron indefinitely without ever running
them.

## What they need

The schema under test lives in a separate repository:

**<https://github.com/kmbhm1/castiron-testbed>**

It is a disposable Postgres + PostgREST apparatus — one command up, one command down — carrying
~57 objects chosen so that every fidelity claim castiron makes has an object that demonstrates
it: the integer-width collapse, domains, enum arrays, two same-named enums in two schemas,
composite keys and composite foreign keys, generated columns, RLS, column-level privileges,
views, a materialized view, function overloads, and a quarantine schema of identifiers that are
not valid Python.

**The schema is deliberately not vendored here.** A database test apparatus is not
version-controlled inside castiron; it is version-controlled in its own right, so that a moved
assertion can always be attributed to *castiron changed* or to *the schema changed* and never to
"somebody edited a scratch directory".

## Running them

```bash
git clone https://github.com/kmbhm1/castiron-testbed
cd castiron-testbed
eval "$(./scripts/up.sh --exports-only)"   # start, rebuild from migrations, export the env

cd /path/to/castiron
make test-integration

cd /path/to/castiron-testbed
./scripts/down.sh                          # containers AND volume, gone
```

`up.sh` prints the four environment variables the suite reads. Read them from the environment —
**never hard-code a port.** The testbed publishes on a 544xx block precisely so it can coexist
with any other local Supabase stack, and that block is its choice to change.

| Variable | Meaning |
| --- | --- |
| `CASTIRON_TEST_POSTGREST_URL` | The PostgREST API root. **The master switch** — unset means every test in this directory skips. |
| `CASTIRON_TEST_POSTGREST_KEY` | The apparatus's local anon JWT. |
| `CASTIRON_TEST_DB_DSN` | The Postgres DSN. **Reserved** for the live-database source; unused today. |
| `CASTIRON_TEST_SEED_REVISION` | The testbed repository's short SHA. Attached to every failing report, so a failure is attributable to a seed revision. |

There is exactly **one** switch, not two. A separate `RUN_DB_TESTS`-style flag would create a
state in which the URL is exported and the tests silently skip anyway — which is the worst
outcome available, because it looks identical to "the tests ran and passed".

## If a test here fails

Read the failure's `castiron-testbed seed revision` section first.

- **The revision changed since the last green run** → the schema moved. Expect assertions to
  move with it; that is the point of stamping the revision.
- **The revision is the same** → castiron moved. That is a real regression, or a real finding.

**Do not "fix" a test by relaxing it to match whatever castiron currently produces.** This suite
exists to falsify castiron's documented model of PostgREST, and it has already succeeded several
times: assertions in this file deliberately contradict what the design spec predicted, because
the apparatus proved the spec wrong. Weakening one destroys the evidence.

The file marks three kinds of claim distinctly, and the distinction matters:

- **A fidelity-floor fact** is asserted positively, with a comment naming what was lost. It is
  not a bug — this source structurally cannot see more.
- **A characterization** pins current behaviour on a shape where the *right* behaviour is
  genuinely undecided, so a future change has to choose deliberately.
- **An `xfail`** names a known defect and its tracking row. When one starts XPASSing, delete the
  marker; do not silence it.
