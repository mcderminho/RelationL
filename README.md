# RelationL

**Recover the join graph of a data platform from the code that defines it.**

RelationL reads SQL, PySpark, Jupyter notebooks and text files, works out which
*physical tables* are being joined and on what conditions, and writes the result
to SQLite as a graph of tables and joins. A bundled web app lets you explore
that graph and find the shortest join path between any two tables.

The hard part, and the reason this is not a regex over `JOIN`, is lineage. Code
does not join tables; it joins CTEs, subqueries, temp views and DataFrames.
RelationL follows those back to the tables underneath.

```sql
WITH recent AS (SELECT o.id, o.cust_id FROM sales.orders o)
SELECT * FROM recent r JOIN crm.customers c ON r.cust_id = c.id
```

```python
orders = spark.table("sales.orders")
customers = spark.read.table("crm.customers")
recent = orders.filter(orders.d > 1).select("id", "cust_id")
recent.join(customers, recent.cust_id == customers.id)
```

Both record exactly the same edge:

```
sales.orders  INNER  crm.customers   ON crm.customers.id = sales.orders.cust_id
```

Not `recent <-> c`. Not `recent <-> customers`. The tables.

---

## Install

```bash
pip install relationl          # core: scanning and the CLI
pip install 'relationl[web]'   # adds the graph explorer
```

Python 3.10+.

## Use

```bash
relationl init                 # write a starter relationl.yaml
relationl scan                 # parse the configured sources into SQLite
relationl tables               # what was found
relationl joins --min-count 2  # joins written at least twice
relationl path sales.orders crm.regions
relationl serve                # graph explorer on http://127.0.0.1:8000
```

### As a library

```python
from relationl import Config, GraphStore, scan

scan(Config.load("relationl.yaml"))

graph = GraphStore("relationl.db")
for route in graph.shortest_paths("sales.orders", "crm.regions", min_occurrences=2):
    print(" -> ".join(route.tables))
    for edge in route.edges:
        print("   ", edge.join_type, edge.condition)
```

---

## Configuration

Everything is driven by a YAML file declaring one or more **sources**. A source
is a named body of code with its own roots, filters and naming rules.

```yaml
version: 1
database: relationl.db

defaults:
  include: ["**/*.py", "**/*.sql", "**/*.ipynb", "**/*.txt"]
  exclude: ["**/.venv/**", "**/node_modules/**"]
  sql_dialect: spark

sources:
  - name: analytics
    roots: [../analytics-etl]

    # Regexes against the repo-relative path.
    file_exclude:
      - "(^|/)tests?/"
      - "_archive/"

    # Bare names inherit these: `orders` becomes `prod.analytics.orders`.
    default_catalog: prod
    default_schema: analytics

    # Keep warehouse tables, drop scratch space.
    table_include: ["^prod\\.(analytics|raw)\\."]
    table_exclude: ["_tmp$", "^scratch\\."]

    # Collapse environments so dev and prod land on one node.
    table_rewrite:
      - pattern: "^(dev|staging)_"
        replacement: ""

  - name: reporting
    roots: [../reporting]
    sql_dialect: snowflake
    default_schema: reporting
```

