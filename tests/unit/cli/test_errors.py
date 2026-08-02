"""Exit codes, the error boundary, secret redaction, and the ``Hint:`` lines."""

import json
import os
import socket
import subprocess
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

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
    _key_spellings,
    cli_error_handling,
    key_hint,
    key_option_callback,
    key_provenance,
    redact,
    redact_source,
    reject_url_userinfo,
    sanitize_key,
    schema_hint,
    source_error_hint,
    source_option_callback,
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


# ⚠ `redact` masks a LITERAL match, so any surface that *renders* the key escapes past it.
# This is the reproduction, and it needs no network: `http.client.putheader` validates header
# values before the socket is opened, and reports the bad one with `%r`.
JWT = 'eyJhbGciOiJIUzI1NiJ9.SUPERSECRETVALUE'


@pytest.mark.unit
class TestRedactRenderedKeys:
    @pytest.mark.parametrize('suffix', ['\r', '\n', '\r\n', ' ', '\t'])
    def test_it_masks_a_key_whose_trailing_whitespace_was_escaped_away(self, suffix: str) -> None:
        # `'Invalid header value %r' % b'Bearer <jwt>\r'` renders the \r as two ordinary
        # characters, so the raw key no longer occurs in the text being redacted.
        rendered = f'Invalid header value {("Bearer " + JWT + suffix).encode()!r}'
        assert JWT in rendered
        assert JWT not in redact(rendered, JWT + suffix)

    def test_it_masks_a_repr_rendered_key(self) -> None:
        assert JWT not in redact(f'value {JWT + chr(13)!r} rejected', JWT + '\r')

    def test_it_masks_a_json_escaped_key(self) -> None:
        assert JWT not in redact(f'body {json.dumps({"apikey": JWT + chr(10)})}', JWT + '\n')

    def test_a_clean_key_is_masked_exactly_as_before(self) -> None:
        # The spelling expansion must not change the ordinary case.
        assert redact(f'HTTP 401 for {JWT}', JWT) == f'HTTP 401 for {REDACTED}'

    def test_a_short_key_is_still_left_alone_in_every_spelling(self) -> None:
        assert redact('the code is nope and nope', 'nop\r') == 'the code is nope and nope'

    # ⚠ CI-065. The CI-061 final review falsified that row's own "zero mutation survivors"
    # claim: `_key_spellings` emits four spellings, but each *new* one independently satisfied
    # every existing assertion, so none was individually pinned. These four tests pin them.
    def test_only_the_escaped_spelling_is_present_and_it_is_masked(self) -> None:
        # An interior literal backslash, chosen so `key.strip(_TRIMMABLE_KEY_CHARS) == key` and
        # the trimmed spelling collapses onto the raw one. `repr` doubles the backslash, so only
        # the unicode_escape spelling occurs in the text at all.
        key = 'abcdefg\\hijk'
        text = f'value {key!r} rejected'
        assert key not in text  # not vacuous: the raw and trimmed spellings are absent
        assert 'abcdefg\\\\hijk' in text  # ...and the escaped one is what is really there
        assert 'abcdefg' not in redact(text, key)

    def test_only_the_trimmed_spelling_is_present_and_it_is_masked(self) -> None:
        # A leading control character, in text where some layer already stripped it.
        key = '\rabcdefghij'
        text = 'the server rejected abcdefghij'
        assert key not in text  # not vacuous
        assert key.encode('unicode_escape').decode('ascii') not in text  # nor the escaped one
        assert 'abcdefghij' not in redact(text, key)

    def test_the_spellings_are_ordered_longest_first_with_a_stable_tie_break(self) -> None:
        # Four spellings of lengths 11/10/10/8. `sorted` is stable, so before the total-order
        # key the two ten-character ones kept their set-iteration order.
        assert _key_spellings('\rabcdefgh ') == ['\\rabcdefgh ', '\rabcdefgh ', '\\rabcdefgh', 'abcdefgh']

    def test_the_spelling_order_does_not_depend_on_the_hash_seed(self) -> None:
        # Measured fallible: the unpatched code returns two different orders across these ten
        # seeds (0/3/6/7 give one, the rest the other), so a false pass is under 1% likely.
        script = 'from castiron.cli.errors import _key_spellings; print(_key_spellings(chr(13) + "abcdefgh "))'
        orders = {
            subprocess.run(
                [sys.executable, '-c', script],
                capture_output=True,
                text=True,
                check=True,
                env={**os.environ, 'PYTHONHASHSEED': str(seed)},
            ).stdout
            for seed in range(10)
        }
        assert len(orders) == 1

    def test_spellings_of_several_secrets_are_merged_and_ordered(self) -> None:
        # The variadic form CI-066's userinfo mask relies on: one union, one sort, one place the
        # order lives. Short spellings are still dropped, and duplicates collapse.
        assert _key_spellings(None) == []
        assert _key_spellings() == []
        assert _key_spellings('zzzzzzzzzz', 'aaaaaaaaaaaa', 'short', 'zzzzzzzzzz') == [
            'aaaaaaaaaaaa',
            'zzzzzzzzzz',
        ]


