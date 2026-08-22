# KPI YAML reference

Everything the engine can calculate, and how to declare it in YAML.

Use this document while writing `udfs/config/kpis/<kpi_id>.yaml` and `udfs/config/models/<model_id>.yaml`.

Related docs:

- [kpi-yaml-preparation-guide.md](kpi-yaml-preparation-guide.md) — write a KPI YAML: function catalog, columns vs expressions, when to use what, current limits
- [kpi-onboarding-guide.md](kpi-onboarding-guide.md) — the step-by-step process and which files to change
- [README.md](README.md) — folders, install, request path
- [kpi-framework-plan.md](kpi-framework-plan.md) — architecture and locked decisions

---

## 1. Cheat sheet

```yaml
kpi_id: 3004                 # must match execution.kpi_id
version: 1                   # optional, defaults to 1
model: sotif                 # config/models/sotif.yaml

time:
  column: event_month        # date column on the extract
  grain: month               # day | month | quarter | year
  filter_code: reporting_month   # context filter that carries the selected period
  calendar: gregorian        # gregorian | fiscal
  fiscal_start_month: 4      # only read when calendar: fiscal

dimensions:
  - { name: reason_code, kind: dimension }
  - { name: region, kind: dimension }
  # optional after extract: from + map + default (see §3)

base_measures:               # internal facts; the UI does not request these
  sotif_value:
    sql: amount              # physical column (or expr: for nested + - * /)
    agg: sum                 # Pandas folds the retrieved rows
                             # or: columns: [ontime, fullqty] + op: multiply

cuts:
  - name: G
    group_by: [reason_code]
    ignore_filters: [region]
    also_emit: [R]
  - name: R
    group_by: [reason_code, region]
    ignore_filters: []

default_cut: G
row_set: span_union          # span_union | anchor_only

measures:                    # every measure_key the UI can send
  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }
```

| I need… | Use |
|---|---|
| One period's value | `op: point` |
| Trailing / leading / YTD window | `op: window` (`range: trailing` / `leading` / `cumulative`) |
| An array for a graph | `op: trend` |
| YoY, ratio, share, n-ary add/sub | `op: arithmetic` |
| A formula over retrieved columns | `expr:` on a base measure (see §4) |
| A formula over other measures | `op: expr` (see §5.6) |
| A named function over other measures' values | `op: fn` + `inputs:` (see §5.5) |
| A named function over retrieved columns | `columns:` + `op:` on a base measure (see §10.1) |
| A dimension echoed as a `measure_key` | `op: dimension` |
| Math the catalog cannot express | `op: hook` (see §10.3) |

---

## 2. How a request is calculated

Understanding this makes the YAML obvious.

1. The **anchor** is the single value of the `time.filter_code` filter, truncated to `time.grain`. It is never applied as `WHERE month IN (...)`.
2. The engine reads the **requested** `measure_key`s, works out the deepest lookback among them, and scans `[anchor − lookback, anchor]` as a **date range**. Measures nobody asked for cost nothing.
3. DuckDB retrieves **model columns only** (time bucket, dimensions, and physical columns named by KPI YAML). It does not run `agg:`, `op:`, or `expr:`.
4. Pandas computes every base measure on those rows, folds with `agg:`, then puts the result on a **dense period spine** so shifts move by the calendar, not by row position.
5. Each cut re-aggregates that spine to its own `group_by`, then every requested measure is evaluated per dimension combination.

The practical consequences:

- A missing month stays missing. "Same month last year" cannot silently pick up the row that happens to sit 12 rows back.
- One request can return several grains (`also_emit`) from a single scan.
- Adding a measure to YAML does not widen the scan until someone requests it.

---

## 3. `time` — grain, calendar, anchor

| Key | Values | Default | Notes |
|---|---|---|---|
| `column` | column name | required if `time:` is present | Date or timestamp on the extract |
| `grain` | `day`, `month`, `quarter`, `year` | `month` | The period every measure counts in |
| `filter_code` | context filter key | required if `time:` is present | Case- and space-insensitive (`Reporting Month` matches `reporting_month`). **Not** hardcoded to `reporting_month` — each KPI names its own filter. |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal affects `quarter` and `year` only |
| `fiscal_start_month` | 1–12 | `4` | First month of the fiscal year |
| `format` | `yyyy-mm-dd`, `yyyy-mm`, `yyyymmdd`, `yyyymm`, `mmyyyy`, or a strptime string | ISO `YYYY-MM` / `YYYY-MM-DD` | How the physical column and the context time filter are stored (`062026` → `format: mmyyyy`) |

