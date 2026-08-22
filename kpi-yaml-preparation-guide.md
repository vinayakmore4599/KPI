# KPI YAML preparation guide

How to write `udfs/config/kpis/<kpi_id>.yaml` (and the model it points at) so the host can request measures and the engine can compute them.

This is the **write-it** document: what to declare, which function to pick, how columns and expressions work, and what the engine will not do.

Related docs:

- [kpi-onboarding-guide.md](kpi-onboarding-guide.md) — process (which files to change)
- [kpi-yaml-reference.md](kpi-yaml-reference.md) — full key-by-key reference
- [README.md](README.md) — folders and request path

---

## 1. Two files, two jobs

| File | Job | Never put here |
|---|---|---|
| `udfs/config/models/<model_id>.yaml` | What DuckDB reads (physical tables/joins or a SQL/CTE extract) | KPI math, `agg:`, `op:`, `expr:` |
| `udfs/config/kpis/<kpi_id>.yaml` | Time, dimensions, base facts, cuts, requestable measures | ADLS paths, Python, free SQL |

**KPI YAML never becomes DuckDB SQL.** DuckDB only retrieves physical columns (time, dimensions, and columns named by `sql:` / `columns:` / `expr:`). Pandas then builds every base fact and every requested measure.

**`model:` on the KPI must equal `model_id:` on the model file.** Folding covers case, spaces, and underscores (`Sotif` = `sotif`). It does **not** treat `sotif` and `sotif_sql` as the same id. If the model file says `model_id: sotif_sql`, the KPI must say `model: sotif_sql`.

---

## 2. How a request is calculated (so YAML makes sense)

1. The host sends `execution.kpi_id` and a list of keys (`measures_required` or `measures_requested`).
2. Those keys must exist under `measures:`. An empty list computes **nothing** — it does not run the whole catalog.
3. The engine walks dependencies (`of:`, `left`/`right`, `inputs:`, `expr:`) and extracts only the base facts that graph needs.
4. The time filter is the **anchor** (exactly one value). It is never `WHERE month IN (...)`. Lookback widens the scan from the requested graph (`previous_year_value` → 12 months).
5. DuckDB returns physical columns. Pandas folds host spellings (`Amount` → `amount`), applies `op:` / `expr:`, then `agg:`.
6. Each cut re-aggregates, then each requested measure is evaluated per dimension combination.

Host names fold onto YAML names: `Previous Year Value`, `previous_year_value`, and `previousyearvalue` are the same key.

---

## 3. When to use what

Work down this list and stop at the first row that fits.

### 3.1 Choosing the layer

| I need… | Use | Do not use |
|---|---|---|
| Tables / joins / eligibility / timezone conversion / messy source shape | **Model YAML** (`kind: physical` or `kind: sql`) | KPI `sql:` as free SQL |
| One physical column, then fold rows | `base_measures` + `sql:` + `agg:` | A measure `op:` for the raw fact |
| Row math across retrieved columns (product, ratio, coalesce) | `base_measures` + `columns:` + `op:` | `agg:` of two columns independently if you wanted the product first |
| Nested `+ - * /` on retrieved columns | `base_measures` + `expr:` | SQL functions (`SUM`, `COALESCE`) in KPI YAML |
| One period’s value (current, last year, last quarter) | `measures` + `op: point` | A window of length 1 unless you really want window null/zero rules |
| Trailing / leading / YTD total or avg | `op: window` | Summing several `point` measures by hand |
| A graph series | `op: trend` | Returning many `point` keys |
| YoY, ratio, share, add/sub of **measures** | `op: arithmetic` or `op: fn` | Repeating the same formula on `base_measures` |
| Nested formula over **other measures** | `op: expr` | `base_measures.expr` (that is per-row, then aggregated) |
| A fixed target / goal | `op: constant` | Hard-coding the number in every `expr` |
| Rank reasons / regions | `op: rank` | Sorting in the host after the fact if you need engine ranks |
| Echo a dimension as a `measure_key` | `op: dimension` | A fake numeric measure |
| Math the catalog cannot express (whole series, iterative) | `op: hook` (registered) | `eval()`, import paths, or editing the engine per KPI |