# ---------------------------------------------------------------------------
# CI-066. Two live credential paths, both reproduced on `main` @ 026af0f with no
# network: a userinfo password (which http.client quotes back *twice*, the second
# time with no scheme in front of it) and a `?service_role_key=` value, invisible
# to a pattern that anchored the credential word to the `?`/`&` itself.
# ---------------------------------------------------------------------------

#: Long enough to be masked by `_key_spellings` if the URL passes were removed -- so the tests
#: below that call `redact(text)` with **no key** are testing the URL passes and nothing else.
PASSWORD = 'SECRETPASSWORD123'

#: The §3.1(a) reproduction, verbatim. `http.client.HTTPConnection._get_hostport` raises
#: `InvalidURL("nonnumeric port: '%s'" % host[i + 1:])` where `host` is the whole netloc, so the
#: password appears a second time as a bare `<password>@<host>` with no `scheme://` in front.
NONNUMERIC_PORT = (
    f"Could not reach https://user:{PASSWORD}@x.supabase.co/rest/v1/: nonnumeric port: '{PASSWORD}@x.supabase.co'"
)

SERVICE_ROLE_KEY = 'SUPERSECRETVALUE123'
SERVICE_ROLE_URL = f'https://x.supabase.co/rest/v1/?service_role_key={SERVICE_ROLE_KEY}'


def encodings(value: str) -> list[tuple[str, str]]:
    """Render ``value`` the ways a printed string can actually carry it (CI-063's lesson)."""
    return [
        ('canonical', value),
        ('repr', f'rejected {value!r}'),
        ('repr-bytes', f'Invalid header value {value.encode()!r}'),
        ('json', json.dumps({'url': value})),
        ('quoted', f'"{value}"'),
    ]


