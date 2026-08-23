# KPI YAML reference

Everything the engine can calculate, and how to declare it in YAML.

Use this document while writing `udfs/config/kpis/<kpi_id>.yaml` and `udfs/config/models/<model_id>.yaml`.

Related docs:

- [udfs/kpi_engine/registries/CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) — live catalog of every op, function, and hook. New names go in `capabilities/` + `registries/` only; do not edit `core/`.
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
  grain: month               # day | week | month | quarter | year
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

# How context filters land (values still come from the request). Omit = today's IN at extract.
# filters:
#   effective_day: { column: day, op: lte, optional: true, apply: extract }

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
| YoY, ratio, same-row share, n-ary add/sub | `op: arithmetic` |
| Share of **all groups on this cut** | `op: percent_of_total` (see §5.3c) |
| Rank of groups on a cut | `op: rank` |
| A formula over retrieved columns | `expr:` on a base measure (see §4) — `+ - * /`, CASE, allowlisted calls |
| A formula over other measures | `op: expr` (see §5.6) |
| A named function over other measures' values | `op: fn` + `inputs:` (see §5.5) |
| A named function over retrieved columns | `columns:` + `op:` on a base measure (see §10.1) |
| A dimension echoed as a `measure_key` | `op: dimension` |
| Quartiles, Pareto, lag, vs-target, series stats | add-on ops / hooks — [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) |
| Math no listed kind can express | new hook under `capabilities/hooks/` + `registries/hooks.yaml` (see §10.3) |

---

## 2. How a request is calculated

Understanding this makes the YAML obvious.

1. The **anchor** is the single value of the `time.filter_code` filter, truncated to `time.grain`. It is never applied as `WHERE month IN (...)`.
2. The engine reads the **requested** `measure_key`s, works out the deepest lookback among them, and scans `[anchor − lookback, anchor]` as a **date range**. Measures nobody asked for cost nothing.
3. DuckDB retrieves **model columns only** (time bucket, dimensions, and physical columns named by KPI YAML). `filters:` with `apply: extract` become the extract `WHERE` (plus undeclared context `IN` lists). It does not run `agg:`, `op:`, or `expr:`.
4. Pandas computes every base measure on those rows, applies `apply: calc` masks, folds with `agg:`, then puts the result on a **dense period spine** so shifts move by the calendar, not by row position.
5. Each cut re-aggregates that spine to its own `group_by` (skipping `ignore_filters` on `apply: calc` filters), then every requested measure is evaluated per dimension combination. `apply: result` then drops output rows without changing the math.

The practical consequences:

- A missing month stays missing. "Same month last year" cannot silently pick up the row that happens to sit 12 rows back.
- One request can return several grains (`also_emit`) from a single scan.
- Adding a measure to YAML does not widen the scan until someone requests it.

---

## 3. `time` — grain, calendar, anchor

| Key | Values | Default | Notes |
|---|---|---|---|
| `column` | column name | required if `time:` is present | Date or timestamp on the extract |
| `grain` | `day`, `week`, `month`, `quarter`, `year` | `month` | Default period every measure counts in. `week` is ISO Monday. |
| `source_grain` | same set as `grain` | `time.grain` | Stored grain of the time column. A request pick **finer** than this is rejected (day < week < month < quarter < year). Monthly facts cannot become daily or weekly. |
| `grains` | list of grains | omitted = `{grain}` only | Allowlist for `execution.time_grain`. `time.grain` must appear in the list. |
| `filter_code` | context filter key | required if `time:` is present | Case- and space-insensitive (`Reporting Month` matches `reporting_month`). **Not** hardcoded to `reporting_month` — each KPI names its own filter. When `compose:` is set, this is the **synthetic** name after concat (need not exist on the context). |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal affects `quarter` and `year` only |
| `fiscal_start_month` | 1–12 | `4` | First month of the fiscal year |
| `format` | `yyyy-mm-dd`, `yyyy-mm`, `yyyy/mm`, `yyyymmdd`, `yyyymm`, `mmyyyy`, or a strptime string | ISO `YYYY-MM` / `YYYY-MM-DD` | How the physical column and the context time filter are stored (`062026` → `format: mmyyyy`) |
| `compose` | `{ template: "{year}{month:02}" }` | omitted | Build `filter_code` from segregated context keys. Literals between `{placeholders}` are kept (`{year}/{month:02}` → `2026/04`). `{month:02}` zero-pads. The part keys are then removed so they are not leftover `IN` filters. If `filter_code` is already on the context, that scalar wins. |

