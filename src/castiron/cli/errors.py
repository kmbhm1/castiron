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
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from urllib.parse import unquote

import click
from click.core import ParameterSource

from castiron.cli.config import URL_SCHEMES
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

#: Credential words a query- or fragment-parameter name can carry. A longer spelling precedes
#: its own prefix (``authorization`` before ``auth``, ``credentials`` before ``credential``) so
#: the boundary lookahead is not defeated by the shorter alternative matching first.
_SECRET_WORD = (
    r'(?i:api[-_]?key|authorization|auth|credentials|credential|password|passwd|pwd'
    r'|signature|sig|secret|session|token|bearer|jwt|key)'
)

#: A parameter name reads as credential-bearing when a secret word is a whole *segment* of it --
#: delimited by ``-``, ``_``, ``.``, the ends of the name, or a camelCase hump. The predecessor
#: pattern anchored the word to the ``?``/``&`` itself, so every prefixed spelling
#: (``service_role_key``, ``sb-publishable-key``, ``x-api-key``) slipped past unmasked, and a
#: service-role key is strictly more dangerous than an anon key.
#:
#: ⚠ The boundaries are deliberately CASE-SENSITIVE while the words are not. A blanket ``(?i)``
#: would turn ``(?<=[a-z0-9])(?=[A-Z])`` into "any position after an alphanumeric", which matches
#: almost everywhere — hence the scoped ``(?i:...)`` on the word list alone.
_SECRET_PARAM_NAME = re.compile(rf'(?:^|[-_.]|(?<=[a-z0-9])(?=[A-Z])){_SECRET_WORD}(?=$|[-_.]|[A-Z])')

#: One ``?name=value`` / ``&name=value`` / ``#name=value`` pair. ``#`` is in the leading class
#: because a Supabase auth redirect puts ``access_token`` in the URL *fragment*, not the query.
_QUERY_PARAM = re.compile(r'([?&#])([^&\s=#]*)=([^&\s#]*)')

#: ``scheme://userinfo@host``. ``[^/?#\s]*`` is greedy on purpose: it backtracks to the **last**
#: ``@`` before the path, which is how ``urllib.parse.SplitResult`` derives userinfo
#: (``netloc.rpartition('@')``). On ``https://a@b:c@host/x`` urllib reports the password as ``c``
#: and so does this; a lazy or ``@``-excluding class would leak it.
_URL_USERINFO = re.compile(r'([A-Za-z][A-Za-z0-9+.\-]*://)([^/?#\s]*)@([^/?#\s]*)')

#: Shorter values are too likely to be a common substring to blindly replace.
_MIN_REDACTABLE_KEY = 8

#: Trimmed from both ends of a key: every C0 control character (space included) plus DEL.
_TRIMMABLE_KEY_CHARS = ''.join(chr(code) for code in range(0x21)) + '\x7f'

#: Refused *inside* a key. ``http.client.putheader`` rejects these outright, and its
#: ``ValueError`` names the offending value with ``%r`` — see :func:`sanitize_key`.
_CONTROL_CHARACTER = re.compile(r'[\x00-\x1f\x7f]')