Omit the entire `time:` block when the KPI has no period column. The engine then aggregates the filtered extract as a snapshot: no month filter, no date range, no dense spine. Snapshot KPIs may only use `point` (no offset), `dimension`, `arithmetic`, and `hook` without lookback. Windows, trends, and period offsets require `time:`.

Rules the engine enforces:

- When `time:` is declared, that filter must carry **exactly one** value. Two values or zero values is an error, not a range.
- A missing time filter on a time-based KPI is an error. The engine never defaults to "latest data" or to `business_date`.
- `grain: day` requires a full `YYYY-MM-DD` unless `time.format` says otherwise.
- Truncation happens in SQL (`date_trunc` after the column is parsed with `time.format`), so a mid-period date anchors on the period start.
- A `kind: sql` model may contain any number of CTEs. Context IN filters and the time range are applied on the **wrapper around the final SELECT**, not inside earlier CTEs. `Region` / `region` / `Reason_code` / `reason_code` are the same name.

Fiscal example — with `fiscal_start_month: 4`, March 2026 belongs to fiscal year starting **2025-04-01** and to the fiscal quarter starting **2026-01-01**.

```yaml
time:
  column: posting_date
  grain: quarter
  filter_code: reporting_quarter
  calendar: fiscal
  fiscal_start_month: 4
```

A dimension can rewrite retrieved codes in Pandas (`from` + `map` + `default`) or truncate a date column (`grain:`). Joins stay in the model YAML; this is not SQL in the KPI file.

```yaml
dimensions:
  - name: order_status
    from: o_orderstatus
    map: { O: Open, P: Processing, F: Fulfilled }
    default: Other
```

---

## 4. `base_measures` — the built-in aggregations

A base measure is the raw fact. It is **not** requestable by the UI; calculated measures point at it with `of:`. Compute `sotif_value` once; `current_value`, `value_3m`, `trend_12m`, and hooks all reuse that same series. Arithmetic then reuses those **measures** (`left` / `right` / `of: [a, b]`), not the base fact a second time.

```yaml
base_measures:
  sotif_value:
    sql: amount        # physical column DuckDB retrieves
    agg: sum           # Pandas SUM of that column
  harmonic:
    expr: (col_a * col_b) / (col_a + col_b)   # per row, then agg
    agg: sum
  line_value:
    columns: [ontime, fullqty]
    op: multiply       # Pandas: ontime * fullqty on each retrieved row
    agg: sum           # optional; fold remaining rows (SUM of products)
  fill_rate:
    columns: { numerator: shipped_qty, denominator: ordered_qty }
    op: divide         # named, so the operands cannot end up the wrong way round
    agg: avg
  distinct_suppliers:
    sql: supplier_name
    agg: count_distinct
  snapshot_balance:
    sql: balance
    agg: last          # semiadditive: last row in the period, not a sum of days
  p90_delay:
    sql: delay_days
    agg: percentile
    percentile: 90     # 90 and 0.9 both mean the 90th percentile
  open_amount:
    columns: [amount]
    agg: sum
    where:
      column: status
      op: in           # in | eq | ne
      values: [O]
```

`sql: amount` + `agg: sum` names the physical column DuckDB retrieves. Pandas then sums it. KPI YAML formulas never appear in the DuckDB SQL.

Prefer `expr:` for nested arithmetic, or `columns:` + `op:` for a registered function. `expr: (col_a * col_b) / (col_a + col_b)` with `agg: sum` is the **sum of per-row ratios**. The ratio of totals is a measure-level `op: expr` over two summed facts (see §5.6).

Prefer `columns:` + `op:` when Pandas must combine retrieved columns. `op: multiply` with `columns: [ontime, fullqty]` is the row-wise product, then `agg: sum` adds those products (`1*10 + 0*4 = 10`, not `(1+0)*(10+4)`). `agg` may be omitted; it defaults to `sum` so a one-row extract is just the product.

`op:` names a function in the column registry, and every entry in `columns:` is handed to it as a numeric Series. Register your own for a custom calculation (see §10.1). These are built in:

| `op` | Columns | Does |
|---|---|---|
| `sum` | any number | `a + b + c …` across one row |
| `subtract` | any number | `a - b - c …` |
| `multiply` | any number | `a * b * c …` |
| `min` / `max` / `avg` | any number | reduces **across** the listed columns of one row |
| `coalesce` | any number | first non-null, left to right |
| `divide` | `numerator`, `denominator` | zero denominator yields null, not `inf` |
| `percent_of` | `part`, `whole` | the share, scaled to 0-100 |
| `abs` | `column` | absolute value |
| `value` | `column` | passes the column through |