Omit the entire `time:` block when the KPI has no period column. The engine then aggregates the filtered extract as a snapshot: no month filter, no date range, no dense spine. A snapshot measure may not use a nonzero `offset`, `trailing`, or any kind that needs time (`window`, `trend`, `lag`, period hooks, …). `point` + `offset: { months: 0 }` is allowed. `constant` + `trailing` is not.

Rules the engine enforces:

- When `time:` is declared, that filter must carry **exactly one** value. Two values or zero values is an error, not a range.
- A missing time filter on a time-based KPI is an error. The engine never defaults to "latest data" or to `business_date`.
- `execution.time_grain` (adapter, not a filter) picks one allowlisted grain. Missing → `time.grain`. Not in `time.grains` (or not equal to `grain` when `grains` is omitted) → bind error. After bind, plan / DuckDB bucket / densify / `trailing.periods` use the pick.
- A day pick requires a full `YYYY-MM-DD` unless `time.format` says otherwise. Week / month / quarter / year may accept `YYYY-MM` and truncate to the pick.
- Truncation happens in SQL (ISO Monday for `week`; otherwise `date_trunc` after the column is parsed with `time.format`), so a mid-period date anchors on the period start.
- Top-level `data_points` is a positive int when `grains` is omitted or a single grain. More than one allowed grain requires a map with a positive int for **every** listed grain. `trailing: { from: data_points }` reads the pick's length.
- `meta.KPI` / `ParentKPI` / `IsChild` are echoed onto the response. `meta.SelectedMetrics` is the default projection only when the host **omits** `measures_required` / `measures_requested`. An explicit `[]` still computes nothing.
- `green_when` is exactly one of `above:` or `below:`, plus `of:`. The row flag `green` is true when the named measure is `>= above` or `<= below`. `of` is always added to the resolved graph. Response `meta` copies YAML meta plus `{above\|below, of}`.
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

When the host sends `year` / `month` (and optionally `day`) instead of one period key, concatenate them. `time.format` must parse the **result**. Lookback still widens from requested measures.

```yaml
time:
  column: posting_date
  grain: month
  filter_code: reporting_month
  format: yyyy/mm
  compose:
    template: "{year}/{month:02}"   # 2026 + / + 04 → 2026/04
```

`"{year}{month:02}"` → `202607` with `format: yyyymm`. `"{year}-{month:02}-{day:02}"` for a day grain. Do not `IN` the part keys onto the extract.

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
| `if_null` / `nullif` / `if_else` | 2 or 3 | null helpers; `if_else` is `cond`, `then`, `other` |
| `zero_if_null` / `null_if_zero` / `is_null` / `is_not_null` | 1 | fill or flag |
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

`sql:` / `expr:` name physical columns (and optional `+ - * /`, CASE, or allowlisted calls). SQL `SUM()` / subqueries are rejected; put the aggregation in `agg:`. DuckDB only SELECTs the column names; Pandas evaluates the formula.

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

Platform kinds are documented below. Add-on kinds (`ntile`, `lag`, `diff`, `top_n`, …) and hooks (`ewma`, `hit_rate`, `cagr`, …) are listed with examples in [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md). A new name is `capabilities/` + `registries/` — not `core/`.

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

### 5.2 `window` — trailing, leading, period-to-date, or full period

