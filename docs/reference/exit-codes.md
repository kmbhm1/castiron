# Exit codes

castiron fails loudly. Every failure carries a documented, stable exit code, so a script
can tell "your input was wrong" from "castiron has a bug" without parsing text.

| Code | Meaning | Typical causes |
| --- | --- | --- |
| `0` | Success | Files written, or `--dry-run` completed |
| `1` | An actionable failure — something you can fix | Unreachable source, bad key, unreadable OpenAPI document, a schema with no visible tables, a bad `[tool.castiron]` table, a target that exists under `--no-overwrite`, an unwritable output path |
| `2` | Usage error | Unknown option, unknown `--emit` value, `--filename` with two emitters, a `--config` file that does not exist, a `--from` that is missing or is neither a URL nor an existing file, a `--from` URL carrying credentials in its userinfo, a `--key` containing a control character |
| `3` | **Reserved** — not returned today | Reserved for drift detected by the future `castiron check`; declared now so the code never has to be renumbered |
| `70` | Internal error — a castiron bug | An unexpected exception. `70` is `EX_SOFTWARE` from BSD `sysexits` |

## What each one means for you

**`0`** — the run did what you asked. Under `--dry-run` this means the pipeline succeeded
and nothing was written, which is exactly what you want in a "would this work?" CI step.

**`1` — fix your input, your key, or your config.** The message names the thing that
failed, and where a next step exists castiron adds a `Hint:` line:

```
Error: https://abcdefgh.supabase.co/rest/v1/ returned HTTP 401: check the API key and the role's privileges (PostgREST hides objects the API role cannot access).
Hint: the key came from CASTIRON_KEY. Check it is current and that its role can read the schema.
```

castiron treats an empty schema as a failure rather than writing an empty models file —
the common anon-key-plus-RLS case — because a zero-byte `schema.py` committed to your
repo is a much more expensive mistake than a red build:

```
Error: The OpenAPI document exposes no tables or views for schema 'public'; check the API key's role privileges (PostgREST hides objects the role cannot access) and the Accept-Profile schema.
Hint: try --schema <name> if your tables do not live in 'public', or a key whose role can see them. castiron read empty.json. castiron refuses to write an empty models file.
```

**`2` — you called the command wrong.** These come from `click` and always print the
usage line, so the fix is usually visible in the output:

```
Usage: castiron gen [OPTIONS]
Try 'castiron gen --help' for help.

Error: No schema source. Pass --from <url|path>, set CASTIRON_FROM, or add `from = "..."` under [tool.castiron] in pyproject.toml.
```

Two of the exit-`2` paths are not typos at all. Both inputs are well-formed; castiron is
declining to *use* a credential you passed it, and **neither message repeats the value it
rejected** — echoing it back is precisely the leak the refusal exists to prevent.

A **`--from` URL with credentials in its userinfo** (`https://user:password@host/`, or a
bare `https://token@host/`) is refused before any request:

```
Error: The --from URL carries credentials in its userinfo (the `user:password@` before the host). castiron will not use it: the HTTP client rejects such a URL before it opens a socket, and the error it raises quotes the host back -- which would print your password. Drop the `user:password@` and pass the key with --key or CASTIRON_KEY.
```

Nothing is lost: Python's HTTP client never applies userinfo as Basic auth, so that URL
could not have fetched a document. Pass the credential as `--key` or `CASTIRON_KEY`. Only
`http` and `https` values are refused — a database DSN's password is left alone, and
masked wherever it is printed. See [the `--from` table](cli.md#two-ways-to-name-a-source).

A **`--key` with a control character inside it** — a key pasted across two lines, most
often — is refused for the same reason: the failure it would otherwise cause quotes the
value back at you.

```
Error: The API key contains a control character (a newline, carriage return or tab). A key pasted across two lines, or read from a file with Windows (CRLF) line endings, is the usual cause -- re-save it with LF endings or strip it (`tr -d "\r" < key.txt`). castiron will not send it: an HTTP header cannot carry that value, and the error the HTTP client raises quotes the value back with repr(), which would print your key.
```

Control characters *around* the key are trimmed rather than refused, so a key file with
CRLF endings needs no fixing at all. Only an interior one — where the value is not the key
you think it is — stops the run.

**`3` — you will not see this yet.** `gen` never returns it. It is reserved for the
planned `castiron check` drift-guard so that scripts written against `check` in future
will not have to change when it lands, and so that `3` can never come to mean something
else in the meantime.

**`70` — please report it.** castiron distinguishes its own bugs from your input so a bug
report is actionable. The traceback is hidden by default and the message says exactly how
to get it:

```
castiron: internal error (RuntimeError: kaboom). This is a bug in castiron, please report it at https://github.com/kmbhm1/castiron/issues -- rerun with --debug for the traceback.
```

!!! note "`--debug` adds the traceback without changing the exit code"
    `--debug` prints the full traceback — chained exceptions included — after the message
    above, and the process still exits **`70`**. castiron prints that traceback itself, so it
    passes through the same redaction as every other string castiron prints: an API key, a
    URL's `user:password@`, or a `?service_role_key=` value in the traceback is masked before
    you see it. Paste the whole thing into a bug report.

## Scripting against them

```bash
#!/usr/bin/env bash
set -uo pipefail

castiron gen --from "$SOURCE" --emit pydantic --output src/myapp/models
case $? in
  0)  echo "models regenerated" ;;
  1)  echo "castiron could not read the schema or write the output"; exit 1 ;;
  2)  echo "bad castiron invocation — check the flags"; exit 1 ;;
  70) echo "castiron bug — rerun with --debug and open an issue"; exit 1 ;;
esac
```

Note the deliberate absence of `set -e` on the `castiron` line: with `set -e` the script
exits before it can read `$?`.

## Why this is a stated contract

castiron's predecessor, [`supabase-pydantic`](https://github.com/kmbhm1/supabase-pydantic),
logged connection failures and returned — exiting **`0`** on failure, so a broken CI job
looked green and stale models shipped. castiron will not do that. Every path out of `gen`
ends at one of the codes above.
