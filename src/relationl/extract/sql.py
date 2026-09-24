"""Extract table-to-table joins from SQL.

The job this module actually does is *column lineage*: a join written against a
CTE or a derived table names an alias, and we have to trace that alias back to
the physical tables it selects from before the join means anything.

    WITH recent AS (SELECT o.id, o.cust_id FROM sales.orders o)
    SELECT * FROM recent r JOIN crm.customers c ON r.cust_id = c.id

The edge recorded here is ``sales.orders <-> crm.customers``, never ``recent``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import product
from typing import NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

from ..models import Join, JoinCondition, TableRef, column_expr
from ..naming import TableNormaliser

#: A cheap pre-filter: text that cannot possibly contain a join is never parsed.
SQL_HINT = re.compile(r"\b(join|from)\b", re.IGNORECASE)
_SELECT_HINT = re.compile(
    r"\b(select|insert|update|delete|merge|create|with)\b.*?\bfrom\b|\bjoin\b",
    re.IGNORECASE | re.DOTALL,
)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

#: A line whose first non-space character is ``#``.  MySQL and Hive accept this
#: as a comment and people write it in Spark SQL too, but most sqlglot dialects
#: reject it outright, which would cost us the whole statement.
_HASH_COMMENT = re.compile(r"^([ 	]*)#.*$", re.MULTILINE)


def strip_hash_comments(text: str) -> str:
    """Blank out ``#`` comment lines, preserving the line count.

    Only whole lines are stripped, never a ``#`` appearing mid-line, because
    that could be inside a string literal or an identifier.
    """
    if "#" not in text:
        return text
    return _HASH_COMMENT.sub(lambda m: m.group(1), text)


def looks_like_sql(text: str) -> bool:
    """Heuristic gate used before handing a string literal to the parser."""
    if len(text) < 12:
        return False
    return bool(_SELECT_HINT.search(text))


class SqlAnalyzer:
    """Extract joins from SQL text, resolving aliases down to base tables."""

    def __init__(
        self,
        normaliser: TableNormaliser,
        *,
        dialect: str | None = None,
        language: str = "sql",
        report_errors: bool = True,
    ) -> None:
        self.normaliser = normaliser
        self.dialect = dialect
        self.language = language
        # Prose files contain paragraphs that are not SQL by design; reporting
        # those as parse failures would bury the errors that matter.
        self.report_errors = report_errors

    # -- public API ---------------------------------------------------------

    def analyze(
        self,
        sql: str,
        *,
        base_line: int = 1,
        views: dict[str, frozenset[TableRef]] | None = None,
    ) -> tuple[list[Join], set[TableRef], list[str]]:
        """Return ``(joins, base_tables, errors)`` for a SQL script.

        ``views`` maps temp-view names registered elsewhere (typically by
        ``createOrReplaceTempView`` in PySpark) onto the base tables behind
        them, so a view referenced from SQL still resolves to real tables.
        """
        joins: list[Join] = []
        tables: set[TableRef] = set()
        errors: list[str] = []

        # Commented-out SQL must never become an edge.  sqlglot already drops
        # ``--`` and ``/* */``; ``#`` it rejects, so it is removed up front.
        sql = strip_hash_comments(sql)

        for statement, offset in self._statements(sql, errors):
            if statement is None:
                continue
            resolver = _Resolver(self.normaliser, views or {})
            try:
                root = build_scope(statement)
            except (SqlglotError, RecursionError) as err:  # pragma: no cover - defensive
                errors.append("scope error: %s" % err)
                continue
            if root is None:
                tables.update(self._bare_tables(statement))
                continue

            locator = _LineLocator(sql, base_line + offset)
            for scope in root.traverse():
                tables.update(resolver.scope_tables(scope))
                joins.extend(self._scope_joins(scope, resolver, locator))
            joins.extend(self._merge_joins(statement, resolver, locator))

        # A join can only exist between tables we also record as nodes.
        for join in joins:
            tables.add(join.left)
            tables.add(join.right)
        return joins, tables, errors

    def statement_tables(self, sql: str) -> set[TableRef]:
        """Base tables a statement reads from, used for DataFrame lineage."""
        _, tables, _ = self.analyze(sql)
        return tables

    # -- parsing ------------------------------------------------------------

    def _statements(
        self, sql: str, errors: list[str]
    ) -> Iterable[tuple[exp.Expression | None, int]]:
        """Parse a script, tolerating individual broken statements."""
        try:
            parsed = sqlglot.parse(sql, dialect=self.dialect)
        except (SqlglotError, RecursionError):
            parsed = None

        if parsed is not None:
            offset = 0
            for statement in parsed:
                yield statement, offset
            return

        # The script as a whole failed; salvage what we can statement by
        # statement so one bad DDL block does not cost us a whole file.
        for chunk, offset in _split_statements(sql):
            if not SQL_HINT.search(chunk):
                continue
            try:
                yield sqlglot.parse_one(chunk, dialect=self.dialect), offset
            except (SqlglotError, RecursionError) as err:
                if self.report_errors:
                    errors.append(
                        "parse error near line %d: %s" % (offset + 1, _brief(err))
                    )

    def _merge_joins(
        self, statement: exp.Expression, resolver: _Resolver, locator: _LineLocator
    ) -> list[Join]:
        """``MERGE INTO target USING source ON ...`` is a join in every respect.

        It sits outside the scope machinery, so target and source aliases are
        bound directly here.
        """
        found: list[Join] = []
        for merge in statement.find_all(exp.Merge):
            on = merge.args.get("on")
            if on is None:
                continue
            bindings: dict[str, frozenset[TableRef]] = {}
            for side in (merge.this, merge.args.get("using")):
                bindings.update(self._bind_relation(side, resolver))
            if len(bindings) < 2:
                continue

            def resolve(column: exp.Column, _b=bindings) -> frozenset[ColumnRef]:
                if column.table:
                    tables = _b.get(column.table.lower(), frozenset())
                elif len(_b) == 1:
                    tables = next(iter(_b.values()))
                else:
                    tables = frozenset()
                return _columns_for(tables, column.name)

            line = locator.line_for(_relation_hint(merge))
            merged = _group_conjuncts(_conjuncts(on), resolve)
            for (left, right), conditions in merged.items():
                found.append(
                    Join.create(
                        left,
                        right,
                        "INNER",
                        conditions,
                        line=line,
                        language=self.language,
                        ambiguous=(left, right) in merged.ambiguous,
                    )
                )
        return found

    def _bind_relation(
        self, node: exp.Expression | None, resolver: _Resolver
    ) -> dict[str, frozenset[TableRef]]:
        """Map every name a MERGE relation can be referenced by to its tables."""
        if node is None:
            return {}
        if isinstance(node, exp.Alias):
            inner = self._bind_relation(node.this, resolver)
            merged = frozenset().union(*inner.values()) if inner else frozenset()
            return {**inner, node.alias.lower(): merged}
        if isinstance(node, exp.Table):
            tables = resolver.table_or_view(node)
            out = {node.name.lower(): tables}
            if node.alias:
                out[node.alias.lower()] = tables
            return out
        if isinstance(node, exp.Subquery):
            tables = frozenset(self.statement_tables(node.this.sql(dialect=self.dialect)))
            return {node.alias.lower(): tables} if node.alias else {}
        return {}

    def _bare_tables(self, statement: exp.Expression) -> set[TableRef]:
        out = set()
        for node in statement.find_all(exp.Table):
            ref = self.normaliser(_table_name(node))
            if ref is not None:
                out.add(ref)
        return out

    # -- join discovery -----------------------------------------------------

    def _scope_joins(
        self, scope: Scope, resolver: _Resolver, locator: _LineLocator
    ) -> list[Join]:
        select = scope.expression
        if not isinstance(select, exp.Select):
            return []

        relations = _relation_keys(select)
        joins_ast = select.args.get("joins") or []

        found: list[Join] = []
        explicit_pairs: set[tuple[str, str]] = set()
        # Joins with no ON/USING: comma joins and CROSS JOIN.  Their condition,
        # if any, lives in the WHERE clause, so they are resolved afterwards.
        unconditioned: list[tuple[str | None, str | None, str, int]] = []

        for index, join_ast in enumerate(joins_ast, start=1):
            right_key = relations[index] if index < len(relations) else None
            left_key = relations[index - 1] if index - 1 < len(relations) else None
            join_type = _join_type(join_ast)
            on = join_ast.args.get("on")
            using = join_ast.args.get("using")
            line = locator.line_for(_relation_hint(join_ast))

            if on is not None:
                grouped = _group_conjuncts(_conjuncts(on), resolver.binder(scope))
            elif using:
                grouped = _using_conditions(using, scope, resolver, left_key, right_key)
            else:
                unconditioned.append((left_key, right_key, join_type, line))
                continue

            if not grouped:
                # A natural join, or an ON clause we could not resolve, still
                # connects two relations; record the edge without conditions.
                pairs = _relation_pairs(scope, resolver, left_key, right_key)
                grouped = Grouped(
                    {pair: [] for pair in pairs},
                    set(pairs) if len(pairs) > 1 else set(),
                )

            # ``_group_conjuncts`` keys pairs in sorted order, which loses which
            # side the SQL put on the left.  Outer joins are not symmetric, so
            # the original orientation is restored here before ``Join.create``
            # canonicalises it (mirroring LEFT into RIGHT if it swaps).
            right_tables = resolver.relation_tables(scope, right_key)
            for pair, conditions in grouped.items():
                explicit_pairs.add(_pair_key(*pair))
                left, right = pair
                if left in right_tables and right not in right_tables:
                    left, right = right, left
                found.append(
                    Join.create(
                        left,
                        right,
                        join_type,
                        conditions,
                        line=line,
                        language=self.language,
                        ambiguous=pair in grouped.ambiguous,
                    )
                )

        # The WHERE clause is scanned for comma joins, and for correlated
        # subqueries whose inner predicate references an enclosing table.
        where_grouped = Grouped()
        if unconditioned or scope.parent is not None:
            where = select.args.get("where")
            if where is not None:
                where_grouped = _group_conjuncts(
                    _conjuncts(where.this), resolver.binder(scope)
                )

        for left_key, right_key, join_type, line in unconditioned:
            for pair in _relation_pairs(scope, resolver, left_key, right_key):
                key = _pair_key(*pair)
                conditions = where_grouped.get(pair)
                if conditions:
                    found.append(
                        Join.create(
                            *pair,
                            "INNER",
                            conditions,
                            line=line,
                            language=self.language,
                            implicit=True,
                            ambiguous=pair in where_grouped.ambiguous,
                        )
                    )
                elif key not in explicit_pairs:
                    found.append(
                        Join.create(*pair, join_type, [], line=line, language=self.language)
                    )
                explicit_pairs.add(key)

        for pair, conditions in where_grouped.items():
            if not conditions or _pair_key(*pair) in explicit_pairs:
                continue
            found.append(
                Join.create(
                    *pair,
                    "INNER",
                    conditions,
                    line=locator.line_for(pair[1].name),
                    language=self.language,
                    implicit=True,
                    ambiguous=pair in where_grouped.ambiguous,
                )
            )
        return found


# ---------------------------------------------------------------------------
# alias -> base table resolution
# ---------------------------------------------------------------------------


class ColumnRef(NamedTuple):
    """A column of a physical table.

    Carrying the *name* alongside the table is what makes a renamed CTE column
    resolve properly: ``SELECT k AS j FROM b.t2`` referenced later as ``s.j``
    has to come back as ``b.t2.k``, not ``b.t2.j``.
    """

    table: TableRef
    name: str


def _columns_for(tables: Iterable[TableRef], name: str) -> frozenset[ColumnRef]:
    return frozenset(ColumnRef(t, name) for t in tables)


class _Resolver:
    """Resolves ``alias.column`` to the physical columns it originates from."""

    def __init__(
        self, normaliser: TableNormaliser, views: dict[str, frozenset[TableRef]]
    ) -> None:
        self.normaliser = normaliser
        self.views = {k.lower(): v for k, v in views.items()}
        self._columns: dict[tuple[int, str], frozenset[ColumnRef]] = {}
        self._scope_tables: dict[int, frozenset[TableRef]] = {}
        self._active: set[tuple[int, str]] = set()

    # -- tables -------------------------------------------------------------

    def table_ref(self, node: exp.Table) -> TableRef | None:
        name = _table_name(node)
        return self.normaliser(name) if name else None

    def scope_tables(self, scope: Scope) -> set[TableRef]:
        """Physical tables selected directly by this scope."""
        out: set[TableRef] = set()
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                out.update(self.table_or_view(source))
        return out

    def table_or_view(self, node: exp.Table) -> frozenset[TableRef]:
        """A Table node may actually name a registered temp view."""
        raw = _table_name(node)
        if not raw:
            return frozenset()
        view = self.views.get(raw.lower()) or self.views.get(node.name.lower())
        if view is not None:
            return view
        ref = self.normaliser(raw)
        return frozenset({ref}) if ref is not None else frozenset()

    def all_tables(self, scope: Scope) -> frozenset[TableRef]:
        """Every physical table reachable from this scope, recursively."""
        cached = self._scope_tables.get(id(scope))
        if cached is not None:
            return cached
        self._scope_tables[id(scope)] = frozenset()  # recursion guard
        out: set[TableRef] = set()
        for branch in _branch_scopes(scope):
            out.update(self.all_tables(branch))
        for source in scope.sources.values():
            if isinstance(source, exp.Table):
                out.update(self.table_or_view(source))
            elif isinstance(source, Scope):
                out.update(self.all_tables(source))
        result = frozenset(out)
        self._scope_tables[id(scope)] = result
        return result

    # -- columns ------------------------------------------------------------

    def binder(self, scope: Scope) -> Callable[[exp.Column], frozenset[ColumnRef]]:
        """A column resolver bound to one scope, for :func:`_group_conjuncts`."""

        def resolve(column: exp.Column) -> frozenset[ColumnRef]:
            return self.resolve_column(scope, column)

        return resolve

    def resolve_column(self, scope: Scope, column: exp.Column) -> frozenset[ColumnRef]:
        """Map a column reference onto the base columns that can supply it."""
        alias = column.table
        name = column.name
        if not name:
            return frozenset()

        if alias:
            source, _owner = self.lookup(scope, alias)
            if source is None:
                return frozenset()
            if isinstance(source, exp.Table):
                return _columns_for(self.table_or_view(source), name)
            return self.resolve_in(source, name)

        # Unqualified: only safe when the scope has exactly one source.
        sources = list(scope.sources.values())
        if len(sources) == 1:
            source = sources[0]
            if isinstance(source, exp.Table):
                return _columns_for(self.table_or_view(source), name)
            return self.resolve_in(source, name)
        return frozenset()

    def lookup(self, scope: Scope, alias: str) -> tuple[object | None, Scope | None]:
        """Find an alias in this scope, then in enclosing scopes (correlation)."""
        key = alias.lower()
        current: Scope | None = scope
        while current is not None:
            for name, source in current.sources.items():
                if name.lower() == key:
                    return source, current
            current = current.parent
        return None, None

    def resolve_in(self, scope: Scope, column: str) -> frozenset[ColumnRef]:
        """Resolve an output column of a sub-scope back to its base columns."""
        cache_key = (id(scope), column.lower())
        cached = self._columns.get(cache_key)
        if cached is not None:
            return cached
        if cache_key in self._active:  # recursive CTE
            return frozenset()

        self._active.add(cache_key)
        try:
            result = self._resolve_in_uncached(scope, column)
        finally:
            self._active.discard(cache_key)
        self._columns[cache_key] = result
        return result

    def _resolve_in_uncached(self, scope: Scope, column: str) -> frozenset[ColumnRef]:
        expression = scope.expression

        if isinstance(expression, exp.SetOperation):
            out: set[ColumnRef] = set()
            for branch in _branch_scopes(scope):
                out.update(self.resolve_in(branch, column))
            return frozenset(out) or _columns_for(self.all_tables(scope), column)

        if not isinstance(expression, exp.Select):
            return _columns_for(self.all_tables(scope), column)

        wanted = column.lower()
        has_star = False
        for projection in expression.selects:
            if isinstance(projection, exp.Star):
                has_star = True
                continue
            if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                has_star = True
                continue
            if (projection.alias_or_name or "").lower() == wanted:
                return self._resolve_expression(scope, projection)

        if has_star:
            # ``SELECT *``: the column comes from somewhere below, and with a
            # single source that is exact.  With several we cannot tell which,
            # so every source is a candidate.  A star preserves column names.
            return _columns_for(self.all_tables(scope), column)
        return frozenset()

    def _resolve_expression(self, scope: Scope, node: exp.Expression) -> frozenset[ColumnRef]:
        out: set[ColumnRef] = set()
        for inner in node.find_all(exp.Column):
            out.update(self.resolve_column(scope, inner))
        return frozenset(out)

    def relation_tables(self, scope: Scope, key: str | None) -> frozenset[TableRef]:
        """All base tables behind one FROM/JOIN relation."""
        if key is None:
            return frozenset()
        source, _ = self.lookup(scope, key)
        if isinstance(source, exp.Table):
            return self.table_or_view(source)
        if isinstance(source, Scope):
            return self.all_tables(source)
        return frozenset()


# ---------------------------------------------------------------------------
# predicate handling
# ---------------------------------------------------------------------------


#: Cap on the number of column-assignment variants explored for one conjunct
#: when a reference is ambiguous (a UNION branch, or ``SELECT *`` over several
#: sources).  Without a cap a wide ambiguous predicate could explode.
_MAX_VARIANTS = 8


@dataclass
class Grouped:
    """Conjuncts attributed to table pairs, with a confidence marker."""

    conditions: dict[tuple[TableRef, TableRef], list[JoinCondition]] = field(
        default_factory=dict
    )
    #: Pairs that only exist because a column could not be pinned to one table.
    ambiguous: set[tuple[TableRef, TableRef]] = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.conditions)

    def items(self):
        return self.conditions.items()

    def get(self, pair):
        return self.conditions.get(pair)


def _group_conjuncts(
    conjuncts: Sequence[exp.Expression],
    resolve_column: Callable[[exp.Column], frozenset[ColumnRef]],
) -> Grouped:
    """Attribute each ON/WHERE conjunct to the table pair(s) it connects.

    A column that resolves ambiguously (``u.k`` where ``u`` is a UNION of two
    tables) produces one variant of the predicate per candidate, so both
    ``t2`` and ``t3`` get a properly qualified edge rather than a bare one.
    Those variants are marked ambiguous: at most one of them is the real join.
    """
    grouped: dict[tuple[TableRef, TableRef], list[JoinCondition]] = defaultdict(list)
    ambiguous_pairs: set[tuple[TableRef, TableRef]] = set()
    certain_pairs: set[tuple[TableRef, TableRef]] = set()
    for conjunct in conjuncts:
        columns = list(conjunct.find_all(exp.Column))
        if not columns:
            continue

        # Columns are keyed by (qualifier, name): the same reference always
        # resolves the same way, and the key survives the tree copy that
        # ``_requalify`` performs.
        candidates: dict[tuple[str, str], list[ColumnRef | None]] = {}
        for column in columns:
            key = (column.table.lower(), column.name.lower())
            if key not in candidates:
                options = sorted(resolve_column(column), key=lambda c: (c.table.key, c.name))
                candidates[key] = list(options) or [None]

        keys = list(candidates)
        variants = 1
        for key in keys:
            variants *= len(candidates[key])
        if variants > _MAX_VARIANTS:
            # Too ambiguous to enumerate: keep only the certain resolutions.
            candidates = {k: (v if len(v) == 1 else [None]) for k, v in candidates.items()}

        uncertain = any(len(candidates[k]) > 1 for k in keys)
        for combination in product(*(candidates[k] for k in keys)):
            mapping = dict(zip(keys, combination, strict=True))
            involved = {ref.table for ref in combination if ref is not None}
            if len(involved) < 2:
                continue  # a filter, not a join

            def resolve(column: exp.Column, _map=mapping) -> ColumnRef | None:
                return _map.get((column.table.lower(), column.name.lower()))

            condition = JoinCondition.from_expression(_requalify(conjunct, resolve))
            ordered = sorted(involved, key=lambda t: t.key)
            for i, left in enumerate(ordered):
                for right in ordered[i + 1 :]:
                    bucket = grouped[(left, right)]
                    if condition not in bucket:
                        bucket.append(condition)
                    (ambiguous_pairs if uncertain else certain_pairs).add((left, right))

    return Grouped(dict(grouped), ambiguous_pairs - certain_pairs)


def _requalify(
    conjunct: exp.Expression, resolve: Callable[[exp.Column], ColumnRef | None]
) -> exp.Expression:
    """Rebuild a predicate in terms of base tables and their real column names.

    ``resolve`` is called on the *copied* nodes rather than looked up by
    identity, because :meth:`sqlglot.exp.Expression.transform` clones the tree
    before visiting it.
    """

    def transform(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Column):
            resolved = resolve(node)
            if resolved is None:
                return column_expr(None, node.name)
            return column_expr(resolved.table, resolved.name)
        return node

    return conjunct.transform(transform, copy=True)


def _using_conditions(
    using: Sequence[exp.Expression],
    scope: Scope,
    resolver: _Resolver,
    left_key: str | None,
    right_key: str | None,
) -> Grouped:
    """``JOIN b USING (id)`` means ``left.id = right.id``."""
    grouped: dict[tuple[TableRef, TableRef], list[JoinCondition]] = defaultdict(list)
    uncertain = False
    for identifier in using:
        column = identifier.name if isinstance(identifier, exp.Expression) else str(identifier)
        lefts = _resolve_relation_column(scope, resolver, left_key, column)
        rights = _resolve_relation_column(scope, resolver, right_key, column)
        uncertain = uncertain or len(lefts) > 1 or len(rights) > 1
        for left in lefts:
            for right in rights:
                if left.table == right.table:
                    continue
                first, second = (
                    (left, right) if left.table.key < right.table.key else (right, left)
                )
                pair = (first.table, second.table)
                condition = JoinCondition.from_expression(
                    exp.EQ(
                        this=column_expr(first.table, first.name),
                        expression=column_expr(second.table, second.name),
                    )
                )
                if condition not in grouped[pair]:
                    grouped[pair].append(condition)
    return Grouped(dict(grouped), set(grouped) if uncertain else set())


def _resolve_relation_column(
    scope: Scope, resolver: _Resolver, key: str | None, column: str
) -> frozenset[ColumnRef]:
    if key is None:
        return frozenset()
    source, _ = resolver.lookup(scope, key)
    if isinstance(source, exp.Table):
        return _columns_for(resolver.table_or_view(source), column)
    if isinstance(source, Scope):
        return resolver.resolve_in(source, column) or _columns_for(
            resolver.all_tables(source), column
        )
    return frozenset()


def _relation_pairs(
    scope: Scope, resolver: _Resolver, left_key: str | None, right_key: str | None
) -> list[tuple[TableRef, TableRef]]:
    lefts = resolver.relation_tables(scope, left_key)
    rights = resolver.relation_tables(scope, right_key)
    # Ordered the same way ``_group_conjuncts`` keys its pairs, so the two can
    # be matched up directly.
    pairs = {
        (left, right) if left.key < right.key else (right, left)
        for left in lefts
        for right in rights
        if left != right
    }
    return sorted(pairs, key=lambda p: (p[0].key, p[1].key))


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    """Split an expression on top-level AND."""
    if node is None:
        return []
    out: list[exp.Expression] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, exp.Paren):
            stack.append(current.this)
        elif isinstance(current, exp.And):
            stack.extend([current.expression, current.this])
        else:
            out.append(current)
    return out


def _pair_key(left: TableRef, right: TableRef) -> tuple[str, str]:
    return (left.key, right.key) if left.key < right.key else (right.key, left.key)


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _branch_scopes(scope: Scope) -> list[Scope]:
    """Sub-scopes of a UNION/INTERSECT/EXCEPT.

    ``Scope.union_scopes`` was renamed to ``set_operation_scopes`` in sqlglot
    26; support both so the package works across the range we declare.
    """
    branches = getattr(scope, "set_operation_scopes", None)
    if branches is None:  # pragma: no cover - older sqlglot
        branches = getattr(scope, "union_scopes", None)
    return list(branches or ())


def _table_name(node: exp.Table) -> str:
    if not isinstance(node.this, (exp.Identifier, exp.Dot)):
        return ""
    parts = [p for p in (node.catalog, node.db, node.name) if p]
    return ".".join(parts)


def _join_type(join: exp.Expression) -> str:
    if (join.args.get("method") or "").upper() == "NATURAL":
        prefix = "NATURAL "
    else:
        prefix = ""
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    label = ("%s %s" % (side, kind)).strip()
    if not label:
        label = "CROSS" if not join.args.get("on") and not join.args.get("using") else "INNER"
    return (prefix + label).strip()


def _relation_keys(select: exp.Select) -> list[str | None]:
    """Alias keys of the FROM relation followed by each joined relation."""
    keys: list[str | None] = []
    from_clause = _find_from(select)
    keys.append(_alias_key(from_clause.this) if from_clause is not None else None)
    for join in select.args.get("joins") or []:
        keys.append(_alias_key(join.this))
    return keys


def _find_from(select: exp.Select) -> exp.From | None:
    """Locate the FROM clause without depending on the args key name."""
    for value in select.args.values():
        if isinstance(value, exp.From):
            return value
    return None


def _alias_key(node: exp.Expression | None) -> str | None:
    if node is None:
        return None
    alias = node.alias if hasattr(node, "alias") else None
    if alias:
        return alias
    if isinstance(node, exp.Table):
        return node.name
    return None


def _relation_hint(join: exp.Expression) -> str:
    node = join.this
    if isinstance(node, exp.Table):
        return node.name
    alias = getattr(node, "alias", None)
    return alias or ""


class _LineLocator:
    """Best-effort line numbers.

    ``sqlglot`` does not carry source positions on expressions, so a join is
    attributed to the next line on which its right-hand relation name appears.
    A cursor per name keeps repeated joins against the same table advancing
    instead of all collapsing onto the first occurrence.
    """

    __slots__ = ("_lines", "_cursor", "_base")

    def __init__(self, text: str, base_line: int) -> None:
        self._base = base_line
        self._lines: dict[str, list[int]] = defaultdict(list)
        for offset, line in enumerate(text.splitlines()):
            for word in _WORD.findall(line):
                lowered = word.lower()
                bucket = self._lines[lowered]
                if not bucket or bucket[-1] != offset:
                    bucket.append(offset)
        self._cursor: dict[str, int] = defaultdict(int)

    def line_for(self, name: str | None) -> int:
        if not name:
            return self._base
        key = name.lower()
        occurrences = self._lines.get(key)
        if not occurrences:
            return self._base
        index = min(self._cursor[key], len(occurrences) - 1)
        self._cursor[key] = index + 1
        return self._base + occurrences[index]


def _split_statements(sql: str) -> list[tuple[str, int]]:
    """Split on semicolons outside quotes, tracking each chunk's line offset."""
    out: list[tuple[str, int]] = []
    buf: list[str] = []
    line = 0
    start_line = 0
    quote: str | None = None
    index = 0
    length = len(sql)
    while index < length:
        ch = sql[index]
        if ch == "\n":
            line += 1
        if quote:
            if ch == quote:
                quote = None
            buf.append(ch)
        elif ch in "'\"`":
            quote = ch
            buf.append(ch)
        elif ch == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue
        elif ch == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end == -1:
                break
            line += sql.count("\n", index, end)
            index = end + 2
            continue
        elif ch == ";":
            chunk = "".join(buf).strip()
            if chunk:
                out.append((chunk, start_line))
            buf = []
            start_line = line + 1
        else:
            buf.append(ch)
        index += 1
    chunk = "".join(buf).strip()
    if chunk:
        out.append((chunk, start_line))
    return out


def _brief(err: Exception) -> str:
    text = str(err).splitlines()[0] if str(err) else err.__class__.__name__
    return text[:160]