```yaml
value_3m:
  of: sotif_value
  op: window
  trailing: { months: 3 }
  inclusive: true          # default

value_qtd:
  of: sotif_value
  op: window
  range: qtd               # quarter start through the (offset) reference

value_ytd:
  of: sotif_value
  op: window
  range: ytd               # year start through the reference (`cumulative` is an alias)

qtd_ly:
  of: sotif_value
  op: window
  range: qtd
  offset: { years: 1 }     # same as point: offset is backwards

value_next_3m:
  of: sotif_value
  op: window
  range: leading
  trailing: { months: 3 }
  inclusive: true
```

| `range` | Window |
|---|---|
| `trailing` (default) | last N periods relative to the reference |
| `leading` | next N periods; requires `trailing:` for the length |
| `mtd` / `qtd` / `ytd` / `wtd` | calendar period start through the reference (`cumulative` = `ytd`; `wtd` needs the **effective** grain `day`) |
| `full_month` / `full_quarter` / `full_year` | whole calendar period containing the reference |

`offset` on a window shifts the reference **backwards**, then the range is applied. Named PTD / `full_*` ranges stay calendar and do not change meaning when the grain pick changes (`qtd` is still quarter-to-date; month + `qtd` is valid). `wtd` binds when `day` is allowed and fails at evaluate unless the pick is `day`. Named ranges cannot also set `trailing:`. `inclusive` applies only to trailing/leading. Fiscal vs calendar follows `time.calendar`.

| `inclusive` | Trailing window | With anchor March |
|---|---|---|
| `true` (default) | the last N periods **including** the anchor | Jan, Feb, Mar |
| `false` | N periods **ending one period before** the anchor | Dec, Jan, Feb |

`trailing` / `offset` keys `days`, `weeks`, `months`, `quarters`, `years` are **calendar** and keep that meaning after a grain pick (`trailing: { months: 3 }` on a week pick is three calendar months, not three weeks). `trailing: { periods: N }` and `trailing: { from: data_points }` count **picked grain** steps.

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

- Returns a fixed-length array; the shared x-axis is in `trend_axes` (ISO period starts) and `trend_labels` (fixed English: `23 Mar`, `2026-W30`, `Jul 2026`, `2026-Q1`, `2026`). Same keys and lengths. Labels are unique enough for a chart and are not locale-dependent.
- A period with no rows keeps its slot: `0` for `sum`/`count`, `null` for everything else.
- Trends are emitted **only on the default cut** unless `cuts:` lists more. This is deliberate — a trend on a high-cardinality cut multiplies the payload.
- Guardrail: rows × array length may not exceed **50,000 cells** per cut, otherwise the request fails and asks you to narrow `cuts`.

`cuts:` is honoured for **trend**, **rank**, and **percent_of_total**. On other ops it is ignored.

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
  partition_by: [reason_code]  # optional; group_by: is an alias
  order: desc                # desc (default) or asc
  cuts: [G]
```

Pandas `RANK()` after the cut (ties share a rank; the next rank skips). Rank is across the whole cut when `partition_by` is omitted or equals that cut's keys. A **subset** of the cut keys restarts the rank inside each group. Null sources stay null. Defaults to `default_cut` unless `cuts:` lists more.

### 5.3c `percent_of_total` — share of groups on a cut

```yaml
percent_gt:
  op: percent_of_total
  of: current_value          # or a base measure (anchor point)
  cuts: [R]
```

This is `value * 100 / SUM(value) OVER ()` on the **emitted cut rows** (after extract and cut filters). It is **not** `fn: percent`, which only sees the current row.

| YAML | Job |
|---|---|
| `cuts[].group_by` | Which rows exist (`GROUP BY reason_code, site_category`) |
| `op: percent_of_total` | Share of those rows × 100 |
| `partition_by` | Optional `OVER (PARTITION BY …)`. Omit for the whole cut. |
| `measures.*.cuts` | Emit this column on these cuts (default: `default_cut` only) |

```yaml
percent_within_site:
  op: percent_of_total
  of: current_value
  partition_by: [site_category]
  cuts: [R]
