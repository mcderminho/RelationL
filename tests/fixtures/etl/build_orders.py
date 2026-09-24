"""PySpark build of the enriched orders mart.

This is deliberately the same set of joins as ``warehouse/orders_enriched.sql``.
The point of the fixture is that both files must produce identical edges and
identical condition strings, despite one naming DataFrames and the other CTEs.
"""

from pyspark.sql import SparkSession
from pyspark.sql import functions as F

spark = SparkSession.builder.appName("orders").getOrCreate()

# Reads: each DataFrame is bound to exactly one physical table.
orders = spark.table("warehouse.fct_orders")
customers = spark.read.table("warehouse.dim_customer")
items = spark.table("warehouse.fct_order_items")
products = spark.table("warehouse.dim_product")
stores = spark.table("warehouse.dim_store")

# A chain of transforms must not disturb the lineage: recent_orders is still
# warehouse.fct_orders.
recent_orders = (
    orders.filter(F.col("order_ts") >= "2026-01-01")
    .select("order_id", "customer_id", "store_id", "amount")
    .repartition(16)
)

customer_orders = recent_orders.join(
    customers,
    recent_orders.customer_id == customers.customer_id,
    "inner",
)

order_lines = items.join(
    products,
    (items.product_id == products.product_id) & (products.is_current == True),
    how="left",
)

enriched = customer_orders.join(
    order_lines,
    customer_orders.order_id == order_lines.order_id,
)

with_store = enriched.join(
    stores,
    (enriched.store_id == stores.store_id) & (stores.is_active == True),
    "left",
)

# Bracket syntax and an aliased DataFrame resolve the same way.
by_alias = orders.alias("o").join(
    stores.alias("st"),
    F.col("o.store_id") == F.col("st.store_id"),
    "inner",
)

bracketed = orders.join(customers, orders["customer_id"] == customers["customer_id"])

# A function call on both sides: the rendered condition must match the SQL
# extractor's rendering of the same expression.
normalised = stores.join(
    spark.table("warehouse.dim_region"),
    F.lower(stores.region_code) == F.lower(spark.table("warehouse.dim_region").region_code),
)

# An earlier attempt, left in place.  It must not appear in the graph.
# retired = orders.join(spark.table("staging.retired_lookup"), orders.id == 1)

"""
Also retired, commented out by wrapping it in quotes:

    SELECT 1 FROM warehouse.fct_orders o
    JOIN staging.retired_block b ON o.id = b.order_id
"""

with_store.createOrReplaceTempView("enriched_orders")

# SQL against a temp view still resolves through to the base tables.
summary = spark.sql(
    """
    SELECT e.order_id, r.region_name
    FROM enriched_orders e
    -- JOIN staging.retired_inline x ON e.order_id = x.order_id
    # JOIN staging.retired_hash y ON e.order_id = y.order_id
    JOIN warehouse.dim_region r
        ON e.region_id = r.region_id
    """
)

summary.write.mode("overwrite").saveAsTable("warehouse.mart_orders")
