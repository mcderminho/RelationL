"""SQL and PySpark must produce identical edges for equivalent joins.

This is the property the whole design is arranged around: both extractors
re-qualify columns with resolved base tables, render through the same sqlglot
parser, and pass through one canonicaliser.  If that ever stops being true,
these tests are what should catch it.
"""

from __future__ import annotations

import textwrap

import pytest

READS = """
orders = spark.table("sales.orders")
customers = spark.table("crm.customers")
"""

#: (label, SQL, the PySpark spelling of the same join)
EQUIVALENTS = [
    (
        "simple equality",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c ON o.cid = c.id",
        "orders.join(customers, orders.cid == customers.id)",
    ),
    (
        "operands written the other way round",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c ON c.id = o.cid",
        "orders.join(customers, customers.id == orders.cid)",
    ),
    (
        "two predicates",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON o.cid = c.id AND o.region = c.region",
        "orders.join(customers, (orders.cid == customers.id) "
        "& (orders.region == customers.region))",
    ),
    (
        "predicates in the opposite order",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON o.region = c.region AND o.cid = c.id",
        "orders.join(customers, (orders.cid == customers.id) "
        "& (orders.region == customers.region))",
    ),
    (
        "function call on both sides",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON lower(o.name) = lower(c.name)",
        "orders.join(customers, F.lower(orders.name) == F.lower(customers.name))",
    ),
    (
        "inequality, mirrored",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c ON c.score < o.score",
        "orders.join(customers, orders.score > customers.score)",
    ),
    (
        "range join",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON o.ts BETWEEN c.valid_from AND c.valid_to",
        "orders.join(customers, orders.ts.between(customers.valid_from, customers.valid_to))",
    ),
    (
        "USING versus a list of names",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c USING (cid)",
        'orders.join(customers, ["cid"])',
    ),
    (
        "OR condition",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON (o.cid = c.id OR o.email = c.email)",
        "orders.join(customers, (orders.cid == customers.id) | (orders.email == customers.email))",
    ),
    (
        "a constant filter is excluded from both",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON o.cid = c.id AND c.active = TRUE",
        "orders.join(customers, (orders.cid == customers.id) & (customers.active == True))",
    ),
    (
        "cast on one side",
        "SELECT 1 FROM sales.orders o JOIN crm.customers c "
        "ON CAST(o.cid AS STRING) = c.id",
        'orders.join(customers, orders.cid.cast("string") == customers.id)',
    ),
    (
        "left outer join",
        "SELECT 1 FROM sales.orders o LEFT JOIN crm.customers c ON o.cid = c.id",
        'orders.join(customers, orders.cid == customers.id, "left")',
    ),
]


@pytest.mark.parametrize(
    "label,sql_text,spark_text", EQUIVALENTS, ids=[e[0] for e in EQUIVALENTS]
)
def test_sql_and_pyspark_agree(sql, pyspark, label, sql_text, spark_text):
    sql_joins, _, _ = sql.analyze(sql_text)
    spark_joins, _, _ = pyspark.analyze(READS + textwrap.dedent(spark_text))

    assert len(sql_joins) == 1, "SQL side produced %d joins" % len(sql_joins)
    assert len(spark_joins) == 1, "PySpark side produced %d joins" % len(spark_joins)

    assert sql_joins[0].edge_key == spark_joins[0].edge_key, (
        "%s:\n  sql   = %r\n  spark = %r" % (label, sql_joins[0].edge_key, spark_joins[0].edge_key)
    )


def test_cte_and_dataframe_chains_agree(sql, pyspark):
    """The headline example: a CTE and a DataFrame chain are one edge."""
    sql_joins, _, _ = sql.analyze(
        """
        WITH recent AS (
            SELECT o.order_id, o.cid FROM sales.orders o WHERE o.d > 1
        )
        SELECT 1 FROM recent r JOIN crm.customers c ON r.cid = c.id
        """
    )
    spark_joins, _, _ = pyspark.analyze(
        READS
        + textwrap.dedent(
            """
            recent = orders.filter(orders.d > 1).select("order_id", "cid")
            recent.join(customers, recent.cid == customers.id)
            """
        )
    )
    assert sql_joins[0].edge_key == spark_joins[0].edge_key


def test_fixture_files_agree(sql, pyspark):
    """The SQL and PySpark fixture files describe the same mart."""
    from conftest import FIXTURES

    sql_joins, _, _ = sql.analyze(
        (FIXTURES / "warehouse" / "orders_enriched.sql").read_text(encoding="utf-8")
    )
    spark_joins, _, _ = pyspark.analyze(
        (FIXTURES / "etl" / "build_orders.py").read_text(encoding="utf-8")
    )

    def confident(joins):
        return {(j.left.key, j.right.key, j.condition) for j in joins if not j.ambiguous}

    shared = confident(sql_joins) & confident(spark_joins)
    # The four joins both files genuinely have in common.
    assert (
        "warehouse.dim_customer",
        "warehouse.fct_orders",
        "warehouse.dim_customer.customer_id = warehouse.fct_orders.customer_id",
    ) in shared
    assert (
        "warehouse.dim_product",
        "warehouse.fct_order_items",
        "warehouse.dim_product.product_id = warehouse.fct_order_items.product_id",
    ) in shared