```

Null source → null. Zero or null total → null (never `inf`). Scale is 0–100. Same two-phase pass as `rank`; cannot be `left` / `inputs` / `expr` of another measure in the same request. `group_by:` is accepted as an alias for `partition_by`.

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

Identifiers are other measure keys. The engine computes them first (same memo and cycle rules as `fn`). A zero or null denominator yields null. The same CASE / `IS NULL` / allowlisted-call grammar as base `expr:` applies; calls resolve in the **measure** function registry (`zero_if_null(current_value)`, not SQL `SUM()`).

This is the **ratio of totals**. The same formula on `base_measures.expr` is the **sum of per-row ratios**. Parentheses follow normal `+ - * /` precedence.

### 5.7 `dimension` — echo an attribute

```yaml
reason_code:
  kind: dimension
```

Only needed when the platform sends a dimension as a `measure_key`. The key must match a name in `dimensions:`.

### 5.8 `hook` — an allowlisted Python function

See §10.3. The function must be listed in `registries/hooks.yaml`.

### 5.9 Add-on kinds

Cut-phase (`ntile`, `dense_rank`, `row_number`, `percent_rank`, `cumulative_share`, `running_total`, `running_avg`, `contribution`, `gap_to_leader`, `gap_to_avg`, `zscore`, `top_n`) and period-phase (`lag`, `lead`, `index`, `vs_target`, `threshold`, `diff`, `pct_change`) ops are allowlisted add-ons. YAML keys are kind-specific (`tiles`, `n`, `vs`, `cmp`, `offset`). Copy the example from [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md). Do not invent a name that is not in the registry.

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

**How `ignore_filters` works.** A filter ignored by *any* emitted cut is kept out of the DuckDB `WHERE` clause and applied per-cut in Pandas (`apply: calc`). That is what lets `region=NA` narrow the R rows while G still reports worldwide from the same scan. Declare that filter with `apply: calc` (or leave it undeclared — undeclared codes used in `ignore_filters` are deferred automatically). `apply: extract` plus `ignore_filters` is a bind error. The response reports where each filter ran under `applied_filters` (`stage` / `apply`: `extract`, `calc`, or `result`) and lists skipped ones under `ignored_filters`.

Every cut re-aggregates the spine from scratch, so a global average is a true weighted average, not a mean of regional averages.

### `row_set` — which combinations get a row

| Value | Emits a row for a dimension combination when… |
|---|---|
| `span_union` (default) | it has data **anywhere in the scanned span** |
| `anchor_only` | it has data **at the selected period** |

Use `anchor_only` when the page should list only what is active now; use `span_union` when a combination that dropped to zero this month should still show its history.

---

## 7. Filters

YEAR / MONTH belong on `time.filter_code` (one selected period → a date range plus lookback), or on `time.compose.template` when the host sends segregated keys (`year` + `month` → `202607` / `2026/04`). Do not also leave year and month as leftover `IN` filters — compose strips them.

Other predicates are **row filters**. Values come from the **context**. KPI YAML `filters:` says **how** and **at which of three stages**.

```yaml
filters:
  effective_day:
    column: day
    op: lte                 # or "<="; aliases are case-insensitive
    optional: true          # omitted or null → skip (SP: @EffectiveDay IS NULL OR …)
    apply: extract          # extract | calc | result  (default extract)

  region:
    column: region
    op: in
    apply: calc             # Pandas before measures; required when a cut ignore_filters this code

  reason_code:
    column: reason_code
    op: in
    apply: result           # drop JSON rows after measures; denominators stay unfiltered

  posting_key:
    column: posting_key
    op: eq
    apply: extract
    compose:
      template: "{year}/{month}"   # only when this is NOT the KPI time column
