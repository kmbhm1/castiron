"""Unit tests for the OpenAPI fetcher.

``castiron.sources.openapi.fetch.urlopen`` is monkeypatched in every test that exercises
:func:`fetch_openapi_document`; **no test opens a socket**. The URL/header helpers are pure
and are tested directly.
"""

import http.client
import io
import json
import ssl
from types import TracebackType
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from castiron import __version__
from castiron.cli.errors import redact
from castiron.sources import SourceFetchError, fetch_openapi_document, normalize_postgrest_url
from castiron.sources.openapi import build_request_headers
from castiron.sources.openapi import fetch as fetch_module

FIXTURE_DOCUMENT = {'swagger': '2.0', 'definitions': {}, 'paths': {}}


class FakeResponse:
    """A minimal stand-in for the object :func:`urlopen` returns."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> 'FakeResponse':
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class Recorder:
    """Capture the request and timeout a call to ``urlopen`` would have used."""

    def __init__(self, body: bytes = b'{}') -> None:
        self.body = body
        self.request: Request | None = None
        self.timeout: float | None = None

    def __call__(self, request: Request, timeout: float | None = None) -> FakeResponse:
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.body)


def raiser(exc: BaseException) -> Any:
    """Return a fake ``urlopen`` that raises ``exc``."""

    def _raise(request: Request, timeout: float | None = None) -> FakeResponse:
        raise exc

    return _raise


def http_error(status: int, body: bytes = b'{"message":"boom"}') -> HTTPError:
    """Build an :class:`HTTPError` with a readable body.

    The ``hdrs``/``fp`` arguments are typed as ``email.message.Message`` and
    ``IO[bytes]`` in typeshed, but at runtime ``HTTPError`` accepts any mapping and any
    readable object -- which is exactly what a fake response needs to be.
    """
    headers: Any = {}
    return HTTPError('https://example.supabase.co/rest/v1/', status, 'Nope', headers, io.BytesIO(body))


def headers_of(request: Request) -> dict[str, str]:
    """Return a request's headers, lower-cased (``Request`` capitalizes header names)."""
    return {name.lower(): value for name, value in request.headers.items()}


# ---------------------------------------------------------------------------
# URL normalization.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizePostgrestUrl:
    @pytest.mark.parametrize(
        ('given', 'expected'),
        [
            ('https://abc.supabase.co', 'https://abc.supabase.co/rest/v1/'),
            ('https://abc.supabase.co/', 'https://abc.supabase.co/rest/v1/'),
            ('  https://abc.supabase.co  ', 'https://abc.supabase.co/rest/v1/'),
            ('https://abc.supabase.in', 'https://abc.supabase.in/rest/v1/'),
            ('https://abc.supabase.co/rest/v1/', 'https://abc.supabase.co/rest/v1/'),
            ('https://abc.supabase.co/rest/v1', 'https://abc.supabase.co/rest/v1/'),
            ('http://localhost:3000', 'http://localhost:3000/'),
            ('http://localhost:3000/', 'http://localhost:3000/'),
            ('https://api.example.com/pg', 'https://api.example.com/pg/'),
        ],
    )
    def test_normalization(self, given: str, expected: str) -> None:
        assert normalize_postgrest_url(given) == expected

    def test_a_query_string_survives(self) -> None:
        assert normalize_postgrest_url('http://localhost:3000?x=1') == 'http://localhost:3000/?x=1'

    def test_an_empty_url_is_refused(self) -> None:
        with pytest.raises(SourceFetchError, match='No source URL'):
            normalize_postgrest_url('   ')

    # ``urlsplit``'s two raising branches, enumerated rather than sampled (CI-072): the
    # bracket check (``Invalid IPv6 URL``) and ``_checknetloc``'s NFKC check. Both sit behind
    # ``url[:2] == '//'`` after the scheme is stripped, so this is the whole surface.
    @pytest.mark.parametrize(
        'malformed',
        [
            'http://[::1',  # unbalanced bracket
            'https://user@[::1',  # ...with userinfo in front of it
            'https://exa℀mple.com/',  # netloc that changes under NFKC normalization
        ],
    )
    def test_a_malformed_url_becomes_a_source_error_not_a_value_error(self, malformed: str) -> None:
        # CI-089. A bare ValueError here escaped `fetch_openapi_document`'s documented
        # `Raises: SourceFetchError`, and an escaping exception is what printed the API key
        # under `pytest --showlocals` in the live suite.
        with pytest.raises(SourceFetchError) as excinfo:
            normalize_postgrest_url(malformed)
        assert 'Could not parse' in str(excinfo.value)
        assert malformed in str(excinfo.value)
        # The chain is preserved for a debugger, but the ValueError is no longer what callers see.
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_the_message_never_echoes_urlsplits_own_text(self) -> None:
        # Measured, and the reason `str(exc)` is not interpolated: `_checknetloc` raises
        # "netloc 'user:SECRET@exa..' contains invalid characters under NFKC normalization" --
        # the netloc WITH userinfo and WITHOUT a `scheme://`, which is the anchor
        # `redact`'s _URL_USERINFO pattern needs. Echoing it would leak a password while
        # closing a leak.
        secret = 'SUPERSECRETPASSWORD'
        with pytest.raises(SourceFetchError) as excinfo:
            normalize_postgrest_url(f'https://user:{secret}@exa℀mple.com/')
        assert 'NFKC' not in str(excinfo.value)
        assert secret in str(excinfo.value.__cause__ or '')  # the cause still carries it...
        assert redact(str(excinfo.value)) != str(excinfo.value)  # ...and what we print is maskable
        assert secret not in redact(str(excinfo.value))