### 3.2 Columns vs expressions (the usual confusion)

There are **two** expression layers. They look similar and mean different things.

| Layer | YAML | Identifiers are | When to use | Result |
|---|---|---|---|---|
| Base fact | `base_measures.*.expr:` or `sql: a * b` | **Physical columns** on the extract | Combine columns **per retrieved row**, then `agg:` | e.g. **sum of per-row ratios** |
| Requestable measure | `measures.*.op: expr` | **Other measure keys** | Combine already-aggregated values | e.g. **ratio of two totals** |

```yaml
# Per-row then sum  —  (a/b) + (a/b) + …
base_measures:
  line_ratio:
    expr: shipped / ordered
    agg: sum

# Ratio of totals  —  sum(shipped) / sum(ordered)
base_measures:
  shipped_qty: { sql: shipped, agg: sum }
  ordered_qty: { sql: ordered, agg: sum }
measures:
  shipped_now: { of: shipped_qty, op: point, offset: { months: 0 } }
  ordered_now: { of: ordered_qty, op: point, offset: { months: 0 } }
  fill_rate:
    op: expr
    expr: shipped_now / ordered_now
```

Prefer `columns:` + `op:` when a **named function** is clearer than a formula (`multiply`, `divide`, `coalesce`). Prefer `expr:` when you need parentheses and mixed `+ - * /`.

Do **not** mix `expr:` with `columns:` / `op:` / `sql:` on the same base measure.

### 3.3 Named function vs `expr:` vs hook

| Situation | Pick |
|---|---|
| Built-in row op (`multiply`, `divide`, `coalesce`, …) | `columns:` + `op:` |
| Built-in measure op (`growth_pct`, `percent`, `subtract`, …) | `op: arithmetic` (`left`/`right` or `of: [a, b]`) |
| Same built-in, but operands must not be swapped | `op: fn` + `inputs: { current: …, previous: … }` |
| Nested formula over measures | `op: expr` |
| Custom row math you will reuse | Register a **column function**, then `op:` |
| Custom scalar math you will reuse | Register a **measure function**, then `op: fn` |
| Needs the whole densified series | Register a **hook**, then `op: hook` |

---

## 4. Skeleton — copy and fill

```yaml
kpi_id: 3004                 # must match execution.kpi_id
version: 1
model: sotif                 # MUST equal models/<file>.yaml model_id:

time:
  column: event_month        # date column on the extract
  grain: month               # day | month | quarter | year
  filter_code: reporting_month
  calendar: gregorian        # gregorian | fiscal
  # format: yyyymm           # if the column / filter is 202607, not 2026-07

dimensions:
  - { name: reason_code, kind: dimension }
  - { name: region, kind: dimension }

base_measures:
  sotif_value:
    sql: amount
    agg: sum

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

measures:
  current_value:
    of: sotif_value
    op: point
    offset: { months: 0 }
```

Required:

- `kpi_id`, `model`, at least one `cut`, at least one `measure`
- Either `time:` (period KPIs) or omit it entirely (snapshot KPIs)
- Every host `measure_key` declared under `measures:`

---

## 5. `time`

| Key | Values | Default | When to set |
|---|---|---|---|
| `column` | identifier | required if `time:` is present | The date/timestamp DuckDB retrieves |
| `grain` | `day`, `month`, `quarter`, `year` | `month` | The period every measure counts in |
| `filter_code` | context filter key | required if `time:` is present | The **selected period**. Not hardcoded to `reporting_month` |
| `calendar` | `gregorian`, `fiscal` | `gregorian` | Fiscal changes `quarter` and `year` only |
| `fiscal_start_month` | 1–12 | `4` | Only when `calendar: fiscal` |
| `format` | see below | ISO `YYYY-MM` / `YYYY-MM-DD` | When the column or filter is not ISO |

**`format` aliases:** `yyyy-mm-dd`, `yyyy-mm`, `yyyymmdd`, `yyyymm`, `mmyyyy`, `mm-yyyy`, `dd-mm-yyyy`, `dd/mm/yyyy`, or a strptime string (`%d/%m/%Y`).

