# CLI reference

Everything on this page is generated from the `castiron` command itself, so it cannot
drift from `castiron --help`.

There is exactly one console command, `castiron`, and no short alias — one name to
document, one name to put in a bug report.

## Two ways to name a source

`--from` accepts either kind of value and decides by URL scheme:

| Value | What castiron does |
| --- | --- |
| `https://<ref>.supabase.co` | Rewrites it to the REST root `https://<ref>.supabase.co/rest/v1/` and fetches the OpenAPI document |
| `https://api.example.com/` (any PostgREST root) | Fetches the OpenAPI document from that root, appending a trailing slash if needed |
| `https://user:pw@api.example.com/` (any `http(s)` URL with a `user@` or `user:password@`) | **Refuses it** — exits `2` before any request, without echoing the URL back |
| `./openapi.json` (any existing file) | Reads and parses the file — **no network access at all** |
| anything else | Exits `2`: *"is neither a URL nor an existing file"* — castiron never silently prepends `https://` |

Two of those rows exit `2`, and they are not the only ones: see [exit codes](exit-codes.md)
for the full list.

### Credentials in the URL are refused, not fetched

A `--from` URL on `http` or `https` that carries credentials in its userinfo — the
`user:password@`, or a bare `token@`, before the host — is rejected at the command line,
before castiron opens a socket:

```
Usage: castiron gen [OPTIONS]
Try 'castiron gen --help' for help.

Error: The --from URL carries credentials in its userinfo (the `user:password@` before the host). castiron will not use it: the HTTP client rejects such a URL before it opens a socket, and the error it raises quotes the host back -- which would print your password. Drop the `user:password@` and pass the key with --key or CASTIRON_KEY.
```

**The message never contains the URL you passed** — printing it back is exactly the leak
this refusal exists to prevent. Nothing is lost by refusing: Python's HTTP client does not
apply userinfo as HTTP Basic auth, so such a URL could never have fetched anything. Pass
the credential as `--key` or `CASTIRON_KEY` instead.

The check is attached to the resolved value rather than to the flag, so it fires however
you supplied the source — `--from`, `CASTIRON_FROM`, `SUPABASE_URL`, or `from = "..."`
under `[tool.castiron]`.

**Only `http` and `https` are refused, deliberately.** A `postgresql://`, `postgres://` or
`mysql://` value keeps its userinfo, because a password in the connection string is the
normal, correct form for a database DSN and the planned live-database source will read
them. None of those is a source `gen` can read today, so passing one still fails — but
with the password masked:

```
Error: --from 'postgresql://user:***@localhost/db' is neither a URL nor an existing file. Pass a Supabase/PostgREST URL (https://...) or a path to an OpenAPI JSON document.
```

## Verbosity, quietness and debugging

Three orthogonal knobs, worth keeping straight:

- `-v` / `-vv` set the **log** level on stderr (`-v` = info, `-vv` = debug). `-v` tells
  you which config file was read and prints the OpenAPI fidelity note.
- `-q` suppresses the **summary** on stdout. Errors still print.
- `--debug` logs at debug level *and* shows the full traceback when castiron itself
  fails unexpectedly (exit `70`).

Secrets are masked in every string the CLI prints, at every verbosity — including debug
logs, the traceback `--debug` shows, and the URL echoed back in an error. That covers the
`--key` value, a URL's `user:password@`, and the value of any query- or fragment-parameter
whose name reads as a credential (`?apikey=`, `?service_role_key=`), each replaced by
`***`.

::: mkdocs-click
    :module: castiron.cli.main
    :command: cli
    :prog_name: castiron
    :depth: 1

## Notes on individual options

`--key`
: Whitespace and line endings around the value are trimmed, so a key read from a file with
  Windows (CRLF) endings just works. A control character **inside** the value — a key
  pasted across two lines — is refused instead, exiting `2` with an explanation and never
  the key itself, because such a value cannot be sent as an HTTP header at all.

`--emit`
: Repeat the flag to run more than one emitter (`--emit pydantic --emit sqlalchemy`).
  `pydantic` is the only emitter registered today, so repeating the flag currently just
  names the same emitter twice — which castiron rejects as a filename collision (exit `1`)
  rather than writing the same file twice. A list in the config file is **replaced**, not
  merged, by any `--emit` on the command line.

`--output`
: A **directory**, created (with parents) when missing. The file name comes from the
  emitter — `schema.py` for `pydantic` — unless `--filename` overrides it.