@pytest.mark.unit
class TestRedactUrlCredentials:
    def test_a_userinfo_password_is_masked_in_the_url(self) -> None:
        masked = redact(NONNUMERIC_PORT)
        assert PASSWORD not in masked
        assert 'https://user:***@x.supabase.co' in masked

    def test_a_userinfo_password_is_masked_where_http_client_repeats_it(self) -> None:
        # ⚠ The test a URL-only regex fails: the second occurrence has no `scheme://` prefix,
        # so only the exact `<password>@<host>` mop-up closes it.
        masked = redact(NONNUMERIC_PORT)
        assert f'{PASSWORD}@x.supabase.co' not in masked
        assert "nonnumeric port: '***@x.supabase.co'" in masked

    def test_a_short_userinfo_password_is_masked_too(self) -> None:
        # Four characters -- far under _MIN_REDACTABLE_KEY, so the spelling pass cannot touch it.
        # The mop-up is positional, which is exactly why it is not length-gated.
        text = "Could not reach https://user:pw12@x.supabase.co/rest/v1/: nonnumeric port: 'pw12@x.supabase.co'"
        masked = redact(text)
        assert 'pw12' not in masked
        assert masked.count(REDACTED) == 2

    def test_a_password_containing_a_colon_is_masked_where_http_client_repeats_it(self) -> None:
        # ⚠ Fix round. `_get_hostport` slices the netloc at `host.rfind(':')`, so the fragment it
        # quotes back is the password's SUFFIX AFTER ITS LAST COLON, not the password. A mop-up
        # keyed on the whole password matched nothing and the JWT printed in full. Note the
        # urlsplit oracle cannot catch this: urlsplit().password is '1:eyJ...', which is absent
        # from the message, so `password not in redact(...)` passes while the secret prints.
        jwt = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'
        text = f"Could not reach https://user:1:{jwt}@x.supabase.co/rest/v1/: nonnumeric port: '{jwt}@x.supabase.co'"
        assert jwt in text  # not vacuous
        masked = redact(text)
        assert jwt not in masked
        assert "nonnumeric port: '***@x.supabase.co'" in masked

    def test_the_mop_up_is_anchored_on_the_host_not_on_the_secret(self) -> None:
        # The generalization of the test above: whatever a renderer chooses as its split point,
        # the fragment always ends in `@<host>`, so that is what the mop-up anchors on.
        text = "https://user:a:b:SECRETTAIL@h.example.com/x -- rejected 'b:SECRETTAIL@h.example.com'"
        masked = redact(text)
        assert 'SECRETTAIL' not in masked
        assert masked.count(REDACTED) == 2

    def test_a_userinfo_password_is_masked_where_it_recurs_bare(self) -> None:
        # ⚠ Fix round. Pins the VARIADIC WIRING: `redact` must feed what `_mask_url_userinfo`
        # found into `_key_spellings`. Mutating `_key_spellings(key, *url_secrets)` to
        # `_key_spellings(key)` passed 880/880 before this test -- the function was tested,
        # the call was not. The bare recurrence is deliberately NOT adjacent to `@host`, so the
        # mop-up cannot reach it and only the spelling pass can.
        text = f'Could not reach https://user:{PASSWORD}@x.supabase.co/ -- the value {PASSWORD} was also logged bare'
        assert text.count(PASSWORD) == 2  # not vacuous
        masked = redact(text)
        assert PASSWORD not in masked
        assert 'was also logged bare' in masked

    def test_a_colonless_userinfo_is_masked_where_it_recurs_bare(self) -> None:
        # ⚠ Fix round. Pins that the colonless branch REGISTERS its host for the mop-up.
        # Deliberately a SHORT token (under _MIN_REDACTABLE_KEY) so the spelling pass cannot
        # mask it and only the mop-up can -- otherwise the two mechanisms mask each other's
        # mutants and neither is pinned.
        text = 'https://ghp_x@github.com/o/r rejected: ghp_x@github.com'
        masked = redact(text)
        assert 'ghp_x' not in masked
        assert masked == 'https://***@github.com/o/r rejected: ***@github.com'

    def test_the_host_anchor_keeps_the_port(self) -> None:
        # ⚠ Fix round. Narrowing the host group to stop at `:` survived every other test, because
        # `_URL_USERINFO.sub` reconstructs exactly the span it matched, so the URL renders the
        # same either way. The discriminator is the mop-up's anchor: with a port-only authority
        # the truncated group comes out EMPTY, the empty-host guard then skips the mop-up
        # entirely, and the bare recurrence prints. Measured -- it is the one input out of eight
        # where the two variants differ, and the mutant is the one that leaks.
        #
        # The password is four characters on purpose: over _MIN_REDACTABLE_KEY the spelling pass
        # would mask the recurrence too and the mop-up would not be under test.
        assert redact('https://u:pw12@:5432/x -- repeated pw12@:5432') == (
            'https://u:***@:5432/x -- repeated ***@:5432'
        )

    def test_an_unrelated_address_at_another_host_is_left_alone(self) -> None:
        # ⚠ Fix round. The mop-up is NOT "exact and cannot mis-fire" -- it is an anchored
        # positional rewrite, and the anchor is the host. A URL whose host group came out empty
        # registers nothing, so an ordinary email address in the same message survives. Against
        # the pre-fix substring replace this printed `bo***@example.com`.
        assert redact('Could not reach https://a:b@ ; contact bob@example.com') == (
            'Could not reach https://a:***@ ; contact bob@example.com'
        )

    def test_a_colonless_userinfo_is_masked_whole(self) -> None:
        # A colon-less userinfo castiron prints is far more often a bearer token than a username.
        masked = redact('https://ghp_abcdefghijklmnop@github.com/o/r')
        assert 'ghp_abcdefghijklmnop' not in masked
        assert masked == 'https://***@github.com/o/r'

    def test_the_username_survives_so_the_error_stays_diagnostic(self) -> None:
        # "You connected as postgres, not app_user" is the whole diagnostic value of the line,
        # and CI-010's postgresql://user:password@host/db DSNs depend on this shape.
        masked = redact('postgresql://postgres:hunter2pass@db.x.supabase.co:5432/postgres')
        assert masked == 'postgresql://postgres:***@db.x.supabase.co:5432/postgres'

    @pytest.mark.parametrize(
        'url',
        [
            'https://user:HUNTERPASSWORD@x.supabase.co/rest/v1/',
            'postgresql://postgres:HUNTERPASSWORD@db.x.supabase.co:5432/postgres',
            # urllib derives userinfo with netloc.rpartition('@') -- the LAST `@`, not the first --
            # so here it reports username='a@b', password='HUNTERPASSWORD'. A lazy or
            # @-excluding class would split at the first `@` and leak the password.
            'https://a@b:HUNTERPASSWORD@host/x',
            # The malformed shape CI-066(a) actually fires on: urlsplit().port raises here.
            'https://u:HUNTERPASSWORD@h:notaport/',
            'https://user:HUNTERPASSWORD@host',
        ],
    )
    def test_the_mask_agrees_with_urlsplit(self, url: str) -> None:
        # Pins the regex against urllib's own notion of userinfo rather than against itself.
        # (The passwords are deliberately distinctive: a one-character password would make the
        # `not in` assertion pass or fail on unrelated prose.)
        password = urlsplit(url).password
        assert password == 'HUNTERPASSWORD'  # not vacuous, and urllib agrees on where it is
        assert password not in redact(f'Could not reach {url}: boom')

    @pytest.mark.parametrize(
        'text',
        [
            f'https://user:{PASSWORD}@[::1',  # urlsplit raises `Invalid IPv6 URL` on this one
            f'https://u:{PASSWORD}@h:notaport/',  # and `Port could not be cast` on this one
            '://@',
            '@',
            '',
            'https://user@',
        ],
    )
    def test_a_malformed_url_never_raises(self, text: str) -> None:
        # Malformed input must degrade to "mask anyway", never to "raise" and never to "give up":
        # the URLs this row exists to defend are precisely the ones urllib itself chokes on.
        masked = redact(text)
        assert PASSWORD not in masked

    def test_a_url_without_userinfo_is_untouched(self) -> None:
        assert (
            redact('Could not reach https://x.supabase.co/rest/v1/') == 'Could not reach https://x.supabase.co/rest/v1/'
        )

    @pytest.mark.parametrize(('encoding', 'text'), encodings(f'https://user:{PASSWORD}@x.supabase.co/rest/v1/'))
    def test_it_survives_every_encoding_the_password_can_arrive_in(self, encoding: str, text: str) -> None:
        # CI-063: a masking function must be tested against the encodings its input can actually
        # arrive in, not only the canonical one.
        assert PASSWORD in text  # not vacuous
        assert PASSWORD not in redact(text)

    def test_a_percent_encoded_password_is_masked(self) -> None:
        # The mask is positional inside the userinfo, so it does not care how the value is spelled.
        assert 'SEC%52ETPASS' not in redact('Could not reach https://user:SEC%52ETPASS@x.supabase.co/')

    def test_a_password_truncated_out_of_its_url_is_the_documented_gap(self) -> None:
        # `fetch._snippet` cuts a body at 200 characters before `redact` ever sees it. A userinfo
        # cut before its `@` has no shape left to match, so the prefix survives -- spec §10.5,
        # inherent to substring masking. Asserted rather than pretended closed.
        cut = f'Could not reach https://user:{PASSWORD}'
        assert redact(cut) == cut
        # ...but a secret supplied as --key is still masked by its spelling, cut or not.
        assert PASSWORD not in redact(cut, PASSWORD)


