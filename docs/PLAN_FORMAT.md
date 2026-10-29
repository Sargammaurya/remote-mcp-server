# Plan format (version 1)

**How to submit a plan**

1. For ONE dataset, call `validate_table_plan` with the dataset_id and the steps as arguments.
   This is the flat form. For several tables, call `validate_plan` with a full plan.
   The tools' input schemas list every step and field.
2. Both check the plan against the real columns and return a `draft_id` and `steps_received`
   (one numbered line per step). Check that it covers everything the user asked for.
3. `compile_plan(draft_id)` builds exactly that draft. The plan is not sent again, so it cannot
   change or lose steps on the way.

Flat form:

```json
{"dataset_id": "ds_…", "table": "orders",
 "steps": [{"op": "trim", "columns": ["customer"]}, {"op": "dedupe", "key": ["order_id"], "order_by": ["order_date"], "keep": "last"}],
 "checks": [{"column": "order_id", "check": "unique"}],
 "outputs": [], "exports": []}
```

The full form is described below.

A plan says **what** to do with uploaded datasets. The service checks it against
the datasets' real columns and compiles it into a Bruin pipeline. Unknown fields
are rejected, so a typo is an error, never a step silently skipped.

```json
{
  "version": 1,
  "name": "orders_prep",
  "sources": [{"table": "orders", "dataset_id": "ds_…"}],
  "clean": [{
    "table": "orders",
    "steps": [ … in order … ],
    "checks": [{"column": "order_id", "check": "unique"}],
    "expect_columns": [{"name": "order_id", "type": "BIGINT"}]
  }],
  "outputs": [ … aggregate | split | sql … ],
  "exports": [{"asset": "clean.orders", "format": "csv"}]
}
```

- Every source column starts as **VARCHAR**: the raw layer reads the file as text.
  Types change only through `cast` and `parse_date`, never by guessing.
- Names a plan creates (renames, derived columns, outputs, the plan name) must be
  snake_case. Source column names may be anything (`"Order ID"`).
- Assets are named `raw.<table>`, `clean.<table>` (one per cleaned source table)
  and `mart.<name>` (outputs).

## Steps

Each step sees only the columns the previous step produced.

| op | fields | notes |
|---|---|---|
| `rename` | `mapping` {old: new} | |
| `select` / `drop` | `columns` | `select` keeps exactly these, in order |
| `trim` | `columns` | VARCHAR only |
| `change_case` | `columns`, `to`: upper\|lower | VARCHAR only |
| `replace_values` | `column`, `mapping` {from: to} | exact matches |
| `strip_chars` | `columns`, `chars` | removes every listed character |
| `null_if` | `columns`, `values` | listed values become NULL |
| `cast` | `columns` {col: type} | TRY_CAST: a value that does not convert becomes NULL |
| `parse_date` | `column`, `formats` [strftime], `output`: DATE\|TIMESTAMP | explicit formats only; `%d/%m/%Y` vs `%m/%d/%Y` states the day/month order |
| `fill_null` | `column`, `value` | value must fit the column type |
| `filter` | `where` (condition) | rows that fail are removed |
| `derive` | `name`, `expression` (SQL), `type`? | row-level expression over existing columns; checked with sqlglot |
| `dedupe` | `key`?, `order_by`, `keep`: first\|last | no key: exact duplicate rows; with key: one row per key |

Types: VARCHAR, BIGINT, INTEGER, DOUBLE, BOOLEAN, DATE, TIMESTAMP.

Condition: `{"column", "operator", "value"}` with operator eq, ne, gt, ge, lt, le, in,
not_in, is_null, not_null, like; or `{"all": [...]}` / `{"any": [...]}`.

## Checks

`not_null`, `unique`, `accepted_values` (list), `positive`, `non_negative`, `min`, `max`,
`pattern`. They become Bruin column checks and run with the pipeline. A failing check
fails the run.

## Outputs

- `{"kind": "aggregate", "name", "source", "group_by", "measures": [{"name", "fn", "column"}], "where"?}`.
  `fn` is one of count, count_distinct, sum, avg, min, max.
- `{"kind": "split", "name", "source", "key", "parts": {"train": 0.8, "test": 0.2}, "seed"}`.
  Splits by a hash of the key, so the result is deterministic. Use it only when a split was
  asked for.
- `{"kind": "sql", "name", "sources": ["clean.orders"], "query": "SELECT …"}`. This is the
  fallback for what the steps cannot express. It must be one SELECT over the listed sources,
  using allowed functions only, with no table functions (`read_csv`, …) and no
  DDL/DML/ATTACH/COPY. Every column must exist.

## Saved plans

`list_saved_plans` / `instantiate_saved_plan(name, parameters)` return ordinary plans
(`invoice_cleanup`, `ml_split`), checked and compiled by the same engine.
