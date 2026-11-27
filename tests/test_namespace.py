"""The shared lupaxa namespace must stay a PEP 420 namespace."""

from __future__ import annotations

import importlib.machinery
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"


def test_lupaxa_portion_has_no_init() -> None:
    """The checkout must not own the shared lupaxa namespace."""
    assert not (_SRC / "lupaxa" / "__init__.py").exists()


def test_lupaxa_portion_is_a_namespace() -> None:
    """Finding lupaxa from this src tree must not bind a regular package loader."""
    spec = importlib.machinery.PathFinder.find_spec("lupaxa", [str(_SRC)])
    assert spec is not None
    assert spec.loader is None