```yaml
time:
  column: current_month
  grain: month
  filter_code: current_month
  format: yyyymm            # 202607
```

Rules:

- The time filter must carry **exactly one** value. Two values is not a range.
- A missing time filter on a time-based KPI is an error. The engine never defaults to “latest” or `business_date`.
- `time.timezone` is rejected. Convert in a `kind: sql` model if needed.
- Omit the whole `time:` block for a snapshot KPI. Then only `point` (no offset), `dimension`, `arithmetic`, `fn`, `expr`, `constant`, `rank`, and hooks **without** lookback are allowed.

---

## 6. `dimensions`

Attributes that split rows. Not numbers.

```yaml
dimensions:
  - reason_code                          # shorthand
  - { name: region, kind: dimension }
  - name: order_status                   # rewrite after retrieve
    from: o_orderstatus
    map: { O: Open, P: Processing, F: Fulfilled }
    default: Other
  - name: event_quarter                  # truncate a date column
    from: event_month
    grain: quarter                       # day | month | quarter | year
```

`from` + `map` / `grain` run in Pandas after retrieve. Joins stay in the model YAML.

---

## 7. `base_measures` — internal facts

The UI does **not** request these names. Measures point at them with `of:`.

Each entry needs **one** of: `sql:`, `columns:` + `op:`, or `expr:`. Then `agg:` folds rows.

### 7.1 One column

```yaml
base_measures:
  sotif_value:
    sql: amount              # physical column DuckDB SELECTs
    agg: sum                 # Pandas SUM after retrieve
```

`sql: amount * weight` is allowed as a **column expression** (same grammar as `expr:`). DuckDB still only SELECTs `amount` and `weight`; Pandas multiplies.

### 7.2 Named column functions (`columns:` + `op:`)

Use when a registered function should combine columns **on each row**, then `agg:` folds the result.

```yaml
base_measures:
  line_value:
    columns: [ontime, fullqty]
    op: multiply             # aliases: mul, product
    agg: sum                 # SUM of products, not product of SUMs

  fill_rate:
    columns: { numerator: shipped_qty, denominator: ordered_qty }
    op: divide               # named so order cannot invert
    agg: avg

  first_qty:
    columns: [primary_qty, fallback_qty]
    op: coalesce
    agg: sum
```

A list binds positionally. A `{parameter: column}` mapping binds by name (prefer this for `divide` / `percent_of`).

### 7.3 Built-in column functions (`base_measures.op`)

| `op` | Aliases | Arguments | What it does |
|---|---|---|---|
| `value` | `identity` | 1 column | Pass through |
| `abs` | | 1 column | Absolute value |
| `sum` | `add` | 2+ columns | `a + b + c …` **across one row** |
| `subtract` | `sub` | 2+ columns | `a - b - c …` |
| `multiply` | `mul`, `product` | 2+ columns | `a * b * c …` |
| `divide` | `div`, `ratio` | `numerator`, `denominator` | `/`; zero denominator → null |
| `percent_of` | `share` | `part`, `whole` | ratio × 100 |
| `min` | | 2+ columns | Row-wise min |
| `max` | | 2+ columns | Row-wise max |
| `avg` | `mean` | 2+ columns | Row-wise mean |
| `coalesce` | | 2+ columns | First non-null, left to right |

`op: sum` reduces **across columns of one row**. `agg: sum` reduces **down the rows of a period**. A base measure often uses both.

Unknown `op`, wrong arity, or a bad parameter name fails at **bind** and lists the registered names.

### 7.4 `expr:` on a base measure

Allowed grammar: identifiers, numbers, `+ - * /`, parentheses. No function calls, no quotes, no comments, no `;`.

```yaml
base_measures:
  harmonic:
    expr: (col_a * col_b) / (col_a + col_b)
    agg: sum
```

Identifiers must be simple SQL names: `[A-Za-z_][A-Za-z0-9_]*`.

### 7.5 `agg:` — how rows fold

