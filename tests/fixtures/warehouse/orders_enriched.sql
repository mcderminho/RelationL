-- Builds the enriched orders mart.
-- Exercises: CTE lineage, a chained CTE, a multi-predicate ON clause and a
-- derived table.  Every join here must resolve to warehouse.* base tables,
-- never to the CTE names.

WITH recent_orders AS (
    SELECT
        o.order_id,
        o.customer_id,
        o.store_id,
        o.order_ts,
        o.amount
    FROM warehouse.fct_orders o
    WHERE o.order_ts >= '2026-01-01'
),

customer_orders AS (
    SELECT
        r.order_id,
        r.store_id,
        r.amount,
        c.customer_id,
        c.region_id,
        c.segment
    FROM recent_orders r
    INNER JOIN warehouse.dim_customer c
        ON r.customer_id = c.customer_id
),

order_lines AS (
    SELECT
        i.order_id,
        i.product_id,
        i.quantity,
        p.category
    FROM warehouse.fct_order_items i
    LEFT JOIN warehouse.dim_product p
        ON i.product_id = p.product_id
       AND p.is_current = TRUE
)

SELECT
    co.order_id,
    co.segment,
    ol.category,
    s.store_name,
    totals.line_total
FROM customer_orders co
JOIN order_lines ol
    ON co.order_id = ol.order_id
LEFT JOIN warehouse.dim_store s
    ON co.store_id = s.store_id
   AND s.is_active = TRUE
JOIN (
    SELECT order_id, SUM(quantity) AS line_total
    FROM warehouse.fct_order_items
    GROUP BY order_id
) totals
    ON totals.order_id = co.order_id;
