"""Exit codes, the error boundary, secret redaction, and the ``Hint:`` lines."""

import json
import os
import random
import re
import socket
import subprocess
import sys
import timeit
from collections.abc import Callable, Sequence
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

import click
import pytest

from castiron.cli import errors
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

    @pytest.mark.parametrize('name', ['monkey', 'keyword', 'keys', 'tokens', 'turnkey', 'select', 'order', 'limit'])
    def test_a_doubled_lead_does_not_widen_the_credential_word_list(self, name: str) -> None:
        # ⚠ CI-150 splits a name on `?` before testing it, and this is the guard that the split
        # only changes where a candidate name BEGINS -- never what counts as a credential word.
        # `a?monkey` is `a` and `monkey`, and `monkey` is no more a credential than it was.
        text = f'https://x.supabase.co/rest/v1/table?a?{name}=eq.5'
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


# ---------------------------------------------------------------------------
# CI-073. `redact` was quadratic in the length of the text, and the row is only reachable through
# `RedactingFilter`, which runs it on every log record and on a rendered traceback -- neither
# length-capped the way `_snippet` caps an error body. Measured on `main` @ d4455ae:
#
#   20 000 characters of DSNs           0.515 s   (2 000 characters: 0.005 s -- 99x for 10x)
#   20 000 characters of base64          0.185 s   ...carrying NO secret at all
#   both in one message                  0.979 s
#
# Three independent causes, two of them fixed here: a `re.sub` per host, a `str.replace` per key
# spelling, and a greedy pattern restarted from every position of a run.
#
# ⚠ The whole point of this row is that it must not cost coverage, so the tests below are the
# regression checklist rather than a benchmark. `TestRedactableShapes` pins the exact output of
# every shape `redact` masks -- generated from `main`'s implementation and asserted byte-for-byte
# against this one -- and `TestRedactPlantedSecrets` is the property version of the same claim.
# ---------------------------------------------------------------------------

#: Every parameter name the credential-word list and its boundary rules accept. Each has its value
#: masked whole, and the name echoed back exactly as the user spelled it.
CREDENTIAL_PARAM_NAMES = (
    'apikey',
    'APIKEY',
    'api_key',
    'api-key',
    'api.key',
    'api%5Fkey',
    'x-api-key',
    'service_role_key',
    'serviceRoleKey',
    'sb-publishable-key',
    'anon_key',
    'keyMaterial',
    'key',
    'client_secret',
    'secret',
    'refresh_token',
    'access_token',
    'token',
    'authorization',
    'x-auth',
    'user-credentials',
    'user-credential',
    'password',
    'db-passwd',
    'db-pwd',
    'request-signature',
    'request-sig',
    'session_id',
    'session',
    'bearer',
    'jwt',
)

