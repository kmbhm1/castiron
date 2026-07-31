"""Command-line entrypoint for castiron.

``cli.py`` became this package in CI-006 (the command surface, the project config file, the
write path, the error boundary and the fidelity notices are separate concerns, and CI-021
adds ``check.py`` beside ``gen.py``). The re-export below keeps
``[project.scripts] castiron = "castiron.cli:cli"`` and ``from castiron.cli import cli``
resolving exactly as before.

This module holds **no logic** on purpose: ``[tool.coverage.run] omit = ["*/__init__.py"]``
would hide it from the coverage gate.
"""

from castiron.cli.main import cli

__all__ = ['cli']
