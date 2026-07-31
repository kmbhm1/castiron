# Environment variables

castiron reads exactly five environment variables. They exist for the two things that
should not be committed (the source URL and the API key) and for wiring castiron into
CI — everything else belongs in [the config file](configuration.md), where it is
reviewable.

| Variable | Sets | Fallback for |
| --- | --- | --- |
| `CASTIRON_CONFIG` | `--config` | — |
| `CASTIRON_FROM` | `--from` | — |
| `SUPABASE_URL` | `--from` | used when `CASTIRON_FROM` is unset or empty |
| `CASTIRON_KEY` | `--key` | — |
| `SUPABASE_KEY` | `--key` | used when `CASTIRON_KEY` is unset or empty |

Environment variables sit **below the command line and above the config file** in the
[precedence chain](configuration.md#precedence): a flag always wins, and an environment
variable always beats `[tool.castiron]`.

```bash
export CASTIRON_KEY='eyJhbGciOi...'
castiron gen --from https://abcdefgh.supabase.co --emit pydantic
```

`--help` lists the variable names next to each option (`[env var: CASTIRON_KEY,
SUPABASE_KEY]`) and never a value.

## Prefer the key in the environment

A key passed as `--key` lands in your shell history and is visible in `ps` output. The
option's own help text says so, and so does the hint castiron prints when the source
rejects it:

```
Error: https://abcdefgh.supabase.co/rest/v1/ returned HTTP 401: check the API key and the role's privileges (PostgREST hides objects the API role cannot access).
Hint: the key came from --key. Set CASTIRON_KEY instead to keep it out of your shell history.
```

The key is never written into generated output, never appears in a summary line, and is
masked out of every message castiron prints — including debug logs and the URL echoed
back in an error, which is where a `?apikey=` query string would otherwise leak:

```
Error: https://abcdefgh.supabase.co/rest/v1/?apikey=*** returned HTTP 401: ...
```

It is also [rejected outright](configuration.md#the-api-key-is-rejected-here) as a config
file key.

## The `SUPABASE_*` fallbacks, honestly

If you already export `SUPABASE_URL` and `SUPABASE_KEY` — the convention `supabase-py`
uses — `castiron gen` runs with no flags at all. That convenience is deliberate, and so
is its risk: **a stale or unrelated `SUPABASE_KEY` sitting in your shell will be picked
up silently.**

castiron does not pretend otherwise. When authentication fails, the hint names *which
source the key came from* — never its value — so an ambient key is diagnosable in one
line rather than an afternoon:

```
Error: https://abcdefgh.supabase.co/rest/v1/ returned HTTP 401: check the API key and the role's privileges (PostgREST hides objects the API role cannot access).
Hint: the key came from SUPABASE_KEY, which castiron falls back to when CASTIRON_KEY is unset -- check it belongs to this project, and set CASTIRON_KEY to be explicit.
```

And when there is no key at all:

```
Hint: no key was given. Pass --key or set CASTIRON_KEY -- a Supabase project needs one even to read its schema.
```

If you want none of this ambiguity, set `CASTIRON_KEY` explicitly; it always wins over
`SUPABASE_KEY`.

## No `.env` file

castiron reads the process environment and nothing else. It does not load `.env`, and it
never will implicitly — a code generator that silently sources dotfiles is a surprise
waiting to happen, and it would put a secret one shell redirection away from your
generated output. Load it yourself if you want it:

```bash
set -a; source .env; set +a
castiron gen
```
