# KPI YAML reference

Everything the engine can calculate, and how to declare it in YAML.

Use this document while writing `udfs/config/kpis/<kpi_id>.yaml` and `udfs/config/models/<model_id>.yaml`.

Related docs:

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

base_measures:               # internal facts; the UI does not request these
  sotif_value:
    sql: amount              # a COLUMN NAME, never an expression
    agg: sum                 # sum avg count min max count_distinct median percentile

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
| Trailing 3/6/12 periods as one number | `op: window` |
| An array for a graph | `op: trend` |
| YoY, ratio, share | `op: arithmetic` |
| A dimension echoed as a `measure_key` | `op: dimension` |
| Math the catalog cannot express | `op: hook` (see §10) |

---

## 2. How a request is calculated

Understanding this makes the YAML obvious.

1. The **anchor** is the single value of the `time.filter_code` filter, truncated to `time.grain`. It is never applied as `WHERE month IN (...)`.
2. The engine reads the **requested** `measure_key`s, works out the deepest lookback among them, and scans `[anchor − lookback, anchor]` as a **date range**. Measures nobody asked for cost nothing.
3. DuckDB scans once and groups to the finest grain any cut needs (time + all dimensions + cut keys).
4. Pandas puts the result on a **dense period spine** — every partition × every period in the span — so shifts move by the calendar, not by row position.
5. Each cut re-aggregates that spine to its own `group_by`, then every requested measure is evaluated per dimension combination.

The practical consequences:

- A missing month stays missing. "Same month last year" cannot silently pick up the row that happens to sit 12 rows back.
- One request can return several grains (`also_emit`) from a single scan.
- Adding a measure to YAML does not widen the scan until someone requests it.

---

## 3. `time` — grain, calendar, anchor

| Key | Values | Default | Notes |
|---|---|---|---|
| `column` | column name | required | Date or timestamp on the extract |
| `grain` | `day`, `month`, `quarter`, `year` | `month` | The period every measure counts in |
| `filter_code` | context filter key | required | Case- and space-insensitive (`Reporting Month` matches `reporting_month`) |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal affects `quarter` and `year` only |
| `fiscal_start_month` | 1–12 | `4` | First month of the fiscal year |
| `timezone` | string | `UTC` | Parsed and stored, **not currently applied** |

Rules the engine enforces:

- The time filter must carry **exactly one** value. Two values or zero values is an error, not a range.
- A missing time filter is an error. The engine never defaults to "latest data" or to `business_date`.
- `grain: day` requires a full `YYYY-MM-DD`. A `YYYY-MM` value is rejected.
- Truncation happens in SQL (`date_trunc`), so a mid-period date anchors on the period start.

Fiscal example — with `fiscal_start_month: 4`, March 2026 belongs to fiscal year starting **2025-04-01** and to the fiscal quarter starting **2026-01-01**.

```yaml
time:
  column: posting_date
  grain: quarter
  filter_code: reporting_quarter
  calendar: fiscal
  fiscal_start_month: 4
```

---

## 4. `base_measures` — the built-in aggregations

A base measure is the raw fact. It is **not** requestable by the UI; calculated measures point at it with `of:`.

```yaml
base_measures:
  sotif_value:
    sql: amount        # column name only — "amount * 1.2" is rejected
    agg: sum
  distinct_suppliers:
    sql: supplier_name
    agg: count_distinct
  p90_delay:
    sql: delay_days
    agg: percentile
    percentile: 90     # 90 and 0.9 both mean the 90th percentile
```

### Built-in aggregations

| `agg` | Computed in | Combines across a coarser cut by | Empty period returns |
|---|---|---|---|
| `sum` | DuckDB `SUM` | adding | `0` in windows and trends, `null` at a point |
| `count` | DuckDB `COUNT` | adding | `0` in windows and trends, `null` at a point |
| `min` | DuckDB `MIN` | taking the minimum | `null` |
| `max` | DuckDB `MAX` | taking the maximum | `null` |
| `avg` | DuckDB `SUM` + `COUNT` | adding both parts, then dividing | `null` |
| `count_distinct` | Pandas over raw rows | recomputing from rows | `null` |
| `median` | Pandas over raw rows | recomputing from rows | `null` |
| `percentile` | Pandas over raw rows | recomputing from rows | `null` |

Three behaviours worth knowing before you pick an aggregation:

**`avg` is weighted, never an average of averages.** It travels as `SUM` and `COUNT` and divides at the very end. A 3-month average over months of 2, 1 and 1 rows is `total / 4`, not `(a + b + c) / 3`.