# ---------------------------------------------------------------------------
# Headers.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRequestHeaders:
    def test_an_anonymous_request_carries_no_credentials(self) -> None:
        headers = build_request_headers(None, 'public')
        assert headers == {
            'Accept': 'application/openapi+json',
            'Accept-Profile': 'public',
            'User-Agent': f'castiron/{__version__}',
        }

    def test_a_key_is_sent_as_both_apikey_and_bearer(self) -> None:
        # Supabase reads ``apikey``; plain PostgREST reads only ``Authorization``.
        headers = build_request_headers('secret-key', 'api')
        assert headers['apikey'] == 'secret-key'
        assert headers['Authorization'] == 'Bearer secret-key'
        assert headers['Accept-Profile'] == 'api'

    def test_headers_are_sorted_for_determinism(self) -> None:
        headers = build_request_headers('k', 'public')
        assert list(headers) == sorted(headers)

    def test_an_empty_key_is_treated_as_anonymous(self) -> None:
        assert 'apikey' not in build_request_headers('', 'public')


# ---------------------------------------------------------------------------
# The request itself.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFetchOpenApiDocument:
    def test_it_normalizes_the_url_and_sends_the_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(json.dumps(FIXTURE_DOCUMENT).encode())
        monkeypatch.setattr(fetch_module, 'urlopen', recorder)

        document = fetch_openapi_document('https://abc.supabase.co', key='k', schema='api', timeout=5.0)

        assert document == FIXTURE_DOCUMENT
        assert recorder.request is not None
        assert recorder.request.full_url == 'https://abc.supabase.co/rest/v1/'
        assert recorder.request.get_method() == 'GET'
        assert recorder.timeout == 5.0
        headers = headers_of(recorder.request)
        assert headers['accept'] == 'application/openapi+json'
        assert headers['accept-profile'] == 'api'
        assert headers['apikey'] == 'k'
        assert headers['authorization'] == 'Bearer k'
        assert headers['user-agent'] == f'castiron/{__version__}'

    def test_without_a_key_no_credentials_are_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(b'{}')
        monkeypatch.setattr(fetch_module, 'urlopen', recorder)

        fetch_openapi_document('http://localhost:3000')

        assert recorder.request is not None
        headers = headers_of(recorder.request)
        assert 'apikey' not in headers
        assert 'authorization' not in headers

    def test_the_default_timeout_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        recorder = Recorder(b'{}')
        monkeypatch.setattr(fetch_module, 'urlopen', recorder)

        fetch_openapi_document('http://localhost:3000')

        assert recorder.timeout == fetch_module.DEFAULT_TIMEOUT


