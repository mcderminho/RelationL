"""Read Jupyter notebooks as Python, with SQL magics routed to the SQL parser.

Code cells are concatenated into one module so DataFrame lineage carries across
cell boundaries -- which is how notebooks are actually written.  A line map is
kept alongside so reported positions refer to the notebook's own logical line
numbering (code cells only, counted in order) rather than the JSON file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

from ..models import ColumnRef, Join, TableRef
from ..naming import TableNormaliser
from .python import PythonAnalyzer
from .sql import SqlAnalyzer

#: ``%%sql`` / ``%%spark_sql`` and friends: the whole cell is SQL.
_CELL_MAGIC = re.compile(r"^\s*%%\s*(sql|sparksql|spark_sql|bigquery|athena|trino)\b", re.I)
#: ``%sql SELECT ...``: the rest of the line is SQL.
_LINE_MAGIC = re.compile(r"^\s*%\s*(sql|sparksql|spark_sql)\b(.*)$", re.I)
#: Any other magic or shell escape, which is not valid Python.
_OTHER_MAGIC = re.compile(r"^\s*[%!]")
#: Databricks notebooks exported as .py use this marker between cells.
_DBX_CELL = re.compile(r"^#\s*COMMAND\s*-+", re.I)


class NotebookAnalyzer:
    """Extract joins from a ``.ipynb`` document."""

    def __init__(
        self,
        normaliser: TableNormaliser,
        *,
        dialect: str | None = None,
        language: str = "notebook",
    ) -> None:
        self.normaliser = normaliser
        self.language = language
        self.python = PythonAnalyzer(normaliser, dialect=dialect, language=language)
        self.sql = SqlAnalyzer(normaliser, dialect=dialect, language=language)

    def analyze(
        self, text: str
    ) -> tuple[list[Join], set[TableRef], set[ColumnRef], list[str]]:
        try:
            document = json.loads(text)
        except (ValueError, UnicodeDecodeError) as err:
            return [], set(), set(), ["invalid notebook JSON: %s" % err]
        if not isinstance(document, dict):
            return [], set(), set(), ["notebook root is not an object"]

        joins: list[Join] = []
        tables: set[TableRef] = set()
        columns: set[ColumnRef] = set()
        errors: list[str] = []

        python_lines: list[str] = []
        line_map: list[int] = []
        logical = 0

        for source in _code_cells(document):
            lines = source.splitlines()
            if _CELL_MAGIC.match(source):
                body = "\n".join(lines[1:])
                cell_joins, cell_tables, cell_columns, cell_errors = self.sql.analyze(
                    body, base_line=logical + 2
                )
                joins.extend(cell_joins)
                tables.update(cell_tables)
                columns.update(cell_columns)
                errors.extend(cell_errors)
                logical += len(lines)
                continue

            for line in lines:
                logical += 1
                magic = _LINE_MAGIC.match(line)
                if magic:
                    inline = magic.group(2).strip()
                    if inline:
                        (
                            cell_joins,
                            cell_tables,
                            cell_columns,
                            cell_errors,
                        ) = self.sql.analyze(inline, base_line=logical)
                        joins.extend(cell_joins)
                        tables.update(cell_tables)
                        columns.update(cell_columns)
                        errors.extend(cell_errors)
                    python_lines.append("")
                elif _OTHER_MAGIC.match(line):
                    python_lines.append("")  # keep the line count aligned
                else:
                    python_lines.append(line)
                line_map.append(logical)

        if python_lines:
            code_joins, code_tables, code_columns, code_errors = self.python.analyze(
                "\n".join(python_lines), line_map=line_map
            )
            joins.extend(code_joins)
            tables.update(code_tables)
            columns.update(code_columns)
            errors.extend(code_errors)

        for join in joins:
            tables.add(join.left)
            tables.add(join.right)
        return joins, tables, columns, errors


def _code_cells(document: dict) -> Iterator[str]:
    for cell in document.get("cells") or ():
        if not isinstance(cell, dict) or cell.get("cell_type") != "code":
            continue
        source = cell.get("source")
        if isinstance(source, list):
            yield "".join(str(s) for s in source)
        elif isinstance(source, str):
            yield source
