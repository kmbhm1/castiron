"""The option surface, pinned.

``docs/reference/cli.md`` renders the command tree with ``mkdocs-click``, and click lists options
in **decorator order**. So the moment ``gen``'s options were extracted into shared stacks
(CI-021b), the order of those stacks became the order of a published reference page — and a
cosmetic reshuffle would silently rewrite it.

These literal lists are the guard. ``GEN_PARAMS`` is copied from ``origin/main`` *before* the
extraction, so it asserts the refactor was a pure rename; ``CHECK_PARAMS`` is derived from it by
subtraction, so "check is gen minus the write" is a claim the test can check rather than one a
reviewer has to believe.

⚠ **If one of these fails, updating the literal is the wrong first move.** A changed list means the
published CLI reference changed; that is a deliberate decision, not a green test.
"""

import pytest

from castiron.cli.check import check
from castiron.cli.gen import gen

#: ``[p.name for p in gen.params]`` exactly as it read on ``origin/main`` @ 906fb28, before the
#: option surface moved into ``castiron.cli.options``.
GEN_PARAMS = [
    'config_path',
    'source',
    'key',
    'emit',
    'output',
    'filename',
    'schema',
    'timeout',
    'overwrite',
    'dry_run',
    'infer_generated_primary_keys',
    'crud_models',
    'enums',
    'foreign_keys',
    'null_parent_classes',
    'singular_names',
    'model_prefix_protection',
    'verbose',
    'quiet',
    'debug',
]

#: The two write-path-only options ``check`` does not take. ``check`` never writes, so
#: ``--overwrite`` has nothing to overwrite and ``--dry-run`` is what the whole command already is.
WRITE_ONLY_PARAMS = ['overwrite', 'dry_run']

#: Derived, not transcribed: a hand-written second list could drift from the first, which is the
#: exact failure the extraction exists to prevent.
CHECK_PARAMS = [name for name in GEN_PARAMS if name not in WRITE_ONLY_PARAMS]


@pytest.mark.unit
class TestTheOptionSurfaceIsPinned:
    def test_gen_takes_exactly_the_options_it_took_before_the_extraction(self) -> None:
        assert [parameter.name for parameter in gen.params] == GEN_PARAMS, (
            'gen`s option list changed. mkdocs-click renders it into docs/reference/cli.md, so '
            'this is a documentation change, not a refactor -- decide it deliberately.'
        )

    def test_check_takes_gens_options_minus_the_two_that_only_make_sense_when_writing(self) -> None:
        assert [parameter.name for parameter in check.params] == CHECK_PARAMS

    def test_the_two_commands_differ_by_exactly_the_write_options(self) -> None:
        # Set difference as well as list equality: the lists above could both be wrong in the same
        # way, but this states the RELATIONSHIP rather than two independent transcriptions.
        gen_names = {parameter.name for parameter in gen.params}
        check_names = {parameter.name for parameter in check.params}
        assert gen_names - check_names == set(WRITE_ONLY_PARAMS)
        assert check_names - gen_names == set()

    def test_gen_takes_twenty_options_and_check_takes_eighteen(self) -> None:
        assert (len(gen.params), len(check.params)) == (20, 18)


@pytest.mark.unit
class TestTheSharedOptionsAreTheSameObjectsInBothCommands:
    def test_every_shared_option_declares_the_same_help_text_in_both_commands(self) -> None:
        # The point of extracting the stacks: one declaration, so `--from`'s help cannot say one
        # thing under `gen` and another under `check`.
        gen_help = {parameter.name: getattr(parameter, 'help', None) for parameter in gen.params}
        for parameter in check.params:
            assert getattr(parameter, 'help', None) == gen_help[parameter.name], parameter.name

    def test_every_shared_option_declares_the_same_default_in_both_commands(self) -> None:
        gen_defaults = {parameter.name: parameter.default for parameter in gen.params}
        for parameter in check.params:
            assert parameter.default == gen_defaults[parameter.name], parameter.name

    def test_every_shared_option_declares_the_same_flags_in_both_commands(self) -> None:
        gen_opts = {parameter.name: (parameter.opts, parameter.secondary_opts) for parameter in gen.params}
        for parameter in check.params:
            assert (parameter.opts, parameter.secondary_opts) == gen_opts[parameter.name], parameter.name