```

| `apply` | Where | Affects SUM / `percent_of_total`? | Use when |
|---|---|---|---|
| **`extract`** (default) | DuckDB `WHERE` on the model query | **Yes** | Physical column; every cut uses the same mask |
| **`calc`** | Pandas after retrieve, before densify / cut measures | **Yes** (same math as extract) | After maps/join, or some cuts skip it (`ignore_filters`) |
| **`result`** | After `compute_cuts`, on JSON rows | **No** | Hide groups in the payload; shares still include dropped rows |

`ignore_filters` is not a fourth apply. It only names which cuts skip a **`calc`** filter. Do not combine it with `apply: result`. Bind error if `apply: extract` and the code is in `ignore_filters`.

Undeclared context codes keep today's behaviour: `IN` at extract, unless a cut lists them in `ignore_filters` (then calc). `filter_map` still remaps a code to a column (`op: in`, `apply: extract`).

`optional: true` skips when the context omits the key or sends null. Empty `in` that is not optional matches no rows (`FALSE`). Empty `in` is not `IS NULL`.

`apply: result` may name **dimension** columns on the output row only, not measure keys.

### Operators (all three `apply` stages)

| Canonical | YAML aliases | Values |
|---|---|---|
| `in` | `IN` (default if `op` omitted) | 0+ (empty + not optional → no rows) |
| `eq` | `==`, `=`, `equals` | exactly one |
| `ne` | `<>`, `!=`, `not_equals` | exactly one |
| `lt` | `<` | exactly one |
| `lte` | `<=`, `le` | exactly one |
| `gt` | `>` | exactly one |
| `gte` | `>=`, `ge` | exactly one |
| `like` | `LIKE` | exactly one string (`%` / `_` from the host) |
| `ilike` | `ILIKE` | exactly one string |
| `not_like` | `NOT LIKE`, `notlike` | exactly one string |
| `between` | `BETWEEN` | exactly two, low then high |
| `not_between` | `NOT BETWEEN` | exactly two |
| `is_null` | `IS NULL`, `isnull` | none (context key present = apply) |
| `is_not_null` | `IS NOT NULL`, `notnull` | none |

`DAY <= @EffectiveDay` is `op: lte` with one context value. Null column values do not pass comparisons; use `is_null` to keep nulls.

You can still declare nothing when metadata already maps the filter, or the code equals the column name:

| Situation | What to do |
|---|---|
| Metadata already maps the filter to a column | Nothing — `filter_column_mappings` is used (`IN`, extract unless ignored) |
| The filter code equals the column name | Nothing — it binds by name |
| No mapping exists, or this KPI needs a different column | Add `filter_map` to the KPI YAML |
| Comparison other than `IN`, optional skip, or a chosen `apply` | Add `filters:` |

```yaml
filter_map:
  plant_code: region      # context filter_code → column name (must be an identifier)
```

`filter_map` takes precedence over context mappings for this KPI. `filters:` `column:` wins over both for that code.

Contract details:

- An unmapped filter is a **hard error**, never silently dropped.
- An empty `in` list means "nothing selected" and matches no rows (compiled as `FALSE`).
- `input_text: heir` is rejected — expand hierarchies in the context builder.
- Values are always bound as SQL parameters; nothing is concatenated into SQL text.
- Unknown `op` or wrong value count is a bind error listing the expected arity.

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
- Two models with no `model_relations` is fine when each request stays on one extract. A measure graph that spans extracts (`fn` / `arithmetic` / `of` mixing both) needs `model_relations` — it is not an implicit cross join.
- Cuts and dimensions stay on the KPI YAML. Do not set `model:` on them. A cut applies to an extract when every `group_by` column is on that retrieve; a measure that names an incompatible cut is a bind error.

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
| Row maths across retrieved columns | **Column function** — impl + `registries/functions/column.yaml`, then `columns:` + `op:` |
| Maths over other measures' results | **Measure function** — impl + `registries/functions/measure.yaml`, then `op: fn` + `inputs:` |
| A one-off algorithm needing the whole period series | **Hook** — impl + `registries/hooks.yaml` |

A new name never requires an engine change. Add the Python under `capabilities/` and a row under `registries/`. Then regenerate `registries/CAPABILITIES.md`. Both registries are validated at bind time, so a typo names the registered alternatives instead of failing mid-request. See [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md).

This registry does **not** cover filter operators, compose templates, time format aliases, or aggregations — those stay platform code in `core/`.

### 10.1 Column functions — `base_measures.op`

A column function receives one numeric pandas Series per entry in `columns:` and returns a Series of the same length. It runs per retrieved row, before `agg:` folds the result.

**Your signature is the contract.** The engine reads arity and parameter names off the function itself, so you choose how many columns it takes and what YAML may call them.

```python
# capabilities/functions/column/impl.py
def weighted_score(hits, weight):
    """Exactly two columns; both arguments are pandas Series."""
    return hits * weight * 10

