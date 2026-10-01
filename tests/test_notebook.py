"""Notebook handling: magics, cell stitching and malformed documents."""

from __future__ import annotations

import json

from conftest import FIXTURES, edge_map


def notebook_json(*cells: str) -> str:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "source": source.splitlines(keepends=True),
                    "outputs": [],
                    "execution_count": None,
                    "metadata": {},
                }
                for source in cells
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def test_lineage_crosses_cell_boundaries(notebook):
    document = notebook_json(
        'orders = spark.table("sales.orders")\n',
        'customers = spark.table("crm.customers")\n',
        "orders.join(customers, orders.cid == customers.id)\n",
    )
    joins, _, _, errors = notebook.analyze(document)
    assert errors == []
    assert len(joins) == 1
    assert (joins[0].left.key, joins[0].right.key) == ("crm.customers", "sales.orders")


def test_sql_cell_magic_is_parsed(notebook):
    document = notebook_json(
        "%%sql\nSELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k\n",
    )
    joins, _, _, _ = notebook.analyze(document)
    assert joins[0].condition == "a.t1.k = b.t2.k"


def test_sql_line_magic_is_parsed(notebook):
    document = notebook_json("%sql SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k\n")
    joins, _, _, _ = notebook.analyze(document)
    assert joins[0].condition == "a.t1.k = b.t2.k"


def test_other_magics_do_not_break_parsing(notebook):
    document = notebook_json(
        "%matplotlib inline\n"
        "!pip install pandas\n"
        '%load_ext autoreload\n'
        'orders = spark.table("sales.orders")\n'
        'customers = spark.table("crm.customers")\n'
        "orders.join(customers, orders.cid == customers.id)\n"
    )
    joins, _, _, errors = notebook.analyze(document)
    assert errors == []
    assert len(joins) == 1


def test_markdown_cells_are_ignored(notebook):
    document = json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "source": ["# JOIN a.t1 ON nonsense\n"], "metadata": {}},
                {
                    "cell_type": "code",
                    "source": ['spark.sql("SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k")'],
                    "outputs": [],
                    "execution_count": None,
                    "metadata": {},
                },
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )
    joins, _, _, _ = notebook.analyze(document)
    assert len(joins) == 1


def test_invalid_json_is_reported_not_raised(notebook):
    joins, tables, _, errors = notebook.analyze("{not json at all")
    assert joins == []
    assert tables == set()
    assert errors and "invalid notebook JSON" in errors[0]


def test_empty_notebook(notebook):
    joins, tables, _, errors = notebook.analyze(json.dumps({"cells": []}))
    assert joins == []
    assert errors == []


def test_fixture_notebook(notebook):
    document = (FIXTURES / "etl" / "region_analysis.ipynb").read_text(encoding="utf-8")
    joins, tables, _, errors = notebook.analyze(document)
    assert errors == []
    pairs = set(edge_map(joins))
    # From the PySpark cell.
    assert ("warehouse.dim_region", "warehouse.dim_store") in pairs
    # From the %%sql cell.
    assert ("warehouse.dim_customer", "warehouse.dim_region") in pairs
    # From the path read plus a USING join.
    assert ("staging.raw_events", "warehouse.dim_store") in pairs


def test_line_numbers_are_notebook_relative(notebook):
    document = notebook_json(
        'orders = spark.table("sales.orders")\n',
        'customers = spark.table("crm.customers")\n',
        "\n\norders.join(customers, orders.cid == customers.id)\n",
    )
    joins, _, _, _ = notebook.analyze(document)
    # Cell 1 is line 1, cell 2 is line 2, then two blank lines in cell 3.
    assert joins[0].line == 5