@pytest.mark.unit
class TestRedactSecretParameters:
    @pytest.mark.parametrize(
        'name',
        [
            'service_role_key',
            'sb-publishable-key',
            'x-api-key',
            'anon_key',
            'client_secret',
            'refresh_token',
            'serviceRoleKey',
            'api%5Fkey',
            'session_id',
            'authorization',
            'password',
        ],
    )
    def test_it_masks_a_prefixed_credential_parameter(self, name: str) -> None:
        # The old pattern required the credential word to start immediately after the `?`/`&`,
        # so every prefixed spelling was invisible to it -- and a service-role key is strictly
        # more dangerous than an anon key.
        masked = redact(f'https://x.supabase.co/rest/v1/?{name}={SERVICE_ROLE_KEY}')
        assert SERVICE_ROLE_KEY not in masked
        assert f'?{name}={REDACTED}' in masked

    def test_it_masks_a_fragment_parameter(self) -> None:
        # A Supabase auth redirect puts the token in the URL *fragment*, not the query.
        masked = redact(f'https://x.supabase.co/#access_token={SERVICE_ROLE_KEY}&refresh_token=OTHERSECRET1')
        assert SERVICE_ROLE_KEY not in masked
        assert 'OTHERSECRET1' not in masked

    # ⚠ Fix round. One assertion per alternative in `_SECRET_WORD`, enumerated rather than
    # sampled. Eight of the sixteen (auth, credentials, credential, passwd, pwd, signature, sig,
    # bearer) could each be deleted with the whole 880-test suite green -- a typo in any of them
    # was invisible. That is the CI-061 failure mode in new code: a harness only proves the
    # mutants you chose to write, so when a construct has N alternatives you enumerate all N.
    #
    # Each name below exercises exactly ONE alternative. `auth` is spelled `x-auth` and not
    # `auth` alone so it cannot be satisfied by `authorization`; `credential`/`credentials` and
    # `sig`/`signature` and `pwd`/`passwd` are likewise separated by a delimiter, since the
    # alternation is ordered longest-first and a bare `credentials` would also match `credential`
    # + a boundary lookahead failure.
    @pytest.mark.parametrize(
        ('name', 'alternative'),
        [
            ('apikey', 'api[-_]?key'),
            ('authorization', 'authorization'),
            ('x-auth', 'auth'),
            ('user-credentials', 'credentials'),
            ('user-credential', 'credential'),
            ('password', 'password'),
            ('db-passwd', 'passwd'),
            ('db-pwd', 'pwd'),
            ('request-signature', 'signature'),
            ('request-sig', 'sig'),
            ('client_secret', 'secret'),
            ('session_id', 'session'),
            ('access_token', 'token'),
            ('bearer', 'bearer'),
            ('jwt', 'jwt'),
            ('service_role_key', 'key'),
        ],
    )
    def test_every_credential_word_is_pinned(self, name: str, alternative: str) -> None:
        masked = redact(f'https://x.supabase.co/rest/v1/?{name}={SERVICE_ROLE_KEY}')
        assert SERVICE_ROLE_KEY not in masked, f'the {alternative!r} alternative masks nothing'
        assert f'?{name}={REDACTED}' in masked

    # ⚠ Fix round. Every member of both delimiter classes, and both camelCase boundaries, one
    # assertion each -- the same "enumerate, do not sample" rule the word list needed. Dropping
    # `.` from either class, `-` from the trailing class, or `[A-Z]` from the trailing lookahead
    # each survived the whole suite before these.
    @pytest.mark.parametrize(
        ('name', 'boundary'),
        [
            ('api.key', 'leading `.`'),
            ('key.id', 'trailing `.`'),
            ('x-api-key', 'leading `-`'),
            ('key-id', 'trailing `-`'),
            ('anon_key', 'leading `_`'),
            ('key_id', 'trailing `_`'),
            ('serviceRoleKey', 'leading camelCase hump'),
            # ⚠ `keyMaterial`, not `authToken`: in `authToken` the trailing `[A-Z]` is redundant,
            # because `Token` also matches via the *leading* hump, so it does not discriminate.
            # Here `Material` matches no credential word, so the trailing `[A-Z]` is the only
            # thing that can match `key`. Measured against the pattern with and without it.
            ('keyMaterial', 'trailing camelCase hump'),
        ],
    )
    def test_every_word_boundary_is_pinned(self, name: str, boundary: str) -> None:
        masked = redact(f'https://x.supabase.co/rest/v1/?{name}={SERVICE_ROLE_KEY}')
        assert SERVICE_ROLE_KEY not in masked, f'the {boundary} boundary matches nothing'
        assert f'?{name}={REDACTED}' in masked

    def test_a_base64_value_with_padding_is_masked_whole(self) -> None:
        # ⚠ Fix round. The `=` in the value class was unpinned: narrowing `[^&\s#]*` to
        # `[^&\s#=]*` survived, because no test had a value containing `=` -- which is the common
        # real shape (base64 padding, and a JWT's own `=`-padded segments).
        value = 'YWJjZGVmZ2hpamtsbW5vcA=='
        masked = redact(f'https://x.supabase.co/rest/v1/?apikey={value}')
        assert masked == 'https://x.supabase.co/rest/v1/?apikey=***'
        assert 'YWJjZGVmZ2hpamtsbW5vcA' not in masked

    @pytest.mark.parametrize('name', ['monkey', 'keyword', 'keys', 'tokens', 'turnkey', 'select', 'order', 'limit'])
    def test_it_leaves_a_diagnostic_parameter_alone(self, name: str) -> None:
        # A guard against over-masking, not a regression test: a credential word that is merely a
        # substring of the parameter name is not a credential.
        text = f'https://x.supabase.co/rest/v1/table?{name}=eq.5'
        assert redact(text) == text

    @pytest.mark.parametrize(('encoding', 'text'), encodings(SERVICE_ROLE_URL))
    def test_it_survives_every_encoding_the_value_can_arrive_in(self, encoding: str, text: str) -> None:
        assert SERVICE_ROLE_KEY in text  # not vacuous
        assert SERVICE_ROLE_KEY not in redact(text)

    def test_a_percent_encoded_parameter_name_still_matches(self) -> None:
        # The name is matched decoded and rewritten as it was found, so the user's own spelling
        # of their URL survives in the message.
        masked = redact(f'https://x.supabase.co/rest/v1/?service%5Frole%5Fkey={SERVICE_ROLE_KEY}')
        assert SERVICE_ROLE_KEY not in masked
        assert 'service%5Frole%5Fkey=***' in masked

    def test_a_percent_encoded_value_is_masked_whole(self) -> None:
        assert 'SEC%52ET' not in redact('https://x.supabase.co/rest/v1/?apikey=SEC%52ET')

    def test_a_truncated_value_is_still_masked_because_the_mask_is_positional(self) -> None:
        # Unlike the substring gap above, cutting the *value* costs nothing here: the parameter
        # mask keys on the name, so whatever survives the cut is still masked.
        assert SERVICE_ROLE_KEY[:8] not in redact(f'{SERVICE_ROLE_URL[:60]}...')


