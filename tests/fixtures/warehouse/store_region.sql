-- Exercises: an implicit comma join, a USING join, a non-equi join and a
-- correlated subquery.  These four shapes all have to produce the same kind of
-- edge as an ordinary explicit JOIN.

-- 1. Implicit join in the WHERE clause.
SELECT
    s.store_id,
    g.region_name
FROM warehouse.dim_store s, warehouse.dim_region g
WHERE s.region_id = g.region_id
  AND s.is_active = TRUE;

-- 2. USING, which means dim_store.region_id = dim_region.region_id.
SELECT s.store_id
FROM warehouse.dim_store s
JOIN warehouse.dim_region g USING (region_id);

-- 3. A non-equi (range) join onto a slowly changing dimension.
SELECT o.order_id
FROM warehouse.fct_orders o
LEFT JOIN warehouse.dim_customer c
    ON o.customer_id = c.customer_id
   AND o.order_ts BETWEEN c.valid_from AND c.valid_to;

-- 4. A correlated subquery: still a join between the two tables.
SELECT c.customer_id
FROM warehouse.dim_customer c
WHERE EXISTS (
    SELECT 1
    FROM warehouse.fct_orders o
    WHERE o.customer_id = c.customer_id
);

-- 5. MERGE, whose ON clause is a join by any other name.
MERGE INTO warehouse.dim_customer t
USING staging.raw_customers s
    ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET t.segment = s.segment;

-- 6. A composite key.  The model view has to fan the relationship out to both
--    columns rather than draw a single line.
SELECT i.order_id
FROM warehouse.fct_order_items i
JOIN warehouse.dim_product p
    ON i.product_id = p.product_id
   AND i.region_id = p.region_id;

-- 7. Commented-out code must never become an edge.  None of the joins below
--    exist, and the scan must agree.
-- SELECT 1
-- FROM warehouse.dim_store s
-- JOIN staging.retired_lookup r ON s.store_id = r.store_id;

/*
SELECT 1
FROM warehouse.dim_region g
JOIN staging.retired_regions rr ON g.region_id = rr.region_id;
*/

# JOIN staging.retired_hash h ON h.store_id = s.store_id
