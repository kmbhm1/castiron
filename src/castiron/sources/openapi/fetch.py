"""The PostgREST OpenAPI fetcher — **the only code in castiron that touches the network**.

One authenticated ``GET`` against a PostgREST API root, returning the parsed JSON object.
Everything downstream (:mod:`castiron.sources.openapi.parse`) is a pure function of that
``dict``, which is why no parser test needs an HTTP mock and why CI-006 gets a
``--from ./openapi.json`` offline path for free.

Implemented on stdlib :mod:`urllib.request` — castiron makes exactly one request in its
entire lifetime, and a schema compiler should not make every user install an async HTTP
stack for it (decision CI5-D3, zero new runtime dependencies). The accepted cost is that
``urllib`` verifies TLS against the **operating system's** trust store rather than a
bundled ``certifi``; the SSL error path says so explicitly.

References:
    PostgREST ``docs/references/api/openapi.rst`` — the document is served on the API
    **root path** (a non-root path returns 406).
    PostgREST ``docs/references/api/schemas.rst`` — ``Accept-Profile`` selects the schema
    for open-api output; the first entry of ``db-schemas`` is the default.
    Supabase API docs — the base URL is ``https://<ref>.supabase.co/rest/v1/`` and the key
    travels in the ``apikey`` header (also sent as ``Authorization: Bearer`` so plain
    PostgREST, which reads only ``Authorization``, works too).
"""

import json
import logging
import ssl
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from castiron import __version__
from castiron.sources.errors import SourceFetchError

logger = logging.getLogger(__name__)

#: Seconds to wait for the OpenAPI document before giving up.
DEFAULT_TIMEOUT: float = 30.0

#: Hosts whose bare project URL is rewritten to the Supabase REST root.
SUPABASE_HOST_SUFFIXES: tuple[str, ...] = ('.supabase.co', '.supabase.in')

#: The REST API root path a Supabase project serves PostgREST on.
SUPABASE_REST_PATH = '/rest/v1/'

#: Characters of a failed response body to quote back in an error message.
_SNIPPET_LIMIT = 200


def normalize_postgrest_url(url: str) -> str:
    """Return the PostgREST API-root URL for ``url``, with a trailing slash.

    A bare Supabase project URL (``https://<ref>.supabase.co``) is rewritten to its REST
    root (``https://<ref>.supabase.co/rest/v1/``); any other URL is used as given, with a
    trailing slash appended so the document is requested on the API **root** path (a
    non-root path returns 406).

    ⚠ **``urlsplit`` raises a bare** ``ValueError`` **on a malformed URL, and this function is
    responsible for converting it.** ``urlsplit('http://[::1')`` is ``ValueError: Invalid IPv6
    URL`` -- not a ``SourceFetchError`` -- and letting it escape falsified two shipped contracts
    at once: :func:`fetch_openapi_document` documents ``Raises: SourceFetchError`` and called
    this *outside* its own ``try``, and :func:`castiron.cli.gen.source_origin` documents "never
    raises" while catching only :class:`~castiron.sources.SourceError`. It also leaked: an
    escaping ``ValueError`` produces a real traceback, and ``pytest --showlocals`` printed the API
    key held in the live-suite fixture's closure three times over (CI-089).

    The message names the URL but **never** ``str(exc)``, which is deliberate and measured:
    ``urlsplit`` -> ``_checknetloc`` raises ``netloc 'user:SECRET@exa℀mple.com' contains
    invalid characters under NFKC normalization`` -- the netloc, userinfo included, with **no**
    ``scheme://`` in front, which is exactly the anchor :func:`castiron.cli.errors.redact` needs.
    Echoing the exception would therefore have opened a leak while closing one. The URL as given
    keeps its scheme, so the mask does reach it.

    Args:
        url: The URL the user supplied.

    Returns:
        The normalized API-root URL.

    Raises:
        SourceFetchError: ``url`` is empty or blank, or is not a URL Python can parse.
    """
    trimmed = url.strip()
    if not trimmed:
        raise SourceFetchError('No source URL was given; expected a PostgREST API root or a Supabase project URL.')

    try:
        parts = urlsplit(trimmed)
        host = parts.hostname or ''
    except ValueError as exc:
        raise SourceFetchError(
            f'Could not parse {trimmed} as a URL: check the scheme, the host, and the [brackets] '
            f'around an IPv6 address.'
        ) from exc

    path = parts.path
    if host.endswith(SUPABASE_HOST_SUFFIXES) and not path.strip('/'):
        path = SUPABASE_REST_PATH
    elif not path.endswith('/'):
        path = f'{path}/'

    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def build_request_headers(key: str | None, schema: str) -> dict[str, str]:
    """Build the headers for the OpenAPI root request.

    Args:
        key: The API key (a Supabase anon/service key, or a PostgREST JWT). When ``None``,
            the request is anonymous.
        schema: The database schema to request (sent as ``Accept-Profile``).

    Returns:
        The headers, with sorted keys so the request is deterministic.
    """
    headers = {
        'Accept': 'application/openapi+json',
        'Accept-Profile': schema,
        'User-Agent': f'castiron/{__version__}',
    }
    if key:
        headers['apikey'] = key
        headers['Authorization'] = f'Bearer {key}'
    return dict(sorted(headers.items()))