#: (name, text, key, expected) for every *other* shape. The expected column is the output of the
#: implementation this row replaced -- not of this one -- so a shape whose masking changes fails
#: here rather than being silently re-baselined.
REDACTABLE_SHAPES: tuple[tuple[str, str, str | None, str], ...] = (
    # -- URL userinfo, http and DSN. The username survives; the password does not. --
    (
        'userinfo password',
        f'Could not reach https://user:{PASSWORD}@x.supabase.co/rest/v1/: boom',
        None,
        'Could not reach https://user:***@x.supabase.co/rest/v1/: boom',
    ),
    (
        'userinfo dsn',
        'postgresql://postgres:hunter2pass@db.x.supabase.co:5432/postgres',
        None,
        'postgresql://postgres:***@db.x.supabase.co:5432/postgres',
    ),
    ('userinfo mysql dsn', 'mysql://root:hunter2pass@127.0.0.1:3306/app', None, 'mysql://root:***@127.0.0.1:3306/app'),
    ('userinfo last at sign wins', f'https://a@b:{PASSWORD}@host/x', None, 'https://a@b:***@host/x'),
    ('userinfo colonless', 'https://ghp_abcdefghijklmnop@github.com/o/r', None, 'https://***@github.com/o/r'),
    ('userinfo bad port', f'https://u:{PASSWORD}@h:notaport/', None, 'https://u:***@h:notaport/'),
    ('userinfo truncated ipv6', f'https://user:{PASSWORD}@[::1', None, 'https://user:***@[::1'),
    ('userinfo percent-encoded', 'https://user:SEC%52ETPASS@x.supabase.co/', None, 'https://user:***@x.supabase.co/'),
    (
        'userinfo empty host',
        'reached https://a:b@ ; contact bob@example.com',
        None,
        'reached https://a:***@ ; contact bob@example.com',
    ),
    (
        'userinfo port-only host',
        'https://u:pw12@:5432/x -- repeated pw12@:5432',
        None,
        'https://u:***@:5432/x -- repeated ***@:5432',
    ),
    (
        'userinfo none present',
        'Could not reach https://x.supabase.co/rest/v1/',
        None,
        'Could not reach https://x.supabase.co/rest/v1/',
    ),
    (
        'userinfo cut before its at sign',
        f'Could not reach https://user:{PASSWORD}',
        None,
        f'Could not reach https://user:{PASSWORD}',
    ),
    # -- the bare `<secret>@host` http.client quotes back with no scheme in front --
    (
        'nonnumeric port',
        NONNUMERIC_PORT,
        None,
        "Could not reach https://user:***@x.supabase.co/rest/v1/: nonnumeric port: '***@x.supabase.co'",
    ),
    (
        'nonnumeric port, password under the length gate',
        "Could not reach https://user:pw12@x.supabase.co/: nonnumeric port: 'pw12@x.supabase.co'",
        None,
        "Could not reach https://user:***@x.supabase.co/: nonnumeric port: '***@x.supabase.co'",
    ),
    (
        'nonnumeric port, only the password suffix quoted back',
        f"https://user:1:{JWT}@x.supabase.co/: nonnumeric port: '{JWT}@x.supabase.co'",
        None,
        "https://user:***@x.supabase.co/: nonnumeric port: '***@x.supabase.co'",
    ),
    (
        'bare recurrence away from its host',
        f'https://user:{PASSWORD}@x.supabase.co/ -- the value {PASSWORD} was logged bare',
        None,
        'https://user:***@x.supabase.co/ -- the value *** was logged bare',
    ),
    (
        'colonless userinfo recurring bare',
        'https://ghp_x@github.com/o/r rejected: ghp_x@github.com',
        None,
        'https://***@github.com/o/r rejected: ***@github.com',
    ),
    # The documented mop-up gap, asserted rather than pretended closed: the run class excludes
    # quotes so a message keeps its own quoting, and a password carrying one is masked only from
    # its last quote onward.
    (
        'quote inside the password strands its prefix',
        "https://u:SECRETPREFIX9'x@x.supabase.co/ -- nonnumeric port: 'SECRETPREFIX9'x@x.supabase.co'",
        None,
        "https://u:***@x.supabase.co/ -- nonnumeric port: 'SECRETPREFIX9'***@x.supabase.co'",
    ),
    # -- the --key value, in every spelling a renderer can produce --
    ('key literal', f'HTTP 401 for {JWT}', JWT, 'HTTP 401 for ***'),
    ('key too short to mask', 'the code is nope and nope', 'nop', 'the code is nope and nope'),
    ('key too short in every spelling', 'the code is nope and nope', 'nop\r', 'the code is nope and nope'),
    ('key rendered by repr', f'value {JWT + chr(13)!r} rejected', JWT + '\r', "value '***' rejected"),
    (
        'key rendered by repr of bytes',
        f'Invalid header value {("Bearer " + JWT + chr(13)).encode()!r}',
        JWT + '\r',
        "Invalid header value b'Bearer ***'",
    ),
    (
        'key trimmed of its control character',
        'the server rejected abcdefghij',
        '\rabcdefghij',
        'the server rejected ***',
    ),
    ('key with its backslash escaped', "value 'abcdefg\\\\hijk' rejected", 'abcdefg\\hijk', "value '***' rejected"),
    ('no key in play', 'plain message', None, 'plain message'),
    # -- everything at once, and the shapes a traceback carries --
    (
        'userinfo, parameter and key in one message',
        f'https://u:{PASSWORD}@x.supabase.co/rest/v1/?apikey={JWT}: {PASSWORD}@x.supabase.co',
        JWT,
        # ⚠ The parameter value class is greedy up to `&`, `#` or whitespace, so it swallows the
        # message's own `:` as well -- a documented accepted cost of `redact`, pinned here.
        'https://u:***@x.supabase.co/rest/v1/?apikey=*** ***@x.supabase.co',
    ),
    (
        'two DSNs, two hosts',
        'postgresql://a:pw1234@h1.example.com/d and postgresql://b:pw5678@h2.example.com/d',
        None,
        'postgresql://a:***@h1.example.com/d and postgresql://b:***@h2.example.com/d',
    ),
    (
        'two DSNs, one host',
        'postgresql://a:pw1234@h.example.com/d and postgresql://b:pw5678@h.example.com/d',
        None,
        'postgresql://a:***@h.example.com/d and postgresql://b:***@h.example.com/d',
    ),
    (
        'quoted url',
        f"Could not reach 'https://user:{PASSWORD}@x.supabase.co/'",
        None,
        "Could not reach 'https://user:***@x.supabase.co/'",
    ),
    (
        'json-rendered url',
        f'{{"url": "https://user:{PASSWORD}@x.supabase.co/"}}',
        None,
        '{"url": "https://user:***@x.supabase.co/"}',
    ),
    ('empty text', '', None, ''),
    ('a bare at sign', '@', None, '@'),
    ('a scheme with nothing in it', '://@', None, '://@'),
    # A colonless userinfo is masked whole -- in a URL castiron prints it is far more often a
    # bearer token than a username -- even with no host after it.
    ('userinfo with no host at all', 'https://user@', None, 'https://***@'),
)


