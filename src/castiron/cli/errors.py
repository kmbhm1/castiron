"""Exit codes, the CLI error boundary, and secret redaction.

The predecessor's ``gen`` logged a connection failure and ``return``ed — exiting **0** on
failure, which is the DX defect this module exists to prevent. Every castiron failure maps
onto a documented, stable exit code (constants live here so CI-021's ``check`` reuses them
rather than renumbering), and every string the CLI prints passes through :func:`redact`
first.

Why redaction is not paranoia: ``normalize_postgrest_url`` preserves the query string and
the source's error messages embed the normalized target, so
``--from 'https://x.supabase.co/rest/v1/?apikey=SECRET'`` would otherwise print the secret
on any failure.

It also owns the ``Hint:`` line. A source failure states *what* went wrong; the hint says
what to do about it, and for a 401 it names **where the key came from** — which is the
mitigation CI6-Q2 accepted the ambient ``SUPABASE_KEY``/``SUPABASE_URL`` fallbacks on. A
user who silently picks up another project's ``SUPABASE_KEY`` has no other way to find out.
"""

import os
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import click
from click.core import ParameterSource

from castiron.sources import SourceError

#: Everything worked: files were written, or ``--dry-run`` completed.
EXIT_OK = 0

#: An actionable failure the user can fix (bad source, bad config, unwritable output).
EXIT_ERROR = 1

#: A usage error. click raises these itself; the constant exists so the table is complete.
EXIT_USAGE = 2

#: **RESERVED for ``castiron check`` drift (CI-021).** ``gen`` never returns it. Declared
#: now so CI-021 does not have to renumber a code users may already have scripted against.
EXIT_DRIFT = 3

#: An unexpected exception — a castiron bug (``EX_SOFTWARE`` from BSD ``sysexits``).
EXIT_INTERNAL = 70

#: The placeholder every masked secret is replaced with.
REDACTED = '***'

#: Query-string parameters that carry a credential.
_SECRET_QUERY = re.compile(r'(?i)([?&](?:apikey|api[-_]?key|key|token|access_token|jwt)=)[^&\s]*')

#: Shorter values are too likely to be a common substring to blindly replace.
_MIN_REDACTABLE_KEY = 8


def redact(text: str, key: str | None = None) -> str:
    """Mask API keys in anything castiron prints.

    Args:
        text: The message about to be shown to the user.
        key: The API key in play, if any. Replaced literally when it is long enough to be
            unambiguous (short values are left alone: a three-character "key" would mangle
            unrelated text).

    Returns:
        ``text`` with credential-bearing query parameters and the key's literal value
        replaced by :data:`REDACTED`.
    """
    masked = _SECRET_QUERY.sub(rf'\1{REDACTED}', text)
    if key and len(key) >= _MIN_REDACTABLE_KEY:
        masked = masked.replace(key, REDACTED)
    return masked


def internal_error_message(exc: BaseException) -> str:
    """Build the message shown when castiron itself fails unexpectedly."""
    return (
        f'castiron: internal error ({type(exc).__name__}: {exc}). This is a bug in castiron, '
        'please report it at https://github.com/kmbhm1/castiron/issues -- rerun with --debug '
        'for the traceback.'
    )


# ---------------------------------------------------------------------------
# Hints (spec §3.1's four transcripts).
# ---------------------------------------------------------------------------

#: Shown when castiron never got a document: the value itself is the thing to fix.
FROM_HINT = (
    '--from takes a Supabase project URL (https://<ref>.supabase.co), a PostgREST API root, or a '
    'path to an OpenAPI JSON document.'
)

#: Message fragments owned by :mod:`castiron.sources.openapi` that select each hint. The
#: coupling to the source's wording is real; ``tests/unit/cli/test_errors.py`` drives the
#: actual source code paths rather than hand-written strings, so a reworded engine message
#: fails the suite instead of silently dropping the hint.
_UNREACHABLE_MARKERS = ('Could not reach ', 'TLS verification failed for ', 'did not return JSON', 'HTTP 404')
_AUTH_MARKERS = ('returned HTTP 401', 'returned HTTP 403')
_EMPTY_SCHEMA_MARKERS = ('exposes no tables or views for schema', 'exposes no readable columns for schema')