Older files may use `add`, `sub`, `mul`, `product`, `mean`, `ratio`, `share`, `div`, or `identity`; these are aliases of the names above and keep working.

Note `op: sum` reduces **across** the columns of a single row, while `agg: sum` reduces **down** the rows of a period. A base measure often uses both.

**Naming the columns.** When a function's arguments are not interchangeable, write `columns:` as a mapping instead of a list and the columns bind by parameter name, so key order in the file cannot invert the result:

```yaml
base_measures:
  fill_rate:
    columns: { numerator: shipped_qty, denominator: ordered_qty }
    op: divide
    agg: avg
```

How many columns an `op` accepts, and what its parameters are called, come from the function's own Python signature. A function declared `def rate(shipped, ordered)` takes exactly two; one declared `def total(*columns)` takes any number and cannot be called by name. An unknown op, the wrong number of columns, or a parameter that does not exist all fail at bind time with the valid alternatives listed.

`sql:` / `expr:` name physical columns (and optional `+ - * /` on a base measure). Function calls (`COALESCE`, `SUM`, …) and free SQL are rejected; put the aggregation in `agg:`. DuckDB only SELECTs the column names; Pandas evaluates the formula.

Do **not** use `agg: sum` on a per-row ratio if you wanted a ratio of two totals. Declare two base measures and `op: expr` (or `op: arithmetic`) instead.

### Built-in aggregations

| `agg` | Computed in | Combines across a coarser cut by | Empty period returns |
|---|---|---|---|
| `sum` | Pandas | adding | `0` in windows and trends, `null` at a point |
| `count` | Pandas | adding | `0` in windows and trends, `null` at a point |
| `min` | Pandas | taking the minimum | `null` |
| `max` | Pandas | taking the maximum | `null` |
| `avg` | Pandas `sum` + `count` | adding both parts, then dividing | `null` |
| `count_distinct` | Pandas over raw rows | recomputing from rows | `null` |
| `median` | Pandas over raw rows | recomputing from rows | `null` |
| `percentile` | Pandas over raw rows | recomputing from rows | `null` |
| `first` | Pandas over raw rows | taking the first event | `null` |
| `last` | Pandas over raw rows | taking the last event | `null` |

Three behaviours worth knowing before you pick an aggregation:

**`avg` is weighted, never an average of averages.** It travels as `SUM` and `COUNT` and divides at the very end. A 3-month average over months of 2, 1 and 1 rows is `total / 4`, not `(a + b + c) / 3`.

**`min` and `max` are recomputed at every cut.** A global cut takes the minimum across regions; it does not add regional minima together.

**`count_distinct`, `median`, `percentile`, `first` and `last` are non-additive**, so Pandas keeps the retrieved rows and recomputes over the window. `last` on a balance is the latest snapshot in the period — it does not add daily balances. This costs more memory than an additive measure; keep the dimensionality sensible.

`percentile` requires the `percentile:` key. Values above 1 are read as percentages (`90` → `0.9`); values must land in 0–1 or 0–100.

### Multi-model base measures

Add `model:` to source a base measure from a different model, then declare how to join (see §8).

```yaml
base_measures:
  defects:  { sql: defect_count, agg: sum }
  shipments: { sql: shipped_qty, agg: sum, model: shipments }
```

---

## 5. `measures` — the requestable operations

Every `measures_required[].measure_key` the UI can send must be a key here. Unknown keys fail at bind time with the valid list.

`op:` and `kind:` are interchangeable spellings.

### 5.1 `point` — one period

```yaml
current_value:
  of: sotif_value
  op: point
  offset: { months: 0 }

previous_year_value:
  of: sotif_value
  op: point
  offset: { years: 1 }     # 1 year BEFORE the anchor
```

`offset` counts **backwards** from the anchor. Available units: `days`, `months`, `quarters`, `years`; they add together, so `{ years: 1, months: 2 }` is 14 months back. Month-end dates clamp (31 Mar − 1 month = 28/29 Feb).

Returns `null` when that period has no rows for this dimension combination.

### 5.2 `window` — trailing, leading, or cumulative

```yaml
value_3m:
  of: sotif_value
  op: window
  trailing: { months: 3 }
  inclusive: true          # default

value_ytd:
  of: sotif_value
  op: window
  range: cumulative        # calendar (or fiscal) year start through the anchor

value_next_3m:
  of: sotif_value
  op: window
  range: leading
  trailing: { months: 3 }
  inclusive: true
```

