"""Exit codes, the error boundary, secret redaction, and the ``Hint:`` lines."""

from typing import Any
from urllib.error import HTTPError, URLError

import click
import pytest

from castiron.cli.errors import (
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    FROM_HINT,
    REDACTED,
    cli_error_handling,
    key_hint,
    key_provenance,
    redact,
    schema_hint,
    source_error_hint,
)
from castiron.sources import SourceError, SourceFetchError, SourceParseError, build_schema_from_document
from castiron.sources.openapi import fetch_openapi_document


@pytest.mark.unit
class TestRedact:
    @pytest.mark.parametrize(
        'text',
        [
            'https://x.supabase.co/rest/v1/?apikey=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?APIKEY=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?x=1&api_key=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?x=1&api-key=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?token=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?access_token=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?jwt=SUPERSECRET',
            'https://x.supabase.co/rest/v1/?key=SUPERSECRET',
        ],
    )
    def test_it_masks_credential_query_parameters(self, text: str) -> None:
        masked = redact(text)
        assert 'SUPERSECRET' not in masked
        assert REDACTED in masked

    def test_it_keeps_the_parameter_name_and_the_rest_of_the_url(self) -> None:
        assert (
            redact('https://x.supabase.co/rest/v1/?apikey=abc&z=9') == 'https://x.supabase.co/rest/v1/?apikey=***&z=9'
        )

    def test_it_masks_a_literal_key_value(self) -> None:
        assert redact('HTTP 401 for eyJhbGciOi', 'eyJhbGciOi') == f'HTTP 401 for {REDACTED}'

    def test_it_leaves_a_short_key_alone(self) -> None:
        # A three-character "key" is far more likely to be a common substring than a secret.
        assert redact('the code is nope and nope', 'nop') == 'the code is nope and nope'

    def test_a_message_without_a_secret_is_unchanged(self) -> None:
        assert (
            redact('Could not reach https://x.supabase.co/rest/v1/') == 'Could not reach https://x.supabase.co/rest/v1/'
        )

    def test_no_key_given_is_a_no_op_on_the_literal_pass(self) -> None:
        assert redact('plain message', None) == 'plain message'


@pytest.mark.unit
class TestExitCodes:
    def test_the_codes_are_the_documented_ones(self) -> None:
        assert (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERNAL) == (0, 1, 2, 70)

    def test_drift_is_reserved_for_check(self) -> None:
        # EXIT_DRIFT is declared here but never returned by `gen`; CI-021's `castiron check`
        # owns it. Asserting the number now means CI-021 never renumbers a code users script against.
        assert EXIT_DRIFT == 3

    def test_every_code_is_distinct(self) -> None:
        codes = [EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_DRIFT, EXIT_INTERNAL]
        assert len(set(codes)) == len(codes)


@pytest.mark.unit
class TestCliErrorHandling:
    @pytest.mark.parametrize('error', [SourceFetchError, SourceParseError])
    def test_a_source_error_becomes_a_click_exception(self, error: type[Exception]) -> None:
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise error('boom')
        assert excinfo.value.exit_code == EXIT_ERROR
        assert 'boom' in excinfo.value.message

    def test_a_source_error_message_is_redacted(self) -> None:
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key='eyJhbGciOi'):
                raise SourceFetchError('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET failed for eyJhbGciOi')
        assert 'SUPERSECRET' not in excinfo.value.message
        assert 'eyJhbGciOi' not in excinfo.value.message

    def test_a_click_exception_propagates_untouched(self) -> None:
        original = click.ClickException('already exit-coded')
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise original
        assert excinfo.value is original

    def test_a_usage_error_propagates_untouched(self) -> None:
        with pytest.raises(click.UsageError) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise click.UsageError('bad usage')
        assert excinfo.value.exit_code == EXIT_USAGE

    def test_an_abort_propagates_untouched(self) -> None:
        with pytest.raises(click.Abort):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise click.Abort()

    def test_an_unexpected_exception_exits_seventy(self) -> None:
        with pytest.raises(SystemExit) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise RuntimeError('kaboom')
        assert excinfo.value.code == EXIT_INTERNAL

    def test_the_internal_error_echo_is_redacted(self, capsys: pytest.CaptureFixture[str]) -> None:
        # CI6-D7: *every* printed string is redacted, and the internal-error echo is printed
        # -- with an invitation to paste it into a public issue, which makes it the worst
        # possible place for a key to survive. A castiron bug can carry the URL (and so the
        # key) in its str() from anywhere in the pipeline.
        with pytest.raises(SystemExit):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key='eyJhbGciOi'):
                raise RuntimeError('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET broke on eyJhbGciOi')
        printed = capsys.readouterr().err
        assert 'internal error (RuntimeError' in printed
        assert 'SUPERSECRET' not in printed
        assert 'eyJhbGciOi' not in printed

    def test_debug_re_raises_the_original_exception(self) -> None:
        with pytest.raises(RuntimeError):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=True, key=None):
                raise RuntimeError('kaboom')

    def test_a_clean_block_yields_and_returns(self) -> None:
        seen = []
        with cli_error_handling(debug=False, key=None):
            seen.append('ran')
        assert seen == ['ran']


# ---------------------------------------------------------------------------
# Hints. Spec §3.1's four transcripts; CI6-Q2 accepted the ambient SUPABASE_KEY
# fallback *on the strength of* the 401 hint naming the key's provenance.
# ---------------------------------------------------------------------------


