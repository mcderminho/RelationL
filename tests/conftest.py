from __future__ import annotations

from pathlib import Path

import pytest

from relationl.config import Config, Source
from relationl.extract.notebook import NotebookAnalyzer
from relationl.extract.python import PythonAnalyzer
from relationl.extract.sql import SqlAnalyzer
from relationl.naming import TableNormaliser

FIXTURES = Path(__file__).parent / "fixtures"


def make_source(**overrides) -> Source:
    defaults = {"name": "test", "roots": (FIXTURES,), "sql_dialect": "spark"}
    defaults.update(overrides)
    return Source(**defaults)


@pytest.fixture
def normaliser() -> TableNormaliser:
    return TableNormaliser(make_source())


@pytest.fixture
def sql(normaliser) -> SqlAnalyzer:
    return SqlAnalyzer(normaliser, dialect="spark")


@pytest.fixture
def pyspark(normaliser) -> PythonAnalyzer:
    return PythonAnalyzer(normaliser, dialect="spark")


@pytest.fixture
def notebook(normaliser) -> NotebookAnalyzer:
    return NotebookAnalyzer(normaliser, dialect="spark")


@pytest.fixture
def scanned(tmp_path) -> tuple[Config, object]:
    """A completed scan of the fixture corpus, in a throwaway database."""
    from relationl.scanner import scan

    config = Config.from_dict(
        {
            "database": str(tmp_path / "graph.db"),
            "sources": [
                {"name": "fixtures", "roots": [str(FIXTURES)], "sql_dialect": "spark"}
            ],
        },
        base_dir=tmp_path,
    )
    stats = scan(config, workers=1)
    return config, stats


def edge_map(joins) -> dict[tuple[str, str], list]:
    """Index joins by their (left, right) table pair."""
    out: dict[tuple[str, str], list] = {}
    for join in joins:
        out.setdefault((join.left.key, join.right.key), []).append(join)
    return out


def conditions_for(joins, left: str, right: str) -> set[str]:
    """Every distinct condition recorded for one table pair."""
    pair = tuple(sorted((left, right)))
    return {
        j.condition
        for j in joins
        if tuple(sorted((j.left.key, j.right.key))) == pair
    }