| `range` | Window |
|---|---|
| `trailing` (default) | last N periods relative to the anchor |
| `leading` | next N periods relative to the anchor |
| `cumulative` | from the start of the calendar/fiscal year through the anchor |

| `inclusive` | Trailing window | With anchor March |
|---|---|---|
| `true` (default) | the last N periods **including** the anchor | Jan, Feb, Mar |
| `false` | N periods **ending one period before** the anchor | Dec, Jan, Feb |

`trailing: { days: N }` is a calendar-day window. `periods`, `months`, `quarters`, and `years` are counts of **KPI grain periods**.

The window aggregates using the base measure's own `agg`: a `min` base gives the minimum over the window, a `sum` base gives the total, a `last` base gives the last snapshot in the window.

### 5.3 `trend` — an array for a graph

```yaml
trend_12m:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  inclusive: true
  cuts: [G]                # optional; default is default_cut only
```

- Returns a fixed-length array; the shared x-axis is in `trend_axes` in the response, so length always matches.
- A period with no rows keeps its slot: `0` for `sum`/`count`, `null` for everything else.
- Trends are emitted **only on the default cut** unless `cuts:` lists more. This is deliberate — a trend on a high-cardinality cut multiplies the payload.
- Guardrail: rows × array length may not exceed **50,000 cells** per cut, otherwise the request fails and asks you to narrow `cuts`.

`cuts:` is honoured for **trend** and **rank**. On other ops it is ignored.

### 5.3a `constant` — a literal number

```yaml
target:
  op: constant
  value: 0.98
```

The same scalar on every cut combo. Use it as `left` / `right` / `inputs` / `expr` for ratios against a goal. No extract and no lookback.

### 5.3b `rank` — rank values on a cut

```yaml
reason_code_rank:
  op: rank
  of: current_value          # or a base measure (anchor point)
  group_by: [reason_code]    # Reason_Code matches reason_code
  order: desc                # desc (default) or asc
  cuts: [G]
```

Pandas `RANK()` after the cut (ties share a rank; the next rank skips). Rank is across the whole cut when `group_by` is omitted or equals that cut's keys. A **subset** of the cut keys restarts the rank inside each group. Null sources stay null. Defaults to `default_cut` unless `cuts:` lists more.

### 5.4 `arithmetic` — combine measures

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value       # keys of other measures
  right: previous_year_value

net_value:
  op: arithmetic
  fn: subtract
  of: [gross_value, opex_value]   # n-ary; folded left to right
```

| `fn` | Parameters | Result | Null / zero handling |
|---|---|---|---|
| `growth_pct` | `current`, `previous` | `(current − previous) / previous` | `null` if `previous` is 0 or null |
| `divide` | `numerator`, `denominator` | the ratio | `null` if `denominator` is 0 or null |
| `percent` | `part`, `whole` | the ratio × 100 | `null` if `whole` is 0 or null |
| `sum` | any number | `a + b + c …` | `null` if any operand is null |
| `subtract` | any number | `a − b − c …` | `null` if any operand is null |
| `multiply` | any number | `a × b × c …` | `null` if any operand is null |
| `min` / `max` / `avg` | any number | reduces the operands, ignoring nulls | `null` only if every operand is null |

Aliases `yoy`, `mom`, `percent_change`, `div`, `ratio`, `share`, `add`, `sub`, and `mul` map onto the same functions and keep older files working.

`growth_pct` returns a **ratio** (0.05 = +5%), not a pre-multiplied percentage. Use `percent` if the UI wants 5.0.

`left` and `right` may reference any other measure, including another `arithmetic` measure. `of: [a, b, c]` is the same fold with more than two operands. The scan span automatically covers the deepest operand.

`fn` names an entry in the measure function registry, so the table above is the built-in set, not a closed one. A two-parameter function given a longer `of:` list is folded two at a time; a function that accepts any number sees every operand at once, which is why `avg` over three operands is a true three-way mean.

### 5.5 `fn` — feed other measures into a named function

```yaml
otd_pct:
  op: fn
  fn: safe_ratio            # a registered measure function
  inputs: [ontime_value, total_value]   # keys of other measures
```

The engine computes each measure in `inputs:` first and passes their scalars to the function. A list binds positionally; a mapping binds by parameter name, which is worth using whenever swapping two operands would still produce a plausible-looking number:

```yaml
yoy_growth:
  op: fn
  fn: growth_pct
  inputs: { current: this_year_value, previous: last_year_value }
```

Inputs may be any measure, including another `fn` measure, so calculations chain:

```yaml
otd_scaled:
  op: fn
  fn: multiply
  inputs: [otd_pct, total_value]
