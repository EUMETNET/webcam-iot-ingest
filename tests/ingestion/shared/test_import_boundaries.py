"""Guard provider-neutral ingestion code against provider-owned imports."""

from __future__ import annotations

import ast
from pathlib import Path


INGESTION_ROOT = Path(__file__).parents[3] / "ingestion"


def test_shared_ingestion_does_not_import_provider_packages() -> None:
    provider_prefixes = (
        "ingestion.windy",
        "ingestion.fintraffic",
        "ingestion.skaping",
    )

    for path in (INGESTION_ROOT / "shared").glob("*.py"):
        imported = _imported_modules(path)
        assert not tuple(
            module
            for module in imported
            if module.startswith(provider_prefixes)
        ), path


def test_fintraffic_and_skaping_do_not_import_windy() -> None:
    for provider in ("fintraffic", "skaping"):
        for path in (INGESTION_ROOT / provider).glob("*.py"):
            imported = _imported_modules(path)
            assert not tuple(
                module for module in imported if module.startswith("ingestion.windy")
            ), path


def _imported_modules(path: Path) -> tuple[str, ...]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    return tuple(modules)