@pytest.mark.unit
class TestRejectUrlUserinfo:
    # The boundary half of the two-layer defence (CI-063's shape, CI066-Q1's ruling): castiron
    # cannot successfully fetch from an http(s) userinfo URL under any circumstance, so refusing
    # it costs nothing a user could have wanted and removes the trigger rather than masking the
    # symptom.
    def test_no_source_stays_none(self) -> None:
        assert reject_url_userinfo(None) is None

    @pytest.mark.parametrize(
        'source',
        [
            'https://x.supabase.co/rest/v1/',
            './openapi.json',
            'openapi.json',
            'https://x.supabase.co/rest/v1/table?select=a,b',
            # An `@` that is not userinfo: it is after the path or query separator.
            'https://x.supabase.co/rest/v1/table?filter=eq.a@b.com',
            './my@dir/openapi.json',
            '',
        ],
    )
    def test_a_source_without_userinfo_is_returned_unchanged(self, source: str) -> None:
        assert reject_url_userinfo(source) == source

    @pytest.mark.parametrize(
        'source',
        [
            f'postgresql://postgres:{PASSWORD}@db.x.supabase.co:5432/postgres',
            f'postgres://postgres:{PASSWORD}@db.x.supabase.co:5432/postgres',
            f'mysql://root:{PASSWORD}@db.example.com:3306/app',
        ],
    )
    def test_a_non_http_userinfo_url_is_accepted(self, source: str) -> None:
        # ⚠ Fix round. The "cannot succeed" measurement is about *HTTP* fetching and does not
        # generalize: `postgresql://user:password@host/db` is the canonical libpq connection
        # string, and CI-010's live-database source will consume it. Refusing it -- with a
        # message telling the user to "pass the key with --key" -- would be wrong, and the same
        # docstring that justifies the userinfo *mask* cites exactly these DSNs. The mask still
        # covers them; only the boundary refusal is scoped to what castiron fetches over HTTP.
        assert reject_url_userinfo(source) == source
        assert PASSWORD not in redact(f'Could not connect to {source}')

    @pytest.mark.parametrize(
        'source',
        [
            f'https://user:{PASSWORD}@x.supabase.co',
            f'https://{PASSWORD}@x.supabase.co/rest/v1/',
            f'http://user:{PASSWORD}@x.supabase.co/rest/v1/',
        ],
    )
    def test_a_userinfo_url_is_refused(self, source: str) -> None:
        with pytest.raises(click.UsageError) as excinfo:
            reject_url_userinfo(source)
        assert 'userinfo' in excinfo.value.message
        assert excinfo.value.exit_code == EXIT_USAGE

    @pytest.mark.parametrize(
        'source',
        [
            f'https://user:{PASSWORD}@x.supabase.co',
            f'https://{PASSWORD}@x.supabase.co/rest/v1/',
        ],
    )
    def test_the_refusal_never_echoes_the_value(self, source: str) -> None:
        # The whole point: the message castiron prints instead of urllib's must not reproduce
        # what urllib's did.
        with pytest.raises(click.UsageError) as excinfo:
            reject_url_userinfo(source)
        assert PASSWORD not in excinfo.value.message
        assert 'x.supabase.co' not in excinfo.value.message

    def test_the_message_says_what_to_do_instead(self) -> None:
        with pytest.raises(click.UsageError) as excinfo:
            reject_url_userinfo(f'https://user:{PASSWORD}@x.supabase.co')
        assert '--key' in excinfo.value.message
        assert 'CASTIRON_KEY' in excinfo.value.message


