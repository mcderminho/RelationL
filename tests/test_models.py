"""The canonicalisation rules that make two spellings of a join one edge."""

from __future__ import annotations

import pytest
from sqlglot import exp

from relationl.models import (
    FileFindings,
    Join,
    JoinCondition,
    TableRef,
    column_expr,
    normalise_join_type,
    render_predicate,
)


class TestTableRef:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("orders", "orders"),
            ("sales.orders", "sales.orders"),
            ("prod.sales.orders", "prod.sales.orders"),
            ("`prod`.`sales`.`orders`", "prod.sales.orders"),
            ('"Sales"."Orders"', "sales.orders"),
            ("[sales].[orders]", "sales.orders"),
            ("  sales.orders ;", "sales.orders"),
            ("SALES.ORDERS", "sales.orders"),
        ],
    )
    def test_parsing(self, text, expected):
        assert TableRef.parse(text).key == expected

    def test_four_parts_fold_into_the_catalog(self):
        assert TableRef.parse("a.b.c.d").key == "a.b.c.d"

    def test_defaults_only_fill_missing_parts(self):
        ref = TableRef.parse("orders", default_schema="sales", default_catalog="prod")
        assert ref.key == "prod.sales.orders"
        ref = TableRef.parse("other.orders", default_schema="sales", default_catalog="prod")
        assert ref.key == "prod.other.orders"

    def test_empty_is_rejected(self):
        with pytest.raises(ValueError):
            TableRef.parse("   ")

    def test_is_hashable_and_ordered(self):
        refs = {TableRef.parse("b.t"), TableRef.parse("a.t"), TableRef.parse("a.t")}
        assert len(refs) == 2
        assert sorted(r.key for r in refs) == ["a.t", "b.t"]


class TestJoinTypes:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, "INNER"),
            ("", "INNER"),
            ("join", "INNER"),
            ("inner", "INNER"),
            ("left", "LEFT OUTER"),
            ("LEFT JOIN", "LEFT OUTER"),
            ("left_outer", "LEFT OUTER"),
            ("LEFT OUTER JOIN", "LEFT OUTER"),
            ("full", "FULL OUTER"),
            ("fullouter", "FULL OUTER"),
            ("leftanti", "LEFT ANTI"),
            ("semi", "LEFT SEMI"),
            ("cross", "CROSS"),
        ],
    )
    def test_normalisation(self, raw, expected):
        assert normalise_join_type(raw) == expected


class TestPredicateCanonicalisation:
    def setup_method(self):
        self.a = TableRef.parse("a.t1")
        self.b = TableRef.parse("b.t2")

    def eq(self, left_table, left_col, right_table, right_col):
        return exp.EQ(
            this=column_expr(left_table, left_col),
            expression=column_expr(right_table, right_col),
        )

    def test_operand_order_does_not_matter(self):
        first = render_predicate(self.eq(self.a, "k", self.b, "k"))
        second = render_predicate(self.eq(self.b, "k", self.a, "k"))
        assert first == second == "a.t1.k = b.t2.k"

    def test_inequality_is_mirrored_when_swapped(self):
        gt = exp.GT(this=column_expr(self.b, "d"), expression=column_expr(self.a, "d"))
        lt = exp.LT(this=column_expr(self.a, "d"), expression=column_expr(self.b, "d"))
        assert render_predicate(gt) == render_predicate(lt)

    def test_and_branches_are_sorted(self):
        first = exp.and_(self.eq(self.a, "k", self.b, "k"), self.eq(self.a, "d", self.b, "d"))
        second = exp.and_(self.eq(self.a, "d", self.b, "d"), self.eq(self.a, "k", self.b, "k"))
        assert render_predicate(first) == render_predicate(second)

    def test_or_branches_are_sorted(self):
        first = exp.or_(self.eq(self.a, "k", self.b, "k"), self.eq(self.a, "d", self.b, "d"))
        second = exp.or_(self.eq(self.a, "d", self.b, "d"), self.eq(self.a, "k", self.b, "k"))
        assert render_predicate(first) == render_predicate(second)

    def test_identifiers_are_lower_cased(self):
        predicate = self.eq(TableRef.parse("A.T1"), "K", self.b, "k")
        assert render_predicate(predicate) == "a.t1.k = b.t2.k"

    def test_unresolved_column_is_marked(self):
        predicate = exp.EQ(
            this=column_expr(None, "k"), expression=column_expr(self.b, "k")
        )
        assert "<unknown>" in render_predicate(predicate)

    def test_join_condition_records_operands(self):
        condition = JoinCondition.from_expression(self.eq(self.b, "k", self.a, "k"))
        assert condition.operator == "="
        assert condition.left_column == "a.t1.k"
        assert condition.right_column == "b.t2.k"


class TestJoin:
    def test_pair_is_sorted_and_type_mirrored(self):
        join = Join.create(TableRef.parse("z.t"), TableRef.parse("a.t"), "LEFT OUTER")
        assert (join.left.key, join.right.key) == ("a.t", "z.t")
        assert join.join_type == "RIGHT OUTER"

    def test_symmetric_types_are_not_mirrored(self):
        join = Join.create(TableRef.parse("z.t"), TableRef.parse("a.t"), "INNER")
        assert join.join_type == "INNER"

    def test_conditions_are_deduplicated_and_sorted(self):
        one = JoinCondition("b.x = a.x")
        two = JoinCondition("a.k = b.k")
        join = Join.create(
            TableRef.parse("a.t"), TableRef.parse("b.t"), "INNER", [one, two, one]
        )
        assert join.condition == "a.k = b.k AND b.x = a.x"

    def test_edge_key_is_stable(self):
        left = Join.create(TableRef.parse("a.t"), TableRef.parse("b.t"), "INNER")
        right = Join.create(TableRef.parse("b.t"), TableRef.parse("a.t"), "INNER")
        assert left.edge_key == right.edge_key


class TestSerialisation:
    def test_findings_survive_a_cache_roundtrip(self):
        join = Join.create(
            TableRef.parse("prod.a.t1"),
            TableRef.parse("b.t2"),
            "LEFT OUTER",
            [JoinCondition("prod.a.t1.k = b.t2.k", "=", "prod.a.t1.k", "b.t2.k")],
            line=12,
            language="python",
            ambiguous=True,
        )
        findings = FileFindings(path="x.py", language="python", joins=[join])
        findings.tables = {TableRef.parse("prod.a.t1"), TableRef.parse("b.t2")}
        findings.errors = ["oops"]

        restored = FileFindings.from_payload("x.py", findings.to_payload())
        assert restored.language == "python"
        assert restored.errors == ["oops"]
        assert restored.tables == findings.tables
        assert restored.joins[0].edge_key == join.edge_key
        assert restored.joins[0].line == 12
        assert restored.joins[0].ambiguous is True

    def test_payload_is_json_serialisable(self):
        import json

        findings = FileFindings(path="x.sql", language="sql")
        findings.joins.append(
            Join.create(TableRef.parse("a.t"), TableRef.parse("b.t"), "INNER")
        )
        assert json.loads(json.dumps(findings.to_payload()))