def _key_spellings(*secrets: str | None) -> list[str]:
    r"""Return every spelling of each secret a printed string might carry, longest first.

    :func:`redact` masks a **literal** match, so any surface that *renders* the key rather
    than printing it defeats it. That is not hypothetical: a key ending in a carriage return
    (a key file saved with CRLF endings, or ``--key "$(pbpaste)"``) makes
    ``http.client.putheader`` raise ``ValueError('Invalid header value %r' % value)`` before
    the socket is even opened. ``%r`` renders that control character as two ordinary
    characters, so the raw key no longer occurs in the message being redacted and the whole
    JWT prints at exit 1. The escaped spelling covers **that** case — a renderer escaping the
    very control character that made the key unusable, which is ``%r`` and ``!r``.

    **Stated limit, measured rather than assumed:** it does *not* cover every renderer.
    ``unicode_escape`` escapes a non-ASCII character (``é`` → ``\xe9``) where ``repr`` and
    ``json.dumps`` do not, and it leaves a quote alone where they escape it — so a key
    containing ``é`` or ``"`` survives a ``json.dumps`` rendering, and one containing both
    quote styles survives a ``repr``. No renderer in ``src/`` produces those forms today: the
    key is rendered only through ``%r`` in ``http.client.putheader``, and :func:`sanitize_key`
    is the boundary layer that stops the value which triggers it from being sent at all. That
    is why the covered case is the one that matters, and why widening the spelling set is not
    the right trade.

    Variadic because a run can have more than one secret in play: the ``--key`` value, plus
    whatever :func:`_mask_url_userinfo` found inside a URL's userinfo. Every secret's spellings
    are unioned and sorted **once**, so the ordering rule lives in exactly one place.

    Args:
        *secrets: The secrets in play. ``None`` and empty values are skipped, so
            ``_key_spellings()`` and ``_key_spellings(None)`` both return ``[]``.

    Returns:
        The distinct spellings long enough to mask, longest first, ties broken lexically. The
        tie-break is what makes the order **total**: ``sorted`` is stable, so equal-length
        spellings otherwise kept their set-iteration order, which varies with
        ``PYTHONHASHSEED`` — nondeterminism in a security-relevant path, and against the spirit
        of Hard Rule #9. Longest-first is tidiness rather than security here: with this
        spelling set, masking a shorter spelling first strands only non-secret residue (a
        ``\r``, a space), never key material.
    """
    spellings: set[str] = set()
    for secret in secrets:
        if not secret:
            continue
        escaped = secret.encode('unicode_escape').decode('ascii')
        spellings |= {secret, secret.strip(_TRIMMABLE_KEY_CHARS), escaped, escaped.strip(_TRIMMABLE_KEY_CHARS)}
    return sorted((s for s in spellings if len(s) >= _MIN_REDACTABLE_KEY), key=lambda s: (-len(s), s))


def _mop_up_bare_userinfo(text: str, host: str) -> str:
    """Mask any bare ``<something>@host`` left over after the URL itself was masked.

    The URL is not the only place a password appears. ``http.client._get_hostport`` slices the
    netloc at ``host.rfind(':')`` and reports the remainder as ``nonnumeric port: '<rest>@<host>'``
    — with **no** ``scheme://`` in front, so :data:`_URL_USERINFO` cannot see it.

    ⚠ Anchored on the **host**, deliberately, rather than on the password we extracted. That
    slice point is the *last* colon, so for ``https://user:1:<jwt>@host`` the exposed fragment is
    ``<jwt>@host`` — a suffix of the password, not the password — and a search for the whole
    password finds nothing while the JWT prints in full. Anchoring on the host subsumes every
    possible split point, and any other renderer's choice of one.

    The real guarantee, stated exactly (it is *not* "cannot mis-fire"): every non-whitespace,
    non-quote run ending in ``@host`` is replaced, for each ``host`` that carried userinfo in
    **this same text**. So an unrelated ``bob@x.supabase.co`` in a message that also carried
    ``https://user:pw@x.supabase.co`` is over-masked. That is the safe direction, and it is why
    the rewrite is positional rather than a substring replace: :data:`_MIN_REDACTABLE_KEY` exists
    to stop a *short substring* from mangling unrelated text, and a positional rewrite anchored on
    a host has no such failure mode. A short password is therefore masked here, which is the whole
    point — it is below the length the spelling pass will touch.

    ⚠ **Known limitation, measured — a quote *inside* the password strands its prefix.** The run
    class excludes ``'`` and ``"`` so the quoting of ``nonnumeric port: '…'`` survives, which means
    a password containing one is masked only from its last quote onward::

        MSG: ... nonnumeric port: 'SECRETPREFIX9'x@x.supabase.co'
        RED: ... nonnumeric port: 'SECRETPREFIX9'***@x.supabase.co'

    ``'`` is a legal RFC 3986 sub-delimiter in userinfo, so this is reachable, not theoretical.
    The trade is deliberate and strictly narrower than the colon hole this anchoring closed:
    including quotes in the run would swallow the message's own quoting on *every* message, and
    a quote-bearing password is far rarer than a colon-bearing one. Recorded here rather than
    fixed so the next reader does not over-trust the paragraph above — and so that whoever needs
    it closed knows the cost of closing it.

    Args:
        text: The partly-masked message.
        host: The host of a URL that carried userinfo. Never empty (the caller guards), or the
            pattern would degrade to "every ``x@y`` in the text".

    Returns:
        ``text`` with every bare ``<something>@host`` masked.
    """

    def mask(match: re.Match[str]) -> str:
        # The URL occurrence was already rewritten to `scheme://user:***@host`; leave it alone so
        # the scheme and the (diagnostic, non-secret) username survive.
        if match.group(0).endswith(f'{REDACTED}@{host}'):
            return match.group(0)
        return f'{REDACTED}@{host}'

    # Quotes are excluded from the run so `nonnumeric port: '<jwt>@host'` keeps its quoting.
    return re.sub(rf'[^\s\'"]*@{re.escape(host)}', mask, text)