@pytest.mark.unit
class TestRedactableShapes:
    """The regression checklist: every shape `redact` masks, pinned to an exact output."""

    @pytest.mark.parametrize('name', CREDENTIAL_PARAM_NAMES)
    def test_a_credential_parameter_value_is_masked_whole(self, name: str) -> None:
        assert redact(f'https://x.supabase.co/rest/v1/?{name}={SERVICE_ROLE_KEY}') == (
            f'https://x.supabase.co/rest/v1/?{name}={REDACTED}'
        )

    @pytest.mark.parametrize('name', CREDENTIAL_PARAM_NAMES)
    def test_a_credential_fragment_parameter_value_is_masked_whole(self, name: str) -> None:
        # A Supabase auth redirect puts its token in the fragment, so `#` leads a pair too.
        assert redact(f'https://x.supabase.co/#{name}={SERVICE_ROLE_KEY}') == (
            f'https://x.supabase.co/#{name}={REDACTED}'
        )

    @pytest.mark.parametrize(
        ('name', 'text', 'key', 'expected'), REDACTABLE_SHAPES, ids=[shape[0] for shape in REDACTABLE_SHAPES]
    )
    def test_the_shape_is_masked_exactly_as_it_was(self, name: str, text: str, key: str | None, expected: str) -> None:
        assert redact(text, key) == expected, f'the {name!r} shape no longer redacts the way it did'


@pytest.mark.unit
class TestRedactPlantedSecrets:
    """The property version: a secret planted in any masked shape, anywhere, never survives."""

    #: Deliberately not `random.seed()`-free. A fixed seed makes a failure reproducible, and Hard
    #: Rule #9's determinism applies to the suite as much as to an emitter.
    SEED = 20260809

    NOISE = (
        ' ',
        "'",
        '"',
        '\r',
        '\n',
        '\t',
        '@',
        ':',
        '/',
        '//',
        '://',
        'https://',
        'postgresql://',
        '?',
        '&',
        '#',
        '=',
        'a',
        'db',
        'user',
        'x.supabase.co',
        'nonnumeric port: ',
        REDACTED,
        '.',
        '-',
        '+',
        '1',
        '5432',
        'ghp_y',
        ',',
        ']',
        '[',
        '%5F',
        'Could not reach ',
        'rest/v1',
    )
    HOSTS = ('x.supabase.co', 'db.example.com', 'h1', 'localhost:5432', '127.0.0.1', 'z', '[::1]', ':5432')

    def shapes(self, secret: str, rng: random.Random) -> list[tuple[str, str | None]]:
        """(text carrying `secret`, key) for one draw of each masked shape."""
        host = rng.choice(self.HOSTS)
        user = rng.choice(['user', 'postgres', 'a@b', 'a:b'])
        name = rng.choice(CREDENTIAL_PARAM_NAMES)
        lead = rng.choice(['?', '&', '#'])
        scheme = rng.choice(['https', 'http', 'postgresql', 'mysql', 'postgres'])
        return [
            (f'{scheme}://{user}:{secret}@{host}/x', None),
            (f'{scheme}://{secret}@{host}/x', None),
            (f"{scheme}://{user}:{secret}@{host}/x: port '{secret}@{host}'", None),
            (f"{scheme}://{user}:1:{secret}@{host}/x: port '{secret}@{host}'", None),
            # ⚠ A colon-free username, deliberately. A recurrence away from its host is reachable
            # only through the spelling pass, and `urlsplit` splits the userinfo at its FIRST colon
            # -- so with a `a:b` username the secret in play is `b:<secret>` and the bare `<secret>`
            # alone is not one of its spellings. That is the documented cost of the host anchor
            # (see `_mop_up_bare_userinfo`), pre-existing and unrelated to this row.
            (f'{scheme}://user:{secret}@{host}/x -- {secret} logged bare', None),
            (f'https://{host}/rest/v1/{lead}{name}={secret}', None),
            (f'HTTP 401 for {secret}', secret),
            (f'HTTP 401 for {secret}', f'\r{secret} '),
            (f'value {secret + chr(13)!r} rejected', f'{secret}\r'),
            (f'Invalid header value {("Bearer " + secret + chr(13)).encode()!r}', f'{secret}\r'),
        ]

    def test_a_planted_secret_never_survives_wherever_it_is_buried(self) -> None:
        # ⚠ The secret is distinctive on purpose: a short or wordlike one would make `not in` pass
        # on unrelated prose and the property would be vacuous.
        secret = 'ZQXSECRETPAYLOAD42'
        rng = random.Random(self.SEED)
        checked = 0
        for _ in range(2_000):
            text, key = rng.choice(self.shapes(secret, rng))
            before = ''.join(rng.choice(self.NOISE) for _ in range(rng.randint(0, 10)))
            after = ''.join(rng.choice(self.NOISE) for _ in range(rng.randint(0, 10)))
            # ⚠ The noise is whitespace-separated from the shape, and that is a concession with a
            # measured reason. Every pass here is a `re.sub`, and `re.sub` matches do not overlap:
            # noise that *starts* a construct immediately in front of the shape -- a second
            # `scheme://` whose host class runs into the real URL's scheme, or a `?` whose parameter
            # name runs through the real `?apikey=` -- consumes the shape's own lead, and the
            # secret prints. Both are pre-existing: `main` @ d4455ae produces byte-identical output
            # on those inputs (checked by the CI-073 differential), so asserting the property
            # without the separator would be asserting something `redact` has never had. Every
            # class of character still lands adjacent to the secret *inside* the shape.
            buried = f'{before} {text} {after}'
            assert secret in buried  # not vacuous
            assert secret not in redact(buried, key), f'the secret survived in {buried!r}'
            checked += 1
        assert checked == 2_000