**`min` and `max` are recomputed at every cut.** A global cut takes the minimum across regions; it does not add regional minima together.

**`count_distinct`, `median` and `percentile` are non-additive**, so the engine issues a second, row-level query for them and computes over the raw rows in the window. A 3-month distinct count is the number of distinct values across those three months — not the sum of three monthly counts. This costs more memory than an additive measure; keep the dimensionality sensible.

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

### 5.2 `window` — trailing periods as one number

```yaml
value_3m:
  of: sotif_value
  op: window
  trailing: { months: 3 }
  inclusive: true          # default
```

| `inclusive` | Window | With anchor March |
|---|---|---|
| `true` (default) | the last N periods **including** the anchor | Jan, Feb, Mar |
| `false` | N periods **ending one period before** the anchor | Dec, Jan, Feb |

> **Caution:** the number under `trailing` is a count of **grain periods**, and the unit key is cosmetic. `periods`, `months`, `days`, `quarters` and `years` are all aliases for the same integer. On a `grain: quarter` KPI, `trailing: { months: 4 }` means **4 quarters**. Prefer `trailing: { periods: 4 }` on non-monthly KPIs to avoid confusion.

The window aggregates using the base measure's own `agg`: a `min` base gives the minimum over the window, a `sum` base gives the total.

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

`cuts:` is only honoured for trend measures; on other ops it is ignored.

### 5.4 `arithmetic` — combine two measures

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct
  left: current_value       # keys of other measures
  right: previous_year_value
```

| `fn` | Result | Null / zero handling |
|---|---|---|
| `growth_pct`, `yoy`, `mom` | `(left − right) / right` | `null` if `right` is 0 or null |
| `div` | `left / right` | `null` if `right` is 0 or null |
| `percent` | `left / right × 100` | `null` if `right` is 0 or null |
| `add` | `left + right` | `null` if either side is null |
| `sub` | `left − right` | `null` if either side is null |
| `mul` | `left × right` | `null` if either side is null |

`growth_pct` returns a **ratio** (0.05 = +5%), not a pre-multiplied percentage. Use `percent` if the UI wants 5.0.

`left` and `right` may reference any other measure, including another `arithmetic` measure. The scan span automatically covers the deepest operand.

### 5.5 `dimension` — echo an attribute

```yaml
reason_code:
  kind: dimension
```

Only needed when the platform sends a dimension as a `measure_key`. The key must match a name in `dimensions:`.

### 5.6 `hook` — a registered Python function

See §10.

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

Reach for a hook only after ruling out YAML. Work down this list and stop at the first row that fits.

| Your need | Answer |
|---|---|
| A different trailing length, offset, or ratio | YAML — combine existing ops |
| A messy source shape, eligibility rule, or derived column | Model YAML — `kind: sql` |
| A different aggregation of the same column | YAML — a second `base_measures` entry |
| Maths every KPI will reuse (a new op or agg) | Engine change in `calc_engine.py` + `binder.py`, then YAML everywhere |
| A genuinely one-off algorithm on already-aggregated data | **Hook** |

Signals that you actually need a hook: the calculation is iterative, needs multiple periods in a non-linear way (cohort survival, custom allocation, a bespoke smoothing), or has branching business rules that would be unreadable as YAML.

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
| `Missing month filter '<code>'` | `time.filter_code` does not match the context filter | KPI YAML or metadata |
| `Month filter must contain exactly one value` | The page sent a multi-select for the period | Metadata — the anchor is a single period |
| `time.grain=day requires a full date` | Day-grain KPI received `YYYY-MM` | Send `YYYY-MM-DD` |
| `Filter '<x>' has no column mapping` | No context mapping and no matching column | Add `filter_column_mappings` or `filter_map` |
| `Filter '<x>' does not bind to a source column` | Mapped to a column the extract does not expose — typically a CTE-internal one | Add it to `output_schema` |
| `Illegal measure sql: 'amount * 2'. Use a simple SQL identifier.` | An expression in `base_measures.sql` | Use a column; move maths into the model |
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

- `time.timezone` is parsed but not applied; timestamps are used as stored.
- `calendar: fiscal` changes `quarter` and `year` only. Fiscal *months* are ordinary calendar months.
- `trailing` counts grain periods; the unit key is an alias, not a converter (§5.2).
- `measures.*.cuts` restricts trends only.
- Physical joins support `inner`, `left` and `right`. Anything else belongs in a `kind: sql` model.
- `base_measures.sql` is a column name. Expressions belong in the model.
- KPI YAML cannot reference another KPI's measures.
- Non-additive aggregations re-read row-level data; they are the expensive option.