def _mask_url_userinfo(text: str) -> tuple[str, list[str]]:
    """Mask the credentials in every ``scheme://userinfo@host`` and report what was found.

    Args:
        text: The message about to be shown to the user.

    Returns:
        The masked text, and the secrets that were masked out of it — so :func:`redact` can
        also mask them wherever *else* they occur, through :func:`_key_spellings`.
    """
    found: list[tuple[str, str]] = []

    def mask(match: re.Match[str]) -> str:
        scheme, userinfo, host = match.group(1), match.group(2), match.group(3)
        user, separator, password = userinfo.partition(':')
        if separator:
            # Keep the username: it is diagnostically valuable ("you connected as postgres,
            # not app_user") and is not conventionally secret.
            found.append((password, host))
            return f'{scheme}{user}:{REDACTED}@{host}'
        # A colon-less userinfo in a URL castiron prints is far more often a bearer token
        # (https://ghp_.../) than a username, so mask the whole of it. The two mechanisms are
        # NOT redundant: this return decides that the *scheme* survives (`https://***@host`),
        # while registering the host below is what masks a bare re-occurrence elsewhere.
        found.append((user, host))
        return f'{scheme}{REDACTED}@{host}'

    masked = _URL_USERINFO.sub(mask, text)
    for _, host in found:
        if host:
            masked = _mop_up_bare_userinfo(masked, host)
    return masked, [secret for secret, _ in found if secret]


def _mask_secret_parameters(text: str) -> str:
    """Mask the value of any query or fragment parameter whose name names a credential.

    The name is matched **decoded** (so ``?api%5Fkey=`` is caught) but rewritten exactly as it
    was found, so the message keeps the user's own spelling of their URL.

    Args:
        text: The message about to be shown to the user.

    Returns:
        ``text`` with each credential-bearing parameter's value replaced by :data:`REDACTED`.
    """

    def mask(match: re.Match[str]) -> str:
        lead, name = match.group(1), match.group(2)
        if _SECRET_PARAM_NAME.search(unquote(name)):
            return f'{lead}{name}={REDACTED}'
        return match.group(0)

    return _QUERY_PARAM.sub(mask, text)


def redact(text: str, key: str | None = None) -> str:
    """Mask API keys, URL credentials and secret parameters in anything castiron prints.

    Three passes, in order: the userinfo of any ``scheme://user:password@host`` (keeping the
    username, which is diagnostic, and masking the password); the value of any query- or
    fragment-parameter whose name carries a credential word as a delimited segment; and finally
    every spelling (:func:`_key_spellings`) of the ``--key`` value **and** of any password the
    first pass found.

    The first pass is not only for HTTP URLs: CI-010's live-database source will carry
    ``postgresql://user:password@host/db`` DSNs in its error messages, and this is where their
    password gets masked.

    Matching is done on **shape**, never on validity — deliberately. ``urlsplit`` raises
    ``ValueError`` on exactly the malformed URLs this function exists to defend
    (``https://user:SECRET@[::1``, ``https://u:p@h:notaport/``), and a masking function that
    raises on malformed input is a masking function that leaks. There is no input on which
    :func:`redact` can raise.

    Two accepted costs, recorded so they are not "fixed" into leaks:

    1. The value class is greedy up to ``&``, ``#`` or whitespace, so it also swallows the
       message's own trailing punctuation (``?apikey=*** boom``, not ``?apikey=***: boom``).
       Nothing positional distinguishes value content from message punctuation, and
       *under*-masking is the fatal direction.
    2. A PostgREST filter on a column literally named ``key`` has its value masked
       (``?key=eq.5`` → ``?key=***``). Pre-existing behaviour; over-masking a diagnostic filter
       is far cheaper than leaking a credential.

    Args:
        text: The message about to be shown to the user.
        key: The API key in play, if any. Every spelling of it is replaced when it is long
            enough to be unambiguous (short values are left alone: a three-character "key"
            would mangle unrelated text).

    Returns:
        ``text`` with URL credentials, credential-bearing parameters and the key's value —
        literal, trimmed, or escaped — replaced by :data:`REDACTED`.
    """
    masked, url_secrets = _mask_url_userinfo(text)
    masked = _mask_secret_parameters(masked)
    for spelling in _key_spellings(key, *url_secrets):
        masked = masked.replace(spelling, REDACTED)
    return masked