```

Rules the binder enforces:

- Every name in `inputs:` must be a declared measure, else bind fails listing the valid keys.
- The number of inputs must fit the function's signature, and a named input must be one of its parameters.
- A dependency cycle (`a -> b -> a`, or a measure naming itself) fails at bind with the cycle spelled out.
- The scan span covers the deepest input, so a `fn` over a year-ago point still scans 12 months.
- A measure named by several parents is evaluated **once** per dimension combination.

See §10.2 for registering the function.

### 5.6 `expr` — a formula over other measures

```yaml
agg_ratio:
  op: expr
  expr: (a_value * b_value) / (a_value + b_value)
```

Identifiers are other measure keys. The engine computes them first (same memo and cycle rules as `fn`). A zero or null denominator yields null.

This is the **ratio of totals**. The same formula on `base_measures.expr` is the **sum of per-row ratios**. Parentheses follow normal `+ - * /` precedence.

### 5.7 `dimension` — echo an attribute

```yaml
reason_code:
  kind: dimension
```

Only needed when the platform sends a dimension as a `measure_key`. The key must match a name in `dimensions:`.

### 5.8 `hook` — a registered Python function

See §10.3.

---

## 6. `cuts` — grouping grains in one response

A cut is a grouping level, not a number. One request can return several.

```yaml
cuts:
  - name: G                    # global: no region
    group_by: [reason_code]
    ignore_filters: [region]   # keep region OUT of the scan
    also_emit: [R]             # return R in the same response
  - name: R
    group_by: [reason_code, region]
    ignore_filters: []

default_cut: G
```

| Key | Meaning |
|---|---|
| `name` | Appears as `output_cut` on every row |
| `group_by` | Dimensions this cut groups by |
| `ignore_filters` | Filter codes or column names this cut ignores |
| `also_emit` | Other cuts to return in the same response (chains are followed, cycles are safe) |
| `default_cut` | The cut the walk starts from; defaults to the first declared cut |

**How `ignore_filters` works.** A filter ignored by *any* emitted cut is kept out of the DuckDB `WHERE` clause and applied per-cut in Pandas instead. That is what lets `region=NA` narrow the R rows while G still reports worldwide from the same scan. The response reports where each filter ran under `applied_filters` (`stage: source` or `stage: cut`) and lists skipped ones under `ignored_filters`.

Every cut re-aggregates the spine from scratch, so a global average is a true weighted average, not a mean of regional averages.

### `row_set` — which combinations get a row

| Value | Emits a row for a dimension combination when… |
|---|---|
| `span_union` (default) | it has data **anywhere in the scanned span** |
| `anchor_only` | it has data **at the selected period** |

Use `anchor_only` when the page should list only what is active now; use `span_union` when a combination that dropped to zero this month should still show its history.

---

## 7. Filters

Filters arrive on the context and default to `IN`. You normally declare nothing.

| Situation | What to do |
|---|---|
| Metadata already maps the filter to a column | Nothing — `filter_column_mappings` is used |
| The filter code equals the column name | Nothing — it binds by name |
| No mapping exists, or this KPI needs a different column | Add `filter_map` to the KPI YAML |

```yaml
filter_map:
  plant_code: region      # context filter_code → column name (must be an identifier)
```

`filter_map` takes precedence over context mappings for this KPI.

Contract details:

- An unmapped filter is a **hard error**, never silently dropped.
- An empty value list means "nothing selected" and matches no rows (compiled as `FALSE`).
- `input_text: heir` is rejected — expand hierarchies in the context builder.
- Values are always bound as SQL parameters; nothing is concatenated into SQL text.

---

## 8. Multi-model KPIs (`model_relations`)

Use this when a KPI needs facts from two models — a ratio of defects to shipments, for example. Each model is extracted and aggregated separately, then joined **after** aggregation. Do not SQL-join raw fact tables for this.

```yaml
model: quality

base_measures:
  defects:   { sql: defect_count, agg: sum }
  shipments: { sql: shipped_qty,  agg: sum, model: shipments }

model_relations:
  - left: defects            # base measure names, not model ids
    right: shipments
    on: [event_month, region]
    how: outer               # outer (default) | inner | left | right

measures:
  defect_value:   { of: defects,   op: point, offset: { months: 0 } }
  shipment_value: { of: shipments, op: point, offset: { months: 0 } }
  defect_rate:
    op: arithmetic
    fn: percent
    left: defect_value       # measure keys, not base measure names
    right: shipment_value
