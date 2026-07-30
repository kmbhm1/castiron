"""Framework-neutral emitter configuration.

supabase-pydantic's "FastAPI" label carried no real coupling (it only picked
class-naming conventions), so it is dropped: every behavioral toggle lives on this one
:class:`EmitterConfig`. Defaults reproduce supabase-pydantic's default output shape
(now including nested FK relationship fields -- CI4-D-scope). CLI wiring of these lands
in CI-006.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class EmitterConfig:
    """Behavioral toggles for an emitter.

    Attributes:
        generate_crud_models: Emit ``Insert``/``Update`` models alongside the base Row model.
        generate_enums: Emit ``Enum`` classes for enum columns; else fall back to ``str``.
        add_null_parent_classes: Emit an all-nullable parent class per table for inheritance.
        disable_model_prefix_protection: Emit ``model_config = ConfigDict(protected_namespaces=())``
            on classes that carry ``model_``-prefixed columns.
        singular_names: Singularize generated class names.
        include_foreign_keys: Emit nested foreign-key relationship fields on operational classes.
        output_filename: The :attr:`castiron.emitters.EmittedFile.path` of the single emitted file.
    """

    generate_crud_models: bool = True
    generate_enums: bool = True
    add_null_parent_classes: bool = False
    disable_model_prefix_protection: bool = False
    singular_names: bool = False
    include_foreign_keys: bool = True
    output_filename: str = 'schema.py'
