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

⚠ **A literal match is not enough on its own.** :func:`redact` masks the key where it appears
verbatim, so any surface that *renders* the key -- ``%r``, ``!r``, ``json.dumps`` -- escapes
its way past the mask. A key ending in a carriage return really did print in full at exit 1
(see :func:`_key_spellings`), so the key is defended twice: :func:`sanitize_key` removes the
trigger at the CLI boundary, and :func:`redact` masks the escaped spellings for whatever
renderer nobody has found yet.

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

#: Trimmed from both ends of a key: every C0 control character (space included) plus DEL.
_TRIMMABLE_KEY_CHARS = ''.join(chr(code) for code in range(0x21)) + '\x7f'

#: Refused *inside* a key. ``http.client.putheader`` rejects these outright, and its
#: ``ValueError`` names the offending value with ``%r`` — see :func:`sanitize_key`.
_CONTROL_CHARACTER = re.compile(r'[\x00-\x1f\x7f]')


def _key_spellings(key: str | None) -> list[str]:
    """Return every spelling of ``key`` a printed string might carry, longest first.

    :func:`redact` masks a **literal** match, so any surface that *renders* the key rather
    than printing it defeats it. That is not hypothetical: a key ending in a carriage return
    (a key file saved with CRLF endings, or ``--key "$(pbpaste)"``) makes
    ``http.client.putheader`` raise ``ValueError('Invalid header value %r' % value)`` before
    the socket is even opened. ``%r`` renders that control character as two ordinary
    characters, so the raw key no longer occurs in the message being redacted and the whole
    JWT prints at exit 1. Masking the escaped and trimmed spellings as well covers ``%r``,
    ``!r``, ``json.dumps`` and every other renderer that escapes the character which made
    the key unusable in the first place.

    Args:
        key: The API key in play, or ``None``.

    Returns:
        The distinct spellings long enough to mask, longest first — a shorter spelling is
        usually a substring of a longer one, and masking it first would strand the remainder.
    """
    if not key:
        return []
    escaped = key.encode('unicode_escape').decode('ascii')
    spellings = {key, key.strip(_TRIMMABLE_KEY_CHARS), escaped, escaped.strip(_TRIMMABLE_KEY_CHARS)}
    return sorted((s for s in spellings if len(s) >= _MIN_REDACTABLE_KEY), key=len, reverse=True)


def redact(text: str, key: str | None = None) -> str:
    """Mask API keys in anything castiron prints.

    Args:
        text: The message about to be shown to the user.
        key: The API key in play, if any. Every spelling of it (:func:`_key_spellings`) is
            replaced when it is long enough to be unambiguous (short values are left alone:
            a three-character "key" would mangle unrelated text).

    Returns:
        ``text`` with credential-bearing query parameters and the key's value — literal,
        trimmed, or escaped — replaced by :data:`REDACTED`.
    """
    masked = _SECRET_QUERY.sub(rf'\1{REDACTED}', text)
    for spelling in _key_spellings(key):
        masked = masked.replace(spelling, REDACTED)
    return masked


def sanitize_key(key: str | None) -> str | None:
    """Trim a key's surrounding control characters; refuse one that hides them inside.

    The second line of castiron's defence against a key that cannot be sent as a header --
    :func:`redact` (via :func:`_key_spellings`) is the first, and this removes the trigger.
    ``http.client.putheader`` raises ``ValueError('Invalid header value %r' % value)`` for a
    value carrying a carriage return or newline, *before* the socket connects, so the failure
    needs no network and reaches the user as an ordinary exit-1 message.

    Trimming rather than refusing the common case is deliberate: a trailing carriage return
    from a CRLF key file is unambiguous user error with exactly one sensible reading, and
    refusing it would only make castiron look broken. An **interior** control character has no such
    reading — the value is not the key the user thinks it is — so it is refused, naming the
    likely cause and never the value.

    Args:
        key: The resolved ``--key`` value, or ``None`` when no key is in play.

    Returns:
        The trimmed key, or ``None`` when there was none.

    Raises:
        click.UsageError: ``key`` carries a control character that trimming cannot remove.
    """
    if key is None:
        return None
    trimmed = key.strip(_TRIMMABLE_KEY_CHARS)
    if _CONTROL_CHARACTER.search(trimmed):
        raise click.UsageError(
            'The API key contains a control character (a newline, carriage return or tab). A key '
            'pasted across two lines, or read from a file with Windows (CRLF) line endings, is the '
            'usual cause -- re-save it with LF endings or strip it (`tr -d "\\r" < key.txt`). '
            'castiron will not send it: an HTTP header cannot carry that value, and the error the '
            'HTTP client raises quotes the value back with repr(), which would print your key.'
        )
    return trimmed


def key_option_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Sanitize ``--key`` (click callback) before anything can use the value.

    On the option rather than in the command body so it also covers the ``CASTIRON_KEY`` /
    ``SUPABASE_KEY`` fallbacks, and so the key is already clean when it is bound into the log
    redactor and the request headers.

    Args:
        ctx: The click context (used only to honor ``resilient_parsing``).
        param: The ``--key`` parameter (unused; part of click's callback contract).
        value: The resolved key, or ``None``.

    Returns:
        The sanitized key.
    """
    del param  # click's callback contract; the parameter itself carries no information here
    if ctx.resilient_parsing:  # shell completion must never raise
        return value
    return sanitize_key(value)


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