| `agg` | Additive? | Empty point | Empty window/trend slot | When to use |
|---|---|---|---|---|
| `sum` | yes | null | `0` | Amounts, counts of events |
| `count` | yes | null | `0` | Row counts |
| `avg` | yes (as sum+count) | null | null | Weighted average (never average of averages) |
| `min` | recomputed | null | null | Worst / earliest numeric |
| `max` | recomputed | null | null | Best / latest numeric |
| `count_distinct` | no — re-reads rows | null | null | Distinct suppliers, SKUs |
| `median` | no | null | null | Typical value |
| `percentile` | no | null | null | Needs `percentile: 90` (or `0.9`) |
| `first` | no | null | null | First event in the period |
| `last` | no | null | null | Snapshot balance (not a sum of days) |

`avg` travels as SUM and COUNT and divides at the end. A 3-month average over months of 2, 1, and 1 rows is `total / 4`, not `(a+b+c)/3`.

Non-additive aggs keep retrieved rows and recompute per cut/window. They cost more memory.

### 7.6 `where:` — row mask (Pandas, not SQL)

```yaml
base_measures:
  open_amount:
    columns: [amount]
    agg: sum
    where:
      column: status
      op: in                 # in | eq | ne
      values: [O]
```

`eq` / `ne` also accept `value:` (singular).

### 7.7 Multi-model facts

```yaml
base_measures:
  defects:   { sql: defect_count, agg: sum }
  shipments: { sql: shipped_qty,  agg: sum, model: shipments }
```

If more than one model is used, declare `model_relations` (see §11).

---

## 8. `measures` — what the host can request

Every host `measure_key` must be a key here. `op:` and `kind:` are the same field.

| `op` | Needs | Returns | When to use |
|---|---|---|---|
| `point` | `of:` base, `offset:` | scalar | One period |
| `window` | `of:` base, `trailing:` or `range:` | scalar | 3m / 6m / 12m / YTD / next N |
| `trend` | `of:` base, `trailing:` | array | Graph |
| `arithmetic` | `fn:` + `left`/`right` or `of: [a, b, …]` | scalar | YoY, ratio, n-ary math |
| `fn` | `fn:` + `inputs:` | scalar | Named function over measures |
| `expr` | `expr:` | scalar | Nested `+ - * /` over measures |
| `constant` | `value:` | scalar | Target / goal |
| `rank` | `of:` measure or base | scalar | Rank within a cut |
| `dimension` | key ∈ `dimensions:` | attribute | Host sent a dim as a measure |
| `hook` | `hook:` + usually `of:` | scalar | Last-resort custom series math |

### 8.1 `point`

```yaml
current_value:
  of: sotif_value
  op: point
  offset: { months: 0 }

previous_year_value:
  of: sotif_value
  op: point
  offset: { years: 1 }       # 1 year BEFORE the anchor
```

`offset` counts **backwards**. Units: `days`, `months`, `quarters`, `years` (they add: `{ years: 1, months: 2 }` = 14 months). Missing period → `null`.

### 8.2 `window`

```yaml
value_3m:
  of: sotif_value
  op: window
  trailing: { months: 3 }
  inclusive: true            # default: last 3 including anchor

value_ytd:
  of: sotif_value
  op: window
  range: cumulative          # year start → anchor (calendar or fiscal)

value_next_3m:
  of: sotif_value
  op: window
  range: leading
  trailing: { months: 3 }
```

| `range` | Window |
|---|---|
| `trailing` (default) | last N periods vs anchor |
| `leading` | next N periods vs anchor |
| `cumulative` | start of calendar/fiscal year through anchor |

`inclusive: false` ends one period **before** the anchor.

`trailing` units: `periods`, `months`, `days`, `quarters`, `years`. The unit is a count of KPI-grain periods (except `days`, which is a calendar-day window). It is not a unit converter.

The window uses the base measure’s own `agg`.

### 8.3 `trend`

```yaml
trend_12m:
  of: sotif_value
  op: trend
  trailing: { months: 12 }
  inclusive: true
  cuts: [G]                  # default: default_cut only
```

Fixed-length array; shared x-axis in `trend_axes`. Empty `sum`/`count` slots are `0`; others `null`. Cap: **50,000 cells** (rows × length) per cut.

`cuts:` is honoured for **trend** and **rank** only.

### 8.4 `arithmetic` — built-in measure functions