def redact_source(source: str) -> str:
    """Redact a ``--from`` value that is about to be echoed back to the user (CI-068).

    :func:`redact` masks userinfo only in a ``scheme://user:password@host`` — matching on
    *shape*, because it scans arbitrary prose where a bare ``a:b@c`` is far more often ordinary
    text than a credential. That anchor leaves a real leak on the one surface that echoes the
    raw ``--from`` value back:

    .. code-block:: console

        $ SUPABASE_URL='postgres:SECRET@db.x.supabase.co:5432/postgres' castiron gen --dry-run
        Error: --from 'postgres:SECRET@db.x.supabase.co:5432/postgres' is neither a URL nor an
        existing file. ...

    ``urlsplit`` reads ``postgres:`` as the scheme, so there is no ``//`` for :data:`_URL_USERINFO`
    to anchor on — and this is exactly the shape ``psql`` connection strings circulate in. The
    secret does **not** have to be on the command line: ``CASTIRON_FROM``, ``SUPABASE_URL`` and a
    ``from = "..."`` in ``pyproject.toml`` all reach the same echo, so "it was in the shell
    history anyway" does not hold. Note the asymmetry this closes: ``postgresql://user:pw@host``
    is masked while the schemeless ``postgres:user:pw@host`` was not.

    This is a **single-option-value** transform, not a prose scan, which is the whole reason it
    can be more aggressive than :func:`redact`: there is no surrounding text for a false positive
    to land in. Everything up to the value's **last** ``@`` is masked (``rpartition``, matching
    how ``urlsplit`` derives userinfo from a netloc).

    Accepted cost, stated rather than discovered later: a *nonexistent* path containing an ``@``
    is over-masked in this one error message (``./my@dir/x.json`` → ``***@dir/x.json``). Only the
    "neither a URL nor an existing file" path prints this, so a path that resolves is never
    affected, and over-masking a filename the user just typed is far cheaper than printing a DSN
    password.

    Args:
        source: The raw ``--from`` value about to be quoted back into an error message.

    Returns:
        ``source`` with any scheme-less userinfo masked, then passed through :func:`redact` so
        the query-parameter and ``scheme://`` rules still apply.
    """
    if '@' in source and '://' not in source:
        _, separator, host = source.rpartition('@')
        source = f'{REDACTED}{separator}{host}'
    return redact(source)


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


