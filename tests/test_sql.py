"""SQL join extraction, from the trivial case up to nested CTE lineage."""

from __future__ import annotations

import pytest
from conftest import conditions_for, edge_map


def joins_of(analyzer, sql):
    found, _, _ = analyzer.analyze(sql)
    return found


class TestSimpleJoins:
    def test_two_table_inner_join(self, sql):
        found = joins_of(sql, "SELECT * FROM a.orders o JOIN b.customers c ON o.cid = c.id")
        assert len(found) == 1
        join = found[0]
        assert (join.left.key, join.right.key) == ("a.orders", "b.customers")
        assert join.join_type == "INNER"
        assert join.condition == "a.orders.cid = b.customers.id"

    def test_edge_orientation_is_canonical(self, sql):
        """``a JOIN b`` and ``b JOIN a`` must land on one edge."""
        left = joins_of(sql, "SELECT 1 FROM b.customers c JOIN a.orders o ON o.cid = c.id")[0]
        right = joins_of(sql, "SELECT 1 FROM a.orders o JOIN b.customers c ON c.id = o.cid")[0]
        assert left.edge_key == right.edge_key

    def test_left_join_is_mirrored_when_pair_is_swapped(self, sql):
        # b.customers sorts after a.orders, so the pair is already canonical.
        found = joins_of(sql, "SELECT 1 FROM a.orders o LEFT JOIN b.customers c ON o.cid = c.id")
        assert found[0].join_type == "LEFT OUTER"
        # Writing it the other way round means the same thing mirrored.
        flipped = joins_of(
            sql, "SELECT 1 FROM b.customers c LEFT JOIN a.orders o ON o.cid = c.id"
        )
        assert flipped[0].join_type == "RIGHT OUTER"
        assert flipped[0].condition == found[0].condition

    @pytest.mark.parametrize(
        "clause,expected",
        [
            ("JOIN", "INNER"),
            ("INNER JOIN", "INNER"),
            ("LEFT JOIN", "LEFT OUTER"),
            ("LEFT OUTER JOIN", "LEFT OUTER"),
            ("RIGHT JOIN", "RIGHT OUTER"),
            ("FULL OUTER JOIN", "FULL OUTER"),
            ("LEFT SEMI JOIN", "LEFT SEMI"),
            ("LEFT ANTI JOIN", "LEFT ANTI"),
        ],
    )
    def test_join_types(self, sql, clause, expected):
        found = joins_of(sql, "SELECT 1 FROM a.t1 x %s b.t2 y ON x.k = y.k" % clause)
        assert found[0].join_type == expected

    def test_multi_predicate_on_clause(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k AND x.d = y.d AND y.flag = TRUE",
        )
        assert len(found) == 1
        # The constant filter is not part of the join.
        assert found[0].condition == "a.t1.d = b.t2.d AND a.t1.k = b.t2.k"

    def test_predicate_order_does_not_matter(self, sql):
        first = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k AND x.d = y.d")
        second = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON y.d = x.d AND y.k = x.k")
        assert first[0].condition == second[0].condition


class TestJoinShapes:
    def test_using_clause_expands_to_equality(self, sql):
        found = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y USING (k)")
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_comma_join_reads_condition_from_where(self, sql):
        found = joins_of(sql, "SELECT 1 FROM a.t1 x, b.t2 y WHERE x.k = y.k AND x.z > 3")
        assert len(found) == 1
        assert found[0].implicit is True
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_cross_join_has_no_condition(self, sql):
        found = joins_of(sql, "SELECT 1 FROM a.t1 x CROSS JOIN b.t2 y")
        assert found[0].join_type == "CROSS"
        assert found[0].condition == ""

    def test_non_equi_join(self, sql):
        found = joins_of(
            sql, "SELECT 1 FROM a.t1 x LEFT JOIN b.t2 y ON y.d BETWEEN x.s AND x.e"
        )
        assert found[0].condition == "b.t2.d BETWEEN a.t1.s AND a.t1.e"

    def test_inequality_is_mirrored_consistently(self, sql):
        first = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.d > y.d")
        second = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON y.d < x.d")
        assert first[0].condition == second[0].condition

    def test_function_call_in_condition(self, sql):
        found = joins_of(
            sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON lower(x.name) = lower(y.name)"
        )
        assert found[0].condition == "LOWER(a.t1.name) = LOWER(b.t2.name)"

    def test_or_condition_is_kept_whole(self, sql):
        found = joins_of(sql, "SELECT 1 FROM a.t1 x JOIN b.t2 y ON (x.k = y.k OR x.j = y.j)")
        assert found[0].condition == "(a.t1.j = b.t2.j OR a.t1.k = b.t2.k)"

    def test_correlated_subquery_is_a_join(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.customers c "
            "WHERE EXISTS (SELECT 1 FROM b.orders o WHERE o.cid = c.id)",
        )
        assert len(found) == 1
        assert found[0].implicit is True
        assert found[0].condition == "a.customers.id = b.orders.cid"

    def test_merge_on_clause_is_a_join(self, sql):
        found = joins_of(
            sql,
            "MERGE INTO w.target t USING s.source u ON t.id = u.id "
            "WHEN MATCHED THEN UPDATE SET t.v = u.v",
        )
        assert len(found) == 1
        assert found[0].condition == "s.source.id = w.target.id"

    def test_three_way_join_produces_three_pairwise_edges(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k "
            "JOIN c.t3 z ON z.k = y.k AND z.p = x.p",
        )
        pairs = set(edge_map(found))
        assert pairs == {("a.t1", "b.t2"), ("b.t2", "c.t3"), ("a.t1", "c.t3")}


