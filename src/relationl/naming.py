"""Turn a raw table reference from code into a filtered, canonical node name.

Normalisation has to happen *during* extraction rather than afterwards, because
the rendered join predicate embeds the resolved table names.  If a rewrite rule
collapses ``dev_sales.orders`` onto ``sales.orders``, the predicate must read
``sales.orders.id`` too, or the two environments would produce different edges
for the same join.
"""

from __future__ import annotations

from .config import Source
from .models import TableRef


class TableNormaliser:
    """Apply a source's defaults, rewrites and include/exclude filters.

    Instances are cheap and picklable, so each worker process builds its own.
    """

    __slots__ = ("_source", "_cache")

    def __init__(self, source: Source) -> None:
        self._source = source
        self._cache: dict[str, TableRef | None] = {}

    @property
    def source(self) -> Source:
        return self._source

    def __call__(self, raw: str) -> TableRef | None:
        """Return the canonical :class:`TableRef`, or ``None`` if filtered out."""
        if not raw:
            return None
        cached = self._cache.get(raw, _MISS)
        if cached is not _MISS:
            return cached  # type: ignore[return-value]
        result = self._normalise(raw)
        self._cache[raw] = result
        return result

    def _normalise(self, raw: str) -> TableRef | None:
        src = self._source
        try:
            ref = TableRef.parse(
                raw,
                default_schema=src.default_schema,
                default_catalog=src.default_catalog,
            )
        except ValueError:
            return None

        if src.table_rewrite:
            rewritten = src.rewrite_table(ref.key)
            if not rewritten:
                return None
            if rewritten != ref.key:
                try:
                    ref = TableRef.parse(
                        rewritten,
                        default_schema=src.default_schema,
                        default_catalog=src.default_catalog,
                    )
                except ValueError:
                    return None

        return ref if src.accepts_table(ref.key) else None

    def __getstate__(self) -> dict:
        return {"source": self._source}

    def __setstate__(self, state: dict) -> None:
        self._source = state["source"]
        self._cache = {}


_MISS = object()