```yaml
yoy_month:
  op: arithmetic
  fn: growth_pct             # aliases: yoy, mom, percent_change
  left: current_value
  right: previous_year_value

net_value:
  op: arithmetic
  fn: subtract
  of: [gross_value, opex_value]
```

| `fn` | Aliases | Parameters | Result | Null / zero |
|---|---|---|---|---|
| `growth_pct` | `yoy`, `mom`, `percent_change` | `current`, `previous` | `(current − previous) / previous` | null if previous is 0 or null |
| `divide` | `div`, `ratio` | `numerator`, `denominator` | ratio | null if denominator is 0 or null |
| `percent` | `percent_of`, `share` | `part`, `whole` | ratio × 100 | null if whole is 0 or null |
| `sum` | `add` | 2+ | `a + b + …` | null if **any** operand is null |
| `subtract` | `sub` | 2+ | `a − b − …` | null if any is null |
| `multiply` | `mul`, `product` | 2+ | `a × b × …` | null if any is null |
| `min` | | 2+ | min of non-nulls | null only if all null |
| `max` | | 2+ | max of non-nulls | null only if all null |
| `avg` | `mean` | 2+ | mean of non-nulls | null only if all null |

`growth_pct` is a **ratio** (`0.05` = +5%). Use `percent` if the UI wants `5.0`.

A two-argument function given `of: [a, b, c]` is folded left to right.

### 8.5 `fn`

Same registry as `arithmetic`. Use `inputs:` (list or `{parameter: measure}`).

```yaml
yoy_growth:
  op: fn
  fn: growth_pct
  inputs: { current: current_value, previous: previous_year_value }
```

Cycles (`a → b → a`) fail at bind. Shared inputs are computed once.

### 8.6 `expr` (measure level)

```yaml
agg_ratio:
  op: expr
  expr: (a_value * b_value) / (a_value + b_value)
```

Same `+ - * / ( )` grammar. Identifiers are **measure keys**. Zero or null denominator → null.

### 8.7 `constant` / `rank` / `dimension`

```yaml
target:
  op: constant
  value: 0.98

reason_code_rank:
  op: rank
  of: current_value          # or a base measure (treated as anchor point)
  group_by: [reason_code]
  order: desc                # desc (default) | asc
  cuts: [G]

reason_code:
  kind: dimension            # key must match dimensions:
```

Rank uses Pandas `RANK()` (ties share; next rank skips). Null sources stay null.

---

## 9. `cuts` and `row_set`

A cut is a grouping grain, not a number.

```yaml
cuts:
  - name: G
    group_by: [reason_code]
    ignore_filters: [region]   # G is worldwide even if the page selected a region
    also_emit: [R]
  - name: R
    group_by: [reason_code, region]
    ignore_filters: []

default_cut: G
row_set: span_union
```

| Key | Meaning |
|---|---|
| `group_by` | Dimensions this cut groups by |
| `ignore_filters` | Kept out of DuckDB `WHERE` and applied per-cut in Pandas |
| `also_emit` | Extra cuts in the same response |
| `row_set: span_union` | Row if the combo has data **anywhere in the scan** |
| `row_set: anchor_only` | Row only if it has data **at the selected period** |

Use `anchor_only` when the page should list only what is active now.

---

## 10. Filters

You normally declare nothing. Context `filter_column_mappings` or a matching column name is enough.

```yaml
filter_map:
  plant_code: region         # this KPI only; wins over context mappings
```

- Unmapped filter → hard error (never dropped).
- Empty value list → no rows (`FALSE`).
- `input_text: heir` / `hier` is rejected; expand hierarchies in the context builder.
- Values are SQL parameters; nothing is concatenated into SQL text.

---

## 11. Multi-model (`model_relations`)

Each model is extracted and aggregated, then joined. Do not SQL-join raw facts for a ratio of two grains.

```yaml
model: quality

base_measures:
  defects:   { sql: defect_count, agg: sum }
  shipments: { sql: shipped_qty,  agg: sum, model: shipments }

model_relations:
  - left: defects            # base measure names
    right: shipments
    on: [event_month, region]
    how: outer               # outer (default) | inner | left | right
                             # full / full_outer → outer

measures:
  defect_value:   { of: defects,   op: point, offset: { months: 0 } }
  shipment_value: { of: shipments, op: point, offset: { months: 0 } }
  defect_rate:
    op: arithmetic
    fn: percent
    left: defect_value
    right: shipment_value
```

