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
"""

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager

import click

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


@contextmanager
def cli_error_handling(*, debug: bool, key: str | None) -> Iterator[None]:
    """Map castiron failures onto exit codes, redacting secrets from every message.

    ``ClickException`` subclasses (:class:`~castiron.cli.config.ConfigError`,
    :class:`~castiron.cli.output.OutputError`, ``UsageError``) and ``Abort`` are already
    exit-coded and are re-raised untouched; a :class:`~castiron.sources.SourceError`
    becomes a redacted ``ClickException`` (exit 1); anything else is a castiron bug and
    exits :data:`EXIT_INTERNAL`, showing the traceback only under ``--debug``.

    Args:
        debug: Re-raise unexpected exceptions so Python prints the traceback.
        key: The API key in play, redacted out of any message.

    Yields:
        ``None`` — the block runs inside the boundary.
    """
    try:
        yield
    except (click.ClickException, click.Abort):
        raise
    except SourceError as exc:
        raise click.ClickException(redact(str(exc), key)) from exc
    except Exception as exc:
        click.echo(redact(internal_error_message(exc), key), err=True)
        if debug:
            raise
        sys.exit(EXIT_INTERNAL)