def key_provenance(key: str | None) -> str | None:
    """Name where the API key came from — the source, never the value.

    Reads click's own ``ParameterSource`` for ``key`` off the active context, so it cannot
    disagree with how the value was actually resolved. ``key`` is a forbidden config entry
    (CI6-D7), so the default map is not a possible origin.

    Args:
        key: The resolved key, or ``None`` when no key is in play.

    Returns:
        ``'--key'``, ``'CASTIRON_KEY'``, ``'SUPABASE_KEY'``, or ``None`` when there is no key.
    """
    if not key:
        return None
    ctx = click.get_current_context(silent=True)
    source = ctx.get_parameter_source('key') if ctx is not None else None
    if source is ParameterSource.ENVIRONMENT:
        # click's resolve_envvar_value takes the first non-empty of the list, in this order.
        return 'CASTIRON_KEY' if os.environ.get('CASTIRON_KEY') else 'SUPABASE_KEY'
    return '--key'


def key_hint(key_source: str | None) -> str:
    """Build the 401/403 hint, naming where the key came from and never its value."""
    if key_source is None:
        return (
            'no key was given. Pass --key or set CASTIRON_KEY -- a Supabase project needs one even to read its schema.'
        )
    if key_source == '--key':
        return 'the key came from --key. Set CASTIRON_KEY instead to keep it out of your shell history.'
    if key_source == 'SUPABASE_KEY':
        return (
            'the key came from SUPABASE_KEY, which castiron falls back to when CASTIRON_KEY is unset -- '
            'check it belongs to this project, and set CASTIRON_KEY to be explicit.'
        )
    return f'the key came from {key_source}. Check it is current and that its role can read the schema.'


def schema_hint(schema: str, origin: str | None) -> str:
    """Build the empty-schema hint, naming the document castiron actually read."""
    read = f' castiron read {origin}.' if origin else ''
    return (
        f'try --schema <name> if your tables do not live in {schema!r}, or a key whose role can see them.'
        f'{read} castiron refuses to write an empty models file.'
    )


def source_error_hint(exc: SourceError, *, key: str | None, schema: str, origin: str | None) -> str | None:
    """Return the ``Hint:`` line for a source failure, or ``None`` when nothing helps.

    Args:
        exc: The failure the source raised.
        key: The API key in play (used only to name its provenance).
        schema: The ``--schema`` value the run asked for.
        origin: The redacted description of where the schema was read from, if known.

    Returns:
        The hint text, without the ``Hint: `` prefix.
    """
    message = str(exc)
    if any(marker in message for marker in _AUTH_MARKERS):
        return key_hint(key_provenance(key))
    if any(marker in message for marker in _EMPTY_SCHEMA_MARKERS):
        return schema_hint(schema, origin)
    if any(marker in message for marker in _UNREACHABLE_MARKERS):
        return FROM_HINT
    return None


@contextmanager
def cli_error_handling(
    *,
    debug: bool,
    key: str | None,
    hint: Callable[[SourceError], str | None] | None = None,
) -> Iterator[None]:
    """Map castiron failures onto exit codes, redacting secrets from every message.

    ``ClickException`` subclasses (:class:`~castiron.cli.config.ConfigError`,
    :class:`~castiron.cli.output.OutputError`, ``UsageError``) and ``Abort`` are already
    exit-coded and are re-raised untouched; a :class:`~castiron.sources.SourceError`
    becomes a redacted ``ClickException`` (exit 1) carrying a ``Hint:`` line when one
    applies; anything else is a castiron bug and exits :data:`EXIT_INTERNAL`, showing the
    traceback only under ``--debug``.

    Args:
        debug: Re-raise unexpected exceptions so Python prints the traceback.
        key: The API key in play, redacted out of any message.
        hint: Builds the ``Hint:`` line for a source failure. Normally
            :func:`source_error_hint` bound to the run's key, schema and origin.

    Yields:
        ``None`` — the block runs inside the boundary.
    """
    try:
        yield
    except (click.ClickException, click.Abort):
        raise
    except SourceError as exc:
        message = redact(str(exc), key)
        advice = hint(exc) if hint is not None else None
        if advice:
            message = f'{message}\nHint: {redact(advice, key)}'
        raise click.ClickException(message) from exc
    except Exception as exc:
        click.echo(redact(internal_error_message(exc), key), err=True)
        if debug:
            raise
        sys.exit(EXIT_INTERNAL)
