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

## Not yet a command

`castiron check` — the drift guard that re-emits in memory and fails CI when the
committed output no longer matches the schema — is **not implemented**. It is not stubbed
and not advertised in `--help`; a subcommand that prints "not implemented" is a promise
broken in your face. Two things are reserved for it today: [exit code
`3`](exit-codes.md) and the `[tool.castiron.check]` table, which `gen`
[parses and ignores](configuration.md#reserved-toolcastironcheck).