def real_fetch_error(monkeypatch: pytest.MonkeyPatch, raiser: Any, url: str = 'https://x.supabase.co') -> SourceError:
    """Drive the real fetcher until it raises, so the hint is matched against real wording.

    The hint selection reads message fragments owned by :mod:`castiron.sources.openapi`.
    Producing those messages from the actual code path -- rather than retyping them here --
    is what turns that coupling into something the suite enforces: reword the engine message
    and these fail, instead of the hint silently disappearing.
    """
    monkeypatch.setattr('castiron.sources.openapi.fetch.urlopen', raiser)
    with pytest.raises(SourceError) as excinfo:
        fetch_openapi_document(url)
    return excinfo.value


def http_error(code: int) -> Any:
    def raiser(request: Any, timeout: float | None = None) -> Any:
        raise HTTPError(request.full_url, code, 'nope', {}, None)  # type: ignore[arg-type] - stdlib accepts None

    return raiser


@pytest.mark.unit
class TestKeyProvenance:
    def test_no_key_has_no_provenance(self) -> None:
        assert key_provenance(None) is None
        assert key_provenance('') is None

    def test_outside_a_click_context_it_reports_the_explicit_flag(self) -> None:
        assert key_provenance('a-key-value') == '--key'

    def test_it_never_returns_the_key_itself(self) -> None:
        assert 'a-key-value' not in str(key_provenance('a-key-value'))


@pytest.mark.unit
class TestKeyHint:
    @pytest.mark.parametrize(
        ('provenance', 'expected'),
        [
            (None, 'no key was given'),
            ('--key', 'came from --key'),
            ('CASTIRON_KEY', 'came from CASTIRON_KEY'),
            ('SUPABASE_KEY', 'came from SUPABASE_KEY'),
        ],
    )
    def test_it_names_where_the_key_came_from(self, provenance: str | None, expected: str) -> None:
        assert expected in key_hint(provenance)

    def test_the_supabase_fallback_warns_that_it_may_be_another_project_s(self) -> None:
        # The whole reason CI6-Q2 accepted the ambient fallback.
        hint = key_hint('SUPABASE_KEY')
        assert 'falls back' in hint
        assert 'belongs to this project' in hint

    def test_the_command_line_case_recommends_the_environment_variable(self) -> None:
        assert 'shell history' in key_hint('--key')


@pytest.mark.unit
class TestSchemaHint:
    def test_it_names_the_requested_schema_and_the_document_read(self) -> None:
        hint = schema_hint('public', 'https://x.supabase.co/rest/v1/')
        assert "'public'" in hint
        assert 'https://x.supabase.co/rest/v1/' in hint
        assert '--schema' in hint
        assert 'refuses to write an empty models file' in hint

    def test_it_copes_with_an_unknown_origin(self) -> None:
        assert 'castiron read' not in schema_hint('public', None)


@pytest.mark.unit
class TestSourceErrorHint:
    @pytest.mark.parametrize('code', [401, 403])
    def test_an_auth_failure_earns_the_key_hint(self, monkeypatch: pytest.MonkeyPatch, code: int) -> None:
        exc = real_fetch_error(monkeypatch, http_error(code))
        hint = source_error_hint(exc, key='a-key-value', schema='public', origin=None)
        assert hint == key_hint('--key')

    def test_an_unreachable_host_earns_the_from_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def raiser(request: Any, timeout: float | None = None) -> Any:
            raise URLError('nodename nor servname provided')

        exc = real_fetch_error(monkeypatch, raiser)
        assert source_error_hint(exc, key=None, schema='public', origin=None) == FROM_HINT

    def test_a_404_earns_the_from_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exc = real_fetch_error(monkeypatch, http_error(404))
        assert source_error_hint(exc, key=None, schema='public', origin=None) == FROM_HINT

    def test_a_non_json_body_earns_the_from_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class Response:
            def read(self) -> bytes:
                return b'<html>not json</html>'

            def __enter__(self) -> 'Response':
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

        exc = real_fetch_error(monkeypatch, lambda request, timeout=None: Response())
        assert source_error_hint(exc, key=None, schema='public', origin=None) == FROM_HINT

    def test_an_empty_schema_earns_the_schema_hint(self) -> None:
        with pytest.raises(SourceParseError) as excinfo:
            build_schema_from_document({'swagger': '2.0', 'definitions': {}, 'paths': {}})
        hint = source_error_hint(excinfo.value, key=None, schema='public', origin='./openapi.json')
        assert hint == schema_hint('public', './openapi.json')

    def test_a_schema_with_no_readable_columns_earns_the_schema_hint(self) -> None:
        document = {'swagger': '2.0', 'definitions': {'t': {'properties': {}}}, 'paths': {}}
        with pytest.raises(SourceParseError) as excinfo:
            build_schema_from_document(document)
        assert source_error_hint(excinfo.value, key=None, schema='public', origin=None) is not None

    def test_an_unrecognized_failure_earns_no_hint(self) -> None:
        assert source_error_hint(SourceFetchError('something else'), key=None, schema='public', origin=None) is None

    def test_the_hint_is_redacted_before_it_is_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        exc = real_fetch_error(monkeypatch, http_error(401), 'https://x.supabase.co/rest/v1/?apikey=SUPERSECRET')
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None, hint=lambda e: f'the URL was {exc}'):
                raise exc
        assert 'SUPERSECRET' not in excinfo.value.message


@pytest.mark.unit
class TestHintPlumbing:
    def test_the_hint_is_appended_on_its_own_line(self) -> None:
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None, hint=lambda exc: 'do the thing'):
                raise SourceFetchError('it broke')
        assert excinfo.value.message == 'it broke\nHint: do the thing'

    def test_no_hint_leaves_the_message_alone(self) -> None:
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None, hint=lambda exc: None):
                raise SourceFetchError('it broke')
        assert excinfo.value.message == 'it broke'