`--filename`
: Single-emitter runs only. With two or more `--emit` values it exits `2`, because two
  emitters writing one file name is a collision, not a preference.

`--overwrite / --no-overwrite`
: Overwriting is the default: regeneration is the whole point. `--no-overwrite` checks
  **every** target for existence before writing **any** of them, so a clash leaves the
  output tree untouched rather than half-generated.

`--dry-run`
: Runs the full pipeline and reports what would be written, creating no file and no
  directory. Reported sizes match a real run exactly.

`--infer-generated-primary-keys`
: An inference, off by default. See
  [What the OpenAPI source can and cannot see](../sources/openapi.md#identity-and-generated-columns).

`--schema`
: Sent to PostgREST as `Accept-Profile`. PostgREST serves one schema per document, so
  one run reads one schema.

`--timeout`
: Applies to the source URL only. Ignored on the `--from ./openapi.json` path, which
  makes no request.

## `castiron check`

`check` is `gen` minus the write. It reads the schema from the same source with the same
options, re-emits every file in memory, compares against the files already under `--output`,
and exits **`3`** if any of them has drifted:

```bash
castiron check --from ./openapi.json --output src/myapp/models
```

```
castiron: read 6 tables, 1 enum and 4 functions from openapi.json
castiron: drift detected in 1 of 1 generated file(s).

  file:     src/myapp/models/schema.py
  size:     8982 chars on disk -> 8975 chars from the schema
  sha256:   e1788d043dd84bb2 on disk -> 457423efdf479c44 from the schema
  lines:    +1 / -1
  showing 1 of 1 hunk(s):
    --- on disk
    +++ produced from the schema
    @@ -87,7 +87,7 @@
         id: int

         # Columns
    -    name: str | None
    +    name: str
  generated by castiron 0.5.0, and you are running 0.5.0 --
  this difference is your schema or a hand edit.

castiron: run `castiron gen` to regenerate.
```

It is the same option surface as `gen` minus `--overwrite/--no-overwrite` and `--dry-run`,
which are write-path only — passing either exits `2`. It reads the same `[tool.castiron]`
table, so `from`/`emit`/`output` written once are honoured by both commands, and a
config-file `output` is anchored to the config file's directory so the verdict does not
depend on which directory you ran from.

### It writes nothing, ever

There is no `--fix` and no `--write`. `check` creates no file and no directory — not even
`--output` when it is missing — on the clean path and on the drift path alike. **`gen` is
the fix.**

### Every difference is drift, including a missing file

A file castiron would write that is not there exits `3`, not `1`. The rule: *every outcome
in which the comparison ran and the answer is "not identical" is `3`; `1` is for "castiron
could not perform the comparison at all"* (an unreadable file, an unreachable source, a bad
config). See [exit codes](exit-codes.md).

### It tells a castiron upgrade apart from a schema change

Every generated module records the castiron version that wrote it in [its provenance
header](generated-code.md#the-provenance-header), and `check` reads it back. When the
recorded version differs from the one you are running, the report says so:

```
  generated by castiron 0.5.0; you are running 0.6.0. Some or all of this difference
  may be castiron's own output changing rather than your schema.
  Run `castiron gen` to adopt the current output.
```

That is deliberately hedged: castiron knows a version change is in play, but it cannot
re-emit as the old version, so it cannot attribute individual hunks to it. A file with no
header at all — hand-written, or generated before castiron `0.5.0` — says so instead.

Either way the exit code is `3`. A version-only difference is still a difference between
the committed file and what `gen` produces, and a `check` that called that clean would be
lying about the file's currency.

### Line endings are normalized before the comparison

The file on disk is decoded in universal-newline mode, so `\r\n` and lone `\r` become
`\n` before anything is compared. castiron always *writes* LF, but git does not always
*check out* LF: a contributor with `core.autocrlf=true` would otherwise see permanent,
unfixable drift on every CI run.

The accepted cost is a false negative confined to line endings — a genuinely CRLF-ified
file reports clean, while `gen` would rewrite it to LF. A UTF-8 BOM is **not** normalized
away: it is real drift, and because it is invisible in a diff the report renders the two
lines with `repr()` instead.

### `-q` suppresses the summary, never the report

`--quiet` drops the "read N tables" line and the per-file "is up to date" lines. It does
not drop the drift report — that is the payload, not a summary, and a CI log that said only
"exit 3" would send someone back to run the command again.