#: A whitespace-free base64 run: what a source adapter logging a document looks like, and the shape
#: that made `redact` quadratic while carrying nothing to mask.
BLOB = 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9w'


@pytest.mark.unit
class TestRedactStaysLinear:
    """Structural guards: nothing here may go back to one full-text pass per secret."""

    @staticmethod
    def dsns(count: int) -> str:
        """`count` DSNs, each with its own host and its own password."""
        return ' '.join(f'postgresql://user{i}:hunter2pass{i}@db{i}.example.com:5432/x' for i in range(count))

    @staticmethod
    def spy_on_the_mop_up(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
        """Record the host list of every `_mop_up_bare_userinfo` call `redact` makes."""
        calls: list[list[str]] = []
        original = errors._mop_up_bare_userinfo

        def spy(text: str, hosts: Sequence[str]) -> str:
            calls.append(list(hosts))
            return original(text, hosts)

        monkeypatch.setattr(errors, '_mop_up_bare_userinfo', spy)
        return calls

    def test_the_mop_up_sees_every_host_in_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ⚠ This is the assertion the row is about. The mop-up used to be called once per host,
        # each call re-scanning the whole message; with the host count growing with the message
        # that is the quadratic. Deleting the alternation and restoring the loop fails here.
        calls = self.spy_on_the_mop_up(monkeypatch)
        redact(self.dsns(20))
        assert len(calls) == 1
        assert len(calls[0]) == 20

    def test_the_spelling_pass_sees_every_spelling_in_one_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        original = errors._mask_spellings

        def spy(text: str, spellings: Sequence[str]) -> str:
            calls.append(list(spellings))
            return original(text, spellings)

        monkeypatch.setattr(errors, '_mask_spellings', spy)
        redact(self.dsns(20), JWT)
        assert len(calls) == 1
        assert len(calls[0]) == 21  # 20 passwords + the key

    def test_the_mop_up_pattern_carries_every_host(self) -> None:
        pattern = errors._bare_userinfo_pattern(['h2.example.com', 'h1.example.com'])
        assert 'h1\\.example\\.com' in pattern.pattern
        assert 'h2\\.example\\.com' in pattern.pattern

    def test_repeated_hosts_collapse_to_one_alternative(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deduplication is what stops n DSNs to one host from costing n scans.
        calls = self.spy_on_the_mop_up(monkeypatch)
        redact(' '.join(f'postgresql://u{i}:pw{i}@one.example.com/x' for i in range(20)))
        assert calls == [['one.example.com']]

    def test_the_hosts_are_ordered_deterministically(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Longest first, ties lexical -- a set's iteration order varies with PYTHONHASHSEED, and a
        # printed string may not (Hard Rule #9).
        calls = self.spy_on_the_mop_up(monkeypatch)
        redact('https://a:p1@bbb.example.com/x https://b:p2@a.example.com/x https://c:p3@b.example.com/x')
        assert calls == [['bbb.example.com', 'a.example.com', 'b.example.com']]

    #: 8x the input. Linear is 8x the time, the worst measured shape here is 13x (the alternation
    #: still tries one branch per secret at a candidate position, which is not free), and quadratic
    #: is 64x -- measured at 64x, 63x and 62x on `main` @ d4455ae for the three shapes below. The
    #: bound sits between, with enough room for a loaded CI matrix runner: it is a ratio of two
    #: measurements on the same machine, so a slow machine slows both.
    GROWTH_BUDGET = 30

    @pytest.mark.parametrize(
        ('name', 'build'),
        [
            # The row's own shape: one password and one host per DSN, so the secret count grows
            # with the message.
            ('many dsns', lambda n: (TestRedactStaysLinear.dsns(n // 60) + ' ')[:n]),
            # ⚠ No secret in it at all. `_URL_USERINFO`'s greedy scheme run restarted from every
            # position of a base64 blob, so `redact` was quadratic on text it never masks -- which
            # is what a source adapter logging a document actually looks like.
            ('a base64 blob', lambda n: (BLOB * (n // len(BLOB) + 1))[:n]),
            # Both at once: the mop-up's own greedy run walk, over a message with no whitespace to
            # break it up.
            (
                'a blob in the same message as a dsn',
                lambda n: f'postgresql://u:hunter2pass@db.example.com/x {(BLOB * (n // len(BLOB) + 1))[:n]}',
            ),
            # ⚠ CI-144. `?` was a member of BOTH the lead class and the old name class, so the
            # greedy name walk ran once per `?` in a run -- 57.9 s at 160 000 characters. The `=`
            # must sit OUTSIDE the `?`-dense run: `_mask_secret_parameters`'s cheap `'=' not in
            # text` guard skips a text without one, and a run whose own terminator IS the `=`
            # matched on the first try and was never slow. Measured through `redact` at these
            # sizes: 64.2x on `main` @ aca5577 against a budget of 30 -- it failed, which is the
            # point -- and 6.1x with the scanner.
            (
                'a ?-dense run',
                lambda n: '?' * (n // 2) + 'a' * (n // 2) + ' ?jwt=hunter2pass',
            ),
            # ⚠ CI-150's two shapes. The fix stops skipping the value of a parameter it did NOT
            # mask, so every `?` inside such a value is now offered to the scan -- and a value
            # SWALLOW CHAIN is exactly where a naive re-scan would go quadratic. Computing the
            # value's end on the non-credential branch "for symmetry" makes the first of these
            # Theta(k * n). Measured through `redact` at these sizes: 7.6x and 8.1x.
            ('a value-swallow chain', lambda n: '?a=1' * (n // 4) + '?jwt=hunter2pass'),
            ('a ?-dense value', lambda n: '?x=' + '?a' * (n // 4) + '=hunter2pass'),
        ],
    )
    def test_the_cost_does_not_grow_quadratically(self, name: str, build: Callable[[int], str]) -> None:
        small, large = build(5_000), build(40_000)

        def cost(text: str) -> float:
            return min(timeit.repeat(lambda: redact(text, JWT), number=1, repeat=5))

        # The small measurement is floored: on a fast machine it is a few microseconds, where timer
        # noise alone would swamp the ratio and make the test flap. The floor cannot hide the defect
        # it guards -- on `main` the large measurement is 0.2-4 s, thousands of times the floor.
        growth = cost(large) / max(cost(small), 1e-4)
        assert growth < self.GROWTH_BUDGET, f'{name} grew {growth:.0f}x for 8x the input'


@pytest.mark.unit
class TestRedactQueryParamStaysLinear:
    """The parameter pass is a hand-written scan, and these are the structural reasons (CI-144)."""

    @pytest.mark.parametrize(
        'text',
        [
            '?' * 5_000,
            '&?#' * 2_000,
            '?' * 2_000 + '=',
            '?a=' + '?' * 2_000,
            '?a=1' * 1_000,
        ],
    )
    def test_it_terminates_on_a_lead_dense_string(self, text: str) -> None:
        # The scan advances by hand, so termination is an obligation rather than a gift from `re`.
        # Every branch must move the cursor strictly forward; one that does not hangs here rather
        # than anywhere a user could see.
        assert isinstance(redact(text), str)

    def test_the_scan_skips_a_whole_run_after_a_non_terminating_lead(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # ⚠ This is the assertion the quadratic is about, in the spirit of
        # `test_the_mop_up_sees_every_host_in_one_call`. 200 leads share one terminator; the scan
        # must consult it ONCE and jump the whole run, not once per lead. Restoring a per-lead
        # walk turns this into ~200 and the growth budget above into a 60x failure.
        calls: list[int] = []

        class CountingSearch:
            """A counting proxy for a compiled pattern -- `re.Pattern.search` is read-only."""

            def __init__(self, pattern: re.Pattern[str]) -> None:
                self._pattern = pattern

            def search(self, text: str, pos: int = 0) -> re.Match[str] | None:
                calls.append(pos)
                return self._pattern.search(text, pos)

        monkeypatch.setattr(errors, '_PARAM_NAME_END', CountingSearch(errors._PARAM_NAME_END))
        redact('?' * 200 + 'a' * 200 + ' ?jwt=hunter2pass')
        assert len(calls) <= 5, f'the scan consulted the terminator {len(calls)} times for 201 leads'

    @pytest.mark.parametrize(
        'text',
        [
            '?' * 100,
            'https://user:hunter2pass@h/x?apikey',
            'no parameters at all',
            '',
        ],
    )
    def test_the_equals_guard_is_a_fast_path_only(self, text: str) -> None:
        # `'=' not in text` returns early. It is an optimization, not a rule: every match the scan
        # can make contains a literal `=`, so deleting the guard changes no output. Recorded here
        # as the one mutant that must NOT be killed -- if this ever stops holding, the guard has
        # quietly become load-bearing and the proof of equivalence is gone with it.
        assert '=' not in text  # not vacuous: the guard is the branch under test
        assert errors._mask_secret_parameters(text) == text

    def test_an_unmasked_value_is_echoed_byte_for_byte(self) -> None:
        # Guards the `emitted`/`pos` bookkeeping: a text with no credential parameter in it must
        # come back out of the scan unchanged, character for character.
        text = 'https://x.supabase.co/rest/v1/table?select=*&order=id&limit=10#anchor=1'
        assert errors._mask_secret_parameters(text) == text
        assert redact(text) == text


@pytest.mark.unit
class TestRedactQueryParamAwkwardShapes:
    """The equivalence checklist the scanner has to survive (CI-144 §10.3).

    Exact expected output, in the discipline of `REDACTABLE_SHAPES`, so a future change to this
    pass fails here rather than being silently re-baselined. The shapes that `main` *leaked* are
    deliberately not in this table: they are pinned in their fixed form by
    `TestRedactQueryParamUnderMasking`, and pinning a leak as expected behaviour in the same PR
    that closes it would be a test asserting the bug.
    """

    @pytest.mark.parametrize(
        ('text', 'expected'),
        [
            ('?apikey=hunter2pass', '?apikey=***'),
            ('abc?token=hunter2pass', 'abc?token=***'),
            ('?x=1&token=hunter2pass', '?x=1&token=***'),
            ('#access_token=hunter2pass', '#access_token=***'),
            ('?apikey=YWJjZGVmZ2hpamtsbW5vcA==', '?apikey=***'),
            # An empty value is still masked: the mask is positional, keyed on the name.
            ('?apikey=', '?apikey=***'),
            # An empty NAME is not a credential, so `?=` is echoed back untouched.
            ('?=', '?='),
            ('?select=*&order=id', '?select=*&order=id'),
            ('?', '?'),
            ('???', '???'),
            ('no parameters at all', 'no parameters at all'),
            # ⚠ The L5 shapes. A credential word BEFORE an inner `?` still owns the name, and any
            # fix that lets the LAST `?` own it instead unmasks these -- which is masking less on a
            # published security boundary. `_PARAM_NAME_END` must never gain a `?`.
            ('?jwt.x?y=hunter2pass', '?jwt.x?y=***'),
            ('?token.a?y=hunter2pass', '?token.a?y=***'),
            # The value class must never gain a `?` either: it would shorten the value on the
            # branch that MASKS it and print everything after the `?` (see
            # `TestRedactQueryParamHalfPrint`).
            ('?token=ab?cd', '?token=***'),
            ('?service%5Frole%5Fkey=hunter2pass', '?service%5Frole%5Fkey=***'),
            ('https://x.supabase.co/rest/v1/table?key=eq.5', 'https://x.supabase.co/rest/v1/table?key=***'),
            ('https://x.supabase.co/rest/v1/table?monkey=eq.5', 'https://x.supabase.co/rest/v1/table?monkey=eq.5'),
        ],
    )
    def test_the_shape_is_masked_exactly_as_it_was(self, text: str, expected: str) -> None:
        assert redact(text) == expected


#: The leaks CI-150 closes: `(text, expected)`. Every one printed its credential **in full** in
#: `0.5.0`, on a path a one-character typo in `--from` reaches (`cli/pipeline.py:202` echoes the
#: value back in the "neither a URL nor an existing file" `UsageError`).
UNDER_MASKED_SHAPES: tuple[tuple[str, str], ...] = (
    # (a) `?` inside a NAME. The old single-pattern name class admitted `?`, so `a?token` was one name
    # and `token` had no `_SECRET_PARAM_NAME` delimiter in front of it -- `?` is not `^`, not
    # `[-_.]`, and not the camelCase hump.
    ('?a?token=hunter2pass', '?a?token=***'),
    ('?x?apikey=hunter2pass', '?x?apikey=***'),
    ('x?y?jwt=hunter2pass', 'x?y?jwt=***'),
    # ⚠ Listed by neither the WORKPLAN row nor CI-144's table: the TRAILING lookahead
    # `(?=$|[-_.]|[A-Z])` fails on `?` too. It is the shape that discriminates a segment split
    # from a fix that only looks at what precedes the word.
    ('?token?x=hunter2pass', '?token?x=***'),
    # The split happens AFTER `unquote`, so an encoded `?` is caught as well.
    ('?a%3Ftoken=hunter2pass', '?a%3Ftoken=***'),
    # (b) `?` inside a VALUE. `?` terminates nothing, so the value of `a` was
    # `1?token=hunter2pass` -- the whole credential -- and `re.sub` matches do not overlap, so the
    # second parameter was never even offered to the mask function.
    ('?a=1?token=hunter2pass', '?a=1?token=***'),
    (
        'https://x.supabase.co/rest/v1/?select=*?apikey=hunter2pass',
        'https://x.supabase.co/rest/v1/?select=*?apikey=***',
    ),
    (
        '?ghp_y-https://x.supabase.co/rest/v1/?jwt=hunter2pass',
        '?ghp_y-https://x.supabase.co/rest/v1/?jwt=***',
    ),
)


@pytest.mark.unit
class TestRedactQueryParamUnderMasking:
    """The credential leaks CI-150 closes -- `?` leads a parameter and terminates nothing.

    ⚠ Every shape here printed a secret **in full** in the published `0.5.0`, and
    `docs/reference/cli.md` promises the opposite ("Secrets are masked in every string the CLI
    prints, at every verbosity"). Nothing in this table may go back to echoing its value.
    """

    @pytest.mark.parametrize(('text', 'expected'), UNDER_MASKED_SHAPES)
    def test_the_credential_is_masked(self, text: str, expected: str) -> None:
        assert redact(text) == expected
        assert 'hunter2pass' not in redact(text)

    @pytest.mark.parametrize(('text', 'expected'), UNDER_MASKED_SHAPES)
    def test_it_is_masked_on_the_from_echo_too(self, text: str, expected: str) -> None:
        # `redact_source` is the surface that echoes a bad `--from` back, and it is where a typo'd
        # second `?` actually reaches a terminal. None of these shapes has an `@`, so
        # `redact_source`'s extra userinfo rule is a no-op and the ordinary rules must carry it.
        assert redact_source(text) == expected
        assert 'hunter2pass' not in redact_source(text)

    def test_the_name_is_split_after_decoding_not_before(self) -> None:
        # Splitting the RAW name on `?` would leave `a%3Ftoken` whole and miss it; splitting the
        # DECODED name finds `a` and `token`. The name is still echoed back exactly as written.
        assert redact('?a%3Ftoken=hunter2pass') == '?a%3Ftoken=***'


@pytest.mark.unit
class TestRedactQueryParamHalfPrint:
    """The leak CI-150 must NOT introduce: a masked value containing a `?` is masked WHOLE.

    ⚠ The obvious fix for the value-swallow -- putting `?` in the value-terminator class -- closes
    the leak and opens another, because it shortens the value on the branch that **masks** it too.
    The fix that ships computes the value's end only when it is about to mask it, and resumes at
    `stop + 1` otherwise, so there is no half-print anywhere. Each row below is a shape the naive
    spelling breaks; `?` is a legal query character (RFC 3986) and a libpq password has no alphabet
    restriction, so these are reachable, not theoretical.
    """

    @pytest.mark.parametrize(
        ('text', 'expected', 'ruled_out'),
        [
            ('?token=ab?cd', '?token=***', 'the naive fix prints `?token=***?cd`'),
            ('?apikey=a?b?c', '?apikey=***', 'the naive fix prints `?apikey=***?b?c`'),
            ('?password=p?w', '?password=***', 'a hand-pasted libpq DSN: `?password=***?w`'),
            ('?token=ab?c=d', '?token=***', 'a tempered-greedy value still prints `?token=***?c=d`'),
            ('?apikey=a?jwt=b', '?apikey=***', 'resuming at `stop + 1` on the MASKED branch splits it in two'),
            ('?apikey=YWJj?ZGVm', '?apikey=***', 'base64url has no `?`, but a PostgREST filter value may'),
        ],
    )
    def test_a_masked_value_is_masked_whole(self, text: str, expected: str, ruled_out: str) -> None:
        masked = redact(text)
        assert masked == expected, ruled_out
        # `***?` is the half-print's signature: every naive spelling truncates the value at its
        # `?` and prints the remainder immediately after the mask. One `***`, nothing behind it.
        assert f'{REDACTED}?' not in masked
        assert masked.count(REDACTED) == 1


@pytest.mark.unit
class TestRedactUrlUserinfoSwallow:
    """⚠ A leak that is STILL OPEN, pinned so the row that closes it can prove the fix.

    `_URL_USERINFO`'s host class `[^/?#\\s]*` happily consumes the `scheme:` of a *following* URL,
    and `re.sub` matches do not overlap, so the second URL's password prints. This is the hazard
    `_URL_USERINFO`'s own docstring records ("over-masking in one place under-masks in another"),
    now shown reachable without the letter-free-run trick that docstring describes.

    **Deliberately NOT fixed by CI-150.** It is a different function, a different trigger character
    (no `?` is involved at all) and a different fix, and it is filed as its own row, **CI-151**.
    CI-144 §12 misclassified it as a sub-case of the `?` family; it is not one. When CI-151 lands,
    these two assertions flip in that PR and the flip is the evidence.
    """

    @pytest.mark.parametrize(
        'text',
        [
            'https://a@b,https://user:hunter2pass@h',
            'postgresql://@x.https://user:hunter2pass@h',
        ],
    )
    def test_the_second_urls_password_still_prints(self, text: str) -> None:
        # ⚠ Asserting a leak. An unpinned leak is a leak nobody can prove was fixed -- and pinning
        # it here is what stops CI-150 from being credited with closing it. CI-151 owns this.
        masked = redact(text)
        assert 'hunter2pass' in masked, 'CI-151 has landed -- flip this assertion in that PR'
        assert masked.count(REDACTED) == 1


@pytest.mark.unit
class TestRedactMopUpVeto:
    """What the URL pass has already masked is skipped per occurrence, not per run (CI-073)."""

    def test_an_already_masked_url_keeps_its_scheme_and_username(self) -> None:
        # The reason the veto exists at all: `https://user:***@host` is more diagnostic than
        # `***@host`, and the username is not conventionally secret.
        assert redact('Could not reach https://user:hunter2pass@x.supabase.co/x') == (
            'Could not reach https://user:***@x.supabase.co/x'
        )

    def test_it_no_longer_vetoes_a_bare_secret_earlier_in_the_same_run(self) -> None:
        # ⚠ A leak on `main` @ d4455ae, found by the CI-073 differential rather than by review.
        # The veto was a test on the whole match, and the run is greedy: with no whitespace between
        # them, the already-masked URL at the end of the run vetoed masking the bare
        # `<password>@host` in front of it, and `main` prints `hunter2pass@h.example.com` here.
        text = 'hunter2pass@h.example.com,https://u:hunter2pass@h.example.com/x'
        masked = redact(text)
        assert masked == '***@h.example.com,https://u:***@h.example.com/x'
        assert 'hunter2pass' not in masked

    def test_a_host_quoted_at_its_end_keeps_the_fast_path(self) -> None:
        # The common shape: a message that quotes a URL puts the closing quote inside the host
        # group. It is a *trailing* delimiter, so the run anchor is still exact.
        assert errors._bare_userinfo_pattern(["x.supabase.co'"]).pattern.startswith('(?<!')
        assert redact("Could not reach 'https://user:hunter2pass@x.supabase.co' -- boom") == (
            "Could not reach 'https://user:***@x.supabase.co' -- boom"
        )

    def test_a_host_quoted_in_its_middle_drops_the_anchor_rather_than_masking_less(self) -> None:
        # ⚠ The one input where the fast path would diverge: `re.sub` resumes mid-run after a match
        # that ended mid-run, and only an interior quote in the host can do that. Speed gives way.
        assert not errors._bare_userinfo_pattern(["ho'st"]).pattern.startswith('(?<!')
        masked = redact("https://u:hunter2pass@ho'st/x SECRETONE@ho'st SECRETTWO@ho'st")
        assert 'SECRETONE' not in masked
        assert 'SECRETTWO' not in masked


@pytest.mark.unit
class TestRedactSchemeMatching:
    """The `scheme://` anchor still matches exactly what it matched (CI-073)."""

    def test_a_scheme_run_starting_with_a_digit_is_still_masked(self) -> None:
        # The lookbehind anchors the match at the start of the scheme run, so the first character
        # can no longer be required to be a letter. This is the input that proves the requirement
        # had to move into a lookahead rather than be dropped.
        assert redact('see 123https://user:hunter2pass@x.supabase.co/x') == ('see 123https://user:***@x.supabase.co/x')

    def test_a_letter_free_scheme_run_still_does_not_match(self) -> None:
        # ⚠ Not a tightening for its own sake. Letting `-://` match makes the match swallow the
        # real `https://` URL that follows it inside the same run -- `re.sub` matches do not
        # overlap -- and the password of the real URL then prints. Measured, not reasoned.
        masked = redact('-://localhostuser@z.https://user:hunter2pass@h/x')
        assert 'hunter2pass' not in masked

    def test_the_scheme_is_echoed_back_exactly_as_it_was_written(self) -> None:
        assert redact('POSTGRESQL://u:hunter2pass@h/x') == 'POSTGRESQL://u:***@h/x'


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

    @pytest.mark.parametrize(
        'source',
        [
            f'https://user:{PASSWORD}@[::1',  # urlsplit: ValueError('Invalid IPv6 URL')
            f'https://u:{PASSWORD}@h:notaport/',
            # U+FF20 NFKC-normalizes to `@`, which is what urlsplit's _checknetloc rejects --
            # and its ValueError quotes the WHOLE netloc back, password included.
            f'https://user:{PASSWORD}@ex＠ample.com/rest/v1/',
            'https://[',
            '://@',
            '@',
            '',
        ],
    )
    def test_a_malformed_url_is_not_a_crash(self, source: str) -> None:
        # ⚠ Round 3. This callback runs inside click's `make_context`, OUTSIDE
        # `cli_error_handling`, so anything it raises but click does not understand escapes the
        # error boundary: a raw, unredacted traceback at exit 1 instead of a redacted message at
        # 70. Using `urlsplit` here to read the scheme reintroduced exactly the failure mode
        # CI-066-D1 rejected it for -- it raises on the malformed URLs this row exists to defend.
        # Either outcome is fine (refuse, or pass through); crashing is not, and neither is
        # echoing the value.
        try:
            assert reject_url_userinfo(source) == source
        except click.UsageError as exc:
            assert PASSWORD not in exc.message

    @pytest.mark.parametrize('scheme', ['https', 'HTTPS', 'Http'])
    def test_the_scheme_match_is_case_insensitive(self, scheme: str) -> None:
        # Splitting the scheme by hand instead of with urlsplit must not lose urlsplit's
        # case-folding: RFC 3986 schemes are case-insensitive.
        with pytest.raises(click.UsageError):
            reject_url_userinfo(f'{scheme}://user:{PASSWORD}@x.supabase.co')


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

    @pytest.mark.parametrize(
        'source',
        [
            f'nosuchfile-{JWT}.json',
            f'nope.json?bearerthing={JWT}',  # a parameter name OUTSIDE the credential-word list
            f'{JWT}',
            f'./dir/{JWT}/openapi.json',
        ],
    )
    def test_the_api_key_is_still_masked(self, source: str) -> None:
        # ⚠ Round 3. `redact_source` wraps `redact`, and it shipped calling `redact(source)` with
        # no key -- so this function, introduced to CLOSE a leak on this surface, opened a
        # different one on the same surface. Every CI-068 test ran without a key, so nothing
        # caught it; the suite actively pinned the key-dropping behaviour. CI6-D7 has no
        # exceptions, and spec §11.2 singles this surface out as needing key coverage.
        assert JWT in source  # not vacuous
        assert JWT not in redact_source(source, JWT)

    def test_without_a_key_nothing_changes(self) -> None:
        # The default is for callers with no key in play; it must not mask anything by itself.
        assert redact_source('nosuchfile.json') == 'nosuchfile.json'

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