@pytest.mark.unit
class TestRedactSource:
    # ⚠ CI-068, folded in by captain ruling. `redact` anchors userinfo on `scheme://` because it
    # scans arbitrary prose, where a bare `a:b@c` is far more often ordinary text than a
    # credential. That leaves the one surface which echoes the raw --from value back exposed to
    # the schemeless shape psql connection strings actually circulate in. `redact_source` is a
    # single-option-value transform, so the "occurs in prose" objection does not apply to it.
    @pytest.mark.parametrize(
        'source',
        [
            f'postgres:{PASSWORD}@db.x.supabase.co:5432/postgres',
            f'postgres://postgres:{PASSWORD}@db.x.supabase.co:5432/postgres',
            f'user:{PASSWORD}@x.supabase.co',
            f'{PASSWORD}@x.supabase.co',
            f'postgresql://u:{PASSWORD}@h/db',
        ],
    )
    def test_a_schemeless_userinfo_value_is_masked(self, source: str) -> None:
        assert PASSWORD in source  # not vacuous
        assert PASSWORD not in redact_source(source)

    def test_it_masks_up_to_the_last_at_sign(self) -> None:
        # `rpartition`, matching how urlsplit derives userinfo from a netloc.
        assert redact_source(f'a@b:{PASSWORD}@db.example.com:5432/x') == '***@db.example.com:5432/x'

    def test_it_still_applies_the_ordinary_rules(self) -> None:
        # It composes with `redact` rather than replacing it: query parameters and scheme-bearing
        # userinfo keep working exactly as before.
        assert redact_source(f'x.supabase.co/rest/v1/?apikey={PASSWORD}') == 'x.supabase.co/rest/v1/?apikey=***'
        assert redact_source(f'https://user:{PASSWORD}@x.supabase.co') == 'https://user:***@x.supabase.co'

    @pytest.mark.parametrize('source', ['nope.json', './dump.json', 'x.supabase.co/rest/v1/', ''])
    def test_a_value_without_an_at_sign_is_untouched(self, source: str) -> None:
        assert redact_source(source) == source

    def test_a_nonexistent_path_containing_an_at_sign_is_over_masked(self) -> None:
        # The accepted cost, asserted rather than left to be discovered. Only the "neither a URL
        # nor an existing file" message prints this, so a path that resolves is never affected,
        # and over-masking a filename the user just typed is cheaper than printing a password.
        assert redact_source('./my@dir/openapi.json') == '***@dir/openapi.json'


