"""The emitter registry — one name → one emitter, shared by the CLI and ``check``.

It lives in :mod:`castiron.emitters` rather than in the CLI because CI-021's ``check``
mode and any programmatic caller need the same lookup. Registering a new emitter
(CI-012's SQLAlchemy, CI-030/031's client) is a single :data:`EMITTERS` entry — no CLI
edit — and ``click.Choice(sorted(EMITTERS))`` derives ``--emit``'s validation, its help
text and its error message from that one dict.
"""

from collections.abc import Callable
from dataclasses import dataclass

from castiron.emitters.base import Emitter
from castiron.emitters.config import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter


@dataclass(frozen=True)
class EmitterSpec:
    """One registered emitter: its CLI name, default output file, and factory.

    Attributes:
        name: The ``--emit`` value that selects this emitter.
        default_filename: The file name used when the caller does not override it.
        build: Constructs the emitter from an :class:`~castiron.emitters.EmitterConfig`.
    """

    name: str
    default_filename: str
    build: Callable[[EmitterConfig], Emitter]


#: Every emitter castiron can run, keyed by its ``--emit`` name.
EMITTERS: dict[str, EmitterSpec] = {
    'pydantic': EmitterSpec('pydantic', 'schema.py', PydanticEmitter),
}


def get_emitter_spec(name: str) -> EmitterSpec:
    """Return the registered emitter named ``name``.

    Args:
        name: The ``--emit`` name of the emitter.

    Returns:
        The registered :class:`EmitterSpec`.

    Raises:
        KeyError: No emitter is registered under that name.
    """
    return EMITTERS[name]