Two models with no `model_relations` is an error (not a cross join).

---

## 12. Model YAML (what DuckDB reads)

File: `udfs/config/models/<model_id>.yaml`. The KPI `model:` value must match `model_id:`.

### Physical

```yaml
model_id: sotif
kind: physical
required_aliases: [sotif]
sources:
  sotif:
    alias: sotif
    table_type: PARQUET      # PARQUET | DELTA
joins: []
```

Joins: `inner` | `left` | `right` (default `left`). Anything richer belongs in `kind: sql`.

### SQL / CTE

```yaml
model_id: sotif_sql
kind: sql
required_aliases: [sotif]
output_schema:
  - { name: current_month, type: date }
  - { name: region, type: varchar }
  - { name: amount, type: decimal }
sql: |
  SELECT current_month, region, amount
  FROM read_parquet($sotif_path)
```

- `$alias_path` — bound path parameter.
- `$alias_scan` — `read_parquet(?)` or `delta_scan(?)` from `table_type`.
- Context IN filters and the time range wrap the **final SELECT**, not inner CTEs.
- Only `output_schema` columns can take a pushed-down `IN`.
- Context path wins over `default_paths`.

---

## 13. Custom functions (only when YAML is not enough)

Register at process startup. YAML may only name allowlisted entries — never a dotted import path.

### Column function — `base_measures.op`

Receives one numeric pandas Series per `columns:` entry; must return a Series of the same length.

```python
from kpi_engine.extensions.functions import register_column_fn

def weighted_score(hits, weight):
    return hits * weight * 10

register_column_fn("weighted_score", weighted_score)
```

```yaml
base_measures:
  score:
    columns: { hits: ontime, weight: fullqty }
    op: weighted_score
    agg: sum
```

### Measure function — `measures.fn` / `arithmetic`

Receives one scalar per `inputs:`; return a number or `None`.

```python
from kpi_engine.extensions.functions import register_measure_fn

def safe_ratio(numerator, denominator):
    if numerator is None or not denominator:
        return None
    return float(numerator) / float(denominator)

register_measure_fn("safe_ratio", safe_ratio)
```

### Hook — last resort

Needs the densified period series. Register in `kpi_engine.extensions.hooks`. Declare `offset:` or `trailing:` so the planner scans enough history. Must not open DuckDB or read storage.

---

## 14. Null and zero

The engine never emits `NaN` or `Infinity`.

| Situation | `sum` / `count` | Other aggs |
|---|---|---|
| Point, no rows | `null` | `null` |
| Trend slot, no rows | `0` | `null` |
| Window, some empty periods | those periods add nothing | skipped / recomputed from rows that exist |
| Window, entire span empty | `0` | `null` |
| Arithmetic, any null (sum/sub/mul/div) | `null` | `null` |
| Divide by zero | `null` | `null` |

Dimensions not in a cut’s `group_by` appear as `null` so every row has the same shape.

---

## 15. Current limitations

Design around these; they are intentional.

**Identity and request**

- KPI `model:` must equal the model file `model_id:` (fold: case / space / underscore only). `sotif` ≠ `sotif_sql`.
- Empty `measures_required` / `measures_requested` computes nothing. It does not expand to every YAML measure.
- Host keys fold onto YAML keys; unknown keys fail at bind with the valid list.
- KPI YAML cannot reference another KPI’s measures.

**SQL vs Pandas**

- KPI formulas never enter DuckDB. No `SUM()`, `COALESCE()`, `CASE`, comments, or quotes in `sql:` / `expr:`.
- Expressions are only `+ - * / ( )`, identifiers, and numbers. Function-call syntax is rejected.
- Identifiers must match `[A-Za-z_][A-Za-z0-9_]*` (no dots, hyphens, or quoted names).
- `expr:` cannot be combined with `columns:` / `op:` / `sql:` on the same base measure.

**Time**