def blended_score(*columns):
    """Any number of columns, added with a decaying weight."""
    return sum(column * 0.5**n for n, column in enumerate(columns))
```

```yaml
# registries/functions/column.yaml
weighted_score:
  role: addon
  enabled: true
  description: Hits × weight × 10.
  example: |
    score:
      columns: { hits: ontime, weight: fullqty }
      op: weighted_score
  module: kpi_engine.capabilities.functions.column.impl
  attr: weighted_score

blended_score:
  role: addon
  enabled: true
  min_args: 2
  description: Decaying weighted sum of columns.
  example: |
    spread:
      columns: [q1, q2, q3, q4]
      op: blended_score
  module: kpi_engine.capabilities.functions.column.impl
  attr: blended_score
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

A `*columns` function has no upper bound, so set `min_args:` in the YAML (default **2** when omitted on a variadic signature). Everything else is bounded by its parameters and can be fed by name. Section 4 lists the built-ins.

### 10.2 Measure functions — `measures.fn`

A measure function receives one **scalar** per entry in `inputs:` and returns one scalar (`None` for undefined). The engine computes the inputs first.

```python
# capabilities/functions/measure/impl.py
def safe_ratio(numerator, denominator):
    """Ratio that reports null instead of dividing by zero."""
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)
```

```yaml
# registries/functions/measure.yaml
safe_ratio:
  role: addon
  enabled: true
  description: Ratio that is null when the denominator is 0.
  example: |
    otd_pct:
      op: fn
      fn: safe_ratio
      inputs: [ontime_value, total_value]
  module: kpi_engine.capabilities.functions.measure.impl
  attr: safe_ratio
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
# udfs/kpi_engine/capabilities/hooks/blend.py
def blend_mom(series, *, kpi, plan, spec, **_):
    """0.5 × current period + 0.5 × previous period."""
    current = _at(series, kpi.time.column, spec.of, plan.anchor)
    prior = _at(series, kpi.time.column, spec.of, add_months(plan.anchor, -1))
    if current is None or prior is None:
        return None
    return 0.5 * current + 0.5 * prior
```

Then allowlist it in `registries/hooks.yaml` (`module` / `attr`, plus `requires_value` or `extra_keys` if needed). Do not edit `core/`.