```

- `on` must list the time column plus any shared dimensions. Join keys are added to the extract automatically.
- Keys missing from one side are dropped from that join, so a global extract without `region` still joins on time.
- `full` and `full_outer` are accepted as aliases for `outer`.
- Two models with no `model_relations` is an error, not an implicit cross join.

---

## 9. Model YAML (`config/models/<model_id>.yaml`)

The model says **what DuckDB reads**. Paths come from the context at runtime.

### Physical model

```yaml
model_id: sotif
kind: physical
required_aliases: [sotif]
sources:
  sotif:
    alias: sotif
    table_type: PARQUET     # PARQUET → read_parquet, DELTA → delta_scan
joins: []
```

With more than one source, declare a join. The first source is the anchor table and its columns are qualified automatically.

```yaml
sources:
  sotif:   { alias: sotif }
  regions: { alias: regions }
joins:
  - left: sotif
    right: regions
    on: [region]
    type: inner             # inner | left | right; defaults to left
```

### SQL / CTE model

Prefer this when the extract needs anything a simple join cannot express.

```yaml
model_id: sotif_multi
kind: sql
required_aliases: [sotif, regions]
output_schema:              # the columns your SELECT exposes
  - { name: event_month, type: date }
  - { name: region,      type: varchar }
  - { name: amount,      type: decimal }
default_paths:              # used only when the context omits the alias
  regions: abfss://container@account/dims/regions.parquet
sql: |
  WITH regions AS (SELECT region, eligible, weight FROM read_parquet($regions_path)),
       facts   AS (SELECT * FROM read_parquet($sotif_path))
  SELECT f.event_month, f.region, f.amount * r.weight AS amount
  FROM facts f
  INNER JOIN regions r ON f.region = r.region
  WHERE r.eligible = TRUE
```

- `$alias_path` tokens are replaced with bound parameters in the order they appear in the SQL.
- `$alias_scan` is the format-aware form: it becomes `delta_scan(?)` or `read_parquet(?)` from `datasets.*.table_type`. Prefer this in production CTEs so one model handles Delta and Parquet.
- **`output_schema` decides what can be filtered.** Only columns in your `SELECT` can take a pushed-down `IN`. A column that exists only inside a CTE (`eligible`, `weight`) is rejected with a message telling you to expose it.
- A context path always wins over `default_paths` / `sources.<alias>.default_path`. Blank YAML paths count as missing.

---

## 10. When you need a custom function

Work down this list and stop at the first row that fits.

| Your need | Answer |
|---|---|
| A different trailing length, offset, or ratio | YAML — combine existing ops |
| A messy source shape, eligibility rule, or derived column | Model YAML — `kind: sql` |
| A different aggregation of the same column | YAML — a second `base_measures` entry |
| Row maths across retrieved columns | **Column function** — register it, then `columns:` + `op:` |
| Maths over other measures' results | **Measure function** — register it, then `op: fn` + `inputs:` |
| A one-off algorithm needing the whole period series | **Hook** |

Registering a function never requires an engine change. Both registries are validated at bind time, so a typo names the registered alternatives instead of failing mid-request.

### 10.1 Column functions — `base_measures.op`

A column function receives one numeric pandas Series per entry in `columns:` and returns a Series of the same length. It runs per retrieved row, before `agg:` folds the result.

**Your signature is the contract.** The engine reads arity and parameter names off the function itself, so you choose how many columns it takes and what YAML may call them.

```python
from kpi_engine.extensions.functions import register_column_fn

def weighted_score(hits, weight):
    """Exactly two columns; both arguments are pandas Series."""
    return hits * weight * 10

def blended_score(*columns):
    """Any number of columns, added with a decaying weight."""
    return sum(column * 0.5**n for n, column in enumerate(columns))

register_column_fn("weighted_score", weighted_score)
register_column_fn("blended_score", blended_score, min_columns=2)
```

```yaml
base_measures:
  score:
    columns: [ontime, fullqty]                 # positional
    op: weighted_score
    agg: sum                                   # optional; folds the per-row results

  named_score:
    columns: { weight: fullqty, hits: ontime } # by parameter, order irrelevant
    op: weighted_score

  spread:
    columns: [q1, q2, q3, q4]                  # blended_score takes as many as you give it
    op: blended_score
```

A `*columns` function has no upper bound, so pass `min_columns` to say how few are too few — the signature alone cannot tell. Everything else is bounded by its parameters and can be fed by name. Section 4 lists the built-ins.

### 10.2 Measure functions — `measures.fn`

A measure function receives one **scalar** per entry in `inputs:` and returns one scalar (`None` for undefined). The engine computes the inputs first.

```python
from kpi_engine.extensions.functions import register_measure_fn

