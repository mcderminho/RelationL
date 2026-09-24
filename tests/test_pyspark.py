"""PySpark extraction: DataFrame lineage must resolve back to tables."""

from __future__ import annotations

import textwrap

import pytest
from conftest import edge_map

READS = """
orders = spark.table("sales.orders")
customers = spark.read.table("crm.customers")
items = spark.table("sales.items")
"""


def joins_of(analyzer, code, *, reads: bool = True):
    """Analyse a snippet, prefixed by the standard table reads."""
    source = (READS if reads else "") + textwrap.dedent(code)
    found, _, _, _ = analyzer.analyze(source)
    return found


class TestLineage:
    def test_join_names_tables_not_dataframes(self, pyspark):
        found = joins_of(pyspark, "orders.join(customers, orders.cid == customers.id)")
        assert len(found) == 1
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_transform_chain_preserves_lineage(self, pyspark):
        found = joins_of(
            pyspark,
            """
            recent = (orders
                .filter(orders.d > 1)
                .select("id", "cid")
                .withColumn("x", F.lit(1))
                .repartition(8)
                .distinct())
            recent.join(customers, recent.cid == customers.id)
            """,
        )
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")

    def test_join_result_is_itself_joinable(self, pyspark):
        found = joins_of(
            pyspark,
            """
            step = orders.join(customers, orders.cid == customers.id)
            step.join(items, items.oid == orders.id)
            """,
        )
        pairs = set(edge_map(found))
        assert ("crm.customers", "sales.orders") in pairs
        assert ("sales.items", "sales.orders") in pairs

    def test_alias_resolves(self, pyspark):
        found = joins_of(
            pyspark,
            'orders.alias("o").join(customers.alias("c"), F.col("o.cid") == F.col("c.id"))',
        )
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_bracket_syntax_resolves(self, pyspark):
        found = joins_of(
            pyspark, 'orders.join(customers, orders["cid"] == customers["id"])'
        )
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_temp_view_resolves_through_spark_sql(self, pyspark):
        found = joins_of(
            pyspark,
            """
            orders.createOrReplaceTempView("o_view")
            spark.sql("SELECT 1 FROM o_view v JOIN crm.customers c ON v.cid = c.id")
            """,
        )
        assert len(found) == 1
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")

    def test_path_read_becomes_a_table(self, pyspark):
        found = joins_of(
            pyspark,
            reads=False,
            code="""
            events = spark.read.parquet("/mnt/lake/staging/raw_events")
            dim = spark.table("warehouse.dim_store")
            events.join(dim, events.store_id == dim.store_id)
            """,
        )
        assert (found[0].left.key, found[0].right.key) == (
            "staging.raw_events",
            "warehouse.dim_store",
        )

    def test_union_merges_lineage(self, pyspark):
        found = joins_of(
            pyspark,
            reads=False,
            code="""
            a = spark.table("s.part_a")
            b = spark.table("s.part_b")
            c = spark.table("d.lookup")
            a.union(b).join(c, F.col("k") == F.col("k2"))
            """,
        )
        pairs = set(edge_map(found))
        assert ("d.lookup", "s.part_a") in pairs
        assert ("d.lookup", "s.part_b") in pairs

    def test_unresolvable_dataframe_yields_nothing(self, pyspark):
        found = joins_of(pyspark, "result = unknown_a.join(unknown_b, unknown_a.k == unknown_b.k)")
        assert found == []


class TestConditions:
    def test_compound_and_condition(self, pyspark):
        found = joins_of(
            pyspark,
            "orders.join(customers, (orders.cid == customers.id) & (orders.r == customers.r))",
        )
        assert found[0].condition == (
            "crm.customers.id = sales.orders.cid AND crm.customers.r = sales.orders.r"
        )

    def test_or_condition(self, pyspark):
        found = joins_of(
            pyspark,
            "orders.join(customers, (orders.cid == customers.id) | (orders.e == customers.e))",
        )
        assert found[0].condition == (
            "(crm.customers.e = sales.orders.e OR crm.customers.id = sales.orders.cid)"
        )

    def test_string_on_is_a_using_join(self, pyspark):
        found = joins_of(pyspark, 'orders.join(customers, "cid")')
        assert found[0].condition == "crm.customers.cid = sales.orders.cid"

    def test_list_on_is_a_using_join(self, pyspark):
        found = joins_of(pyspark, 'orders.join(customers, ["cid", "region"])')
        assert found[0].condition == (
            "crm.customers.cid = sales.orders.cid AND crm.customers.region = sales.orders.region"
        )

    def test_constant_comparison_is_not_a_join_condition(self, pyspark):
        found = joins_of(
            pyspark,
            'orders.join(customers, (orders.cid == customers.id) & (customers.a == True))',
        )
        assert found[0].condition == "crm.customers.id = sales.orders.cid"

    def test_between_condition(self, pyspark):
        found = joins_of(
            pyspark,
            "orders.join(customers, orders.ts.between(customers.v_from, customers.v_to))",
        )
        assert found[0].condition == (
            "sales.orders.ts BETWEEN crm.customers.v_from AND crm.customers.v_to"
        )

    def test_null_safe_equality(self, pyspark):
        found = joins_of(
            pyspark, "orders.join(customers, orders.cid.eqNullSafe(customers.id))"
        )
        assert found[0].condition == (
            "crm.customers.id IS NOT DISTINCT FROM sales.orders.cid"
        )

    def test_cross_join_has_no_condition(self, pyspark):
        found = joins_of(pyspark, "orders.crossJoin(items)")
        assert found[0].join_type == "CROSS"
        assert found[0].condition == ""

    @pytest.mark.parametrize(
        "how,expected",
        [
            ("inner", "INNER"),
            ("left", "LEFT OUTER"),
            ("left_outer", "LEFT OUTER"),
            ("leftanti", "LEFT ANTI"),
            ("semi", "LEFT SEMI"),
            ("full", "FULL OUTER"),
        ],
    )
    def test_how_values(self, pyspark, how, expected):
        # sales.orders sorts after crm.customers, so the pair is swapped and a
        # left-sided join type is mirrored.
        found = joins_of(
            pyspark,
            'customers.join(orders, customers.id == orders.cid, "%s")' % how,
        )
        assert found[0].join_type == expected

    def test_broadcast_is_transparent(self, pyspark):
        found = joins_of(
            pyspark, "orders.join(F.broadcast(customers), orders.cid == customers.id)"
        )
        assert (found[0].left.key, found[0].right.key) == ("crm.customers", "sales.orders")


class TestEmbeddedSql:
    def test_spark_sql_joins_are_found(self, pyspark):
        found = joins_of(
            pyspark,
            'df = spark.sql("SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k")',
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_sql_in_a_module_constant_is_found(self, pyspark):
        found = joins_of(
            pyspark,
            'QUERY = """SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k"""',
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_sql_is_not_double_counted(self, pyspark):
        found = joins_of(
            pyspark, 'spark.sql("SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k")'
        )
        assert len(found) == 1

    def test_fstring_sql_still_parses(self, pyspark):
        found = joins_of(
            pyspark,
            'spark.sql(f"SELECT 1 FROM a.t1 x JOIN b.t2 y ON x.k = y.k AND x.d = {day}")',
        )
        assert found[0].condition == "a.t1.k = b.t2.k"

    def test_syntax_error_is_reported_not_raised(self, pyspark):
        joins, tables, _, errors = pyspark.analyze("def broken(:\n  pass")
        assert joins == []
        assert errors and "syntax error" in errors[0]