@pytest.mark.unit
class TestFetchErrors:
    @pytest.mark.parametrize('status', [401, 403])
    def test_an_auth_failure_points_at_the_key(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(http_error(status)))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co', key='bad')
        message = str(excinfo.value)
        assert str(status) in message
        assert 'API key' in message
        assert 'privileges' in message

    def test_a_malformed_url_honours_the_documented_raises_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The contract as written: "Raises: SourceFetchError ... Nothing else." Asserted at the
        # `fetch_openapi_document` boundary, not only on the helper, because the defect was that
        # `normalize_postgrest_url` is called OUTSIDE this function's own try block (CI-089).
        def explode(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError('a malformed URL must fail before any socket is opened')

        monkeypatch.setattr(fetch_module, 'urlopen', explode)
        with pytest.raises(SourceFetchError, match='Could not parse'):
            fetch_openapi_document('http://[::1', key='irrelevant')

    def test_a_404_asks_about_the_api_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(http_error(404)))
        with pytest.raises(SourceFetchError, match='PostgREST API root'):
            fetch_openapi_document('https://abc.supabase.co/rest/v1/')

    def test_another_http_status_quotes_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(http_error(500, b'internal explosion')))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert '500' in str(excinfo.value)
        assert 'internal explosion' in str(excinfo.value)

    def test_a_long_error_body_is_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(http_error(500, b'x' * 500)))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert str(excinfo.value).endswith('...')
        assert 'x' * 201 not in str(excinfo.value)

    def test_an_unreadable_error_body_does_not_mask_the_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = http_error(503)
        error.read()  # exhaust the body so a second read is empty

        monkeypatch.setattr(fetch_module, 'urlopen', raiser(error))
        with pytest.raises(SourceFetchError, match='503'):
            fetch_openapi_document('https://abc.supabase.co')

    def test_a_network_error_chains_the_cause(self, monkeypatch: pytest.MonkeyPatch) -> None:
        cause = URLError('name resolution failed')
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(cause))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert excinfo.value.__cause__ is cause
        assert 'https://abc.supabase.co/rest/v1/' in str(excinfo.value)

    def test_a_timeout_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(TimeoutError('timed out')))
        with pytest.raises(SourceFetchError, match='Could not reach'):
            fetch_openapi_document('https://abc.supabase.co')

    def test_a_real_cert_verification_failure_names_the_os_trust_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The shape stdlib urllib *actually* produces for a bad certificate.

        ``AbstractHTTPHandler.do_open`` does ``except OSError as err: raise URLError(err)``
        and ``ssl.SSLCertVerificationError`` is an ``OSError``, so the SSL error arrives
        **wrapped** -- never bare. An earlier version of this test injected a bare
        ``ssl.SSLError``, a shape urllib never produces here, and therefore passed while
        the trust-store message (binding per decision CI5-D3) was unreachable.
        """
        cause = URLError(ssl.SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed'))
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(cause))

        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')

        message = str(excinfo.value)
        assert 'trust store' in message
        assert 'Install Certificates.command' in message
        assert 'https://abc.supabase.co/rest/v1/' in message
        assert excinfo.value.__cause__ is cause

    def test_a_bare_ssl_error_also_names_the_os_trust_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Belt and braces: an SSLError raised while *reading* the body is not wrapped.
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(ssl.SSLError('decryption failed')))
        with pytest.raises(SourceFetchError, match='trust store'):
            fetch_openapi_document('https://abc.supabase.co')

    def test_a_non_ssl_url_error_keeps_the_plain_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A wrapped *non*-SSL reason must not be mistaken for a certificate problem.
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(URLError(OSError('no route to host'))))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert 'trust store' not in str(excinfo.value)
        assert 'Could not reach' in str(excinfo.value)

    def test_a_truncated_response_stays_inside_the_error_contract(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # http.client exceptions are neither OSError nor ValueError, so they escaped the
        # SourceFetchError contract entirely.
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(http.client.IncompleteRead(b'partial')))
        with pytest.raises(SourceFetchError, match='Could not reach'):
            fetch_openapi_document('https://abc.supabase.co')

    @pytest.mark.parametrize(
        'exc',
        [
            http.client.BadStatusLine('garbage'),
            http.client.LineTooLong('header line'),
            http.client.RemoteDisconnected('closed'),
        ],
    )
    def test_every_http_protocol_error_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', raiser(exc))
        with pytest.raises(SourceFetchError):
            fetch_openapi_document('https://abc.supabase.co')

    def test_a_malformed_url_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ``Request`` itself raises ValueError("unknown url type") before any I/O happens.
        monkeypatch.setattr(fetch_module, 'urlopen', Recorder(b'{}'))
        with pytest.raises(SourceFetchError, match='Could not reach'):
            fetch_openapi_document('not-a-url')

    def test_a_non_json_body_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', Recorder(b'<html>nope</html>'))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert 'did not return JSON' in str(excinfo.value)
        assert '<html>nope</html>' in str(excinfo.value)

    def test_an_undecodable_body_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', Recorder(b'\xff\xfe\x00garbage'))
        with pytest.raises(SourceFetchError, match='did not return JSON'):
            fetch_openapi_document('https://abc.supabase.co')

    def test_a_json_array_body_is_a_fetch_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fetch_module, 'urlopen', Recorder(b'[1, 2, 3]'))
        with pytest.raises(SourceFetchError) as excinfo:
            fetch_openapi_document('https://abc.supabase.co')
        assert 'JSON list' in str(excinfo.value)
