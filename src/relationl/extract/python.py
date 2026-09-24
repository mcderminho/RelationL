"""Extract joins from Python, following DataFrame lineage back to tables.

The point of this module is that ``df1.join(df2, ...)`` is not interesting --
``sales.orders JOIN crm.customers`` is.  So the analyser walks the module with
an environment mapping each DataFrame variable to the set of physical tables
behind it, built up through reads, transformations, unions and temp views.

Join conditions are turned into SQL *text* and handed to the same
:mod:`sqlglot` parser and the same ``_group_conjuncts`` routine the SQL
extractor uses.  That is deliberate: it is what makes a PySpark join and the
equivalent SQL join produce a byte-identical condition string.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from ..models import Join, JoinCondition, TableRef, column_expr
from ..naming import TableNormaliser
from .sql import (
    ColumnRef,
    Grouped,
    SqlAnalyzer,
    _columns_for,
    _conjuncts,
    _group_conjuncts,
    looks_like_sql,
)

#: Reader methods whose first string argument names a table.
_TABLE_READERS = frozenset({"table", "saveastable"})

#: Reader methods whose first string argument is a path.
_PATH_READERS = frozenset(
    {"load", "parquet", "orc", "csv", "json", "avro", "delta", "text", "format"}
)

#: Methods that produce a new DataFrame from one receiver without changing
#: which tables feed it.  Anything unrecognised is also treated this way, which
#: is the right default: the vast majority of DataFrame methods are transforms.
_TERMINAL_METHODS = frozenset(
    {
        "count",
        "collect",
        "show",
        "first",
        "head",
        "take",
        "toPandas",
        "write",
        "printSchema",
    }
)

_UNION_METHODS = frozenset({"union", "unionall", "unionbyname"})
_VIEW_METHODS = frozenset(
    {"createorreplacetempview", "createtempview", "createorreplaceglobaltempview",
     "createglobaltempview", "registertemptable"}
)
_JOIN_METHODS = frozenset({"join"})
_CROSS_JOIN_METHODS = frozenset({"crossjoin"})
_PASSTHROUGH_FUNCTIONS = frozenset({"broadcast", "cache", "persist"})

#: A path read such as ``/mnt/lake/sales/orders`` is mapped onto ``sales.orders``
#: by taking the last two meaningful segments.
_PATH_SPLIT = re.compile(r"[\\/]+")
_PATH_SCHEME = re.compile(r"^[a-z0-9]+://", re.IGNORECASE)
_PARTITION = re.compile(r"^[a-z0-9_]+=", re.IGNORECASE)

_COMPARE_OPS: dict[type, str] = {
    ast.Eq: "=",
    ast.NotEq: "<>",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}

_COLUMN_METHOD_SQL = {
    "isnull": "{0} IS NULL",
    "isnotnull": "{0} IS NOT NULL",
    "eqnullsafe": "{0} <=> {1}",
    "between": "{0} BETWEEN {1} AND {2}",
    "startswith": "{0} LIKE CONCAT({1}, '%')",
    "endswith": "{0} LIKE CONCAT('%', {1})",
    "contains": "{0} LIKE CONCAT('%', {1}, '%')",
    "like": "{0} LIKE {1}",
    "rlike": "{0} RLIKE {1}",
    "asc": "{0}",
    "desc": "{0}",
    "alias": "{0}",
    "name": "{0}",
}


class UnsupportedCondition(Exception):
    """Raised when a join condition cannot be rendered faithfully as SQL."""


class PythonAnalyzer:
    """Resolve DataFrame lineage and extract table-level joins from Python."""

    def __init__(
        self,
        normaliser: TableNormaliser,
        *,
        dialect: str | None = None,
        language: str = "python",
    ) -> None:
        self.normaliser = normaliser
        self.dialect = dialect or "spark"
        self.language = language
        self.sql_analyzer = SqlAnalyzer(normaliser, dialect=self.dialect, language=language)

    def analyze(
        self, source: str, *, line_map: Sequence[int] | None = None
    ) -> tuple[list[Join], set[TableRef], list[str]]:
        """Return ``(joins, base_tables, errors)`` for a Python module.

        ``line_map`` translates 1-based AST line numbers onto file lines, which
        is how notebook cells are stitched back to their original positions.
        """
        errors: list[str] = []
        try:
            tree = ast.parse(source)
        except SyntaxError as err:
            return [], set(), ["syntax error on line %s: %s" % (err.lineno, err.msg)]

        env = _Environment(self.normaliser, self.sql_analyzer, self.dialect, self.language)
        try:
            env.run(tree)
        except RecursionError:  # pragma: no cover - pathological input
            errors.append("expression nesting too deep")

        # Any remaining SQL string literal is worth parsing: SQL is routinely
        # held in module constants and passed around rather than inlined.
        # Bare string *statements* are skipped, because a string that is never
        # bound or passed anywhere is a docstring or a block of code someone
        # commented out with triple quotes, not SQL the program runs.
        inert = _bare_string_statements(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in env.consumed or id(node) in inert:
                    continue
                if not looks_like_sql(node.value):
                    continue
                env.absorb_sql(node.value, getattr(node, "lineno", 1))

        errors.extend(env.errors)
        joins = [_remap(j, line_map) for j in env.joins]
        tables = set(env.tables)
        for join in joins:
            tables.add(join.left)
            tables.add(join.right)
        return joins, tables, errors


def _bare_string_statements(tree: ast.AST) -> set[int]:
    """Ids of string literals that stand alone as a statement.

    These are docstrings, or code a developer commented out by wrapping it in
    triple quotes.  Either way the SQL inside is not executed, so it must not
    contribute a join.
    """
    inert: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            inert.add(id(value))
    return inert


def _remap(join: Join, line_map: Sequence[int] | None) -> Join:
    if not line_map:
        return join
    index = join.line - 1
    if 0 <= index < len(line_map):
        return Join(
            join.left,
            join.right,
            join.join_type,
            join.conditions,
            line_map[index],
            join.language,
            join.implicit,
            join.ambiguous,
        )
    return join


class _Environment:
    """Walks a module, tracking what each DataFrame variable actually reads."""

    def __init__(
        self,
        normaliser: TableNormaliser,
        sql_analyzer: SqlAnalyzer,
        dialect: str,
        language: str,
    ) -> None:
        self.normaliser = normaliser
        self.sql = sql_analyzer
        self.dialect = dialect
        self.language = language
        self.frames: dict[str, frozenset[TableRef]] = {}
        self.aliases: dict[str, frozenset[TableRef]] = {}
        self.views: dict[str, frozenset[TableRef]] = {}
        self.joins: list[Join] = []
        self.tables: set[TableRef] = set()
        self.errors: list[str] = []
        self.consumed: set[int] = set()

    # -- traversal ----------------------------------------------------------

    def run(self, tree: ast.AST) -> None:
        """Visit statements in source order, keeping one flat environment.

        Function bodies share the module environment rather than getting their
        own.  A separate scope per function would be more faithful, but in
        practice pipeline code assigns DataFrames at module level and uses them
        inside helpers, and the flat model resolves those.
        """
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = self.evaluate(node.value)
                for target in node.targets:
                    self._bind(target, value)
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                if node.value is not None:
                    self._bind(node.target, self.evaluate(node.value))
            elif isinstance(node, ast.Expr):
                self.evaluate(node.value)
            elif isinstance(node, ast.Return) and node.value is not None:
                self.evaluate(node.value)

    def _bind(self, target: ast.AST, value: frozenset[TableRef]) -> None:
        if isinstance(target, ast.Name):
            if value:
                self.frames[target.id] = value
            else:
                self.frames.pop(target.id, None)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._bind(element, value)

    # -- expression evaluation ---------------------------------------------

    def evaluate(self, node: ast.AST | None) -> frozenset[TableRef]:
        """Evaluate an expression to the set of tables the result reads from."""
        if node is None:
            return frozenset()
        if isinstance(node, ast.Name):
            return self.frames.get(node.id, frozenset())
        if isinstance(node, ast.Call):
            return self._call(node)
        if isinstance(node, ast.Attribute):
            return self.evaluate(node.value)
        if isinstance(node, ast.Subscript):
            return self.evaluate(node.value)
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out: set[TableRef] = set()
            for element in node.elts:
                out.update(self.evaluate(element))
            return frozenset(out)
        if isinstance(node, ast.IfExp):
            return self.evaluate(node.body) | self.evaluate(node.orelse)
        if isinstance(node, ast.BoolOp):
            out = set()
            for value in node.values:
                out.update(self.evaluate(value))
            return frozenset(out)
        return frozenset()

    def _call(self, node: ast.Call) -> frozenset[TableRef]:
        func = node.func

        if isinstance(func, ast.Name):
            if func.id.lower() in _PASSTHROUGH_FUNCTIONS and node.args:
                return self.evaluate(node.args[0])
            for argument in node.args:
                self.evaluate(argument)
            return frozenset()

        if not isinstance(func, ast.Attribute):
            return frozenset()

        method = func.attr.lower()
        receiver = func.value

        if method == "sql":
            return self._spark_sql(node)
        if method in _TABLE_READERS:
            self.evaluate(receiver)
            return self._read_table(node)
        if method in _PATH_READERS:
            base = self.evaluate(receiver)
            read = self._read_path(node)
            return read or base
        if method in _JOIN_METHODS:
            return self._join(node, receiver, cross=False)
        if method in _CROSS_JOIN_METHODS:
            return self._join(node, receiver, cross=True)
        if method in _UNION_METHODS:
            left = self.evaluate(receiver)
            right = self.evaluate(node.args[0]) if node.args else frozenset()
            return left | right
        if method in _VIEW_METHODS:
            tables = self.evaluate(receiver)
            name = _string_arg(node, 0)
            if name and tables:
                self.views[name.lower()] = tables
            return tables
        if method == "alias":
            tables = self.evaluate(receiver)
            name = _string_arg(node, 0)
            if name and tables:
                self.aliases[name.lower()] = tables
            return tables
        if method in _TERMINAL_METHODS:
            self.evaluate(receiver)
            return frozenset()
        if method in _PASSTHROUGH_FUNCTIONS and node.args:
            self.evaluate(receiver)
            return self.evaluate(node.args[0])

        # Unknown method: assume a transform, and still walk the arguments so a
        # join nested in them (``df.select(other.join(...))``) is not lost.
        base = self.evaluate(receiver)
        for argument in node.args:
            self.evaluate(argument)
        for keyword in node.keywords:
            self.evaluate(keyword.value)
        return base

    # -- readers ------------------------------------------------------------

    def _read_table(self, node: ast.Call) -> frozenset[TableRef]:
        name = _string_arg(node, 0)
        if not name:
            return frozenset()
        view = self.views.get(name.lower())
        if view is not None:
            return view
        ref = self.normaliser(name)
        if ref is None:
            return frozenset()
        self.tables.add(ref)
        return frozenset({ref})

    def _read_path(self, node: ast.Call) -> frozenset[TableRef]:
        raw = _string_arg(node, 0)
        if not raw:
            return frozenset()
        name = _table_name_from_path(raw)
        if not name:
            return frozenset()
        ref = self.normaliser(name)
        if ref is None:
            return frozenset()
        self.tables.add(ref)
        return frozenset({ref})

    def _spark_sql(self, node: ast.Call) -> frozenset[TableRef]:
        if not node.args:
            return frozenset()
        argument = node.args[0]
        text = _static_string(argument)
        if text is None:
            return frozenset()
        self.consumed.add(id(argument))
        for inner in ast.walk(argument):
            self.consumed.add(id(inner))
        return self.absorb_sql(text, getattr(node, "lineno", 1))

    def absorb_sql(self, text: str, line: int) -> frozenset[TableRef]:
        """Run embedded SQL through the SQL extractor and keep its findings."""
        joins, tables, errors = self.sql.analyze(
            text, base_line=line, views={k: v for k, v in self.views.items()}
        )
        for join in joins:
            self.joins.append(
                Join(
                    join.left,
                    join.right,
                    join.join_type,
                    join.conditions,
                    join.line,
                    self.language,
                    join.implicit,
                    join.ambiguous,
                )
            )
        self.tables.update(tables)
        self.errors.extend(errors)
        return frozenset(tables)

    # -- joins --------------------------------------------------------------

    def _join(self, node: ast.Call, receiver: ast.AST, *, cross: bool) -> frozenset[TableRef]:
        left = self.evaluate(receiver)
        right = self.evaluate(node.args[0]) if node.args else frozenset()
        result = left | right
        if not left or not right:
            return result

        on_node = _argument(node, 1, "on")
        how = _string_arg_by_name(node, 2, "how")
        join_type = "CROSS" if cross else (how or "INNER")
        line = getattr(node, "lineno", 1)

        grouped = Grouped()
        if on_node is not None:
            columns = _string_list(on_node)
            if columns is not None:
                grouped = self._using_conditions(columns, left, right)
            else:
                grouped = self._on_conditions(on_node, left, right)

        if not grouped:
            pairs = _pairs(left, right)
            grouped = Grouped({p: [] for p in pairs}, set(pairs) if len(pairs) > 1 else set())

        # Pairs come back sorted, which loses which DataFrame was the receiver.
        # ``how="left"`` is not symmetric, so the call's orientation is restored
        # before ``Join.create`` canonicalises and mirrors it.
        for pair, conditions in grouped.items():
            first, second = pair
            if first in right and first not in left and second not in right:
                first, second = second, first
            self.joins.append(
                Join.create(
                    first,
                    second,
                    join_type,
                    conditions,
                    line=line,
                    language=self.language,
                    ambiguous=pair in grouped.ambiguous,
                )
            )
        return result

    def _using_conditions(
        self, columns: Sequence[str], left: frozenset[TableRef], right: frozenset[TableRef]
    ) -> Grouped:
        """``df1.join(df2, ["id"])`` means ``df1.id = df2.id``.

        With a multi-table side we cannot tell which of its tables supplies the
        column, so every candidate pair is emitted and marked ambiguous.
        """
        grouped: dict[tuple[TableRef, TableRef], list[JoinCondition]] = {}
        uncertain = len(left) > 1 or len(right) > 1
        for first, second in _pairs(left, right):
            bucket = grouped.setdefault((first, second), [])
            for column in columns:
                condition = JoinCondition.from_expression(
                    exp.EQ(
                        this=column_expr(first, column),
                        expression=column_expr(second, column),
                    )
                )
                if condition not in bucket:
                    bucket.append(condition)
        return Grouped(grouped, set(grouped) if uncertain else set())

    def _on_conditions(
        self, on_node: ast.AST, left: frozenset[TableRef], right: frozenset[TableRef]
    ) -> Grouped:
        """Render the ``on=`` expression as SQL, then resolve it like SQL."""
        try:
            text = self._render(on_node)
        except UnsupportedCondition:
            return Grouped()
        try:
            parsed = sqlglot.parse_one(text, dialect=self.dialect)
        except (SqlglotError, RecursionError):
            self.errors.append("could not parse join condition: %s" % text[:120])
            return Grouped()

        bindings = self._bindings(on_node, left, right)

        def resolve(column: exp.Column) -> frozenset[ColumnRef]:
            qualifier = column.table.lower()
            if not qualifier:
                # Unqualified in a two-sided join: it could be either side, and
                # without column-level schema knowledge we cannot say which.
                return frozenset()
            return _columns_for(bindings.get(qualifier, frozenset()), column.name)

        return _group_conjuncts(_conjuncts(parsed), resolve)

    def _bindings(
        self, on_node: ast.AST, left: frozenset[TableRef], right: frozenset[TableRef]
    ) -> dict[str, frozenset[TableRef]]:
        """Map every qualifier usable in the condition to its base tables."""
        bindings: dict[str, frozenset[TableRef]] = {}
        for name, tables in self.frames.items():
            bindings[name.lower()] = tables
        for name, tables in self.aliases.items():
            bindings[name.lower()] = tables
        # A single-table side can be referenced by the table's own name too.
        for side in (left, right):
            if len(side) == 1:
                ref = next(iter(side))
                bindings.setdefault(ref.name, side)
        return bindings

    # -- condition rendering ------------------------------------------------

    def _render(self, node: ast.AST) -> str:
        """Translate a PySpark Column expression into SQL text."""
        if isinstance(node, ast.Compare):
            if len(node.ops) != len(node.comparators):
                raise UnsupportedCondition
            parts = []
            left = node.left
            for operator, right in zip(node.ops, node.comparators, strict=True):
                symbol = _COMPARE_OPS.get(type(operator))
                if symbol is None:
                    raise UnsupportedCondition
                parts.append(
                    "%s %s %s" % (self._render(left), symbol, self._render(right))
                )
                left = right
            return parts[0] if len(parts) == 1 else "(%s)" % " AND ".join(parts)

        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.BitAnd):
                return "(%s AND %s)" % (self._render(node.left), self._render(node.right))
            if isinstance(node.op, ast.BitOr):
                return "(%s OR %s)" % (self._render(node.left), self._render(node.right))
            raise UnsupportedCondition

        if isinstance(node, ast.BoolOp):
            keyword = "AND" if isinstance(node.op, ast.And) else "OR"
            joined = (" %s " % keyword).join(self._render(v) for v in node.values)
            return "(%s)" % joined

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert):
            return "NOT (%s)" % self._render(node.operand)

        if isinstance(node, ast.Constant):
            return _sql_literal(node.value)

        if isinstance(node, ast.Attribute):
            # ``df1.customer_id`` -> ``df1.customer_id``
            if isinstance(node.value, ast.Name):
                return "%s.%s" % (_quote_ident(node.value.id), _quote_ident(node.attr))
            raise UnsupportedCondition

        if isinstance(node, ast.Subscript):
            # ``df1["customer_id"]``
            key = _static_string(node.slice)
            if key is None or not isinstance(node.value, ast.Name):
                raise UnsupportedCondition
            return "%s.%s" % (_quote_ident(node.value.id), _quote_ident(key))

        if isinstance(node, ast.Name):
            return _quote_ident(node.id)

        if isinstance(node, ast.Call):
            return self._render_call(node)

        if isinstance(node, (ast.Tuple, ast.List)):
            return ", ".join(self._render(e) for e in node.elts)

        raise UnsupportedCondition

    def _render_call(self, node: ast.Call) -> str:
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")).lower()

        if name in ("col", "column"):
            text = _string_arg(node, 0)
            if text is None:
                raise UnsupportedCondition
            return ".".join(_quote_ident(p) for p in text.split("."))
        if name == "lit":
            if not node.args or not isinstance(node.args[0], ast.Constant):
                raise UnsupportedCondition
            return _sql_literal(node.args[0].value)
        if name == "expr":
            text = _string_arg(node, 0)
            if text is None:
                raise UnsupportedCondition
            return "(%s)" % text
        if name == "cast" and isinstance(func, ast.Attribute):
            target = _string_arg(node, 0)
            if target is None:
                raise UnsupportedCondition
            return "CAST(%s AS %s)" % (self._render(func.value), target.upper())
        if name == "isin" and isinstance(func, ast.Attribute):
            values = ", ".join(self._render(a) for a in node.args)
            return "%s IN (%s)" % (self._render(func.value), values)

        template = _COLUMN_METHOD_SQL.get(name)
        if template is not None and isinstance(func, ast.Attribute):
            operands = [self._render(func.value)] + [self._render(a) for a in node.args]
            try:
                return template.format(*operands)
            except IndexError as err:
                raise UnsupportedCondition from err

        # A plain function call such as ``F.lower(df1.name)``.  Rendering it as
        # SQL text and letting sqlglot parse it is what keeps this identical to
        # the SQL extractor's output for the same expression.
        if node.keywords:
            raise UnsupportedCondition
        arguments = ", ".join(self._render(a) for a in node.args)
        if not name.isidentifier():
            raise UnsupportedCondition
        return "%s(%s)" % (name, arguments)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pairs(
    left: Iterable[TableRef], right: Iterable[TableRef]
) -> list[tuple[TableRef, TableRef]]:
    """Canonically ordered table pairs across the two sides of a join."""
    out = {
        (a, b) if a.key < b.key else (b, a)
        for a in left
        for b in right
        if a != b
    }
    return sorted(out, key=lambda p: (p[0].key, p[1].key))


def _argument(node: ast.Call, index: int, name: str) -> ast.AST | None:
    for keyword in node.keywords:
        if keyword.arg == name:
            return keyword.value
    if len(node.args) > index:
        return node.args[index]
    return None


def _string_arg(node: ast.Call, index: int) -> str | None:
    if len(node.args) <= index:
        return None
    return _static_string(node.args[index])


def _string_arg_by_name(node: ast.Call, index: int, name: str) -> str | None:
    argument = _argument(node, index, name)
    return _static_string(argument) if argument is not None else None


def _static_string(node: ast.AST | None) -> str | None:
    """Recover a string literal, including simple concatenations and f-strings."""
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_string(node.left)
        right = _static_string(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        # An f-string: keep the literal text and substitute a neutral token for
        # each interpolation so the surrounding SQL still parses.
        parts = []
        for index, value in enumerate(node.values):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("_relationl_param_%d" % index)
        return "".join(parts)
    return None


def _string_list(node: ast.AST) -> list[str] | None:
    """A ``on=`` given as a column name or list of names (USING semantics)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        values = []
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                values.append(element.value)
            else:
                return None
        return values or None
    return None


def _sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return "'%s'" % value.replace("'", "''")
    raise UnsupportedCondition


def _quote_ident(name: str) -> str:
    if re.match(r"^[A-Za-z_][A-Za-z0-9_$]*$", name):
        return name
    return '"%s"' % name.replace('"', '""')


def _table_name_from_path(raw: str) -> str:
    """Derive a table name from a lakehouse path.

    ``s3://lake/warehouse/sales/orders`` and
    ``/mnt/lake/sales/orders/dt=2026-01-01`` both become ``sales.orders``.
    Partition directories and file extensions are dropped.
    """
    if "." in raw and not _PATH_SPLIT.search(raw):
        return raw  # already a qualified table name
    cleaned = _PATH_SCHEME.sub("", raw)
    segments = [s for s in _PATH_SPLIT.split(cleaned) if s and s not in (".", "..")]
    segments = [s for s in segments if not _PARTITION.match(s) and not s.startswith("*")]
    if not segments:
        return ""
    last = segments[-1]
    if "." in last:
        stem = last.rsplit(".", 1)[0]
        if stem:
            segments[-1] = stem
    return ".".join(segments[-2:])