@pytest.mark.unit
class TestSourceOptionCallback:
    def test_it_passes_a_clean_source_through(self) -> None:
        ctx = click.Context(click.Command('gen'))
        assert source_option_callback(ctx, click.Option(['--from']), 'openapi.json') == 'openapi.json'

    def test_it_refuses_a_userinfo_source(self) -> None:
        ctx = click.Context(click.Command('gen'))
        with pytest.raises(click.UsageError):
            source_option_callback(ctx, click.Option(['--from']), f'https://user:{PASSWORD}@x.supabase.co')

    def test_resilient_parsing_never_raises(self) -> None:
        # Shell completion parses with resilient_parsing set; raising there breaks completion.
        ctx = click.Context(click.Command('gen'))
        ctx.resilient_parsing = True
        raw = f'https://user:{PASSWORD}@x.supabase.co'
        assert source_option_callback(ctx, click.Option(['--from']), raw) == raw


@pytest.mark.unit
class TestSanitizeKey:
    def test_no_key_stays_none(self) -> None:
        assert sanitize_key(None) is None

    @pytest.mark.parametrize('raw', [JWT + '\r', JWT + '\n', JWT + '\r\n', ' ' + JWT + ' ', JWT + '\x00'])
    def test_surrounding_control_characters_are_trimmed(self, raw: str) -> None:
        # A CRLF key file is unambiguous user error with exactly one sensible reading.
        assert sanitize_key(raw) == JWT

    def test_a_clean_key_is_returned_unchanged(self) -> None:
        assert sanitize_key(JWT) == JWT

    @pytest.mark.parametrize('raw', ['eyJhbGci\rOiJIUzI1NiJ9', 'eyJhbGci\nOiJIUzI1NiJ9', 'eyJhbGci\tOiJIUzI1NiJ9'])
    def test_an_interior_control_character_is_refused(self, raw: str) -> None:
        # Trimming cannot repair it: the value is not the key the user thinks it is.
        with pytest.raises(click.UsageError) as excinfo:
            sanitize_key(raw)
        assert 'control character' in excinfo.value.message
        assert 'CRLF' in excinfo.value.message

    def test_the_refusal_never_echoes_the_key(self) -> None:
        with pytest.raises(click.UsageError) as excinfo:
            sanitize_key(f'{JWT}\r{JWT}')
        assert JWT not in excinfo.value.message

    def test_an_all_whitespace_key_trims_to_empty(self) -> None:
        assert sanitize_key(' \r\n ') == ''


@pytest.mark.unit
class TestKeyOptionCallback:
    def test_it_sanitizes_the_resolved_value(self) -> None:
        ctx = click.Context(click.Command('gen'))
        assert key_option_callback(ctx, click.Option(['--key']), JWT + '\r') == JWT

    def test_resilient_parsing_never_raises(self) -> None:
        # Shell completion parses with resilient_parsing set; raising there breaks completion.
        ctx = click.Context(click.Command('gen'))
        ctx.resilient_parsing = True
        raw = f'{JWT}\r{JWT}'
        assert key_option_callback(ctx, click.Option(['--key']), raw) == raw