- No timezone conversion; `time.timezone` is rejected.
- `calendar: fiscal` affects `quarter` and `year` only. Fiscal months are calendar months.
- Time filter is a single anchor, not a range.
- Snapshot KPIs (no `time:`) cannot use windows, trends, or nonzero offsets.
- `trailing` counts grain periods; the unit key is not a converter.

**Cuts and payload**

- `measures.*.cuts` restricts **trend** and **rank** only; other ops ignore it.
- Trends default to `default_cut` only (high-cardinality trends explode the payload).
- Trend cells capped at 50,000 per cut.
- Physical joins: `inner`, `left`, `right` only.

**Filters and context**

- Hierarchies (`input_text: heir`) are not expanded here.
- `business_date` is ignored.
- Unmapped filters error; they are never silently dropped.
- Exactly one `execution.view_details` entry.

**Aggregation**

- Non-additive aggs (`count_distinct`, `median`, `percentile`, `first`, `last`) re-read row-level data.
- `percentile` requires `percentile:`.
- Pandas fold of a column `op` only supports `sum`, `avg`, `count`, `min`, `max`, `first`, `last` when collapsing detail; `count_distinct` / `median` / `percentile` stay on the row path.

**Extensions**

- Custom logic is allowlisted (`register_column_fn`, `register_measure_fn`, `hooks.register`). Dotted import paths and `context.udf.module_path` are rejected.

---

## 16. Validate

```python
from kpi_engine import validate, compute

validate(sample_context)   # bind + compile SQL, no data
compute(sample_context)    # full result
```

`validate` catches unknown measure keys, unmapped filters, bad identifiers, unknown `op`/`fn`, and missing aliases.

```bash
pytest -q
```

Use local parquet fixtures (`tests/conftest.py`: `make_context`, `write_yaml`). Do not read production storage from unit tests.

---

## 17. Error → fix

| Message | Fix |
|---|---|
| `No base_measures attach to model '…'` | Set KPI `model:` to the model file `model_id:` |
| `Unknown measure_key(s)` | Add the key under `measures:`, or correct the host list |
| `Missing month filter` | Set `time.filter_code` to the real filter, or omit `time:` |
| `Month filter must contain exactly one value` | Anchor is one period, not a multi-select |
| `Filter '…' has no column mapping` | Add context mapping or `filter_map` |
| `Illegal measure sql` / function calls not allowed | Use column names and `+ - * /` only; put `SUM` in `agg:` |
| `unknown op` / `unknown fn` | Use a name from §7.3 / §8.4, or register one |
| `uses expr: and cannot also set columns/op/sql` | Pick one style per base measure |
| `Base measures span multiple models` | Add `model_relations` |
| `dependency cycle` | Break the `fn` / `expr` / `arithmetic` loop |
| `input_text=heir` | Expand the hierarchy in the context builder |
| `time.timezone is not supported` | Convert in a `kind: sql` model |
| `Trend … cap 50000` | Narrow `cuts:` on that trend |

---

## 18. Worked examples

### Monthly KPI with YoY and windows

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
  trend_12m:           { of: sotif_value, op: trend, trailing: { months: 12 }, cuts: [G] }
```

### Row product then sum (SOTIF-style)

```yaml
base_measures:
  sotif_value:
    columns: [total_records, po_count]
    op: multiply
    agg: sum
```

### Ratio of totals (not sum of ratios)

```yaml
base_measures:
  ontime_qty: { sql: ontime_qty, agg: sum }
  total_qty:  { sql: total_qty,  agg: sum }
measures:
  ontime_now: { of: ontime_qty, op: point, offset: { months: 0 } }
  total_now:  { of: total_qty,  op: point, offset: { months: 0 } }
  otd_pct:
    op: arithmetic
    fn: percent
    left: ontime_now
    right: total_now
```

### Distinct count over a window

```yaml
base_measures:
  active_suppliers: { sql: supplier_name, agg: count_distinct }
measures:
  suppliers_12m:
    of: active_suppliers
    op: window
    trailing: { months: 12 }
    inclusive: true
```

`suppliers_12m` is distinct across the whole year, not the sum of twelve monthly counts.