def safe_ratio(numerator, denominator):
    """Ratio that reports null instead of dividing by zero."""
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)

register_measure_fn("safe_ratio", safe_ratio)
```

```yaml
measures:
  otd_pct:
    op: fn
    fn: safe_ratio
    inputs: [ontime_value, total_value]

  yoy_growth:
    op: fn
    fn: growth_pct
    inputs: { current: this_year, previous: last_year }   # by parameter name
```

`inputs:` follows the same rules as `columns:`: a list binds positionally, a mapping binds by parameter name, and the arity comes from the function's signature.

Built-ins: `sum`, `subtract`, `multiply`, `min`, `max`, `avg` (any number of inputs), `divide(numerator, denominator)`, `percent(part, whole)`, and `growth_pct(current, previous)`. Aliases `add`, `sub`, `mul`, `div`, `ratio`, `share`, `yoy`, `mom`, and `percent_change` keep older files working. These are the same functions `op: arithmetic` uses; a two-argument function given a longer `of:` list is folded left to right.

### 10.3 Hooks — the last resort

Reach for a hook when the calculation needs the whole period series rather than columns or scalars: iterative maths, multiple periods combined non-linearly (cohort survival, custom allocation, bespoke smoothing), or branching rules that would be unreadable as YAML.

### Writing one

Register the function by **name**. Dotted import paths and `context.udf.module_path` are rejected on purpose — YAML may only call the allowlist.

```python
# kpi_engine/extensions/hooks.py  (or any module imported at startup)
from kpi_engine.extensions.hooks import register

def blend_mom(series, *, kpi, plan, spec, **_):
    """0.5 × current period + 0.5 × previous period."""
    current = _at(series, kpi.time.column, spec.of, plan.anchor)
    prior = _at(series, kpi.time.column, spec.of, add_months(plan.anchor, -1))
    if current is None or prior is None:
        return None
    return 0.5 * current + 0.5 * prior

register("blend_mom", blend_mom)
```

```yaml
measures:
  blended:
    op: hook
    hook: blend_mom          # must exist in REGISTRY, or bind fails
    of: sotif_value
    offset: { months: 1 }    # declares how much history to scan
```

What a hook receives and must respect:

| | |
|---|---|
| `series` | The densified frame for **one** dimension combination, already filtered and aggregated. Includes an `_observed` flag per period. |
| `kpi`, `plan`, `spec` | The parsed KPI spec, the time plan (`anchor`, `span_start`), and this measure's own YAML |
| Returns | A single JSON-safe scalar, or `None` |
| Must not | Open DuckDB, read ADLS, or reach for credentials — it runs after extraction |
| Lookback | Declare `offset:` or `trailing:` so the planner scans enough history; without either, the hook only sees the anchor period |

Hooks are the last resort, not a shortcut. If two KPIs need the same hook, promote it to a catalog op instead.

---

## 11. Null and zero semantics

The single most common review question. This is what the engine guarantees.

| Situation | `sum` / `count` | `min` / `max` / `avg` | `count_distinct` / `median` / `percentile` |
|---|---|---|---|
| Point at a period with no rows | `null` | `null` | `null` |
| Trend slot with no rows | `0` | `null` | `null` |
| Window covering some empty periods | empty periods add nothing | empty periods are skipped | recomputed from the rows that exist |
| Window where the whole span is empty | `0` | `null` | `null` |
| Arithmetic with a null operand | `null` | `null` | `null` |
| Division by zero | `null` | `null` | `null` |

The engine never emits `NaN` or `Infinity`; the whole response is valid JSON.

A row appears for a dimension combination only if `row_set` says it qualifies (§6). Dimensions not in a cut's `group_by` are present on the row as `null`, so every row across every cut has the same shape.

---

## 12. Templates

### A. Standard monthly KPI

```yaml
kpi_id: 3010
model: sotif
time:
  column: event_month
  grain: month
  filter_code: reporting_month
dimensions:
  - { name: reason_code, kind: dimension }
  - { name: region, kind: dimension }
base_measures:
  sotif_value: { sql: amount, agg: sum }
cuts:
  - { name: G, group_by: [reason_code], ignore_filters: [region], also_emit: [R] }
  - { name: R, group_by: [reason_code, region], ignore_filters: [] }
default_cut: G
measures:
  current_value:       { of: sotif_value, op: point,  offset: { months: 0 } }
  previous_year_value: { of: sotif_value, op: point,  offset: { years: 1 } }
  value_3m:            { of: sotif_value, op: window, trailing: { months: 3 }, inclusive: true }
  yoy_month:           { op: arithmetic, fn: growth_pct, left: current_value, right: previous_year_value }
  trend_12m:           { of: sotif_value, op: trend,  trailing: { months: 12 }, cuts: [G] }
