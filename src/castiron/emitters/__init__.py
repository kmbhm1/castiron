"""castiron's emitter layer: the ``Emitter`` abstraction and its concrete emitters.

Public API (the surface CI-006's CLI calls):
``PydanticEmitter(EmitterConfig(...)).emit(schema) -> list[EmittedFile]``, with
:data:`~castiron.emitters.registry.EMITTERS` mapping a ``--emit`` name onto the emitter
that serves it.
"""

from castiron.emitters.base import EmittedFile, Emitter
from castiron.emitters.config import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter
from castiron.emitters.registry import EMITTERS, EmitterSpec, get_emitter_spec

__all__ = [
    'EMITTERS',
    'EmittedFile',
    'Emitter',
    'EmitterConfig',
    'EmitterSpec',
    'PydanticEmitter',
    'get_emitter_spec',
]
