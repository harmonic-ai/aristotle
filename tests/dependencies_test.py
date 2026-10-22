"""The package declares every distribution it imports, so a fresh install has all of them."""

import ast
import importlib.metadata
import sys
from collections.abc import Iterator
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import aristotlelib


def _requirements() -> list[Requirement]:
    """The package's run-time requirements, on any Python; the dev extra's aren't among them."""
    return [Requirement(r) for r in importlib.metadata.requires("aristotlelib") or [] if "extra ==" not in r]


def _declared() -> set[str]:
    """The distributions the package itself requires on this Python, by canonical name."""
    return {canonicalize_name(r.name) for r in _requirements() if r.marker is None or r.marker.evaluate()}


def _imports(tree: ast.AST) -> Iterator[str]:
    """The top-level modules ``tree`` imports, in every branch."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module.split(".")[0]


def _imported() -> set[str]:
    """The top-level modules outside this Python's standard library that the package's own code imports."""
    modules = {
        module
        for path in Path(aristotlelib.__file__).parent.rglob("*.py")
        for module in _imports(ast.parse(path.read_text(encoding="utf-8")))
    }
    return {module for module in modules if module not in sys.stdlib_module_names and module != "aristotlelib"}


def test_every_module_the_package_imports_comes_from_a_declared_dependency():
    # A module installed only as another package's dependency, as certifi was through httpx, is gone once that changes.
    distributions = importlib.metadata.packages_distributions()
    declared = _declared()

    undeclared = {
        module
        for module in _imported()
        if not declared & {canonicalize_name(name) for name in distributions.get(module, [module])}
    }

    assert undeclared == set()


def test_httpx_is_held_below_1():
    # httpx 1.0 is a rewrite, without AsyncClient, Timeout or the exception classes the client uses.
    [httpx] = [r for r in _requirements() if r.name == "httpx"]

    assert not httpx.specifier.contains("1.0.dev6", prereleases=True)
    assert not httpx.specifier.contains("1.0", prereleases=True)


def test_tomli_is_not_required():
    # So a project can install any tomli alongside it, on any Python.
    assert [r for r in _requirements() if r.name == "tomli"] == []
