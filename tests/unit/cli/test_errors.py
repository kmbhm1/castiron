"""Exit codes, the error boundary, and secret redaction."""

import click
import pytest

from castiron.cli.errors import (
    EXIT_DRIFT,
    EXIT_ERROR,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USAGE,
    REDACTED,
    cli_error_handling,
    redact,
)
from castiron.sources import SourceFetchError, SourceParseError


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

    def test_debug_re_raises_the_original_exception(self) -> None:
        with pytest.raises(RuntimeError):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=True, key=None):
                raise RuntimeError('kaboom')

    def test_a_clean_block_yields_and_returns(self) -> None:
        seen = []
        with cli_error_handling(debug=False, key=None):
            seen.append('ran')
        assert seen == ['ran']