def fetch_openapi_document(
    url: str,
    *,
    key: str | None = None,
    schema: str = 'public',
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """GET the PostgREST OpenAPI document and return it as a parsed JSON object.

    Args:
        url: A PostgREST API root or a Supabase project URL (normalized first).
        key: The API key to authenticate with, if any.
        schema: The database schema to request via ``Accept-Profile``.
        timeout: Seconds to wait for the response.

    Returns:
        The decoded document.

    Raises:
        SourceFetchError: The URL could not be parsed, the request failed, or the response body
            was not a JSON object. Nothing else -- see :func:`normalize_postgrest_url`, which is
            called outside the ``try`` below and so has to convert its own ``ValueError``.
    """
    target = normalize_postgrest_url(url)
    logger.debug(f'Fetching the OpenAPI document from {target} (schema {schema!r})')

    try:
        request = Request(target, headers=build_request_headers(key, schema), method='GET')
        with urlopen(request, timeout=timeout) as response:
            body: bytes = response.read()
    except HTTPError as exc:
        raise SourceFetchError(_http_error_message(target, exc)) from exc
    except (OSError, ValueError, HTTPException) as exc:
        # URLError and TimeoutError are OSError subclasses; ValueError covers a malformed
        # URL ("unknown url type"), which ``Request`` itself raises; HTTPException covers
        # the protocol-level failures (IncompleteRead, BadStatusLine, LineTooLong) that
        # are neither of the other two and would otherwise escape SourceFetchError.
        ssl_error = _ssl_cause(exc)
        if ssl_error is not None:
            raise SourceFetchError(_tls_error_message(target, ssl_error)) from exc
        raise SourceFetchError(f'Could not reach {target}: {exc}') from exc

    return _decode_document(target, body)


def _ssl_cause(exc: BaseException) -> ssl.SSLError | None:
    """Return the :class:`ssl.SSLError` behind ``exc``, if there is one.

    A certificate failure almost never arrives bare: ``AbstractHTTPHandler.do_open`` does
    ``except OSError as err: raise URLError(err)``, and ``ssl.SSLCertVerificationError``
    *is* an ``OSError`` -- so the real error is wrapped in ``URLError.reason``. Checking
    only ``isinstance(exc, ssl.SSLError)`` therefore never fires for the failure the
    trust-store message exists to explain.
    """
    if isinstance(exc, ssl.SSLError):
        return exc
    reason = getattr(exc, 'reason', None)
    if isinstance(reason, ssl.SSLError):
        return reason
    return None


def _tls_error_message(target: str, exc: ssl.SSLError) -> str:
    """Build the TLS failure message, naming the OS trust store (decision CI5-D3)."""
    return (
        f'TLS verification failed for {target}: {exc}. castiron verifies certificates against the operating '
        "system's trust store; on a python.org macOS build you may need to run the bundled "
        "'Install Certificates.command' once, or point SSL_CERT_FILE at a CA bundle."
    )


def _decode_document(target: str, body: bytes) -> dict[str, Any]:
    """Decode a response body into a JSON object, or raise :class:`SourceFetchError`."""
    try:
        document: Any = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise SourceFetchError(f'{target} did not return JSON: {_snippet(body)}') from exc

    if not isinstance(document, dict):
        raise SourceFetchError(
            f'{target} returned a JSON {type(document).__name__}, not an object; '
            f'is it the PostgREST API root? Body starts: {_snippet(body)}'
        )

    result: dict[str, Any] = document
    return result


def _http_error_message(target: str, exc: HTTPError) -> str:
    """Build an actionable message for a failed HTTP status."""
    if exc.code in (401, 403):
        return (
            f"{target} returned HTTP {exc.code}: check the API key and the role's privileges "
            f'(PostgREST hides objects the API role cannot access).'
        )
    if exc.code == 404:
        return f'{target} returned HTTP 404: is {target} the PostgREST API root?'
    return f'{target} returned HTTP {exc.code}: {_snippet(_read_error_body(exc))}'


def _read_error_body(exc: HTTPError) -> bytes:
    """Read an error response body, tolerating a body that cannot be read."""
    try:
        body: bytes = exc.read()
    except Exception:  # pragma: no cover - defensive; a consumed/absent body
        return b''
    return body


def _snippet(body: bytes) -> str:
    """Render the first :data:`_SNIPPET_LIMIT` characters of a response body."""
    text = body.decode('utf-8', errors='replace')
    if len(text) > _SNIPPET_LIMIT:
        return f'{text[:_SNIPPET_LIMIT]}...'
    return text
