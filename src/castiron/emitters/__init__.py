"""castiron's emitter layer: the ``Emitter`` abstraction and its concrete emitters.

Public API (the surface CI-006's CLI calls):
``PydanticEmitter(EmitterConfig(...)).emit(schema) -> list[EmittedFile]``.
"""

from castiron.emitters.base import EmittedFile, Emitter
from castiron.emitters.config import EmitterConfig
from castiron.emitters.pydantic import PydanticEmitter

__all__ = ['EmittedFile', 'Emitter', 'EmitterConfig', 'PydanticEmitter']