@pytest.mark.unit
class TestTheHeaderValueLeak:
    def test_a_crlf_key_never_reaches_the_terminal(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The finding, end to end at the source layer: a key with a trailing \r makes
        # `putheader` raise before any socket is opened, the fetcher wraps the ValueError, and
        # `redact` used to match nothing because the \r had been escaped into two characters.
        # Fails against main: the full JWT prints on the ordinary `Error:` line at exit 1.
        def no_sockets(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError('this path must not open a socket')

        monkeypatch.setattr(socket, 'create_connection', no_sockets)
        with pytest.raises(SourceError) as excinfo:
            fetch_openapi_document('https://x.supabase.co', key=JWT + '\r')
        assert JWT in str(excinfo.value)  # not vacuous: the raw message really does carry it
        assert JWT not in redact(str(excinfo.value), JWT + '\r')


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

    @pytest.mark.parametrize(
        ('message', 'secret'),
        [
            ('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET failed', 'SUPERSECRET'),
            (f'{SERVICE_ROLE_URL} failed', SERVICE_ROLE_KEY),
            (NONNUMERIC_PORT, PASSWORD),
            # The shape CI-010's live-DB source will raise, which is why the userinfo mask ships
            # regardless of the boundary rejection.
            ('Could not connect to postgresql://postgres:hunter2pass@db.x.supabase.co:5432/postgres', 'hunter2pass'),
        ],
        ids=['apikey', 'service_role_key', 'url-userinfo', 'dsn'],
    )
    def test_a_source_error_message_is_redacted(self, message: str, secret: str) -> None:
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key='eyJhbGciOi'):
                raise SourceFetchError(f'{message} for eyJhbGciOi')
        assert secret not in excinfo.value.message
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

    @pytest.mark.parametrize(
        ('message', 'secret'),
        [
            ('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET broke', 'SUPERSECRET'),
            (f'{SERVICE_ROLE_URL} broke', SERVICE_ROLE_KEY),
            (NONNUMERIC_PORT, PASSWORD),
        ],
        ids=['apikey', 'service_role_key', 'url-userinfo'],
    )
    def test_the_internal_error_echo_is_redacted(
        self, capsys: pytest.CaptureFixture[str], message: str, secret: str
    ) -> None:
        # CI6-D7: *every* printed string is redacted, and the internal-error echo is printed
        # -- with an invitation to paste it into a public issue, which makes it the worst
        # possible place for a key to survive. A castiron bug can carry the URL (and so the
        # key) in its str() from anywhere in the pipeline. Parametrized over all three secret
        # shapes because "every" is the claim (CI6-Q7).
        with pytest.raises(SystemExit):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key='eyJhbGciOi'):
                raise RuntimeError(f'{message} on eyJhbGciOi')
        printed = capsys.readouterr().err
        assert 'internal error (RuntimeError' in printed
        assert secret not in printed
        assert 'eyJhbGciOi' not in printed

    def test_debug_prints_the_traceback_itself_and_exits_seventy(self, capsys: pytest.CaptureFixture[str]) -> None:
        # CI-062. Re-raising handed the exception to the interpreter, which prints the traceback
        # with no redaction at all -- and exits 1 rather than 70. castiron prints it instead.
        with pytest.raises(SystemExit) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=True, key=None):
                raise RuntimeError('kaboom')
        assert excinfo.value.code == EXIT_INTERNAL
        printed = capsys.readouterr().err
        assert 'Traceback (most recent call last)' in printed
        assert 'RuntimeError: kaboom' in printed

    def test_the_debug_traceback_is_redacted_through_the_chain(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The load-bearing one. The `Error:` line was already clean; the chained "During handling
        # of the above exception" block below it carried the *original* message in full -- and
        # `internal_error_message` invites the user to paste that output into a public issue.
        with pytest.raises(SystemExit) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=True, key=None):
                try:
                    raise SourceFetchError(f'{SERVICE_ROLE_URL} failed')
                except SourceFetchError as inner:
                    raise RuntimeError('inner blew up while handling the fetch failure') from inner
        printed = capsys.readouterr().err
        # Non-vacuity first: a test that passes because nothing was printed is worse than none.
        assert 'Traceback (most recent call last)' in printed
        assert 'The above exception was the direct cause' in printed
        assert 'RuntimeError: inner blew up' in printed
        assert SERVICE_ROLE_KEY not in printed
        assert excinfo.value.code == EXIT_INTERNAL

    @pytest.mark.parametrize(
        ('message', 'secret'),
        [
            ('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET failed', 'SUPERSECRET'),
            (f'{SERVICE_ROLE_URL} failed', SERVICE_ROLE_KEY),
            (NONNUMERIC_PORT, PASSWORD),
        ],
        ids=['apikey', 'service_role_key', 'url-userinfo'],
    )
    def test_the_debug_traceback_is_redacted_in_the_context_chain_too(
        self, capsys: pytest.CaptureFixture[str], message: str, secret: str
    ) -> None:
        # The implicit chain ("During handling of the above exception"), which is what an
        # exception raised *while handling* a SourceFetchError produces -- and it is the shape
        # the CI-061 audit predicted would open this leak.
        with pytest.raises(SystemExit):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=True, key='eyJhbGciOi'):
                try:
                    raise SourceFetchError(message)
                except SourceFetchError:
                    raise RuntimeError('inner blew up while handling the fetch failure')  # noqa: B904
        printed = capsys.readouterr().err
        assert 'During handling of the above exception' in printed  # not vacuous
        assert secret not in printed
        assert 'eyJhbGciOi' not in printed

    def test_without_debug_no_traceback_is_printed(self, capsys: pytest.CaptureFixture[str]) -> None:
        # The guard that --debug is still what turns it on: the default stays a one-line message.
        with pytest.raises(SystemExit):  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None):
                raise RuntimeError('kaboom')
        assert 'Traceback' not in capsys.readouterr().err

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

    @pytest.mark.parametrize(
        ('url', 'secret'),
        [
            ('https://x.supabase.co/rest/v1/?apikey=SUPERSECRET', 'SUPERSECRET'),
            (SERVICE_ROLE_URL, SERVICE_ROLE_KEY),
            (f'https://user:{PASSWORD}@x.supabase.co/rest/v1/', PASSWORD),
        ],
        ids=['apikey', 'service_role_key', 'url-userinfo'],
    )
    def test_the_hint_is_redacted_before_it_is_printed(
        self, monkeypatch: pytest.MonkeyPatch, url: str, secret: str
    ) -> None:
        exc = real_fetch_error(monkeypatch, http_error(401), url)
        assert secret in str(exc)  # not vacuous: the raw message really does carry it
        with pytest.raises(click.ClickException) as excinfo:  # noqa: PT012 - the boundary is the subject
            with cli_error_handling(debug=False, key=None, hint=lambda e: f'the URL was {exc}'):
                raise exc
        assert secret not in excinfo.value.message


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
