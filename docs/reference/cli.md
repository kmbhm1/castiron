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
| `./openapi.json` (any existing file) | Reads and parses the file — **no network access at all** |
| anything else | Exits `2`: *"is neither a URL nor an existing file"* — castiron never silently prepends `https://` |

## Verbosity, quietness and debugging

Three orthogonal knobs, worth keeping straight:

- `-v` / `-vv` set the **log** level on stderr (`-v` = info, `-vv` = debug). `-v` tells
  you which config file was read and prints the OpenAPI fidelity note.
- `-q` suppresses the **summary** on stdout. Errors still print.
- `--debug` logs at debug level *and* shows the full traceback when castiron itself
  fails unexpectedly (exit `70`).

API keys are masked in every string the CLI prints, at every verbosity — including debug
logs and the URL echoed back in an error, which is where a `?apikey=` query string would
otherwise leak.

::: mkdocs-click
    :module: castiron.cli.main
    :command: cli
    :prog_name: castiron
    :depth: 1

## Notes on individual options

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
