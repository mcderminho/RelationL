"""Language dispatch for join extraction.

One :class:`Extractor` is built per source, per worker process, and reused for
every file that worker handles.  Building the analysers is not free (they own
compiled regexes and caches), so they are never constructed per file.
"""

from __future__ import annotations

from ..config import Source
from ..models import FileFindings
from ..naming import TableNormaliser
from .notebook import NotebookAnalyzer
from .python import PythonAnalyzer
from .sql import SqlAnalyzer, looks_like_sql

__all__ = [
    "Extractor",
    "NotebookAnalyzer",
    "PythonAnalyzer",
    "SqlAnalyzer",
    "looks_like_sql",
]


class Extractor:
    """Routes file content to the right analyser for its language."""

    def __init__(self, source: Source) -> None:
        self.source = source
        self.normaliser = TableNormaliser(source)
        dialect = source.sql_dialect
        self.analysers = {
            "sql": SqlAnalyzer(self.normaliser, dialect=dialect, language="sql"),
            "python": PythonAnalyzer(self.normaliser, dialect=dialect, language="python"),
            "notebook": NotebookAnalyzer(self.normaliser, dialect=dialect, language="notebook"),
            "text": SqlAnalyzer(
                self.normaliser, dialect=dialect, language="text", report_errors=False
            ),
        }

    def extract(self, path: str, language: str, text: str) -> FileFindings:
        findings = FileFindings(path=path, language=language)
        analyser = self.analysers.get(language)
        if analyser is None:
            return findings

        if language == "text" and not looks_like_sql(text):
            # Prose files are common in the globs people configure; skipping
            # them before the parser runs is the cheapest possible win.
            return findings

        try:
            joins, tables, errors = analyser.analyze(text)
        except RecursionError:  # pragma: no cover - pathological input
            findings.errors.append("expression nesting too deep")
            return findings
        except Exception as err:  # pragma: no cover - never fail a whole scan
            findings.errors.append("%s: %s" % (type(err).__name__, err))
            return findings

        findings.joins = joins
        findings.tables = tables
        findings.errors = errors
        return findings