def reject_url_userinfo(source: str | None) -> str | None:
    """Refuse an **HTTP(S)** ``--from`` URL that carries credentials in its userinfo.

    Never echoes the value. The boundary half of the two-layer defence :func:`redact` completes
    — the same shape as :func:`sanitize_key`, and settled the same way (CI-063: sanitize at the
    boundary *and* harden the mask). Removing the trigger costs nothing, because **castiron
    cannot successfully fetch from an http(s) userinfo URL under any circumstance**:
    ``urllib.request`` does not apply userinfo as HTTP Basic auth, it hands the whole netloc to
    ``http.client``, which either fails to parse it (``https://u:p@host`` → ``InvalidURL:
    nonnumeric port: 'p@host'``, raised before a socket opens) or fails to resolve a host
    literally named ``u@host``. The first of those quotes the netloc back, which is how the
    password reached the terminal.

    ⚠ **Scoped to** :data:`~castiron.cli.config.URL_SCHEMES` **on purpose.** That measurement is
    about *HTTP* fetching and does not generalize: ``postgresql://user:password@host/db`` is the
    canonical libpq connection string, and CI-010's live-database source will consume it happily.
    Refusing it here — telling a user to "pass the key with --key" for a DSN — would be wrong,
    and it is why :func:`redact` masks DSN userinfo whether or not this refusal fires. The two
    layers deliberately have different scopes: the boundary refuses what cannot work, the mask
    covers everything castiron might print.

    ⚠ **The scheme is split off by hand rather than with** ``urlsplit`` **— that is not a style
    choice.** This callback runs inside click's ``make_context``, *outside*
    :func:`cli_error_handling`, so anything it raises escapes the CLI's error boundary entirely:
    an unhandled ``ValueError`` here prints a raw, **unredacted** traceback and exits 1 instead
    of :data:`EXIT_INTERNAL`. And ``urlsplit`` raises on exactly the inputs this function exists
    to defend — ``urlsplit('https://user:SECRET@[::1')`` is ``ValueError: Invalid IPv6 URL``, and
    ``_checknetloc``'s ``ValueError`` quotes the whole netloc back, password included. It is the
    same argument CI-066-D1 made for :func:`redact` (malformed input must degrade to "refuse or
    pass", never to "raise"), and the boundary is the one place it matters most.

    Args:
        source: The resolved ``--from`` value — a URL, a path, or ``None``.

    Returns:
        ``source`` unchanged when it carries no userinfo, or when its scheme is not one castiron
        fetches over HTTP.

    Raises:
        click.UsageError: ``source`` is an http(s) URL with a ``user:password@`` (exit 2,
            matching :func:`sanitize_key`'s refusal). Nothing else — in particular, never on a
            malformed URL.
    """
    scheme = source.split('://', 1)[0].lower() if source else ''
    if source and scheme in URL_SCHEMES and _URL_USERINFO.search(source):
        raise click.UsageError(
            'The --from URL carries credentials in its userinfo (the `user:password@` before the '
            'host). castiron will not use it: the HTTP client rejects such a URL before it opens a '
            'socket, and the error it raises quotes the host back -- which would print your '
            'password. Drop the `user:password@` and pass the key with --key or CASTIRON_KEY.'
        )
    return source


def source_option_callback(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    """Refuse a credential-bearing ``--from`` (click callback) before anything can use it.

    On the option rather than in the command body so it covers the ``CASTIRON_FROM`` /
    ``SUPABASE_URL`` fallbacks and the ``[tool.castiron]`` default map as well as the flag —
    click runs the callback on the resolved value whatever its origin.

    Args:
        ctx: The click context (used only to honor ``resilient_parsing``).
        param: The ``--from`` parameter (unused; part of click's callback contract).
        value: The resolved source, or ``None``.

    Returns:
        The source, unchanged.
    """
    del param  # click's callback contract; the parameter itself carries no information here
    if ctx.resilient_parsing:  # shell completion must never raise
        return value
    return reject_url_userinfo(value)


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


def _format_traceback(exc: BaseException) -> str:
    """Render ``exc``'s full chained traceback the way the interpreter would.

    The single-argument form of ``traceback.format_exception`` is 3.10+, which is castiron's
    floor. It renders the ``__cause__``/``__context__`` chain — including the ``During handling
    of the above exception`` block — identically to the deprecated three-argument form, which
    matters because that chained block is where the unredacted original message lived.

    Args:
        exc: The exception to render.

    Returns:
        The traceback text, newline-terminated as the interpreter prints it.
    """
    return ''.join(traceback.format_exception(exc))


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
    applies; anything else is a castiron bug and exits :data:`EXIT_INTERNAL`, printing the
    traceback — through :func:`redact`, like every other string castiron prints — only under
    ``--debug``.

    ⚠ castiron prints that traceback itself rather than re-raising. Re-raising hands the
    exception to the interpreter, which renders it with no redaction at all: the ``Error:``
    line above it would be clean while the chained ``During handling of the above exception``
    block below it carried the raw original message. Since the internal-error text invites the
    user to paste the output into a public issue, that is the worst possible surface to leave
    unmasked. Printing it here also means an internal error exits :data:`EXIT_INTERNAL` with
    ``--debug`` exactly as it does without, instead of Python's 1 for an uncaught exception.

    Args:
        debug: Print the redacted traceback of an unexpected exception.
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
            click.echo(redact(_format_traceback(exc), key), err=True)
        sys.exit(EXIT_INTERNAL)