```

### B. Fiscal quarterly KPI

```yaml
time:
  column: posting_date
  grain: quarter
  filter_code: reporting_quarter
  calendar: fiscal
  fiscal_start_month: 4
measures:
  current_quarter:  { of: revenue, op: point,  offset: { quarters: 0 } }
  same_quarter_ly:  { of: revenue, op: point,  offset: { years: 1 } }
  trailing_4q:      { of: revenue, op: window, trailing: { periods: 4 }, inclusive: true }
```

### C. Distinct counts and percentiles

```yaml
base_measures:
  active_suppliers: { sql: supplier_name, agg: count_distinct }
  p95_delay:        { sql: delay_days,    agg: percentile, percentile: 95 }
measures:
  suppliers_now:   { of: active_suppliers, op: point,  offset: { months: 0 } }
  suppliers_12m:   { of: active_suppliers, op: window, trailing: { months: 12 }, inclusive: true }
  delay_p95_3m:    { of: p95_delay,        op: window, trailing: { months: 3 },  inclusive: true }
```

`suppliers_12m` is the distinct count across the whole year, not the sum of twelve monthly counts.

### D. Ratio across two models

See §8.

---

## 13. Validate and test

```python
from kpi_engine import validate, compute

validate(sample_context)   # binds YAML and compiles SQL, reads no data
compute(sample_context)    # full result
```

`validate` is the fast onboarding loop: it catches unknown measure keys, unmapped filters, bad identifiers and missing aliases without touching storage.

Then add a test with a local parquet fixture (see `tests/conftest.py` for `make_context`, `minimal_kpi`, `write_yaml`, `find_row`). Never read production storage from a unit test.

```bash
pytest -q
```

---

## 14. Error message → fix

| Message | Cause | Fix |
|---|---|---|
| `Unknown measure_key(s) [...]` | Context asked for a key not under `measures:` | Add the measure, or correct metadata |
| `Missing month filter '<code>'` | `time.filter_code` does not match the context filter | Set `time.filter_code` to the actual filter key, or omit `time:` for a snapshot KPI |
| `Month filter must contain exactly one value` | The page sent a multi-select for the period | Metadata — the anchor is a single period |
| `time.grain=day requires a full date` | Day-grain KPI received `YYYY-MM` | Send `YYYY-MM-DD` |
| `Filter '<x>' has no column mapping` | No context mapping and no matching column | Add `filter_column_mappings` or `filter_map` |
| `Filter '<x>' does not bind to a source column` | Mapped to a column the extract does not expose — typically a CTE-internal one | Add it to `output_schema` |
| `Illegal measure sql: '…'` | Quotes, function calls, or `;` in `base_measures.sql` | Use `ontime * fullqty` style math only; put `SUM` in `agg:` |
| `Unknown agg '<x>'` | Aggregation not in §4 | Pick a built-in or add one to the engine |
| `agg=percentile requires percentile:` | Missing quantile | Add `percentile: 90` |
| `default_cut '<x>' is not a declared cut` | Typo in `default_cut` or `also_emit` | Match a `cuts[].name` |
| `Trend '<k>' … would emit N cells (cap 50000)` | Trend on a high-cardinality cut | Narrow `measures.<k>.cuts` |
| `Base measures span multiple models; declare model_relations` | Two models, no join declared | Add `model_relations` (§8) |
| `measures.<k> names unknown hook '<x>'` | Hook not registered | `register("<x>", fn)` at startup |
| `Filter '<x>' is hierarchical (input_text=heir)` | Hierarchy not expanded | Fix in the context builder |
| `output.page must be >= 1` | Paging from 0 | Pages are 1-based |

---

## 15. Limits

Known boundaries, so you do not design around something that is not there:

- Timestamps are bucketed as stored; there is no timezone conversion, and `time.timezone` is rejected at bind rather than silently ignored. Convert the column in a `kind: sql` model if you need it.
- `calendar: fiscal` changes `quarter` and `year` only. Fiscal *months* are ordinary calendar months.
- `trailing` counts grain periods; the unit key is an alias, not a converter (§5.2).
- `measures.*.cuts` restricts trends only.
- Physical joins support `inner`, `left` and `right`. Anything else belongs in a `kind: sql` model.
- `base_measures.sql` is a column name. Expressions belong in the model.
- KPI YAML cannot reference another KPI's measures.
- Non-additive aggregations re-read row-level data; they are the expensive option.
