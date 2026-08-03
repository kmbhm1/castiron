# The castiron golden corpus

This directory is the evidence behind **Hard Rule #9** — *"emitter output is byte-stable."* It
exists because `castiron check` (CI-021) will fail users' builds on any drift, and **every false
positive in `check` is a broken build for someone who changed nothing.**

Committed input documents → committed Schema-IR goldens → committed emitted-module goldens → a
committed per-config fingerprint manifest. Everything runs **offline**: every case reads a file
in this directory, and an autouse fixture in `conftest.py` makes any socket call raise.

## Layout

```
inputs/     the source documents, plus a *.provenance.json per document
golden/     the IR (ir.json) and emitted-module (*.py.txt) goldens
fingerprints/  one fingerprint manifest per input, 128 rows -- the full config product
cases.py    the case table, KNOWN_DEFECTS, and the config-axis derivation
pipeline.py the ONE definition of each artifact's bytes (used by the tests AND the tool)
compare.py  assert_golden() -- a golden failure that a reader can act on
regenerate.py  the regeneration entry point (writes nothing by default)
```

> ⚠ The fingerprint directory is `fingerprints/`, **not** `manifest/`, and that is deliberate.
> The repository's `.gitignore` carries a `MANIFEST` line (the distutils artifact). Git matches
> ignore patterns case-**insensitively** on a case-insensitive filesystem, so on macOS
> `MANIFEST` silently shadows a directory named `manifest/` — the files are untracked, `git add`
> skips them and `git status` never mentions them — while on Linux CI it does not match at all.
> A corpus half of whose artifacts are invisible on one platform is worse than no corpus, so the
> directory is named to avoid the collision rather than relying on everyone's filesystem.

## A golden is not an endorsement

A golden is a several-thousand-line assertion that today's output is *correct*. Some of
castiron's current output is **known wrong** — six open WORKPLAN rows produce bytes that reach a
committed golden here. So every known-wrong region is **named**:

- `KNOWN_DEFECTS` in `cases.py` carries one entry per row, with what correct output would be.
- `test_witnesses.py` asserts each defect's evidence **is present**, with a failure message that
  says what to do if the row was fixed. **A witness going red is usually good news.**
- Each case declares `status`: `'characterized'` (a known defect reaches its bytes, and
  `defects` names every one) or `'asserted'` (the stronger claim — none does).
- `compiles` is asserted **exactly**, including `False`. `synthetic-torture` emits a module that
  does not parse (CI-080/CI-085); the day it does parse, that test goes red and names the rows.

Bookkeeping closes in both directions, so neither a stale entry nor an unwitnessed one survives.

## Regenerating

```
uv run python -m tests.unit.corpus.regenerate            # inspect: writes NOTHING, exit 1 on drift
uv run python -m tests.unit.corpus.regenerate --write    # accept:  rewrites committed goldens
```

The default mode writes into `dist/scratch/<date>-ci-007-corpus/` and leaves every committed
golden alone. That is deliberate: a tool whose easy path is "rewrite the file" gets used that way
under pressure, and a golden regenerated whenever it goes red has stopped guarding anything.

It **never** writes `inputs/`. Re-capturing an input is a different operation with different
provenance — it comes from the testbed's `capture.sh`, and a changed input must arrive with a
changed `provenance.json`.

## THE REVIEWER PROCEDURE

1. **Always:** run `uv run python -m tests.unit.corpus.regenerate` on the PR branch.
   - Exit 0 ⇒ every committed golden is exactly what this branch's code produces. This is the
     only *mechanical* proof available.
2. **If no golden changed in the diff, stop here.** The check above is complete.
3. **If a golden changed**, the PR body must contain a `## Golden delta` section, written
   **before** regenerating, stating for each changed golden: the **cause** (which commit /
   behaviour change), the **direction**, and the **predicted magnitude and shape**.
   Your job is to check the actual diff **against the prediction**, and to confirm that
   **nothing outside the prediction moved**. A `Golden delta` section written after the fact is
   a description, not a prediction, and is worth nothing — reject it.
4. **Read the three artifact classes separately; they localize the cause:**
   - emitted golden moved, IR golden did not ⇒ an **emitter** change.
   - IR golden moved ⇒ a **source / IR** change; the emitted golden must have moved too, and a
     way it did not is a bug in one of them.
   - an **input document** moved ⇒ a re-capture; `provenance.json` must move with it
     (`seed_revision` and/or `postgrest_version`), or reject.
   - a **manifest** line moved for some configs and not others ⇒ a **config-conditional**
     change. Check that the set of moved config keys matches the prediction.
5. **Never accept "I regenerated it and the tests pass" as evidence.** That statement is true of
   every possible regression.

## The config sweep: why 128 rows and not one golden

Measured on the `testbed-public` capture: the 128 reachable config points produce **96 distinct
outputs**. Not 128 — the axes *interact*. Two consequences:

- A default-only corpus guards **1 of 96** reachable outputs while `check` users run the other 95.
- Single-flag-flip goldens provably **cannot see an interaction**.

So the manifest enumerates the full product (CI-072: enumerate, do not sample), and the axis set
is derived from `dataclasses.fields(EmitterConfig)` at runtime — adding a seventh toggle fails
`test_manifest.py` until the manifests are regenerated to cover it.

Five readable Tier-A goldens anchor five of those rows: a self-check asserts each Tier-A manifest
row's sha256 equals the sha256 of the committed golden file, so the hashes cannot drift away from
the text a human has actually read.

## Determinism axes

`test_determinism.py` enumerates eleven axes and states, per axis, what is and is not covered.
Two need calling out here:

- ⚠ **A6 (interpreters 3.10–3.13) is covered by the GATE, not by a test.** Spawning a second
  interpreter from a test is not portable. The corpus runs on all four legs of `make test-matrix`
  and of CI, and that is the evidence. **`make validate-fast` is single-interpreter and does not
  cover this axis** — it is not the gate (CI-081/CI-082).
- ⚠ **A8 (multi-file output ordering) is pinned, NOT exercised.** One emitter, one file today, so
  there is no ordering to get wrong yet. The assertion is a tripwire for the day a second file
  appears.

## Adding a case

1. Add the input to `inputs/` with a `*.provenance.json` beside it. A **synthetic** record must
   carry no `seed_revision` and no `postgrest_version` — a hand-authored document must never be
   able to stand as evidence about a real source (that is how CI-076 happened).
2. Add the `InputFamily` and `CorpusCase` to `cases.py`, and bump `EXPECTED_CASE_COUNT`.
3. `uv run python -m tests.unit.corpus.regenerate --write`.
4. **Read the emitted golden.** If any part of it is wrong, do **not** commit it as `asserted`:
   register the defect in `KNOWN_DEFECTS`, cite it on the case, and add a witness test. If the
   defect has no WORKPLAN row yet, **stop and report it** — do not fix it, and do not commit a
   golden that quietly encodes it.