class TestLineage:
    """The core promise: joins resolve to tables, never to CTE or alias names."""

    def test_cte_resolves_to_base_table(self, sql):
        found = joins_of(
            sql,
            "WITH recent AS (SELECT o.id, o.cid FROM sales.orders o) "
            "SELECT 1 FROM recent r JOIN crm.customers c ON r.cid = c.id",
        )
        assert len(found) == 1
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_chained_ctes_resolve_through_both_hops(self, sql):
        found = joins_of(
            sql,
            "WITH a1 AS (SELECT o.id AS oid, o.cid FROM sales.orders o), "
            "     a2 AS (SELECT x.oid, x.cid FROM a1 x) "
            "SELECT 1 FROM a2 y JOIN crm.customers c ON y.cid = c.id",
        )
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_column_alias_is_followed(self, sql):
        found = joins_of(
            sql,
            "WITH r AS (SELECT o.customer_ref AS cid FROM sales.orders o) "
            "SELECT 1 FROM r JOIN crm.customers c ON r.cid = c.id",
        )
        # The join must name the *underlying* column, not the CTE's alias.
        assert found[0].condition == "crm.customers.id = sales.orders.customer_ref"

    def test_derived_table_resolves(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN (SELECT k AS j FROM b.t2) s ON x.k = s.j",
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_union_branches_each_get_an_edge(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN "
            "(SELECT k FROM b.t2 UNION ALL SELECT k FROM b.t3) u ON x.k = u.k",
        )
        pairs = set(edge_map(found))
        assert pairs == {("a.t1", "b.t2"), ("a.t1", "b.t3")}
        assert conditions_for(found, "a.t1", "b.t2") == {"a.t1.k = b.t2.k"}
        # Only one of the two can be the real join, so both are flagged.
        assert all(j.ambiguous for j in found)

    def test_cte_name_is_never_a_table(self, sql):
        _, tables, _ = sql.analyze(
            "WITH recent AS (SELECT id FROM sales.orders) "
            "SELECT 1 FROM recent r JOIN crm.customers c ON r.id = c.id"
        )
        assert {t.key for t in tables} == {"sales.orders", "crm.customers"}

    def test_insert_select_still_yields_the_join(self, sql):
        found = joins_of(
            sql, "INSERT INTO w.tgt SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.id = y.id"
        )
        assert found[0].condition == "a.t1.id = b.t2.id"


class TestRobustness:
    def test_multiple_statements(self, sql):
        found = joins_of(
            sql,
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k;\n"
            "SELECT 1 FROM c.t3 p JOIN d.t4 q ON p.k = q.k;",
        )
        assert len(found) == 2

    def test_broken_statement_does_not_lose_the_others(self, sql):
        found, _, _ = sql.analyze(
            "THIS IS NOT SQL AT ALL (((;\n"
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k;"
        )
        assert len(found) == 1

    def test_comments_are_ignored(self, sql):
        found = joins_of(
            sql,
            "-- a leading comment; with a semicolon\n"
            "/* block comment */\n"
            "SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k",
        )
        assert len(found) == 1

    def test_quoted_identifiers_are_unwrapped(self, sql):
        found = joins_of(
            sql, "SELECT 1 FROM `a`.`t1` x JOIN `b`.`t2` y ON x.`k` = y.`k`"
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_case_is_normalised(self, sql):
        found = joins_of(sql, "SELECT 1 FROM A.T1 X JOIN B.T2 Y ON X.K = Y.K")
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_no_join_means_no_edges(self, sql):
        found, tables, _ = sql.analyze("SELECT * FROM a.t1 WHERE x = 1")
        assert found == []
        assert {t.key for t in tables} == {"a.t1"}

    def test_line_numbers_are_reported(self, sql):
        found = joins_of(
            sql, "SELECT 1\nFROM a.t1 x\nJOIN b.t2 y\n  ON x.k = y.k"
        )
        assert found[0].line == 3
