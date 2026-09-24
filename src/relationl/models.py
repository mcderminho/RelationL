"""Core value types shared by every extractor.

The single most important property of this module is that a join discovered in
SQL and the same join expressed in PySpark must produce byte-identical
``Join.condition`` strings.  That is achieved by funnelling *both* extractors
through :func:`render_predicate`, which consumes ``sqlglot`` expressions whose
columns have already been re-qualified with resolved base-table names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from sqlglot import exp

UNKNOWN_TABLE = "<unknown>"

#: Join types we normalise to.  Anything unrecognised is upper-cased verbatim.
_JOIN_ALIASES = {
    "": "INNER",
    "JOIN": "INNER",
    "INNER": "INNER",
    "CROSS": "CROSS",
    "OUTER": "FULL OUTER",
    "FULL": "FULL OUTER",
    "FULLOUTER": "FULL OUTER",
    "LEFT": "LEFT OUTER",
    "LEFTOUTER": "LEFT OUTER",
    "RIGHT": "RIGHT OUTER",
    "RIGHTOUTER": "RIGHT OUTER",
    "SEMI": "LEFT SEMI",
    "LEFTSEMI": "LEFT SEMI",
    "ANTI": "LEFT ANTI",
    "LEFTANTI": "LEFT ANTI",
    "RIGHTSEMI": "RIGHT SEMI",
    "RIGHTANTI": "RIGHT ANTI",
}

#: When the (left, right) pair is swapped into canonical order, the join type
#: has to be mirrored so the edge still means the same thing.
_JOIN_MIRROR = {
    "LEFT OUTER": "RIGHT OUTER",
    "RIGHT OUTER": "LEFT OUTER",
    "LEFT SEMI": "RIGHT SEMI",
    "RIGHT SEMI": "LEFT SEMI",
    "LEFT ANTI": "RIGHT ANTI",
    "RIGHT ANTI": "LEFT ANTI",
}

#: Comparison operators that keep their meaning when operands are swapped.
_SYMMETRIC_OPS = (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ)

#: Comparison operators that must be mirrored when operands are swapped.
_MIRROR_OPS: dict[type, type] = {
    exp.GT: exp.LT,
    exp.LT: exp.GT,
    exp.GTE: exp.LTE,
    exp.LTE: exp.GTE,
}

_IDENT_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_QUOTE_CHARS = "`\"[]'"


def normalise_join_type(raw: str | None) -> str:
    """Map the many spellings of a join kind onto a canonical label."""
    if raw is None:
        return "INNER"
    token = re.sub(r"[\s_]+", " ", raw.strip()).upper()
    token = re.sub(r"\bJOIN\b", " ", token)
    token = re.sub(r"\s+", " ", token).strip()
    if token in _JOIN_ALIASES:
        return _JOIN_ALIASES[token]
    compact = token.replace(" ", "")
    if compact in _JOIN_ALIASES:
        return _JOIN_ALIASES[compact]
    return token or "INNER"


def mirror_join_type(join_type: str) -> str:
    return _JOIN_MIRROR.get(join_type, join_type)


@dataclass(frozen=True, order=True)
class TableRef:
    """A physical table, identified independently of any alias or DataFrame."""

    name: str
    schema: str | None = None
    catalog: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", self.name.lower())
        if self.schema:
            object.__setattr__(self, "schema", self.schema.lower())
        if self.catalog:
            object.__setattr__(self, "catalog", self.catalog.lower())

    @property
    def key(self) -> str:
        """Fully qualified, lower-cased dotted name.  This is the node identity."""
        return ".".join(p for p in (self.catalog, self.schema, self.name) if p)

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(p for p in (self.catalog, self.schema, self.name) if p)

    def qualified(self, catalog: str | None = None, schema: str | None = None) -> TableRef:
        """Fill in a missing catalog/schema from the source defaults."""
        schema_out = self.schema or schema
        catalog_out = self.catalog or (catalog if schema_out else None)
        if schema_out == self.schema and catalog_out == self.catalog:
            return self
        return TableRef(self.name, schema_out, catalog_out)

    @classmethod
    def parse(
        cls,
        text: str,
        *,
        default_schema: str | None = None,
        default_catalog: str | None = None,
    ) -> TableRef:
        """Parse ``a``, ``s.a`` or ``c.s.a`` (with optional quoting) into a ref."""
        cleaned = text.strip().rstrip(";").strip()
        parts = [p.strip().strip(_QUOTE_CHARS) for p in _split_dotted(cleaned)]
        parts = [p for p in parts if p]
        if not parts:
            raise ValueError("empty table reference: %r" % (text,))
        if len(parts) == 1:
            return cls(parts[0], default_schema, default_catalog)
        if len(parts) == 2:
            return cls(parts[1], parts[0], default_catalog)
        # More than three parts: last is the table, second-to-last the schema,
        # everything before it folds into the catalog.
        return cls(parts[-1], parts[-2], ".".join(parts[:-2]))

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.key


def _split_dotted(text: str) -> list[str]:
    """Split on dots that are not inside backticks, quotes or brackets."""
    out: list[str] = []
    buf: list[str] = []
    closer: str | None = None
    openers = {"`": "`", '"': '"', "'": "'", "[": "]"}
    for ch in text:
        if closer is not None:
            if ch == closer:
                closer = None
            buf.append(ch)
        elif ch in openers:
            closer = openers[ch]
            buf.append(ch)
        elif ch == ".":
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf))
    return out


class ColumnRef(NamedTuple):
    """A column of a physical table.

    Carrying the *name* alongside the table is what makes a renamed CTE column
    resolve properly: ``SELECT k AS j FROM b.t2`` referenced later as ``s.j``
    has to come back as ``b.t2.k``, not ``b.t2.j``.
    """

    table: TableRef
    name: str


def column_expr(table: TableRef | None, column: str) -> exp.Column:
    """Build a sqlglot column qualified by a *resolved base table*.

    Both the SQL and the PySpark extractor call this, which is precisely why
    their rendered predicates agree.
    """
    column = column.strip().strip(_QUOTE_CHARS).lower()
    parts = table.parts if table else (UNKNOWN_TABLE,)
    kwargs: dict = {}
    # Fewer parts than slots is normal: "sales.orders" has no catalog.
    for key, value in zip(("table", "db", "catalog"), reversed(parts), strict=False):
        kwargs[key] = exp.to_identifier(value, quoted=not _IDENT_SAFE.match(value))
    return exp.Column(this=exp.to_identifier(column, quoted=False), **kwargs)


def render_predicate(node: exp.Expression) -> str:
    """Render one join predicate in canonical form.

    Canonicalisation rules, applied so that logically identical predicates
    written differently collapse onto one string:

    * operands of a comparison are ordered lexicographically, and ordered
      comparisons (``>`` ``<`` ``>=`` ``<=``) are mirrored when swapped;
    * ``AND`` / ``OR`` branches are flattened and sorted;
    * identifiers are lower-cased and left unquoted where possible.
    """
    return _canonical(node.copy()).sql(comments=False)


def _canonical(node: exp.Expression) -> exp.Expression:
    if isinstance(node, exp.Paren):
        return _canonical(node.this)

    if isinstance(node, (exp.And, exp.Or)):
        branches = sorted(
            (_canonical(b) for b in _flatten(node)),
            key=lambda b: b.sql(comments=False),
        )
        combiner = exp.and_ if isinstance(node, exp.And) else exp.or_
        out = branches[0]
        for branch in branches[1:]:
            out = combiner(out, branch, copy=False)
        return exp.Paren(this=out) if isinstance(node, exp.Or) else out

    if isinstance(node, exp.Binary) and (
        isinstance(node, _SYMMETRIC_OPS) or type(node) in _MIRROR_OPS
    ):
        left, right = _canonical(node.this), _canonical(node.expression)
        if left.sql(comments=False) > right.sql(comments=False):
            mirrored = _MIRROR_OPS.get(type(node))
            if mirrored is not None:
                return mirrored(this=right, expression=left)
            return type(node)(this=right, expression=left)
        return type(node)(this=left, expression=right)

    for key, value in list(node.args.items()):
        if isinstance(value, exp.Expression):
            node.set(key, _canonical(value))
        elif isinstance(value, list):
            node.set(key, [_canonical(v) if isinstance(v, exp.Expression) else v for v in value])
    return node


def _flatten(node: exp.Expression) -> Iterable[exp.Expression]:
    """Flatten a nested AND/OR chain of the same class into its leaves."""
    kind = type(node)
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Paren):
            stack.append(current.this)
        elif isinstance(current, kind):
            stack.extend([current.expression, current.this])
        else:
            yield current


_OPERATOR_SYMBOLS: dict[type, str] = {
    exp.EQ: "=",
    exp.NEQ: "<>",
    exp.GT: ">",
    exp.LT: "<",
    exp.GTE: ">=",
    exp.LTE: "<=",
    exp.NullSafeEQ: "<=>",
    exp.Like: "LIKE",
}


@dataclass(frozen=True, order=True)
class JoinCondition:
    """One conjunct of an ON clause, already resolved to base tables."""

    predicate: str
    operator: str | None = None
    left_column: str | None = None
    right_column: str | None = None

    @classmethod
    def from_expression(cls, node: exp.Expression) -> JoinCondition:
        canonical = _canonical(node.copy())
        operator = _OPERATOR_SYMBOLS.get(type(canonical))
        left_column = right_column = None
        if operator and isinstance(canonical, exp.Binary):
            if isinstance(canonical.this, exp.Column):
                left_column = canonical.this.sql(comments=False)
            if isinstance(canonical.expression, exp.Column):
                right_column = canonical.expression.sql(comments=False)
        return cls(
            predicate=canonical.sql(comments=False),
            operator=operator,
            left_column=left_column,
            right_column=right_column,
        )


@dataclass(frozen=True)
class Join:
    """A single join occurrence between two *tables*, found at one code site."""

    left: TableRef
    right: TableRef
    join_type: str
    conditions: tuple[JoinCondition, ...]
    line: int = 0
    language: str = "sql"
    implicit: bool = False
    #: True when a column in the condition could not be pinned to one table
    #: (a ``SELECT *`` over several sources, a UNION branch, or a DataFrame
    #: built from an earlier join).  Such an edge is a candidate, not a fact.
    ambiguous: bool = False

    @classmethod
    def create(
        cls,
        left: TableRef,
        right: TableRef,
        join_type: str,
        conditions: Sequence[JoinCondition] = (),
        *,
        line: int = 0,
        language: str = "sql",
        implicit: bool = False,
        ambiguous: bool = False,
    ) -> Join:
        """Build a join in canonical orientation.

        The table pair is sorted so that ``a JOIN b`` and ``b JOIN a`` land on
        the same edge; the join type is mirrored when that swap happens.
        """
        join_type = normalise_join_type(join_type)
        if right.key < left.key:
            left, right = right, left
            join_type = mirror_join_type(join_type)
        ordered = tuple(sorted(set(conditions)))
        return cls(left, right, join_type, ordered, line, language, implicit, ambiguous)

    @property
    def condition(self) -> str:
        """The canonical, order-independent text of the whole ON clause."""
        return " AND ".join(c.predicate for c in self.conditions)

    @property
    def edge_key(self) -> tuple[str, str, str, str]:
        return (self.left.key, self.right.key, self.join_type, self.condition)


@dataclass
class FileFindings:
    """Everything one file contributed to the scan."""

    path: str
    language: str
    joins: list[Join] = field(default_factory=list)
    tables: set[TableRef] = field(default_factory=set)
    #: Columns observed against a resolved table.  This is what the model view
    #: draws; it is what the code references, not a schema.
    columns: set[ColumnRef] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)

    def to_payload(self) -> dict:
        """Serialise for the on-disk parse cache."""
        return {
            "language": self.language,
            "errors": self.errors,
            "tables": [list(t.parts) for t in sorted(self.tables)],
            "columns": [
                [list(c.table.parts), c.name]
                for c in sorted(self.columns, key=lambda c: (c.table.key, c.name))
            ],
            "joins": [
                {
                    "left": list(j.left.parts),
                    "right": list(j.right.parts),
                    "join_type": j.join_type,
                    "line": j.line,
                    "language": j.language,
                    "implicit": j.implicit,
                    "ambiguous": j.ambiguous,
                    "conditions": [
                        [c.predicate, c.operator, c.left_column, c.right_column]
                        for c in j.conditions
                    ],
                }
                for j in self.joins
            ],
        }

    @classmethod
    def from_payload(cls, path: str, payload: dict) -> FileFindings:
        findings = cls(path=path, language=payload["language"], errors=list(payload["errors"]))
        findings.tables = {ref_from_parts(p) for p in payload["tables"]}
        findings.columns = {
            ColumnRef(ref_from_parts(parts), name)
            for parts, name in payload.get("columns", ())
        }
        for raw in payload["joins"]:
            findings.joins.append(
                Join(
                    left=ref_from_parts(raw["left"]),
                    right=ref_from_parts(raw["right"]),
                    join_type=raw["join_type"],
                    conditions=tuple(
                        JoinCondition(p, o, lc, rc) for p, o, lc, rc in raw["conditions"]
                    ),
                    line=raw["line"],
                    language=raw["language"],
                    implicit=raw["implicit"],
                    ambiguous=raw.get("ambiguous", False),
                )
            )
        return findings


def ref_from_parts(parts: Sequence[str]) -> TableRef:
    parts = list(parts)
    if len(parts) == 1:
        return TableRef(parts[0])
    if len(parts) == 2:
        return TableRef(parts[1], parts[0])
    return TableRef(parts[-1], parts[-2], ".".join(parts[:-2]))