```yaml
measures:
  blended:
    op: hook
    hook: blend_mom          # must be allowlisted in registries/hooks.yaml
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
| `Missing month filter '<code>'` | `time.filter_code` does not match the context filter | Set `time.filter_code` to the actual filter key, add `time.compose.template`, or omit `time:` for a snapshot KPI |
| `Compose placeholder 'year' is missing` | A `{placeholder}` was not on the context | Send that filter, or send the composed `filter_code` as one scalar |
| `Month filter must contain exactly one value` | The page sent a multi-select for the period | Metadata — the anchor is a single period |
| `time.grain=day requires a full date` | Day-grain KPI received `YYYY-MM` | Send `YYYY-MM-DD` |
| `Filter '<x>' has no column mapping` | No context mapping and no matching column | Add `filter_column_mappings` or `filter_map` |
| `Unknown filter op '…'` | YAML `filters.*.op` is not in the operator table | Use a name or alias from §7 |
| `Filter '…' op 'between' expects 2 value(s)` | Wrong arity | `between` needs `[low, high]`; `eq` needs one value; `is_null` needs none |
| `filters.x apply: extract cannot be listed in ignore_filters` | DuckDB cannot skip the mask on cut G | Use `apply: calc` |
| `Required filter '…' is missing from context` | `optional: false` (default) and the host omitted it | Send the filter, or set `optional: true` |
| `Filter '<x>' does not bind to a source column` | Mapped to a column the extract does not expose — typically a CTE-internal one | Add it to `output_schema` |
| `Illegal measure sql: '…'` | Comments, `;`, double quotes, or incomplete CASE | Use `+ - * /`, CASE, or an allowlisted call; put `SUM` in `agg:` |
| `names unknown function` | Call is not in that layer's registry | Use a name from [CAPABILITIES.md](udfs/kpi_engine/registries/CAPABILITIES.md) |
| `Unknown agg '<x>'` | Aggregation not in §4 | Pick a built-in or add one to the engine |
| `agg=percentile requires percentile:` | Missing quantile | Add `percentile: 90` |
| `default_cut '<x>' is not a declared cut` | Typo in `default_cut` or `also_emit` | Match a `cuts[].name` |
| `Unknown op/kind 'percent_of_cut_total'` | That name is not an op | Use `op: percent_of_total` |
| `measures.<k> op=percent_of_total requires of:` | Missing source | Point at a measure or base_measure |
| `partition_by '…' is not a cut group_by` | Window key is not a dimension | Use a name from `dimensions` / `cuts[].group_by` |
| `measures.<k> spans models … declare model_relations` | A requested graph reads two extracts with no join | Add `model_relations` (§8), or request one extract at a time |
| `measures.<k> cuts '…' is not on this extract` | That cut's `group_by` is not on the base's model | Set `cuts:` to a grain those columns have, or retrieve them |
| `measures.<k> names unknown hook '<x>'` | Hook not in `registries/hooks.yaml` | Add the function under `capabilities/hooks/` and a registry row |
| `Filter '<x>' is hierarchical (input_text=heir)` | Hierarchy not expanded | Fix in the context builder |
| `output.page must be >= 1` | Paging from 0 | Pages are 1-based |

---

## 15. Limits

Known boundaries, so you do not design around something that is not there:

- Timestamps are bucketed as stored; there is no timezone conversion, and `time.timezone` is rejected at bind rather than silently ignored. Convert the column in a `kind: sql` model if you need it.
- `calendar: fiscal` changes `quarter` and `year` only. Fiscal *months* are ordinary calendar months.
- `trailing` / `offset` calendar keys (`days`, `weeks`, `months`, `quarters`, `years`) do not change meaning when `execution.time_grain` changes. `periods` and `from: data_points` follow the pick (§5.2).
- A day pick plus a large `data_points` widens the extract spine; the 50,000 trend-cell cap still applies.
- `measures.*.cuts` restricts **trend**, **rank**, **percent_of_total**, and other cut-phase kinds (`ntile`, `top_n`, …).
- `percent_of_total` windows **this cut's** rows only. Share of a different cut's total is a different problem (`ignore_filters` / `also_emit`, or a later op).
- `filters:` is one mask for the pipeline (then cuts / `ignore_filters`). There is no per-measure filter block.
- `rank` and `percent_of_total` cannot feed `arithmetic` / `fn` / `expr` in the same request.
- Physical joins support `inner`, `left` and `right`. Anything else belongs in a `kind: sql` model.
- `base_measures.sql` is a column name. Expressions belong in the model.
- KPI YAML cannot reference another KPI's measures.
- Non-additive aggregations re-read row-level data; they are the expensive option.
- `expr:` CASE is Pandas, not DuckDB. No `SUM(CASE)`, `LIKE`, `IN ('A','B')`, or simple `CASE status WHEN 'O'`. `columns:` + `op:` stays numeric.