| Key | Meaning |
|---|---|
| `roots` | Directories to walk. Relative paths resolve against the config file. |
| `include` / `exclude` | Globs. Excluded directories are pruned, not walked. |
| `file_include` / `file_exclude` | Regexes on the repo-relative path. |
| `table_include` / `table_exclude` | Regexes on the fully qualified table name. |
| `table_rewrite` | `pattern`/`replacement` pairs applied to the table name. |
| `sql_dialect` | Any [sqlglot](https://github.com/tobymao/sqlglot) dialect. |
| `default_catalog` / `default_schema` | Qualify otherwise-bare table names. |
| `languages` | Restrict to `sql`, `python`, `notebook`, `text`. |
| `max_file_bytes` | Skip anything larger. Default 2 MB. |

`table_rewrite` runs before the include/exclude filters, and before the join
condition is rendered — so a rewritten table name appears in the condition too,
and dev and prod versions of the same join collapse onto one edge.

---

## What it understands

**SQL**

- explicit joins of every type, including `SEMI` / `ANTI`
- `USING`, `NATURAL`, and `CROSS`
- implicit comma joins, with the condition recovered from `WHERE`
- correlated subqueries (`WHERE EXISTS (... WHERE inner.id = outer.id)`)
- `MERGE ... USING ... ON`
- `INSERT INTO ... SELECT`
- CTEs, chained CTEs, derived tables and `UNION` branches
- non-equi joins (`BETWEEN`, `>`, `<`), function calls, and `OR` conditions

**PySpark**

- `spark.table`, `spark.read.table`, path reads (`/mnt/lake/sales/orders`
  becomes `sales.orders`)
- DataFrame lineage through arbitrary transform chains
- `df1.join(df2, cond, how)`, `crossJoin`, `on=` as a string or list of strings
- conditions written with attributes, `df["col"]`, `F.col("alias.col")`,
  `&` / `|`, `.between()`, `.isin()`, `.cast()`, `.eqNullSafe()`
- `.alias("x")` and `F.col("x.id")`
- `createOrReplaceTempView`, so later `spark.sql` resolves through the view
- `spark.sql(...)` and SQL held in ordinary string constants

SQL held in `spark.sql("""...""")`, in module constants, and in f-strings is
parsed the same way as a `.sql` file, including CTEs and comments inside it.

**Notebooks** - code cells are stitched into one module so lineage crosses cell
boundaries; `%%sql` cells and `%sql` line magics go to the SQL parser; other
magics are neutralised without disturbing line numbers.

### Commented-out code is never a join

Dead code left in a repository would otherwise inflate every count, so it is
excluded at the parser level rather than by pattern-matching:

| Form | Handling |
|---|---|
| `-- JOIN ...` and `/* ... */` | dropped by the SQL parser |
| `# JOIN ...` | stripped before parsing, preserving line numbers |
| `# df1.join(df2, ...)` | never reaches the Python AST |
| A block wrapped in `"""..."""` | a bare string *statement* is a docstring or a commented-out block, so its SQL is skipped |

That last rule is the subtle one. RelationL reads SQL out of string literals, and
"comment it out with triple quotes" is a common Python habit. A string that is
assigned to a name or passed as an argument is live code and is parsed; a string
that is a statement on its own is not.

A `#` comment used to cost the whole statement, taking valid joins on
neighbouring lines with it. It no longer does.

### Identical output from SQL and PySpark

This is a design guarantee, not a coincidence. The PySpark extractor renders
each condition back to SQL text, parses it with the *same* sqlglot parser, and
runs it through the *same* attribution routine. Both then go through one
canonicaliser, which:

- qualifies every column with its resolved base table,
- orders the operands of a comparison lexicographically, mirroring `>` into `<`
  when it swaps them,
- flattens and sorts `AND` / `OR` branches,
- lower-cases identifiers and strips quoting.

So `x.d > y.d` and `y.d < x.d`, in either language, are one edge.

---

## Output

A scan writes a SQLite database. The two tables you probably want:

**`nodes`** — one row per table per source, with `file_count` (how many files it
appears in), `join_count`, `degree` and the source it came from.

**`edges`** — one row per distinct (table pair, join type, condition), with
`occurrence_count` and `file_count`.

Plus `occurrences` (every code site, with line number), `edge_conditions` (the
predicates broken out individually), `files`, `repos`, `scans` and
`parse_errors`. The views `v_nodes`, `v_edges` and `v_occurrences` join these up
for ad-hoc querying:

```sql
SELECT left_table, right_table, join_type, condition, occurrence_count
FROM v_edges
WHERE occurrence_count >= 3 AND ambiguous = 0
ORDER BY occurrence_count DESC;
```

### Git provenance

For every folder scanned, RelationL finds the containing repository and records
its branch, commit and remote against each file, so every join can be traced to
where it came from. It reads `.git` directly rather than shelling out, and
memoises per repository. Credentials embedded in a remote URL are stripped
before anything is written.

### Confidence: the `ambiguous` flag

Sometimes a column genuinely cannot be pinned to one table — `SELECT *` over
several sources, a `UNION` branch, or a DataFrame that is itself the result of
an earlier join. RelationL emits an edge for each candidate and marks them
`ambiguous = 1`: at most one is the real join.

These are recorded rather than guessed at or silently dropped. Filter them out
with `--no-ambiguous`, the web app's **Confident only** toggle, or
`WHERE ambiguous = 0`. Resolving them properly needs column-level schema
information that is not present in the code.

---

## Performance

Scans are built to run over large repositories repeatedly:

- directory entries are filtered before any file is opened, and excluded
  directories are pruned rather than descended into;
- a **content-addressed parse cache** keyed by file digest *and* by the source's
  own settings means an unchanged file is never re-parsed — a re-scan costs
  little more than hashing, and editing `table_rewrite` correctly invalidates it;
- parsing runs across a process pool (CPU-bound work, so the GIL would otherwise
  serialise it), dropping to in-process for small scans where the pool's
  start-up cost would dominate;
- one file failing to parse never fails the scan, or even the rest of that file.

---

## The web app

```bash
relationl serve --port 8000
```

A force-directed view of the graph, sized by how often each table is joined.
Select two tables to get the shortest join path between them, with the full
condition at every hop. The occurrence-count filter is the main control: raise
it to strip one-off joins and leave the relationships the codebase actually
relies on, and **Confident only** hides the candidate joins described above.

Solid edges are joins; dashed edges are candidates. Tables left with no join at
the current threshold are hidden, and counted underneath the filter.

The filters and the current path query live in the URL, so a route you have
found can be pasted into a ticket and reopened exactly as it was:

```
http://127.0.0.1:8000/?from=sales.orders&to=crm.regions&min=2&confident=1
```

There is no build step and no CDN: it is plain CSS and ES modules served from
the package, so it works on an air-gapped host. It opens the database read-only,
so it is safe to point at one a scheduled scan is rewriting.

---

## Limitations

- **No schema knowledge.** RelationL reads code, not a catalog. Where a column
  is ambiguous it says so (see `ambiguous` above) rather than guessing.
- **Dynamic SQL** assembled at runtime is only partly recoverable; f-string
  interpolations are replaced with a neutral token so the surrounding SQL still
  parses.
- **Function-local DataFrames** are resolved with one flat module environment.
  A DataFrame that only ever exists as a function parameter cannot be resolved
  to a table.
- **Path reads** are mapped to table names by taking the last two path
  segments, which is a convention rather than a fact. Use `table_rewrite` if
  yours differs.

---

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
```

The test corpus in `tests/fixtures/` deliberately contains the same joins
expressed as SQL, PySpark and a notebook; `test_parity.py` asserts they produce
identical conditions. It also contains retired joins in every comment style,
which `test_comments.py` and the end-to-end scan assert never reach the graph.

## Licence

MIT.
